"""End-to-end integration tests for the AODv2 daemon.

Runs AODv2 in two transports and verifies signal handling + bundle
contents across the full production anomaly surface:

  * smb.latency  -> smbslower  (eBPF)
  * smb.sockconn -> ss         (userspace)
  * nfs.latency  -> nfsslower  (eBPF)
  * nfs.sockconn -> ss         (userspace)
  * nfs.error    -> nfsiosnoop (eBPF)

Quick-action coverage is the union of every handler the source tree
provides (dmesg, journalctl, mounts, syslogs, stats, debugdata,
smbinfo), so each shutdown/snapshot bundle must aggregate them all
(modulo SMB or NFS-specific files on hosts without cifs.ko loaded).

Two suites:

  TestControllerIntegration         -- bare subprocess.Popen, signals
                                       delivered with proc.send_signal()
  TestControllerSystemdIntegration  -- transient systemd unit mirroring
                                       aodv2.service; signals delivered
                                       via systemctl kill / stop, cgroup
                                       inspected for orphans

Skipped automatically when the host can't run the daemon:
  - not root
  - eBPF binaries missing under src/bin/
  - zstandard not installed
"""

import io
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from conftest import install_fake_tcpdump, install_fake_tracecmd

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(REPO_ROOT, "src")
BIN_DIR = os.path.join(SRC_DIR, "bin")

# eBPF binaries that the comprehensive test config needs. Each one
# corresponds to a tool listed in CONFIG_TEMPLATE; if a host is missing
# any of them, Controller would crash one of its supervisors on startup
# and the integration tests would be measuring noise rather than
# behaviour.
_REQUIRED_BINS = ("smbslower", "nfsslower", "nfsiosnoop", "libringbuf_shim.so")

_HAS_ROOT = os.geteuid() == 0
_HAS_BINS = all(os.path.exists(os.path.join(BIN_DIR, n)) for n in _REQUIRED_BINS)
try:
    import zstandard  # noqa: F401

    _HAS_DEPS = True
except ImportError:
    _HAS_DEPS = False

_SKIP_REASON = None
if not _HAS_ROOT:
    _SKIP_REASON = "requires root (eBPF, signal delivery to daemon)"
elif not _HAS_BINS:
    missing = sorted(
        n for n in _REQUIRED_BINS if not os.path.exists(os.path.join(BIN_DIR, n))
    )
    _SKIP_REASON = f"eBPF binaries missing under {BIN_DIR}: {missing}"
elif not _HAS_DEPS:
    _SKIP_REASON = "zstandard not installed"


CONFIG_TEMPLATE = """\
watch_interval_sec: 1
aod_output_dir: {output_dir}

anomalies:
  smb:
    latency:
      tool: "smbslower"
      mode: "all"
      acceptable_count: 10
      default_threshold_ms: 20
      track_commands:
        - command: SMB2_WRITE
          threshold: 0
      actions:
        dmesg:
        journalctl:
        debugdata:
        stats:
        mounts:
        smbinfo:
        syslogs:
        tcpdump: ["-s", "65536", "-C", "1", "-W", "2"]
    sockconn:
      tool: "ss"
      actions:
        dmesg:
        journalctl:
        stats:
        tcpdump: ["-s", "65536", "-C", "1", "-W", "2"]

  nfs:
    latency:
      tool: "nfsslower"
      mode: "all"
      acceptable_count: 10
      default_threshold_ms: 50
      actions:
        dmesg:
        journalctl:
        syslogs:
        mounts:
        trace-cmd: ["-e", "nfs", "-e", "nfs4", "-e", "sunrpc", "-b", "1024"]
    sockconn:
      tool: "ss"
      actions:
        dmesg:
    error:
      tool: "nfsiosnoop"
      acceptable_count: 5
      track_commands:
      track_errors:
        - NFS4ERR_BAD_STATEID
        - NFS4ERR_OLD_STATEID
      actions:
        dmesg:
        journalctl:
        syslogs:
        trace-cmd: ["-e", "nfs", "-e", "nfs4", "-e", "sunrpc", "-b", "1024"]

cleanup:
  cleanup_interval_sec: 60
  max_log_age_days: 2
  max_total_log_size_mb: 50

audit:
  enabled: true
"""

