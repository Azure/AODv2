#!/usr/bin/env bash
#
# AODv2 auto-upload setup.
#
#   1. 'deploy'        - first host: creates the shared Azure resources and
#                        attaches the uploader identity. Needs Azure admin rights.
#   2. 'join'          - every later host: attaches the same identity to it.
#                        No Owner rights needed.
#   3. 'enable-upload' - on each host, as root: points AODv2 at the container.
#
#   ./setup.sh deploy        -g <rg> --uami <name> [-s <subscription>]
#   ./setup.sh join          -g <rg> --uami <name> [-s <subscription>]
#   ./setup.sh enable-upload -g <rg> [-s <subscription>]
#   ./setup.sh show-config   -g <rg> [-s <subscription>]
#   ./setup.sh set-quota     -g <rg> --quota-gb 100
#   ./setup.sh teardown      -g <rg>
#
# deploy is idempotent: if it fails partway, fix the cause and run it again.
# Nothing is rolled back, because a half-created storage account may already
# hold evidence.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BICEP_FILE="$SCRIPT_DIR/main.bicep"
SWEEPER_DIR="$SCRIPT_DIR/sweeper"
DEPLOYMENT_NAME="aodv2-upload"

RESOURCE_GROUP=""
SUBSCRIPTION=""
PRINCIPAL_IDS=()
USE_UAMI=0
UAMI_NAME="aod-uploader"
UAMI_ID=""
UAMI_PRINCIPAL_ID=""
UAMI_CLIENT_ID=""
SELF_VM_ID=""
QUOTA_GB=50
QUOTA_SET=0
RETENTION_DAYS=30
RETENTION_SET=0
LOCATION=""
ASSUME_YES=0
SKIP_FUNCTION=0
SKIP_RBAC=0
AOD_CONFIG=""
ACCOUNT=""
CONTAINER=""
CLIENT_ID=""

