"""Tests for QuickAction handlers.

Each QuickAction subclass is exercised end-to-end:

  * `get_command()` is asserted to return the expected argv (catches
    accidental regressions in the source path or CLI flags).
  * `execute()` is run via asyncio with the data source faked out, and
    the produced file under `aod_quick_<batch_id>/` is verified to
    match the source bytes -- proving the handler "collected the right
    values".
  * Failure paths (missing source file, empty subprocess stdout) are
    exercised to confirm the handler does *not* crash and does *not*
    create a misleading empty file. This matches QuickAction.execute()'s
    graceful-failure contract.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Same pattern as tests/test_log_collector.py: also allows
# `python -m unittest tests/test_quick_action_handlers.py` outside pytest.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from handlers.CifsstatsQuickAction import CifsstatsQuickAction  # noqa: E402
from handlers.DebugDataQuickAction import DebugDataQuickAction  # noqa: E402
from handlers.DmesgQuickAction import DmesgQuickAction  # noqa: E402
from handlers.JournalctlQuickAction import JournalctlQuickAction  # noqa: E402
from handlers.MountsQuickAction import MountsQuickAction  # noqa: E402
from handlers.SmbinfoQuickAction import SmbinfoQuickAction  # noqa: E402
from handlers.SysLogsQuickAction import SysLogsQuickAction  # noqa: E402


class _FakeProc:
    """Stand-in for asyncio.subprocess.Process used by 'cmd'-type
    QuickActions. Only `communicate()` is consulted by the base class."""

    def __init__(self, stdout: bytes):
        self._stdout = stdout

    async def communicate(self):
        return self._stdout, b""


def _fake_subprocess_factory(stdout_bytes: bytes):
    """Return a coroutine compatible with the signature of
    asyncio.create_subprocess_exec, yielding a _FakeProc with the
    requested stdout."""

    async def _factory(*_args, **_kwargs):
        return _FakeProc(stdout_bytes)

    return _factory


class TestCatQuickActions(unittest.TestCase):
    """`cat`-type quick actions: copy a /proc file byte-for-byte into the
    batch output directory."""

    def _run_cat_success(self, action_cls, expected_filename):
        with tempfile.TemporaryDirectory() as td:
            batches = Path(td) / "batches"
            src_path = Path(td) / "src.bin"
            payload = b"line1\nline2 with binary \x00\x01\xff\n"
            src_path.write_bytes(payload)

            action = action_cls(str(batches))
            # Redirect the action at our fake source. We deliberately
            # patch the bound method rather than the class, so other
            # tests see an unmodified class.
            action.get_command = lambda: (["cat", str(src_path)], "cat")

            asyncio.run(action.execute("batch-ok"))

            out = batches / "aod_quick_batch-ok" / expected_filename
            self.assertTrue(out.exists(), f"{expected_filename} not produced")
            self.assertEqual(
                out.read_bytes(),
                payload,
                "QuickAction must copy source bytes verbatim",
            )
            self.assertGreater(out.stat().st_size, 0, "output unexpectedly empty")
            if __debug__:
                self.assertEqual(action.executions, 1)
                self.assertEqual(action.failures, 0)

    def _run_cat_missing(self, action_cls, expected_filename):
        """Source file absent: handler must swallow the error, produce no
        output file, and bump its failure counter. QuickAction creates the
        batch directory eagerly (before the read), so an empty
        aod_quick_<batch>/ dir may remain -- that's accepted, but it must
        not contain a stub log file."""
        with tempfile.TemporaryDirectory() as td:
            batches = Path(td) / "batches"
            action = action_cls(str(batches))
            action.get_command = lambda: (
                ["cat", "/definitely/does/not/exist/aod_test"],
                "cat",
            )

            # Must not raise -- QuickAction.execute swallows errors so
            # one broken collector cannot take down the daemon.
            asyncio.run(action.execute("batch-missing"))

            batch_dir = batches / "aod_quick_batch-missing"
            out = batch_dir / expected_filename
            self.assertFalse(
                out.exists(),
                "handler must not create an output file when source is missing",
            )
            if batch_dir.exists():
                self.assertEqual(
                    list(batch_dir.iterdir()),
                    [],
                    "batch dir must not contain stub artifacts on failure",
                )
            if __debug__:
                self.assertEqual(action.failures, 1)
                self.assertEqual(action.executions, 0)

    def _run_cat_empty(self, action_cls, expected_filename):
        """Source file present but empty: 0-byte copy is the correct
        behaviour. An empty output here is *expected*, not a bug."""
        with tempfile.TemporaryDirectory() as td:
            batches = Path(td) / "batches"
            src_path = Path(td) / "empty.bin"
            src_path.write_bytes(b"")

            action = action_cls(str(batches))
            action.get_command = lambda: (["cat", str(src_path)], "cat")

            asyncio.run(action.execute("batch-empty"))

            out = batches / "aod_quick_batch-empty" / expected_filename
            self.assertTrue(out.exists())
            self.assertEqual(out.read_bytes(), b"")
            if __debug__:
                self.assertEqual(action.executions, 1)
                self.assertEqual(action.failures, 0)

    # ---- argv shape ---------------------------------------------------

    def test_cifsstats_argv(self):
        argv, kind = CifsstatsQuickAction("/tmp").get_command()
        self.assertEqual(kind, "cat")
        self.assertEqual(argv, ["cat", "/proc/fs/cifs/Stats"])

    def test_debugdata_argv(self):
        argv, kind = DebugDataQuickAction("/tmp").get_command()
        self.assertEqual(kind, "cat")
        self.assertEqual(argv, ["cat", "/proc/fs/cifs/DebugData"])

    def test_mounts_argv(self):
        argv, kind = MountsQuickAction("/tmp").get_command()
        self.assertEqual(kind, "cat")
        self.assertEqual(argv, ["cat", "/proc/mounts"])

    # ---- happy path ---------------------------------------------------

    def test_cifsstats_collects(self):
        self._run_cat_success(CifsstatsQuickAction, "cifsstats.log")

    def test_debugdata_collects(self):
        self._run_cat_success(DebugDataQuickAction, "debug_data.log")

    def test_mounts_collects(self):
        self._run_cat_success(MountsQuickAction, "mounts.log")

    # ---- missing source (must fail gracefully) ------------------------

    def test_cifsstats_missing_source(self):
        self._run_cat_missing(CifsstatsQuickAction, "cifsstats.log")

    def test_debugdata_missing_source(self):
        self._run_cat_missing(DebugDataQuickAction, "debug_data.log")

    def test_mounts_missing_source(self):
        self._run_cat_missing(MountsQuickAction, "mounts.log")

    # ---- empty source (legitimate 0-byte output) ----------------------

    def test_cifsstats_empty_source(self):
        self._run_cat_empty(CifsstatsQuickAction, "cifsstats.log")

    def test_mounts_empty_source(self):
        self._run_cat_empty(MountsQuickAction, "mounts.log")


class TestCmdQuickActions(unittest.TestCase):
    """`cmd`-type quick actions: run a subprocess and dump its stdout."""

    def _assert_nonempty_collected(self, action_cls, expected_filename, payload):
        with tempfile.TemporaryDirectory() as td:
            batches = Path(td) / "batches"
            action = action_cls(str(batches))

            # Patch the create_subprocess_exec used inside QuickAction so
            # the action's argv is never actually executed.
            with mock.patch(
                "base.QuickAction.asyncio.create_subprocess_exec",
                new=_fake_subprocess_factory(payload),
            ):
                asyncio.run(action.execute("batch-ok"))

            out = batches / "aod_quick_batch-ok" / expected_filename
            self.assertTrue(out.exists(), f"{expected_filename} not produced")
            self.assertEqual(out.read_bytes(), payload)
            self.assertGreater(out.stat().st_size, 0, "output unexpectedly empty")
            if __debug__:
                self.assertEqual(action.executions, 1)
                self.assertEqual(action.failures, 0)

    def _assert_empty_stdout_not_written(self, action_cls, expected_filename):
        """When the subprocess emits zero bytes, QuickAction skips the
        write -- so the test treats a missing output *file* as the
        expected outcome rather than an unexpected empty file. The batch
        directory may still exist (created eagerly before the exec) but
        must contain no stub artifacts."""
        with tempfile.TemporaryDirectory() as td:
            batches = Path(td) / "batches"
            action = action_cls(str(batches))

            with mock.patch(
                "base.QuickAction.asyncio.create_subprocess_exec",
                new=_fake_subprocess_factory(b""),
            ):
                asyncio.run(action.execute("batch-empty"))

            out = batches / "aod_quick_batch-empty" / expected_filename
            self.assertFalse(
                out.exists(),
                "no output file should be created for empty subprocess stdout",
            )
            batch_dir = out.parent
            if batch_dir.exists():
                self.assertEqual(
                    list(batch_dir.iterdir()),
                    [],
                    "batch dir must not contain stub artifacts on empty stdout",
                )
            if __debug__:
                # Empty stdout is still a successful execution, not a failure.
                self.assertEqual(action.executions, 1)
                self.assertEqual(action.failures, 0)

    # ---- argv shape ---------------------------------------------------

    def test_dmesg_argv_uses_interval(self):
        argv, kind = DmesgQuickAction("/tmp", anomaly_interval=42).get_command()
        self.assertEqual(kind, "cmd")
        self.assertEqual(argv, ["journalctl", "-k", "--since", "42 seconds ago"])

    def test_journalctl_argv_uses_interval(self):
        argv, kind = JournalctlQuickAction("/tmp", anomaly_interval=7).get_command()
        self.assertEqual(kind, "cmd")
        self.assertEqual(argv, ["journalctl", "--since", "7 seconds ago"])

    def test_syslogs_argv_uses_num_lines(self):
        argv, kind = SysLogsQuickAction("/tmp", num_lines=250).get_command()
        self.assertEqual(kind, "cmd")
        self.assertEqual(argv, ["tail", "-n250", "/var/log/syslog"])

    def test_smbinfo_argv(self):
        argv, kind = SmbinfoQuickAction("/tmp").get_command()
        self.assertEqual(kind, "cmd")
        self.assertEqual(argv, ["smbinfo", "-h", "filebasicinfo"])

    # ---- happy path ---------------------------------------------------

    def test_dmesg_collects(self):
        self._assert_nonempty_collected(
            DmesgQuickAction, "dmesg.log", b"[ 0.0] kernel: hello\n"
        )

    def test_journalctl_collects(self):
        self._assert_nonempty_collected(
            JournalctlQuickAction,
            "journalctl.log",
            b"Jun 23 12:00:00 host systemd[1]: Started unit\n",
        )

    def test_syslogs_collects(self):
        self._assert_nonempty_collected(
            SysLogsQuickAction,
            "syslogs.log",
            b"Jun 23 12:00:01 host kernel: boot complete\n",
        )

    def test_smbinfo_collects(self):
        self._assert_nonempty_collected(
            SmbinfoQuickAction,
            "smbinfo.log",
            b"FileBasicInformation:\n  CreationTime: ...\n",
        )

    # ---- empty stdout (acceptable; must not create empty file) --------

    def test_dmesg_empty_stdout_not_written(self):
        self._assert_empty_stdout_not_written(DmesgQuickAction, "dmesg.log")

    def test_journalctl_empty_stdout_not_written(self):
        self._assert_empty_stdout_not_written(
            JournalctlQuickAction, "journalctl.log"
        )

    def test_syslogs_empty_stdout_not_written(self):
        self._assert_empty_stdout_not_written(SysLogsQuickAction, "syslogs.log")

    def test_smbinfo_empty_stdout_not_written(self):
        self._assert_empty_stdout_not_written(SmbinfoQuickAction, "smbinfo.log")


class TestQuickActionOutputLayout(unittest.TestCase):
    """Cross-cutting invariants the base class enforces for every
    QuickAction subclass."""

    def test_output_path_uses_batch_subdir(self):
        with tempfile.TemporaryDirectory() as td:
            action = MountsQuickAction(td)
            self.assertEqual(
                action.get_output_path("xyz"),
                str(Path(td) / "aod_quick_xyz" / "mounts.log"),
            )
            self.assertEqual(
                action.get_output_dir("xyz"),
                str(Path(td) / "aod_quick_xyz"),
            )

    def test_log_filenames_are_distinct(self):
        """Two QuickActions sharing the same batch must not overwrite
        each other -- guarded by distinct `log_filename` per subclass."""
        roots = "/tmp"
        filenames = {
            CifsstatsQuickAction(roots).log_filename,
            DebugDataQuickAction(roots).log_filename,
            DmesgQuickAction(roots).log_filename,
            JournalctlQuickAction(roots).log_filename,
            MountsQuickAction(roots).log_filename,
            SmbinfoQuickAction(roots).log_filename,
            SysLogsQuickAction(roots).log_filename,
        }
        self.assertEqual(len(filenames), 7, "log_filename collision across actions")


if __name__ == "__main__":
    unittest.main()