# Bundle members produced by LogCollector for a full-system (AOD)
# snapshot/shutdown event, given CONFIG_TEMPLATE above.
#
# `_ALWAYS` files come from QuickActions that don't depend on
# kernel-side state -- they will always be present and non-empty on any
# Linux host running this test.
#
# `_MAYBE` files come from QuickActions that read SMB/NFS-specific procfs
# paths or invoke smbinfo. On hosts without cifs.ko loaded (or without
# the smbinfo binary), QuickAction.execute()'s graceful-failure path
# omits the file from the bundle rather than writing a zero-byte
# placeholder. We don't require these files, but if one IS in the
# bundle it must be non-empty (otherwise the graceful-failure contract
# regressed).

_EXPECTED_QUICK_FILES_ALL = {
    "dmesg.log",
    "journalctl.log",
    "mounts.log",
    "syslogs.log",
    "cifsstats.log",
    "debug_data.log",
    "smbinfo.log",
}

# eBPF supervisors that Controller must spawn for the comprehensive
# config. Used to assert the cgroup membership in the systemd suite.
_EXPECTED_EBPF_TOOLS = frozenset({"smbslower", "nfsslower", "nfsiosnoop"})

_EXPECTED_CAPTURE_PROTOCOLS = frozenset({"smb", "nfs"})

# Mirror of src/LogCollector.LONG_CAPTURE_RESTART_DELAY_SEC -- the
# cooldown window the LongCapture supervisor enforces between a
# snapshot bundle and accepting the next snapshot request. Used by
# test_02 to size the wait after sending two SIGUSR1s.
_LONG_CAPTURE_RESTART_DELAY_SEC = 1.0


def _bundle_file_sizes(tar_path: str) -> dict[str, int]:
    """Return {basename: size_bytes} for regular files inside a .tar.zst
    bundle. Directory entries are excluded."""
    dctx = zstandard.ZstdDecompressor()
    with open(tar_path, "rb") as f, dctx.stream_reader(f) as reader:
        data = reader.read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        return {
            os.path.basename(m.name): m.size for m in tar.getmembers() if m.isfile()
        }


def _wait_for(predicate, timeout: float, interval: float = 0.1) -> bool:
    """Poll predicate() until truthy or timeout. Returns its final value."""
    deadline = time.monotonic() + timeout
    last = predicate()
    while not last and time.monotonic() < deadline:
        time.sleep(interval)
        last = predicate()
    return last


def _assert_full_system_bundle(test_case: unittest.TestCase, bundle: str) -> None:
    """Shared bundle invariant: the full-system (AOD) snapshot/shutdown
    bundle must contain every always-on quick-action file, may contain
    any of the SMB-specific files, must contain nothing else, and must
    not contain any zero-byte files."""
    sizes = _bundle_file_sizes(bundle)
    members = set(sizes)

    unexpected = members - _EXPECTED_QUICK_FILES_ALL
    test_case.assertFalse(
        unexpected, f"unexpected bundle members {unexpected} in {bundle}: {sizes}"
    )

    missing_required = _EXPECTED_QUICK_FILES_ALL - members
    test_case.assertFalse(
        missing_required,
        f"required bundle members missing from {bundle}: {missing_required} "
        f"(got {sorted(members)})",
    )

    empty = sorted(name for name, sz in sizes.items() if sz == 0)
    test_case.assertFalse(empty, f"empty files in {bundle}: {empty} (sizes={sizes})")


def _wait_for_capture_bundles(
    batches_dir: str,
    event_suffix: str,
    before: set[str],
    timeout: float,
) -> set[str]:
    """Wait for one capture bundle per configured protocol to appear in
    `batches_dir`, named aod_capture_*_<event>_<protocol>.tar.zst.

    Returns the newly-appeared bundle paths (the union across protocols),
    or whatever subset arrived before the timeout -- the caller is
    expected to assert completeness.
    """

    def _new_caps() -> set[str]:
        seen = set()
        for proto in _EXPECTED_CAPTURE_PROTOCOLS:
            for name in os.listdir(batches_dir):
                if name.startswith("aod_capture_") and name.endswith(
                    f"{event_suffix}_{proto}.tar.zst"
                ):
                    seen.add(os.path.join(batches_dir, name))
        return seen - before

    deadline = time.monotonic() + timeout
    found = _new_caps()
    while (
        len(found) < len(_EXPECTED_CAPTURE_PROTOCOLS) and time.monotonic() < deadline
    ):
        time.sleep(0.1)
        found = _new_caps()
    return found