die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarn:\033[0m %s\n' "$*" >&2; }

usage() {
    # Print the whole header comment rather than a fixed line range, so adding
    # a command there cannot silently drop out of --help.
    awk 'NR>2 { if (/^#/) { sub(/^# ?/, ""); print } else { exit } }' "${BASH_SOURCE[0]}"
    cat <<'EOF'

Options:
  -g, --resource-group   Target resource group (required)
  -s, --subscription     Subscription id or name (defaults to the active one)
      --principal-id     Grant an identity by principal id; repeatable. For an
                         admin running this from somewhere other than the host.
      --uami [name|id]   Use this existing user-assigned identity: grant it
                         upload rights and attach it to this machine. Create it
                         yourself first, so one identity can be shared by a
                         whole fleet:
                           az identity create -g <rg> -n aod-uploader
                         Accepts a name in the resource group, or a full
                         resource id. Defaults to the name 'aod-uploader'.
      --quota-gb         Size budget for the container, shared by every host
                         using it. On a re-run the existing value is kept
                         unless you pass this. (default 50 on first deploy)
      --retention-days   Age after which packages are deleted, also shared.
                         Kept on a re-run unless you pass this. (default 30)
      --location         Defaults to the resource group's location
      --aod-config       Path to config.yaml (default /etc/linux_diagnostics/config.yaml,
                         falling back to the repo copy when running from source)
      --account          Storage account for 'enable-upload'. Passing this and
      --container        --container skips the deployment lookup, so a host
      --client-id        needs no Azure CLI and no Azure credentials. Take the
                         values from './setup.sh show-config'.
      --skip-function    Do not deploy the sweeper function at all. Use when the
                         subscription has no quota for the Y1 (Consumption) SKU.
                         Everything else deploys and the lifecycle rule still
                         deletes by age; only the --quota-gb size cap is lost.
      --skip-rbac        Do not create or assign roles; prints the commands an
                         admin must run instead. Use when you lack Owner /
                         User Access Administrator.
  -y, --yes              Do not prompt for confirmation
EOF
}

# Scope every az call explicitly, so a machine with several subscriptions
# cannot silently deploy into the wrong one.
az_() {
    if [[ -n "$SUBSCRIPTION" ]]; then
        az "$@" --subscription "$SUBSCRIPTION"
    else
        az "$@"
    fi
}

require_az() {
    command -v az >/dev/null 2>&1 || die "the Azure CLI (az) is required"
    az account show >/dev/null 2>&1 || die "not logged in - run 'az login' first"
    if [[ -n "$SUBSCRIPTION" ]]; then
        az account show --subscription "$SUBSCRIPTION" >/dev/null 2>&1 \
            || die "subscription '$SUBSCRIPTION' not found or not accessible"
    fi
    az_ group show -n "$RESOURCE_GROUP" >/dev/null 2>&1 \
        || die "resource group '$RESOURCE_GROUP' not found in $(current_subscription)"
}

# Creating the custom role and its assignment needs more than Contributor.
# ARM validates the whole template first, so without this check the deployment
# fails with a wall of authorization JSON.
require_rbac_rights() {
    if [[ $SKIP_RBAC -eq 1 ]]; then
        warn "--skip-rbac: roles will not be created or assigned"
        return 0
    fi

    local subscription_id caller roles scope
    subscription_id=$(az_ account show --query id -o tsv)
    scope="/subscriptions/$subscription_id/resourceGroups/$RESOURCE_GROUP"

    caller=$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)
    if [[ -z "$caller" ]]; then
        warn "could not determine the signed-in identity; skipping the permission check"
        return 0
    fi

    roles=$(az_ role assignment list --assignee "$caller" --scope "$scope" \
            --include-inherited --query "[].roleDefinitionName" -o tsv 2>/dev/null || true)

    # deploy both creates resources and grants a role, and no single built-in
    # role but Owner covers both: User Access Administrator cannot create a
    # storage account, and Contributor cannot create or assign a role.
    if grep -qiE '^Owner$' <<<"$roles"; then
        return 0
    fi
    if grep -qiE '^Contributor$' <<<"$roles" \
       && grep -qiE '^User Access Administrator$' <<<"$roles"; then
        return 0
    fi

    local role_list
    role_list=$(tr '\n' ',' <<<"$roles" | sed 's/,$//')
    [[ -n "$role_list" ]] || role_list="none"

    {
        printf '\n\033[31merror:\033[0m insufficient permissions to run '"'"'deploy'"'"'.\n\n'
        printf '  Scope : %s\n' "$scope"
        printf '  Roles : %s\n\n' "$role_list"
        printf '  deploy creates resources AND grants a role, which needs either:\n'
        printf '      Owner, or\n'
        printf '      Contributor + User Access Administrator\n\n'
        printf '  Neither alone is enough: User Access Administrator cannot create\n'
        printf '  a storage account, and Contributor cannot create or assign roles.\n\n'
        printf '  Options:\n'
        printf '    - have an admin run deploy once, then run enable-upload yourself\n'
        printf '    - re-run with --skip-rbac to create everything else now, and\n'
        printf '      hand the printed role commands to an admin\n'
        printf '    - use a resource group where you hold those roles\n\n'
    } >&2
    exit 1
}

