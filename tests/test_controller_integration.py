"""End-to-end integration tests for the Controller daemon.

Spawns the real Controller as a subprocess, signals it directly (no
systemd needed), and inspects the resulting log bundles.

Skipped automatically when the host can't run the daemon:
  - not root
  - eBPF binaries missing under src/bin/
  - zstandard/PyYAML not installed
"""

import io
import os
import signal
import subprocess
import sys
import tarfile
import time
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(REPO_ROOT, "src")
BIN_DIR = os.path.join(SRC_DIR, "bin")

_HAS_ROOT = os.geteuid() == 0
_HAS_BINS = all(
    os.path.exists(os.path.join(BIN_DIR, n))
    for n in ("smbslower", "nfsslower", "libringbuf_shim.so")
)
try:
    import zstandard  # noqa: F401
    import yaml  # noqa: F401

    _HAS_DEPS = True
except ImportError:
    _HAS_DEPS = False

_SKIP_REASON = None
if not _HAS_ROOT:
    _SKIP_REASON = "requires root (eBPF, signal delivery to daemon)"
elif not _HAS_BINS:
    _SKIP_REASON = f"eBPF binaries missing under {BIN_DIR}"
elif not _HAS_DEPS:
    _SKIP_REASON = "PyYAML / zstandard not installed"


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
        mounts:
        syslogs:

cleanup:
  cleanup_interval_sec: 60
  max_log_age_days: 2
  max_total_log_size_mb: 50

audit:
  enabled: true
"""


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


@unittest.skipIf(_SKIP_REASON is not None, _SKIP_REASON)
class TestControllerIntegration(unittest.TestCase):
    """Spawn Controller, signal it, inspect produced bundles."""

    @classmethod
    def setUpClass(cls):
        # tempfile.TemporaryDirectory works fine here but the lifetime is
        # tighter when we manage it ourselves.
        import tempfile

        cls._tmp = tempfile.TemporaryDirectory(prefix="aodv2_it_")
        cls.output_dir = cls._tmp.name
        cls.batches_dir = os.path.join(cls.output_dir, "batches")
        os.makedirs(cls.batches_dir, exist_ok=True)

        cls.config_path = os.path.join(cls.output_dir, "config.yaml")
        with open(cls.config_path, "w") as f:
            f.write(CONFIG_TEMPLATE.format(output_dir=cls.output_dir))

        cls.stderr_path = os.path.join(cls.output_dir, "stderr.log")
        cls.stderr_fh = open(cls.stderr_path, "wb")

        env = os.environ.copy()
        env["PYTHONPATH"] = SRC_DIR
        env["AOD_CONFIG"] = cls.config_path
        env["AOD_LOG_STDERR"] = "1"
        env["AOD_LOG_LEVEL"] = "INFO"

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
        """SIGUSR1 must produce an aod_snapshot quick-action bundle."""
        before = set(self._bundles_matching("_aod_snapshot.tar.zst"))
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
        sizes = _bundle_file_sizes(bundle)
        # All four quick actions in the test config must contribute a file.
        self.assertEqual(
            set(sizes),
            {"dmesg.log", "journalctl.log", "mounts.log", "syslogs.log"},
            f"unexpected snapshot members: {sizes}",
        )
        # Each quick action should produce non-empty output.
        empty = sorted(name for name, sz in sizes.items() if sz == 0)
        self.assertFalse(empty, f"empty snapshot files: {empty} (sizes={sizes})")

    def test_02_shutdown_on_sigterm(self):
        """SIGTERM must produce an aod_shutdown bundle AND exit cleanly."""
        before = set(self._bundles_matching("_aod_shutdown.tar.zst"))
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
        sizes = _bundle_file_sizes(next(iter(new)))
        self.assertEqual(
            set(sizes),
            {"dmesg.log", "journalctl.log", "mounts.log", "syslogs.log"},
            f"unexpected shutdown members: {sizes}",
        )
        empty = sorted(name for name, sz in sizes.items() if sz == 0)
        self.assertFalse(empty, f"empty shutdown files: {empty} (sizes={sizes})")


if __name__ == "__main__":
    unittest.main()