def _assert_capture_bundles(
    test_case: unittest.TestCase,
    batches_dir: str,
    event_suffix: str,
    before: set[str],
    timeout: float = 15.0,
) -> None:
    """Shared invariant: every protocol with a configured long capture
    must produce exactly one new bundle per full-system event, and each
    bundle must be non-empty."""
    found = _wait_for_capture_bundles(batches_dir, event_suffix, before, timeout)
    by_proto = {}
    for path in found:
        for proto in _EXPECTED_CAPTURE_PROTOCOLS:
            if path.endswith(f"{event_suffix}_{proto}.tar.zst"):
                by_proto.setdefault(proto, []).append(path)
                break
    missing = _EXPECTED_CAPTURE_PROTOCOLS - set(by_proto)
    test_case.assertFalse(
        missing,
        f"no capture bundle for protocols {sorted(missing)} after "
        f"{event_suffix} (saw {sorted(found)})",
    )
    for proto, paths in by_proto.items():
        test_case.assertEqual(
            len(paths),
            1,
            f"expected exactly one {proto} capture bundle, got {paths}",
        )
        test_case.assertGreater(
            os.path.getsize(paths[0]),
            0,
            f"{proto} capture bundle is empty: {paths[0]}",
        )


@unittest.skipIf(_SKIP_REASON is not None, _SKIP_REASON)
class TestControllerIntegration(unittest.TestCase):
    """Spawn Controller as a plain subprocess, signal it directly, and
    inspect produced bundles. No systemd involved."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="aodv2_it_")
        cls.output_dir = cls._tmp.name
        cls.batches_dir = os.path.join(cls.output_dir, "batches")
        os.makedirs(cls.batches_dir, exist_ok=True)

        cls.config_path = os.path.join(cls.output_dir, "config.yaml")
        with open(cls.config_path, "w") as f:
            f.write(CONFIG_TEMPLATE.format(output_dir=cls.output_dir))

        cls.stderr_path = os.path.join(cls.output_dir, "stderr.log")
        cls.stderr_fh = open(cls.stderr_path, "wb")

        # Install fake long-capture binaries so the supervisors can run
        # without real tcpdump/trace-cmd.
        cls.fake_tcpdump = install_fake_tcpdump(Path(cls.output_dir))
        cls.fake_tracecmd = install_fake_tracecmd(Path(cls.output_dir))

        env = os.environ.copy()
        env["PYTHONPATH"] = SRC_DIR
        env["AOD_CONFIG"] = cls.config_path
        env["AOD_LOG_STDERR"] = "1"
        env["AOD_LOG_LEVEL"] = "INFO"
        env["AOD_TCPDUMP_BIN"] = str(cls.fake_tcpdump)
        env["AOD_TRACECMD_BIN"] = str(cls.fake_tracecmd)

        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(SRC_DIR, "Controller.py")],
            env=env,
            stdout=cls.stderr_fh,
            stderr=cls.stderr_fh,
            cwd=REPO_ROOT,
        )

        ready = _wait_for(
            lambda: cls._log_contains("started successfully"),
            timeout=20,
        )
        if not ready:
            cls._teardown_proc()
            raise RuntimeError(
                f"Controller never reported ready. Log:\n{cls._read_log()}"
            )

    @classmethod
    def tearDownClass(cls):
        cls._teardown_proc()
        cls.stderr_fh.close()
        cls._tmp.cleanup()

    @classmethod
    def _teardown_proc(cls):
        if cls.proc.poll() is None:
            cls.proc.send_signal(signal.SIGTERM)
            try:
                cls.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(timeout=5)

    @classmethod
    def _read_log(cls) -> str:
        cls.stderr_fh.flush()
        with open(cls.stderr_path, "r", errors="replace") as f:
            return f.read()

    @classmethod
    def _log_contains(cls, needle: str) -> bool:
        return needle in cls._read_log()

    def _bundles_matching(self, suffix: str) -> list[str]:
        return sorted(
            os.path.join(self.batches_dir, n)
            for n in os.listdir(self.batches_dir)
            if n.endswith(suffix)
        )

    # ---- ordered tests ----
    # Order matters: snapshot must run before shutdown because shutdown
    # kills the daemon. unittest sorts by name, so the leading numbers
    # control execution order.

    def test_01_snapshot_on_sigusr1(self):
        """SIGUSR1 must produce one aod_snapshot quick-action bundle
        aggregating every configured quick action, plus one long-capture
        bundle per protocol."""
        before = set(self._bundles_matching("_aod_snapshot.tar.zst"))
        captures_before = {
            os.path.join(self.batches_dir, n)
            for n in os.listdir(self.batches_dir)
            if n.startswith("aod_capture_")
        }
        self.proc.send_signal(signal.SIGUSR1)

        def new_bundle():
            now = set(self._bundles_matching("_aod_snapshot.tar.zst"))
            return (now - before) or None

        new = _wait_for(new_bundle, timeout=10)
        self.assertTrue(
            new,
            f"no snapshot bundle appeared. Log tail:\n"
            f"{self._read_log()[-2000:]}",
        )
        (bundle,) = new
        _assert_full_system_bundle(self, bundle)
        _assert_capture_bundles(
            self, self.batches_dir, "_aod_snapshot", captures_before
        )

    def test_02_consecutive_snapshots_coalesce(self):
        """Two SIGUSR1s arriving while the LongCapture supervisor is
        still bundling or warming up from the first request must produce
        TWO quick bundles but only ONE capture bundle per protocol: the
        second snapshot's capture request lands during cooldown and is
        dropped with a log line pointing the operator at the bundle that
        holds its capture context."""
        # Wait for the captures to be in their idle (post-cooldown) state
        # so the first SIGUSR1 triggers the bundle path, not a drop.
        ready = _wait_for(
            lambda: (
                "tcpdump capture for smb ready after cooldown" in self._read_log()
                and "trace-cmd capture for nfs ready after cooldown"
                in self._read_log()
            ),
            timeout=10,
        )
        self.assertTrue(
            ready,
            f"capture supervisors did not become ready after test_01 "
            f"snapshot. Log tail:\n{self._read_log()[-2000:]}",
        )

        quick_before = set(self._bundles_matching("_aod_snapshot.tar.zst"))
        captures_before = {
            os.path.join(self.batches_dir, n)
            for n in os.listdir(self.batches_dir)
            if n.startswith("aod_capture_") and "_aod_snapshot_" in n
        }
        log_before_len = len(self._read_log())

        # Second SIGUSR1 needs a small gap from the first so that Linux
        # doesn't coalesce the two non-realtime signals at the kernel
        # level before the controller's handler can run for each. 100ms
        # is comfortably more than the handler latency and well under
        # restart_delay_sec (1s), so the second snapshot lands while
        # _is_cooldown is True.
        self.proc.send_signal(signal.SIGUSR1)
        time.sleep(0.1)
        self.proc.send_signal(signal.SIGUSR1)

        # Two distinct aod_snapshot quick bundles must appear -- quick
        # actions are NOT coalesced; each SIGUSR1 always produces its
        # own quick tarball.
        def two_quick():
            now = set(self._bundles_matching("_aod_snapshot.tar.zst"))
            new = now - quick_before
            return new if len(new) == 2 else None

        new_quick = _wait_for(two_quick, timeout=15)
        self.assertTrue(
            new_quick,
            f"expected 2 new snapshot quick bundles, got "
            f"{(set(self._bundles_matching('_aod_snapshot.tar.zst')) - quick_before)}. "
            f"Log:\n{self._read_log()[-2000:]}",
        )

        # Capture bundles are coalesced: only ONE new capture bundle per
        # protocol should materialise from the two SIGUSR1s. Wait long
        # enough for the bundle+respawn+warmup cycle to complete.
        time.sleep(_LONG_CAPTURE_RESTART_DELAY_SEC + 1.0)

        captures_after = {
            os.path.join(self.batches_dir, n)
            for n in os.listdir(self.batches_dir)
            if n.startswith("aod_capture_") and "_aod_snapshot_" in n
        }
        new_captures = captures_after - captures_before
        by_proto: dict[str, list[str]] = {}
        for path in new_captures:
            for proto in _EXPECTED_CAPTURE_PROTOCOLS:
                if path.endswith(f"_aod_snapshot_{proto}.tar.zst"):
                    by_proto.setdefault(proto, []).append(path)
                    break

        for proto in _EXPECTED_CAPTURE_PROTOCOLS:
            self.assertEqual(
                len(by_proto.get(proto, [])),
                1,
                f"expected exactly one coalesced capture bundle for "
                f"{proto}, got {by_proto.get(proto, [])}. "
                f"Log:\n{self._read_log()[-2000:]}",
            )

        # The second SIGUSR1's snapshot request for each protocol must
        # be logged as dropped due to cooldown.
        log_tail = self._read_log()[log_before_len:]
        self.assertIn(
            "DROPPED snapshot",
            log_tail,
            f"expected a 'DROPPED snapshot' warning for the second "
            f"SIGUSR1 landing during cooldown. Log:\n{log_tail[-2000:]}",
        )
        self.assertIn(
            "is in cooldown",
            log_tail,
            f"DROPPED snapshot warning must mention cooldown reason. "
            f"Log:\n{log_tail[-2000:]}",
        )

    def test_03_shutdown_on_sigterm(self):
        """SIGTERM must produce one aod_shutdown bundle, one capture
        bundle per protocol, and exit cleanly."""
        before = set(self._bundles_matching("_aod_shutdown.tar.zst"))
        captures_before = {
            os.path.join(self.batches_dir, n)
            for n in os.listdir(self.batches_dir)
            if n.startswith("aod_capture_")
        }
        # test_02 left the captures in cooldown after their last bundle.
        # Wait until each supervisor has cleared cooldown (logged "ready
        # after cooldown") before sending SIGTERM, otherwise the shutdown
        # snapshot will be dropped and no aod_shutdown capture bundle is
        # produced.
        ready = _wait_for(
            lambda: (
                self._read_log().count(
                    "tcpdump capture for smb ready after cooldown"
                )
                >= 2
                and self._read_log().count(
                    "trace-cmd capture for nfs ready after cooldown"
                )
                >= 2
            ),
            timeout=10,
        )
        self.assertTrue(
            ready,
            f"capture supervisors did not finish cooldown after test_02. "
            f"Log tail:\n{self._read_log()[-2000:]}",
        )
        self.proc.send_signal(signal.SIGTERM)

        try:
            rc = self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.fail(
                f"Controller did not exit within 20s after SIGTERM. Log:\n"
                f"{self._read_log()[-2000:]}"
            )
        self.assertEqual(rc, 0, f"non-zero exit: {rc}\n{self._read_log()[-2000:]}")

        after = set(self._bundles_matching("_aod_shutdown.tar.zst"))
        new = after - before
        self.assertEqual(
            len(new),
            1,
            f"expected exactly one new shutdown bundle, got {new}. Log:\n"
            f"{self._read_log()[-2000:]}",
        )
        _assert_full_system_bundle(self, next(iter(new)))
        # LogCollector._shutdown_captures awaits in-flight capture
        # bundling before the process exits, so by the time wait() above
        # returned the bundles must already be on disk.
        _assert_capture_bundles(
            self, self.batches_dir, "_aod_shutdown", captures_before, timeout=5.0
        )


# ---------------------------------------------------------------------------
# Systemd integration
# ---------------------------------------------------------------------------

_HAS_SYSTEMD = os.path.isdir("/run/systemd/system")
_HAS_SYSTEMD_RUN = shutil.which("systemd-run") is not None
_HAS_SYSTEMCTL = shutil.which("systemctl") is not None
_HAS_JOURNALCTL = shutil.which("journalctl") is not None

_SYSTEMD_SKIP = None
if _SKIP_REASON is not None:
    _SYSTEMD_SKIP = _SKIP_REASON
elif not _HAS_SYSTEMD:
    _SYSTEMD_SKIP = "systemd not running on this host (/run/systemd/system missing)"
elif not (_HAS_SYSTEMD_RUN and _HAS_SYSTEMCTL and _HAS_JOURNALCTL):
    _SYSTEMD_SKIP = "systemd-run/systemctl/journalctl not in PATH"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _proc_comm(pid: int) -> str:
    """Read /proc/<pid>/comm; empty string on any failure."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