# An unregistered provider fails the deployment partway through, after other
# resources exist. Registering is idempotent and Contributor is enough for it.
ensure_providers() {
    local providers=(Microsoft.Storage Microsoft.Web Microsoft.EventGrid)
    local pending=()

    for provider in "${providers[@]}"; do
        local state
        state=$(az_ provider show -n "$provider" --query registrationState -o tsv 2>/dev/null || echo "Unknown")
        if [[ "$state" != "Registered" ]]; then
            info "registering resource provider $provider (currently $state)"
            az_ provider register -n "$provider" >/dev/null 2>&1 \
                || die "could not register $provider - ask a subscription admin to run: az provider register -n $provider"
            pending+=("$provider")
        fi
    done

    [[ ${#pending[@]} -eq 0 ]] && return 0

    info "waiting for provider registration (up to 5 minutes)"
    for provider in "${pending[@]}"; do
        local waited=0
        while [[ $waited -lt 300 ]]; do
            [[ "$(az_ provider show -n "$provider" --query registrationState -o tsv 2>/dev/null)" == "Registered" ]] && break
            sleep 10; waited=$((waited + 10))
        done
        [[ $waited -lt 300 ]] && ok "$provider registered" \
            || warn "$provider still registering; the deployment may fail. Retry shortly."
    done
}

current_subscription() {
    if [[ -n "$SUBSCRIPTION" ]]; then
        az account show --subscription "$SUBSCRIPTION" --query name -o tsv
    else
        az account show --query name -o tsv
    fi
}

parse_args() {
    [[ $# -gt 0 ]] || { usage; exit 1; }
    COMMAND="$1"; shift
    case "$COMMAND" in -h|--help|help) usage; exit 0 ;; esac
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -g|--resource-group) RESOURCE_GROUP="${2:-}"; shift 2 ;;
            -s|--subscription)   SUBSCRIPTION="${2:-}"; shift 2 ;;
            --principal-id)      PRINCIPAL_IDS+=("${2:-}"); shift 2 ;;
            --uami)
                USE_UAMI=1
                if [[ -n "${2:-}" && "${2:0:1}" != "-" ]]; then
                    UAMI_NAME="$2"; shift 2
                else
                    shift
                fi
                ;;
            --quota-gb)          QUOTA_GB="${2:-}"; QUOTA_SET=1; shift 2 ;;
            --retention-days)    RETENTION_DAYS="${2:-}"; RETENTION_SET=1; shift 2 ;;
            --location)          LOCATION="${2:-}"; shift 2 ;;
            --aod-config)        AOD_CONFIG="${2:-}"; shift 2 ;;
            --account)           ACCOUNT="${2:-}"; shift 2 ;;
            --container)         CONTAINER="${2:-}"; shift 2 ;;
            --client-id)         CLIENT_ID="${2:-}"; shift 2 ;;
            --skip-function)     SKIP_FUNCTION=1; shift ;;
            --skip-rbac)         SKIP_RBAC=1; shift ;;
            -y|--yes)            ASSUME_YES=1; shift ;;
            -h|--help)           usage; exit 0 ;;
            *)                   die "unknown argument: $1" ;;
        esac
    done
    # A resource group is only needed when we have to look the deployment up.
    if [[ -z "$RESOURCE_GROUP" && -z "$ACCOUNT" ]]; then
        die "--resource-group is required"
    fi
}

confirm() {
    [[ $ASSUME_YES -eq 1 ]] && return 0
    read -r -p "Proceed? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }
}

