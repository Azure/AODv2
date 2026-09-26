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

Create both definitions in the Azure DevOps `One` project. The build definition
must be named `aodv2-build`, matching the pipeline resource in the release YAML.
Configure production approval on the release environment or ApprovalService
registration before granting operators queue permission.

## Connect the YAML to Azure DevOps

The reference definitions are `azfilesauthenticator` (424477) and
`azfilesauthenticator-release` (434737) in the `msazure` organization, `One`
project, and `\OneBranch` folder. Create separate AODv2 definitions alongside
them rather than changing those definitions.

Before creating either definition, merge this repository's pipeline and deploy
files to the default `main` branch. Azure DevOps cannot select uncommitted YAML
or YAML that exists only on a personal branch for the permanent definition.

### Create the build definition

1. Open `https://msazure.visualstudio.com/One/_build` and select **New
   pipeline**.
2. Select **GitHub**, use the approved Azure Pipelines GitHub App/service
   connection, and select `Azure/AODv2`.
3. Select **Existing Azure Pipelines YAML file**, branch `main`, and path
   `/.github/aodv2-build.yaml`.
4. Save without running. Rename the definition to `aodv2-build` and move it to
   the `\OneBranch` folder.
5. In pipeline settings, keep the hosted `Azure Pipelines` queue used to start
   OneBranch. The YAML selects the shared custom 1ES pools and images for its
   jobs.
6. Grant the pipeline access to `AODv2-Publishing`, its signing service
   connection, secure files, PMC CLI feed, and the governed templates
   repository. Prefer per-pipeline authorization over **Open access**.
7. Set **Maximum concurrent builds per branch** to `1`. The version-reservation
   code also retries conflicting pushes, but serialization avoids unnecessary
   superseded builds.

The GitHub connection used by the build definition must have repository
contents write access. On the `main` branch, allow only that GitHub App or
pipeline identity to bypass the pull-request requirement for commits matching
the generated version-bump workflow. Do not broadly weaken branch protection.
The pipeline preserves the checkout credential and pushes a one-file commit
whose message contains `[skip ci]`.

### Create the release definition

Create a second pipeline using the same repository and branch, selecting
`/.github/aodv2-release.yaml`. Save it as `aodv2-release` under `\OneBranch`.
It must remain manually triggered. Its pipeline resource refers to
`source: aodv2-build` in project `One`, so the build definition name must match
exactly.

Authorize `aodv2-release` to consume artifacts from `aodv2-build`, use the EV2
ApprovalService endpoint, and use the production release environment. Configure
the production approval/check on the environment or EV2 registration so that
the requester cannot approve their own rollout. Operators choose the build run
and `publish` or `rollback` when queuing this definition.

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