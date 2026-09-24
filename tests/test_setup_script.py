"""Behaviour tests for infra/setup.sh.

setup.sh is the part of auto-upload that runs with Azure admin rights, and bash
fails quietly: an unset variable expands to nothing, and a false `[[ ]] &&` at
the end of a function returns non-zero under `set -e`. Every defect these cover
was a real one found by hand.

Azure is stubbed with a fake `az` on PATH, so nothing here touches a cloud.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SETUP = REPO / "infra" / "setup.sh"

# What the fake az reports for a resource group that already has a deployment.
DEPLOYED = {
    "storageAccount": "aoddiagtest",
    "containerName": "aod-diagnostics",
    "functionAppName": "aod-sweeper-test",
    "sweeperPrincipalId": "00000000-0000-0000-0000-00000000beef",
    "uploaderIdentityClientId": "11111111-1111-1111-1111-111111111111",
}

FAKE_AZ = r"""#!/usr/bin/env bash
# Stub Azure CLI. Only the queries setup.sh makes are answered.
# Every invocation is recorded so a test can assert az was never needed.
args="$*"
[[ -n "$AOD_AZ_LOG" ]] && echo "$args" >> "$AOD_AZ_LOG"

case "$args" in
  *"ad signed-in-user show"*)     echo "caller-0000" ;;
  *"account show"*"--query id"*)   echo "sub-0000" ;;
  *"account show"*"--query name"*) echo "Test Subscription" ;;
  *"account show"*)                echo "Test Subscription" ;;
  *"cloud show"*)                  echo "core.windows.net" ;;

  *"role assignment list"*)        echo "$AOD_FAKE_ROLES" ;;
  *"provider register"*|*"provider show"*) echo "Registered" ;;

  *"identity show"*)
      if [[ -n "$AOD_FAKE_IDENTITY" ]]; then
          printf '%s\n' "/subscriptions/sub-0000/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/$AOD_FAKE_IDENTITY"
          printf '%s\n' "principal-of-$AOD_FAKE_IDENTITY"
          printf '%s\n' "client-of-$AOD_FAKE_IDENTITY"
      else
          exit 1
      fi ;;

  *"deployment group show"*)
      [[ -n "$AOD_FAKE_DEPLOYMENT" ]] || exit 1
      for pair in $AOD_FAKE_DEPLOYMENT; do
          key="${pair%%=*}"; value="${pair#*=}"
          if [[ "$args" == *"outputs.$key.value"* ]]; then echo "$value"; exit 0; fi
      done
      exit 1 ;;

  *) exit 0 ;;