@unittest.skipIf(_SYSTEMD_SKIP is not None, _SYSTEMD_SKIP)
class TestControllerSystemdIntegration(unittest.TestCase):
    """Run Controller under a transient systemd unit that mirrors the
    production aodv2.service (Type=simple, KillMode=mixed,
    KillSignal=SIGTERM) and verify it behaves as a well-formed daemon:

      * reaches active(running) with a real MainPID
      * supervises every configured eBPF tool inside the unit's cgroup
      * responds to ``systemctl kill --signal=SIGUSR1`` with a snapshot
      * stops cleanly on ``systemctl stop`` (SIGTERM) -> Result=success,
        ExecMainStatus=0, shutdown bundle written
      * leaves no orphan processes -- systemd never has to escalate to
        the SIGKILL sweep of KillMode=mixed
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="aodv2_systemd_it_")
        cls.output_dir = cls._tmp.name
        cls.batches_dir = os.path.join(cls.output_dir, "batches")
        os.makedirs(cls.batches_dir, exist_ok=True)

        cls.config_path = os.path.join(cls.output_dir, "config.yaml")
        with open(cls.config_path, "w") as f:
            f.write(CONFIG_TEMPLATE.format(output_dir=cls.output_dir))

        cls.unit_name = f"aodv2-it-{os.getpid()}.service"
        # Defensive: if a previous aborted run left this name failed.
        subprocess.run(
            ["systemctl", "reset-failed", cls.unit_name],
            capture_output=True,
        )

        # Install fake long-capture binaries
        cls.fake_tcpdump = install_fake_tcpdump(Path(cls.output_dir))
        cls.fake_tracecmd = install_fake_tracecmd(Path(cls.output_dir))

        # Mirror aodv2.service properties. Restart=no keeps the test
        # deterministic; TimeoutStopSec is shortened so a stuck shutdown
        # fails the test instead of hanging CI for 90s.
        rc = subprocess.run(
            [
                "systemd-run",
                f"--unit={cls.unit_name}",
                "--service-type=simple",
                "--property=KillMode=mixed",
                "--property=KillSignal=SIGTERM",
                "--property=TimeoutStopSec=30",
                "--property=Restart=no",
                f"--setenv=PYTHONPATH={SRC_DIR}",
                f"--setenv=AOD_CONFIG={cls.config_path}",
                "--setenv=AOD_LOG_LEVEL=INFO",
                "--setenv=AOD_SYSLOG_LEVEL=INFO",
                f"--setenv=AOD_TCPDUMP_BIN={cls.fake_tcpdump}",
                f"--setenv=AOD_TRACECMD_BIN={cls.fake_tracecmd}",
                "--",
                sys.executable,
                os.path.join(SRC_DIR, "Controller.py"),
            ],
            capture_output=True,
            text=True,
        )
        if rc.returncode != 0:
            raise RuntimeError(
                f"systemd-run failed (rc={rc.returncode}):\n"
                f"stdout: {rc.stdout}\nstderr: {rc.stderr}"
            )

        # The unit must reach active(running) AND log its ready marker.
        # ActiveState alone isn't enough: Type=simple flips to active the
        # moment the binary execs, well before Controller.run() finishes
        # spinning up its supervisors.
        ready = _wait_for(
            lambda: cls._unit_substate() == "running"
            and cls._journal_contains("started successfully"),
            timeout=30,
        )
        if not ready:
            journal = cls._journal_tail()
            cls._stop_unit()
            raise RuntimeError(
                f"Unit never became ready. ActiveState="
                f"{cls._show('ActiveState')} SubState="
                f"{cls._show('SubState')}\nJournal:\n{journal}"
            )

    @classmethod
    def tearDownClass(cls):
        cls._stop_unit()
        cls._tmp.cleanup()

    # ---- helpers ----

    @classmethod
    def _stop_unit(cls):
        # Best-effort cleanup; the destructive test_03 already stops it.
        subprocess.run(
            ["systemctl", "stop", cls.unit_name],
            capture_output=True,
            timeout=60,
        )
        subprocess.run(
            ["systemctl", "reset-failed", cls.unit_name],
            capture_output=True,
        )

    @classmethod
    def _show(cls, prop: str) -> str:
        rc = subprocess.run(
            ["systemctl", "show", cls.unit_name, "-p", prop, "--value"],
            capture_output=True,
            text=True,
        )
        return rc.stdout.strip()

    @classmethod
    def _unit_substate(cls) -> str:
        return cls._show("SubState")

    @classmethod
    def _journal_tail(cls, n: int = 500) -> str:
        rc = subprocess.run(
            [
                "journalctl",
                "-u",
                cls.unit_name,
                "-n",
                str(n),
                "--no-pager",
                "-o",
                "short",
            ],
            capture_output=True,
            text=True,
        )
        return rc.stdout

    @classmethod
    def _journal_contains(cls, needle: str) -> bool:
        return needle in cls._journal_tail(n=1000)

    @classmethod
    def _cgroup_procs(cls) -> list[int]:
        """PIDs currently in the unit's cgroup (cgroup v2). Empty list
        if the unit has no cgroup, or the procs file is unreadable."""
        cg = cls._show("ControlGroup")
        if not cg:
            return []
        procs_file = os.path.join("/sys/fs/cgroup", cg.lstrip("/"), "cgroup.procs")
        try:
            with open(procs_file) as f:
                return [int(x) for x in f.read().split() if x.strip()]
        except (FileNotFoundError, PermissionError):
            return []

    def _bundles_matching(self, suffix: str) -> list[str]:
        return sorted(
            os.path.join(self.batches_dir, n)
            for n in os.listdir(self.batches_dir)
            if n.endswith(suffix)
        )

    # ---- ordered tests ----
    # Same naming trick as the in-process integration class above:
    # test_03 stops the unit, so it must run last.

    def test_01_unit_active_with_all_ebpf_supervisors(self):
        """Type=simple unit must be active(running) with MainPID set and
        every configured eBPF tool present in the cgroup as a distinct
        process."""
        self.assertEqual(self._show("ActiveState"), "active")
        self.assertEqual(self._show("SubState"), "running")

        main_pid = int(self._show("MainPID"))
        self.assertGreater(main_pid, 0, "MainPID not set on active unit")
        self.assertTrue(
            _pid_alive(main_pid),
            f"MainPID {main_pid} reported by systemd is not alive",
        )

        procs = self._cgroup_procs()
        if not procs:
            self.skipTest("cgroup v2 procs file not readable on this host")
        self.assertIn(
            main_pid,
            procs,
            f"MainPID {main_pid} not in unit cgroup {procs}",
        )

        # Every configured eBPF tool must show up in /proc/<pid>/comm
        # for at least one cgroup member. /proc/<pid>/comm is truncated
        # to 15 chars (TASK_COMM_LEN), and all our tool names fit. ss
        # and the userspace handlers don't fork from Controller, so they
        # don't contribute cgroup PIDs.
        comms = {_proc_comm(pid) for pid in procs if pid != main_pid}
        missing = _EXPECTED_EBPF_TOOLS - comms
        self.assertFalse(
            missing,
            f"eBPF supervisors not running in cgroup. "
            f"missing={missing} children_comms={comms} pids={procs}",
        )

    def test_02_sigusr1_via_systemctl_kill_produces_snapshot(self):
        """``systemctl kill --signal=SIGUSR1 --kill-whom=main`` must reach
        the daemon's SIGUSR1 handler and produce one full-system bundle
        plus one capture bundle per protocol."""
        before = set(self._bundles_matching("_aod_snapshot.tar.zst"))
        captures_before = {
            os.path.join(self.batches_dir, n)
            for n in os.listdir(self.batches_dir)
            if n.startswith("aod_capture_")
        }

        rc = subprocess.run(
            [
                "systemctl",
                "kill",
                "--signal=SIGUSR1",
                "--kill-whom=main",
                self.unit_name,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(rc.returncode, 0, f"systemctl kill failed: {rc.stderr}")

        new = _wait_for(
            lambda: set(self._bundles_matching("_aod_snapshot.tar.zst")) - before,
            timeout=15,
        )
        self.assertTrue(
            new,
            f"no snapshot bundle appeared after SIGUSR1. Journal tail:\n"
            f"{self._journal_tail(n=200)}",
        )
        self.assertEqual(
            len(new), 1, f"expected exactly one new snapshot bundle, got {new}"
        )
        _assert_full_system_bundle(self, next(iter(new)))
        _assert_capture_bundles(
            self, self.batches_dir, "_aod_snapshot", captures_before
        )

        # Unit must still be running -- SIGUSR1 is non-terminating.
        self.assertEqual(self._show("SubState"), "running")

    def test_03_systemctl_stop_clean_shutdown_no_orphans(self):
        """``systemctl stop`` (SIGTERM via KillSignal) must:
        - trigger the SHUTDOWN dump (one full-system bundle written),
        - exit before TimeoutStopSec (Result=success, ExecMainStatus=0),
        - leave no orphan processes alive,
        - never make systemd escalate to KillMode=mixed's SIGKILL sweep.
        """
        before_shutdown = set(self._bundles_matching("_aod_shutdown.tar.zst"))
        captures_before = {
            os.path.join(self.batches_dir, n)
            for n in os.listdir(self.batches_dir)
            if n.startswith("aod_capture_")
        }
        pids_before = self._cgroup_procs()
        self.assertTrue(
            pids_before, "cgroup empty before stop -- unit already gone?"
        )

        # test_02 just triggered a snapshot per protocol, which puts each
        # LongCapture supervisor into post-bundle cooldown for
        # restart_delay_sec. Stopping the unit while cooldown is still
        # active would cause the SHUTDOWN snapshot to be dropped at
        # snapshot() time, and no aod_shutdown capture bundle would be
        # produced. Wait until each supervisor has cleared cooldown
        # (logged "ready after cooldown") before issuing the stop.
        ready = _wait_for(
            lambda: (
                "tcpdump capture for smb ready after cooldown"
                in self._journal_tail(n=500)
                and "trace-cmd capture for nfs ready after cooldown"
                in self._journal_tail(n=500)
            ),
            timeout=10,
        )
        self.assertTrue(
            ready,
            f"capture supervisors did not finish cooldown after test_02. "
            f"Journal tail:\n{self._journal_tail(n=200)}",
        )

        # systemctl stop blocks until the unit is inactive (or
        # TimeoutStopSec expires and systemd SIGKILLs). The outer
        # subprocess timeout is a hard ceiling above TimeoutStopSec.
        rc = subprocess.run(
            ["systemctl", "stop", self.unit_name],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            rc.returncode,
            0,
            f"systemctl stop failed: {rc.stderr}\nJournal:\n"
            f"{self._journal_tail(n=200)}",
        )

        self.assertEqual(self._show("ActiveState"), "inactive")
        self.assertEqual(
            self._show("Result"),
            "success",
            f"unit did not stop cleanly. Journal:\n" f"{self._journal_tail(n=200)}",
        )
        # ExecMainStatus is the main process's exit status. Anything
        # non-zero (or killed by signal) means the daemon didn't return
        # cleanly from run().
        self.assertEqual(
            self._show("ExecMainStatus"),
            "0",
            f"main process exited non-zero. Journal:\n"
            f"{self._journal_tail(n=200)}",
        )

        # Shutdown bundle: SIGTERM -> Controller.stop() ->
        # trigger_snapshot(SHUTDOWN) -> LogCollector writes the
        # *_aod_shutdown.tar.zst with the full quick-action union, plus
        # one *_aod_shutdown_<proto>.tar.zst per protocol that has a
        # configured long capture.
        after_shutdown = set(self._bundles_matching("_aod_shutdown.tar.zst"))
        new = after_shutdown - before_shutdown
        self.assertEqual(
            len(new),
            1,
            f"expected exactly one new shutdown bundle, got {new}",
        )
        _assert_full_system_bundle(self, next(iter(new)))
        _assert_capture_bundles(
            self, self.batches_dir, "_aod_shutdown", captures_before, timeout=5.0
        )

        # No orphans: every PID that lived in the cgroup is gone. systemd
        # only marks the unit inactive after its cgroup is empty, so this
        # is mostly a sanity check that nothing escaped the cgroup (e.g.
        # via daemonize/double-fork into a different scope).
        leftover = [pid for pid in pids_before if _pid_alive(pid)]
        self.assertEqual(
            leftover,
            [],
            f"orphan PIDs survived systemctl stop: {leftover}",
        )

        # KillMode=mixed escalation check: if Controller failed to reap
        # its eBPF children before exiting, systemd would log a
        # "Killing process ... with signal SIGKILL" line as part of the
        # final cgroup sweep. That's a Controller bug -- it means
        # pdeathsig / the process supervisor's SIGINT->SIGKILL ladder
        # didn't reach the child.
        journal = self._journal_tail(n=500)
        self.assertNotIn(
            "with signal SIGKILL",
            journal,
            f"systemd had to SIGKILL leftover processes -- Controller "
            f"left children behind. Journal:\n{journal}",
        )


if __name__ == "__main__":
    unittest.main()
