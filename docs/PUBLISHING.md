# Publishing AODv2 packages

AODv2 uses separate Azure DevOps definitions for producing signed artifacts
and publishing an explicitly selected artifact to packages.microsoft.com.
Publishing never rebuilds or resigns a package.

## Pipeline definitions

- `.github/aodv2-build.yaml` validates the source, builds and install-tests the
  distro matrix, signs packages with ESRP, and assembles an immutable EV2
  `ServiceGroupRoot` artifact.
- `.github/aodv2-release.yaml` has no CI trigger. At queue time, select a
  successful `aodv2-build` run and choose `publish` or `rollback`.

Create both definitions in the Azure DevOps `One` project with 1ES Pipeline
Templates (1ES PT). Both checked-in definitions extend the released native 1ES
official template. The build definition must be named `aodv2-build`, matching
the pipeline resource in the release YAML.
Configure the Approval Service registration used by the production release
stages before granting operators queue permission.

### Pipeline creation access

`Azure/AODv2` is a Microsoft-owned public `github.com` repository. OneBranch's
GitHub guidance explicitly says not to use OneBranch for public GitHub source;
use base 1ES PT instead. OneBranch is only the current C+AI standard for
supported source scenarios while Azure PT is being developed.

Create these definitions through **StartRight**, the common resource-creation
flow for 1ES PT and OneBranch, and select 1ES PT rather than OneBranch. Both
YAML files must exist in the GitHub repository before starting the flow.

Before creation, confirm that `Azure/AODv2` is registered and mapped to the
owning AODv2 service in Product Catalog. StartRight uses the creator's personal
Azure DevOps permissions. Creating either production pipeline requires:

- **Create build pipeline** on the target pipeline folder.
- **Create branch** and **Contribute** on the repository where StartRight saves
  the pipeline resources.
- Service Admin on the Product Catalog service associated with the pipeline.

Public GitHub pipelines must enforce peer review. Do not configure an
automatically triggered pull-request build that can execute unreviewed fork
code. The build definition therefore has no automatic PR trigger; run any
required PR validation only after peer review through the approved workflow.
Follow the GitHub Inside Microsoft security guidance when configuring the
repository connection and triggers.

If **StartRight** is unavailable or denies creation, contact the 1ES PT team at
`1espt-pm@microsoft.com`. Include organization `msazure`, project `One`,
repository `Azure/AODv2`, the owning Product Catalog service, definition names,
YAML paths, and the operators who need creation access.

Internal references:

- [OneBranch: GitHub repositories overview](https://eng.ms/docs/products/onebranch/onboarding/githubrepos/overview)
- [OneBranch: Building a GitHub Repo in OneBranch Pipelines](https://eng.ms/docs/products/onebranch/onboarding/githubrepos/buildingagithubrepoinonebranch)
- [OneBranch: Resource Creation](https://eng.ms/docs/products/onebranch/onboarding/resourcecreation/resourcecreation)
- [1ES Pipeline Templates and OneBranch Governed Templates](https://eng.ms/docs/cloud-ai-platform/devdiv/one-engineering-system-1es/1es-docs/1es-pipeline-templates/1es-onebranch)
- [1ES PT: Pipeline Artifact](https://eng.ms/docs/coreai/devdiv/one-engineering-system-1es/1es-docs/1es-pipeline-templates/features/outputs/pipeline-artifact)
- [1ES PT: Ev2 Region Agnostic](https://eng.ms/docs/coreai/devdiv/one-engineering-system-1es/1es-docs/1es-pipeline-templates/features/releasepipelines/releaseworkflows/ev2-ra)
- [Integrate Azure DevOps with GitHub](https://eng.ms/docs/more/github-inside-microsoft/repos/integrate-ado)

### Create the build definition

1. Open StartRight for the `One` project and start a pipeline creation flow.
2. Select 1ES PT rather than the OneBranch option.
3. Select the approved GitHub service connection and `Azure/AODv2`.
4. Select `/.github/aodv2-build.yaml` from branch `main` and
   complete the flow.
5. Save without running. Rename the definition to `aodv2-build` and move it to
   the approved AODv2 pipeline folder.
6. Configure the approved 1ES Hosted Pool and images required by the YAML.
7. Grant the pipeline access to `AODv2-Publishing`, its signing service
   connection, secure files, PMC CLI feed, and the 1ES Pipeline Templates
   repository. Prefer per-pipeline authorization over **Open access**.
8. Set **Maximum concurrent builds per branch** to `1`. The version-reservation
   code also retries conflicting pushes, but serialization avoids unnecessary
   superseded builds.

The GitHub connection used by the build definition must have repository
contents write access. On the `main` branch, allow only that GitHub App or
pipeline identity to bypass the pull-request requirement for commits matching
the generated version-bump workflow. Do not broadly weaken branch protection.
The pipeline preserves the checkout credential and pushes a one-file commit
whose message contains `[skip ci]`.

### Create the release definition

Repeat the **StartRight** 1ES PT flow using the same GitHub service
connection, repository, and branch, then select
`/.github/aodv2-release.yaml`. Save it as `aodv2-release` in the approved AODv2
pipeline folder. It must remain manually triggered. Its three release paths use
the native 1ES PT `ev2-ra` workflow; preflight sets `validateOnly`, while publish
and rollback use Approval Service production stages. Its pipeline resource refers to `source:
aodv2-build` in project `One`, so the build definition name must match exactly.

Authorize `aodv2-release` to consume artifacts from `aodv2-build`, use the EV2
ApprovalService endpoint, and use its production registration. Configure the
approval policy so that the requester cannot approve their own rollout.
Operators choose the build run and `publish` or `rollback` when queuing this
definition.

### First-run authorization sequence

Run the build manually from `main` after all resources are configured. Azure
DevOps may pause the first run for resource authorization. Approve only the
specific resources named above. Confirm, in order:

1. `PrepareVersion` pushes the expected patch bump to `main` without starting a
   second run.
2. All six package jobs can use their shared 1ES images and publish unsigned
   artifacts.
3. ESRP signs all six packages with the approved profiles.
4. EV2 assembly downloads all four secure files, vendors the hash-pinned PMC
   wheels, and publishes `drop_PublishEv2Artifacts_AODv2EV2`.
5. A manually queued release can select that run and complete `Preflight`
   without changing a PMC repository.

Only after preflight succeeds should production approvers permit the publish
stage.

## Automatic package versions

`pyproject.toml` is the single persisted product-version source. At the start
of every non-PR build of `main`, the `PrepareVersion` stage increments its patch
component, commits that one file with `[skip ci]`, and pushes the commit to
`main`. For example, `0.1.0-1` becomes `0.1.1-1`; the package release remains
`1`. The current run then checks out that exact commit in every build and
signing job.

The checkout identity must have permission to contribute directly to `main`
and bypass only the branch policy needed for this generated version commit.
Limit that permission to the build pipeline identity. The `[skip ci]` marker
prevents the generated commit from starting another build.

Concurrent builds use optimistic Git pushes. If another run reserves the next
version first, the losing run refetches `main`, increments the newer version,
and retries, up to five times. Pull-request builds never commit or consume a
version; they validate the version already present in their source commit.

## Build matrix

The initial package matrix is x86_64 only:

| Target | Package tag | PMC repository |
| --- | --- | --- |
| Ubuntu 22.04 | `jammy` | `microsoft-ubuntu-jammy-prod-apt` |
| Ubuntu 24.04 | `noble` | `microsoft-ubuntu-noble-prod-apt` |
| Azure Linux 3 | `azl3` | `azurelinux-3.0-prod-ms-oss-x86_64-yum` |
| RHEL 9 | `el9` | `microsoft-rhel9.0-prod-yum` |
| RHEL 10 | `el10` | `microsoft-rhel10-prod-yum` |
| SLES 15 | `sles15` | `microsoft-sles15-prod-yum` |

Every package must install with its native package manager and pass
`test/package_builds/validate-package.sh`. A failed dependency resolution is a
release blocker, not a condition to suppress. In particular, verify the source
of Python 3.11 and matching NumPy, PyYAML, and zstandard packages on Ubuntu
22.04, RHEL 9, and SLES before enabling production publication. RHEL 10 and
SLES also require a successful service and tracer-load run on their target VM
images before onboarding their PMC mappings.

## Azure DevOps variable group

Create an `AODv2-Publishing` variable group with these non-secret references:

| Variable | Purpose |
| --- | --- |
| `SigningServiceConnection` | Approved WIF-backed ESRP service connection |
| `AppRegistrationClientId` | ESRP client application ID |
| `AppRegistrationTenantId` | Entra tenant ID |
| `AuthAKVName` | Signing authentication Key Vault |
| `AuthSignCertName` | Signing authentication certificate |
| `DebSigningProfile` | PMC-approved PGP profile for Ubuntu DEBs |
| `DefaultRpmSigningProfile` | Profile for RHEL 9 and SLES 15 RPMs |
| `AzureLinuxSigningProfile` | Profile for Azure Linux 3 RPMs |
| `Rhel10SigningProfile` | Profile for RHEL 10 RPMs |
| `PmcCliFeed` | Azure Artifacts feed containing `pmc-cli` |
| `PmcSettingsSecureFile` | Name of the AODv2 PMC settings secure file |
| `PmcRequirementsSecureFile` | Name of the hash-pinned PMC requirements file |
| `Ev2ServiceModelSecureFile` | Name of the production service model secure file |
| `Ev2ScopeBindingsSecureFile` | Name of the production scope bindings secure file |

The values of signing profiles must come from ESRP onboarding. Do not copy
profile IDs from another product merely because the distro is the same.

## Identity reuse

The pipelines may use the AzFilesAuthenticator WIF service connection. A
dedicated identity is not required when all of the following are approved:

- Its federated credential trusts both specific AODv2 pipeline definitions.
- ESRP authorizes the identity for every configured AODv2 signing profile.
- PMC grants the identity AODv2 upload and repository publication roles.
- Key Vault access and operational ownership cover both products.
- The shared audit trail and failure blast radius are acceptable to both owners.

Grant the service connection to the named pipelines, not to all pipelines in
the project. Use a dedicated AODv2 identity if any ownership or authorization
boundary differs.

## Secure files

Upload these AODv2-specific files to the build definition:

- PMC `settings.toml` configured for managed identity authentication.
- Production `ServiceModel.Prod.json` with approved Service Tree, tenant,
  subscription, managed identity, and network values.
- Production `ScopeBindings.Prod.json` containing the matching substitutions.
- A fully pinned `requirements.txt` for `pmc-cli` and all transitive wheels,
  with a SHA-256 hash on every requirement. The build uses pip
  `--require-hashes` and vendors those wheels into the EV2 artifact.

Secrets, tenant identifiers, and managed identity resource IDs must not be
committed to this repository.

## Build and release flow

1. Queue or merge the build. Confirm `PrepareVersion` reserves the expected
   patch version and that all six package jobs pass native install
   validation.
2. Confirm ESRP signing and inspect
   `drop_SignArtifacts_AODv2SignedPackages/manifest.json`.
3. Confirm `drop_PublishEv2Artifacts_AODv2EV2` contains the expected
   `ServiceGroupRoot` and no unresolved placeholders.
4. Queue `aodv2-release`, select that exact build resource version, and choose
   `publish`. The preflight rollout checks hashes, package cardinality, and all
   six repositories without modifying them.
5. Approve the production rollout after reviewing its selected build and
   package manifest.
6. Verify installation by package name on clean clients for every target.

For rollback, queue the same release pipeline, select the build that introduced
the packages, and choose `rollback`. This removes those package IDs from their
mapped repositories and republishes repository metadata. PMC policy may require
a new higher package release when clients have already consumed a bad version.