esac
"""


class SetupScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        (bin_dir / "az").write_text(FAKE_AZ)
        (bin_dir / "az").chmod(0o755)
        self.bin_dir = bin_dir
        self.az_log = Path(self.tmp.name) / "az-calls.log"

    def tearDown(self):
        self.tmp.cleanup()

    def run_setup(self, *args, roles="Contributor", identity="", deployment="",
                  answer="n"):
        env = dict(os.environ)
        # The stub shadows any real az because it comes first on PATH.
        env["PATH"] = f"{self.bin_dir}:{env['PATH']}"
        env["AOD_FAKE_ROLES"] = roles
        env["AOD_FAKE_IDENTITY"] = identity
        env["AOD_FAKE_DEPLOYMENT"] = deployment
        env["AOD_AZ_LOG"] = str(self.az_log)
        return subprocess.run(
            [str(SETUP), *args], input=f"{answer}\n", capture_output=True,
            text=True, env=env, timeout=60,
        )

    def az_calls(self):
        if not self.az_log.exists():
            return []
        return [line for line in self.az_log.read_text().splitlines() if line]

    # ------------------------------------------------------------ argument handling

    def test_help_lists_every_command(self):
        result = self.run_setup("--help")
        self.assertEqual(result.returncode, 0)
        for command in ("deploy", "join", "enable-upload", "show-config",
                        "set-quota", "teardown"):
            self.assertIn(command, result.stdout)

    def test_unknown_command_is_rejected(self):
        result = self.run_setup("frobnicate", "-g", "rg")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown command", result.stderr)

    def test_unknown_flag_is_rejected(self):
        result = self.run_setup("deploy", "-g", "rg", "--not-a-flag")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown argument", result.stderr)

    def test_resource_group_is_required(self):
        result = self.run_setup("deploy")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("resource-group", result.stderr)

    # ------------------------------------------------------------ permissions gate

    def test_contributor_is_refused_with_the_roles_it_found(self):
        result = self.run_setup("deploy", "-g", "rg", "--uami", "aod-uploader",
                                identity="aod-uploader", roles="Contributor")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("insufficient permissions", result.stderr)
        self.assertIn("Contributor", result.stderr)

    def test_owner_passes_the_gate(self):
        result = self.run_setup("deploy", "-g", "rg", "--uami", "aod-uploader",
                                identity="aod-uploader", roles="Owner")
        # Stops at the confirmation prompt, which is as far as we want to go.
        self.assertIn("Deploying into", result.stdout)

    # ------------------------------------------------------------ plan truthfulness

    def test_plan_reports_the_identity_even_when_rbac_is_skipped(self):
        # The identity is still created under --skip-rbac; a plan that omits it
        # tells the operator nothing will happen when something will.
        result = self.run_setup("deploy", "-g", "rg", "--uami", "aod-uploader",
                                "--skip-rbac", identity="aod-uploader")
        self.assertIn("NOT created or assigned", result.stdout)
        self.assertIn("aod-uploader", result.stdout)

    def test_plan_does_not_claim_roles_are_created_when_skipping_rbac(self):
        result = self.run_setup("deploy", "-g", "rg", "--uami", "aod-uploader",
                                "--skip-rbac", identity="aod-uploader")
        self.assertNotIn("custom role: write without delete", result.stdout)

    def test_missing_identity_names_the_command_that_creates_it(self):
        result = self.run_setup("deploy", "-g", "rg", "--uami", "nope",
                                "--skip-rbac", identity="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("az identity create", result.stderr)

    # ------------------------------------------------------------ join

    def test_join_requires_an_existing_deployment(self):
        result = self.run_setup("join", "-g", "rg", "--uami", "aod-uploader",
                                identity="aod-uploader", deployment="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("run 'deploy' first", result.stderr)

    def test_join_requires_an_identity(self):
        result = self.run_setup("join", "-g", "rg")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--uami", result.stderr)

    # ------------------------------------------------------------ no Azure CLI

    def test_enable_upload_never_calls_azure_when_given_the_values(self):
        # A production host should not have to install the Azure CLI, or hold
        # Azure credentials, just to write a local config file.
        result = self.run_setup(
            "enable-upload", "--account", "acct", "--container", "cont",
            "--client-id", "cid",
        )
        self.assertEqual(self.az_calls(), [],
                         "enable-upload reached for Azure despite being given every value")
        # No resource group was passed and none was demanded; the only thing
        # left blocking it is the root check.
        self.assertNotIn("resource-group", result.stderr)
        self.assertIn("must run as root", result.stderr)

    def test_lookups_do_use_azure(self):
        # Proves the stub is wired up, so the assertion above means something.
        self.run_setup("show-config", "-g", "rg",
                       deployment=" ".join(f"{k}={v}" for k, v in DEPLOYED.items()))
        self.assertTrue(self.az_calls(), "expected a deployment lookup")

    def test_enable_upload_rejects_an_account_without_a_container(self):
        result = self.run_setup("enable-upload", "--account", "acct")
        self.assertNotEqual(result.returncode, 0)


class SetupScriptStaticTest(unittest.TestCase):
    """Guards against bash failure modes that do not show up at runtime."""

    def test_script_parses(self):
        result = subprocess.run(["bash", "-n", str(SETUP)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_function_ends_in_a_bare_conditional_and(self):
        # `[[ cond ]] && action` as the last statement makes the function return
        # non-zero when cond is false, which aborts the script under `set -e`.
        lines = SETUP.read_text().splitlines()
        offenders = []
        for i, line in enumerate(lines):
            if line.strip() == "}" and i > 0:
                previous = lines[i - 1].strip()
                if previous.startswith("[[") and "&&" in previous:
                    offenders.append(f"line {i}: {previous}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_shellcheck_reports_no_errors(self):
        result = subprocess.run(
            ["shellcheck", "--severity=error", str(SETUP)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