principals_json() {
    if [[ ${#PRINCIPAL_IDS[@]} -eq 0 ]]; then echo "[]"; return; fi
    printf '%s\n' "${PRINCIPAL_IDS[@]}" | jq -R . | jq -s -c .
}

# IMDS names the machine we are on, so setup needs no host name from the user.
# Empty output means this is not an Azure VM.
detect_self_vm() {
    curl -fsS --max-time 3 -H Metadata:true \
        "http://169.254.169.254/metadata/instance/compute/resourceId?api-version=2021-02-01&format=text" \
        2>/dev/null || true
}

# The identity is created outside this script so an existing one can be shared
# across a fleet. We only look it up, grant it, and attach it.
resolve_uami() {
    local show=(identity show)
    if [[ "$UAMI_NAME" == /subscriptions/* ]]; then
        show+=(--ids "$UAMI_NAME")
    else
        show+=(-g "$RESOURCE_GROUP" -n "$UAMI_NAME")
    fi

    local found
    found=$(az_ "${show[@]}" --query "[id,principalId,clientId]" -o tsv 2>/dev/null || true)
    if [[ -z "$found" ]]; then
        die "no user-assigned identity '$UAMI_NAME' in $RESOURCE_GROUP.
       Create it first, or pass its full resource id:
         az identity create -g $RESOURCE_GROUP -n $UAMI_NAME"
    fi

    UAMI_ID=$(awk 'NR==1' <<<"$found")
    UAMI_PRINCIPAL_ID=$(awk 'NR==2' <<<"$found")
    UAMI_CLIENT_ID=$(awk 'NR==3' <<<"$found")
    ok "using identity ${UAMI_ID##*/}"
    PRINCIPAL_IDS+=("$UAMI_PRINCIPAL_ID")
}

# Attaching is idempotent, and the role assignment already covers the identity,
# so onboarding host number 50 needs no RBAC rights.
attach_uami() {
    if [[ -n "$SELF_VM_ID" ]]; then
        info "attaching ${UAMI_ID##*/} to this machine"
        # --ids carries its own subscription and resource group.
        az vm identity assign --ids "$SELF_VM_ID" --identities "$UAMI_ID" -o none \
            || die "could not attach ${UAMI_ID##*/} to this machine"
        ok "${SELF_VM_ID##*/}"
    fi
}

deployment_output() {
    az_ deployment group show -g "$RESOURCE_GROUP" -n "$DEPLOYMENT_NAME" \
        --query "properties.outputs.$1.value" -o tsv 2>/dev/null || true
}

endpoint_suffix() {
    az cloud show --query suffixes.storageEndpoint -o tsv 2>/dev/null || echo "core.windows.net"
}

# Mirrors Controller._default_config_path: packaged location first, repo copy
# as the development fallback.
resolve_aod_config() {
    if [[ -n "$AOD_CONFIG" ]]; then
        return 0
    fi
    if [[ -f /etc/linux_diagnostics/config.yaml ]]; then
        AOD_CONFIG="/etc/linux_diagnostics/config.yaml"
    elif [[ -f "$REPO_ROOT/config/config.yaml" ]]; then
        AOD_CONFIG="$REPO_ROOT/config/config.yaml"
    else
        die "could not find config.yaml; pass --aod-config"
    fi
}

# Packaging decides where the unit lands, and running from source installs none
# at all, so check before touching systemd.
unit_installed() {
    [[ -e /run/systemd/system ]] || return 1
    local dir
    for dir in /etc/systemd/system /usr/lib/systemd/system /lib/systemd/system; do
        if [[ -f "$dir/linux_diagnostics.service" ]]; then
            return 0
        fi
    done
    return 1
}

# What to tell the operator to type, which differs before packaging exists.
cli_hint() {
    if command -v aodv2 >/dev/null 2>&1; then
        echo "sudo aodv2"
    else
        echo "sudo PYTHONPATH=$REPO_ROOT/src python3 -m cli"
    fi
}

# Prefer the installed command, but fall back to the source tree so the
# documented run-from-source workflow works before packaging exists.
aod_cli() {
    if command -v aodv2 >/dev/null 2>&1; then
        aodv2 "$@"
    elif [[ -f "$REPO_ROOT/src/cli.py" ]]; then
        PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m cli "$@"
    else
        die "neither the 'aodv2' command nor $REPO_ROOT/src/cli.py was found"
    fi
}

# The container and its budget are shared by every host, so a later host must
# not silently reset them to this script's defaults.
inherit_existing_settings() {
    local function_app bytes retention
    function_app=$(deployment_output functionAppName)
    [[ -n "$function_app" ]] || return 0

    if [[ $QUOTA_SET -eq 0 ]]; then
        # set-quota writes the live value here, so the app setting wins over
        # whatever the last deployment was given.
        bytes=$(az_ functionapp config appsettings list -g "$RESOURCE_GROUP" -n "$function_app" \
                --query "[?name=='AOD_QUOTA_BYTES'].value | [0]" -o tsv 2>/dev/null || true)
        if [[ -n "$bytes" && "$bytes" != "None" ]]; then
            QUOTA_GB=$(( bytes / 1024 / 1024 / 1024 ))
            info "keeping the container's existing quota of ${QUOTA_GB} GB"
        fi
    fi

    if [[ $RETENTION_SET -eq 0 ]]; then
        retention=$(az_ deployment group show -g "$RESOURCE_GROUP" -n "$DEPLOYMENT_NAME" \
                    --query "properties.parameters.retentionDays.value" -o tsv 2>/dev/null || true)
        if [[ -n "$retention" && "$retention" != "None" ]]; then
            RETENTION_DAYS="$retention"
            info "keeping the existing retention of ${RETENTION_DAYS} days"
        fi
    fi
}

# Onboard a host onto infrastructure that already exists. Attaching an identity
# needs no Owner rights, so only the first host needs an Azure admin.
cmd_join() {
    require_az
    [[ $USE_UAMI -eq 1 ]] || die "join needs --uami <name|id>"

    local account
    account=$(deployment_output storageAccount)
    [[ -n "$account" ]] || die "no '$DEPLOYMENT_NAME' deployment in $RESOURCE_GROUP - run 'deploy' first"

    resolve_uami

    local granted
    granted=$(deployment_output uploaderIdentityClientId)
    if [[ -n "$granted" && "$granted" != "$UAMI_CLIENT_ID" ]]; then
        warn "this is not the identity the deployment granted; uploads will fail"
        warn "with 403 until an admin grants it upload rights."
    fi

    SELF_VM_ID=$(detect_self_vm)
    [[ -n "$SELF_VM_ID" ]] || die "join must run on the Azure VM you are onboarding"
    attach_uami

    echo
    ok "attached. Now run: sudo $0 enable-upload -g $RESOURCE_GROUP"
}

cmd_deploy() {
    require_az
    command -v jq >/dev/null 2>&1 || die "jq is required"
    [[ -f "$BICEP_FILE" ]] || die "missing $BICEP_FILE"
    require_rbac_rights
    ensure_providers
    inherit_existing_settings

    if [[ $USE_UAMI -eq 1 ]]; then
        resolve_uami
        SELF_VM_ID=$(detect_self_vm)
        if [[ -n "$SELF_VM_ID" ]]; then
            info "this machine: ${SELF_VM_ID##*/}"
        else
            warn "this is not an Azure VM, so the identity will be granted but"
            warn "attached to nothing. Attach it yourself with:"
            warn "  az vm identity assign --ids <vm-id> --identities $UAMI_ID"
        fi
    elif [[ ${#PRINCIPAL_IDS[@]} -eq 0 ]]; then
        warn "neither --uami nor --principal-id given, so no host can upload yet."
    fi

    echo
    info "Deploying into '$RESOURCE_GROUP' (subscription: $(current_subscription)):"
    echo "     storage account, dedicated, Entra-only, soft delete off"
    echo "     container + lifecycle rule: delete after ${RETENTION_DAYS} days"
    if [[ $SKIP_RBAC -eq 1 ]]; then
        echo "     roles: NOT created or assigned (--skip-rbac)"
        echo "            upload will fail with 403 until an admin assigns them"
    else
        echo "     custom role: write without delete, scoped to the container"
    fi
    if [[ $USE_UAMI -eq 1 ]]; then
        if [[ -n "$SELF_VM_ID" ]]; then
            echo "     identity ${UAMI_ID##*/}: attached to this machine"
        else
            echo "     identity ${UAMI_ID##*/}: not attached to anything"
        fi
        if [[ $SKIP_RBAC -eq 0 ]]; then
            echo "     granted once, covering every VM it is attached to"
        fi
    elif [[ $SKIP_RBAC -eq 0 ]]; then
        echo "     hosts granted upload: ${#PRINCIPAL_IDS[@]}"
    fi
    if [[ $SKIP_FUNCTION -eq 1 ]]; then
        echo "     sweeper function: NOT deployed (--skip-function)"
        echo "                       age-based deletion still applies; no size cap"
    else
        echo "     sweeper function + Event Grid subscription, quota ${QUOTA_GB} GB"
    fi
    echo
    confirm

    local rbac_flag=true
    [[ $SKIP_RBAC -eq 1 ]] && rbac_flag=false

    local args=(-g "$RESOURCE_GROUP" -n "$DEPLOYMENT_NAME" -f "$BICEP_FILE"
                -p "vmPrincipalIds=$(principals_json)"
                   "quotaGb=$QUOTA_GB" "retentionDays=$RETENTION_DAYS"
                   "deployRbac=$rbac_flag")
    if [[ $USE_UAMI -eq 1 ]]; then
        args+=(-p "uploaderClientId=$UAMI_CLIENT_ID")
    fi
    if [[ $SKIP_FUNCTION -eq 1 ]]; then
        args+=(-p "deploySweeper=false")
    fi
    [[ -n "$LOCATION" ]] && args+=(-p "location=$LOCATION")

    info "deploying infrastructure (a couple of minutes)"
    az_ deployment group create "${args[@]}" -p deployEventGrid=false -o none
    ok "infrastructure deployed"

    if [[ $USE_UAMI -eq 1 ]]; then
        attach_uami
    fi

    local function_app
    function_app=$(deployment_output functionAppName)

    if [[ $SKIP_FUNCTION -eq 0 ]]; then
        if command -v func >/dev/null 2>&1; then
            info "publishing the sweeper to $function_app"
            if (cd "$SWEEPER_DIR" && func azure functionapp publish "$function_app" --python); then
                ok "sweeper published"
                # Only now does the function exist for Event Grid to validate.
                info "subscribing the sweeper to BlobCreated events"
                az_ deployment group create "${args[@]}" -p deployEventGrid=true -o none \
                    && ok "event subscription created" \
                    || warn "event subscription failed; the daily timer and lifecycle rule still apply"
            else
                warn "publish failed; retry: (cd $SWEEPER_DIR && func azure functionapp publish $function_app --python)"
            fi
        else
            # The lifecycle rule still deletes by age, so this is not fatal.
            warn "Azure Functions Core Tools (func) not found; sweeper code not deployed."
            warn "install it, then: (cd $SWEEPER_DIR && func azure functionapp publish $function_app --python)"
            warn "then re-run this command to create the event subscription."
        fi
    fi

    echo
    [[ $SKIP_RBAC -eq 1 ]] && print_manual_rbac
    cmd_show_config
}

# Without Owner the deployment cannot assign roles, so upload will fail with 403
# until someone who can runs these.
print_manual_rbac() {
    local subscription_id container_scope sweeper_principal uploader_principal
    subscription_id=$(az_ account show --query id -o tsv)
    container_scope="/subscriptions/$subscription_id/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Storage/storageAccounts/$(deployment_output storageAccount)/blobServices/default/containers/$(deployment_output containerName)"
    sweeper_principal=$(deployment_output sweeperPrincipalId)
    uploader_principal="$UAMI_PRINCIPAL_ID"

    cat <<EOF

------------------------------------------------------------------------
ACTION REQUIRED: roles were not assigned (--skip-rbac)

Upload will fail with 403 until an Owner or User Access Administrator runs:

  # 1. Let each AODv2 host write packages
EOF
    if [[ -n "$uploader_principal" ]]; then
        printf '  #    one assignment covers every VM holding the shared identity\n'
        printf '  az role assignment create --assignee %s \\\n' "$uploader_principal"
        printf '    --role "Storage Blob Data Contributor" --scope "%s"\n' "$container_scope"
    elif [[ ${#PRINCIPAL_IDS[@]} -eq 0 ]]; then
        printf '  #    (no identity was granted; get this host'"'"'s id with:\n'
        printf '  #     az vm identity show -g %s -n <vm> --query principalId -o tsv)\n' "$RESOURCE_GROUP"
        printf '  az role assignment create --assignee <host-principal-id> \\\n'
        printf '    --role "Storage Blob Data Contributor" --scope "%s"\n' "$container_scope"
    else
        for pid in "${PRINCIPAL_IDS[@]}"; do
            printf '  az role assignment create --assignee %s \\\n' "$pid"
            printf '    --role "Storage Blob Data Contributor" --scope "%s"\n' "$container_scope"
        done
    fi
    cat <<EOF

  # 2. Let the sweeper delete old packages
  az role assignment create --assignee $sweeper_principal \\
    --role "Storage Blob Data Contributor" --scope "$container_scope"

Note: the built-in role above includes delete. The tighter write-without-delete
custom role is in infra/main.bicep (resource 'clientRole') and needs the same
permissions to create.
------------------------------------------------------------------------
EOF
}

cmd_show_config() {
    require_az
    local account container client_id cli restart
    account=$(deployment_output storageAccount)
    container=$(deployment_output containerName)
    client_id=$(deployment_output uploaderIdentityClientId)
    [[ -n "$account" ]] || die "no '$DEPLOYMENT_NAME' deployment in $RESOURCE_GROUP - run 'deploy' first"

    # Before packaging exists the daemon is run from the source tree, so the
    # command to type and the way to restart both differ.
    cli=$(cli_hint)
    if unit_installed; then
        restart="sudo systemctl restart linux_diagnostics.service"
    else
        restart="restart the daemon (sudo python3 $REPO_ROOT/src/Controller.py)"
    fi

    cat <<EOF
Storage account : $account
Container       : $container
EOF
    [[ -n "$client_id" ]] && echo "Identity        : shared, client id $client_id"
    cat <<EOF

On each AODv2 host, as root:

  ./setup.sh enable-upload -g $RESOURCE_GROUP${SUBSCRIPTION:+ -s $SUBSCRIPTION}

or, without this script and without Azure access:

  $cli upload enable --account $account --container $container \\
       --endpoint-suffix $(endpoint_suffix)${client_id:+ \\
       --client-id $client_id}
  $cli upload validate
  $restart
EOF
}

cmd_enable_upload() {
    [[ $EUID -eq 0 ]] || die "enable-upload must run as root on the AODv2 host"
    resolve_aod_config

    local account container client_id
    if [[ -n "$ACCOUNT" ]]; then
        # Values supplied, so this host needs no Azure access at all.
        account="$ACCOUNT"
        container="$CONTAINER"
        client_id="$CLIENT_ID"
        [[ -n "$container" ]] || die "--container is required alongside --account"
    else
        require_az
        account=$(deployment_output storageAccount)
        container=$(deployment_output containerName)
        client_id=$(deployment_output uploaderIdentityClientId)
        [[ -n "$account" ]] || die "no '$DEPLOYMENT_NAME' deployment in $RESOURCE_GROUP - run 'deploy' first"
    fi

    info "pointing AODv2 at $account/$container"
    local enable_args=(--account "$account" --container "$container"
                       --endpoint-suffix "$(endpoint_suffix)")

    # A shared identity must be named explicitly: IMDS cannot guess which of a
    # VM's identities to mint a token for.
    if [[ -n "$client_id" ]]; then
        info "using the shared uploader identity"
        enable_args+=(--client-id "$client_id")
    fi

    aod_cli --config "$AOD_CONFIG" upload enable "${enable_args[@]}"

    info "checking identity and permissions"
    if ! aod_cli --config "$AOD_CONFIG" upload validate; then
        warn "validation failed. Upload stays configured but will not work until fixed."
        warn "If the role assignment was just created, wait a minute and re-run:"
        warn "  $(cli_hint) upload validate"
        return 1
    fi

    if unit_installed; then
        info "restarting linux_diagnostics.service"
        systemctl restart linux_diagnostics.service \
            && ok "service restarted" \
            || warn "could not restart the service; do it manually"
    else
        info "restart the daemon for the new settings to take effect"
    fi

    echo
    ok "upload is on. Packages already on disk are uploaded too."
    echo "     watch progress with: $(cli_hint) upload status"
}

# Deliberately does not delete the storage account: it holds collected
# evidence, and losing that is worse than leaving a resource behind.
cmd_teardown() {
    require_az
    local account function_app
    account=$(deployment_output storageAccount)
    function_app=$(deployment_output functionAppName)
    [[ -n "$account" ]] || die "no '$DEPLOYMENT_NAME' deployment in $RESOURCE_GROUP"

    echo
    info "This will remove from '$RESOURCE_GROUP':"
    echo "     the sweeper function app and its hosting plan"
    echo "     the Event Grid subscription"
    echo "     the custom role and its assignments"
    echo
    echo "     KEPT: storage account $account and everything in it."
    echo "     KEPT: the managed identity, which this script did not create."
    echo
    echo "     Hosts keep uploading until you run 'upload disable' on them."
    echo
    confirm

    if [[ -n "$function_app" ]]; then
        info "deleting $function_app"
        az_ functionapp delete -g "$RESOURCE_GROUP" -n "$function_app" -o none 2>/dev/null \
            || warn "could not delete the function app"
    fi

    local scope role_id
    scope=$(az_ storage account show -g "$RESOURCE_GROUP" -n "$account" --query id -o tsv 2>/dev/null || true)
    if [[ -n "$scope" ]]; then
        role_id=$(az_ role definition list --custom-role-only true --scope "$scope" \
                  --query "[?starts_with(roleName,'AODv2 Diagnostic Uploader')].name | [0]" -o tsv 2>/dev/null || true)
        if [[ -n "$role_id" && "$role_id" != "None" ]]; then
            info "removing role assignments"
            az_ role assignment delete --role "$role_id" --scope "$scope" -o none 2>/dev/null || true
            az_ role definition delete --name "$role_id" -o none 2>/dev/null \
                || warn "could not delete the custom role; needs Owner"
        fi
    fi

    echo
    ok "removed. To delete the evidence as well:"
    echo "     az storage account delete -g $RESOURCE_GROUP -n $account"
}

cmd_set_quota() {
    require_az
    local function_app
    function_app=$(deployment_output functionAppName)
    [[ -n "$function_app" ]] || die "no '$DEPLOYMENT_NAME' deployment in $RESOURCE_GROUP"

    local bytes=$(( QUOTA_GB * 1024 * 1024 * 1024 ))
    info "setting the sweeper quota to ${QUOTA_GB} GB"
    az_ functionapp config appsettings set -g "$RESOURCE_GROUP" -n "$function_app" \
        --settings "AOD_QUOTA_BYTES=$bytes" -o none
    ok "quota updated; takes effect on the next sweep"
}

parse_args "$@"
case "$COMMAND" in
    deploy)        cmd_deploy ;;
    join)          cmd_join ;;
    enable-upload) cmd_enable_upload ;;
    show-config)   cmd_show_config ;;
    set-quota)     cmd_set_quota ;;
    teardown)      cmd_teardown ;;
    *)             die "unknown command '$COMMAND' (deploy, join, enable-upload, show-config, set-quota, teardown)" ;;
esac
