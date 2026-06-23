"""Integration tests for LogCollector.

Covered behaviour:
  * one LongCapture instance per protocol regardless of how many anomalies
    under that protocol declare the same capture tool
    (LogCollector._build_captures + ConfigManager._validate_capture_exclusivity)
  * all_handlers deduplicates QuickAction instances by class
  * the queue-draining loop produces one tarball per anomaly event and the
    bundles contain the configured handler outputs
  * snapshot events (Protocol.AOD) fan out to the deduplicated union of every
    configured handler, each running exactly once
  * a single shared capture process serves multiple anomalies under one
    protocol, still producing a distinct bundle per snapshot request
  * the long-capture supervisor exits cleanly and reaps its child when
    stop_event is set

Real QuickAction subclasses are used, but the long-capture binary is stubbed out
via AOD_TCPDUMP_BIN so the tests do not require tcpdump/trace-cmd or root.
"""

import asyncio
import os
import queue
import stat
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import zstandard as zstd

# pyproject.toml puts src/ on PYTHONPATH for pytest. Keep this so the file
# also runs under `python -m unittest`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ConfigManager import ConfigManager  # noqa: E402
from LogCollector import LogCollector  # noqa: E402
from handlers.TcpdumpCapture import TcpdumpCapture  # noqa: E402
from handlers.TraceCmdCapture import TraceCmdCapture  # noqa: E402
from handlers.MountsQuickAction import MountsQuickAction  # noqa: E402
from handlers.JournalctlQuickAction import JournalctlQuickAction  # noqa: E402
from handlers.DmesgQuickAction import DmesgQuickAction  # noqa: E402
from utils.anomaly_type import AnomalyType, Protocol  # noqa: E402
from utils.config_schema import AnomalyKey  # noqa: E402


# --- helpers ---------------------------------------------------------------


def _fake_controller(config) -> SimpleNamespace:
    """Minimal Controller stand-in. LogCollector touches .config,
    .anomalyActionQueue, and .stop_event only."""
    return SimpleNamespace(
        config=config,
        anomalyActionQueue=queue.Queue(),
        stop_event=threading.Event(),
    )


def _write_config(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip("\n"))


def _bundle_entries(tar_path: Path) -> list[tuple[str, int]]:
    """Return [(basename, size_bytes), ...] for regular files in a .tar.zst,
    preserving duplicates so callers can detect collisions."""
    dctx = zstd.ZstdDecompressor()
    with open(tar_path, "rb") as f, dctx.stream_reader(f) as reader:
        with tarfile.open(fileobj=reader, mode="r|") as tar:
            return [
                (os.path.basename(m.name), m.size)
                for m in tar
                if m.isfile()
            ]


def _bundle_members(tar_path: Path) -> dict[str, int]:
    """Return {basename: size_bytes} for regular files in a .tar.zst. Use
    _bundle_entries instead when you need to detect duplicate names."""
    return dict(_bundle_entries(tar_path))


def _make_event(protocol: Protocol, anomaly_type: AnomalyType, ts: int) -> dict:
    return {
        "anomaly_key": AnomalyKey(protocol, anomaly_type),
        "timestamp": ts,
    }


def _wait_for(predicate, timeout: float, interval: float = 0.05):
    """Poll predicate() until truthy or timeout. Returns its final value."""
    deadline = time.monotonic() + timeout
    last = predicate()
    while not last and time.monotonic() < deadline:
        time.sleep(interval)
        last = predicate()
    return last


def _drain_join(thread: threading.Thread, timeout: float = 60.0) -> None:
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise AssertionError(
            f"LogCollector thread did not exit within {timeout}s"
        )


# Bash stub used in place of tcpdump. Writes a non-empty `cap.pcap` to the
# path passed via `-w`, then idles until LongCapture._stop sends SIGINT to
# the process group.
_FAKE_TCPDUMP_SCRIPT = """\
#!/usr/bin/env bash
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -w) out="$2"; shift 2 ;;
    *)  shift ;;
  esac
done
if [ -n "$out" ]; then
  mkdir -p "$(dirname "$out")"
  printf 'fake-pcap pid=%d\\n' "$$" > "$out"
fi
trap 'exit 0' INT TERM
# Background `sleep` so `wait` is interruptible and the SIGINT trap fires
# without waiting out the full sleep window.
while : ; do
  sleep 0.05 &
  wait "$!"
done
"""


def _install_fake_tcpdump(tmp: Path) -> Path:
    script = tmp / "fake_tcpdump.sh"
    script.write_text(_FAKE_TCPDUMP_SCRIPT)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


# Bash stub used in place of trace-cmd. Honors the four subcommands the
# supervisor invokes:
#   reset                -> exit 0
#   start <user_args>    -> exit 0 (no long-running process)
#   stop                 -> exit 0
#   extract -o <path>    -> write a non-empty file at <path>, exit 0
# Anything else exits 0 so unrelated invocations don't fail the test.
_FAKE_TRACECMD_SCRIPT = """\
#!/usr/bin/env bash
sub="$1"; shift || true
case "$sub" in
  extract)
    out=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -o) out="$2"; shift 2 ;;
        *)  shift ;;
      esac
    done
    if [ -n "$out" ]; then
      mkdir -p "$(dirname "$out")"
      printf 'fake-tracecmd pid=%d\\n' "$$" > "$out"
    fi
    ;;
esac
exit 0
"""


def _install_fake_tracecmd(tmp: Path) -> Path:
    script = tmp / "fake_tracecmd.sh"
    script.write_text(_FAKE_TRACECMD_SCRIPT)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


# --- tests -----------------------------------------------------------------


class TestLogCollector(unittest.TestCase):
    """Smoke test the original construction path against the shipping config."""

    def test_init(self):
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "config", "config.yaml"
        )
        config = ConfigManager(config_path).data
        controller = _fake_controller(config)
        collector = LogCollector(controller)
        self.assertIsNotNone(collector)


class TestOneLongCapturePerProtocol(unittest.TestCase):
    """Pin the invariant: every protocol gets at most one LongCapture
    instance, even when multiple anomalies under that protocol declare the
    same capture tool. ConfigManager has already validated args agreement;
    LogCollector._build_captures enforces the instance-count rule."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="aodv2_lc_oneproto_")
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_two_smb_anomalies_share_single_tcpdump_instance(self):
        cfg = self.tmp / "config.yaml"
        _write_config(
            cfg,
            f"""
            watch_interval_sec: 1
            aod_output_dir: {self.tmp}

            anomalies:
              smb:
                latency:
                  tool: "smbslower"
                  mode: "all"
                  acceptable_count: 10
                  default_threshold_ms: 20
                  actions:
                    mounts:
                    tcpdump: ["-C", "1", "-W", "2"]
                sockconn:
                  tool: "ss"
                  actions:
                    mounts:
                    tcpdump: ["-C", "1", "-W", "2"]

            cleanup:
              cleanup_interval_sec: 60
              max_log_age_days: 2
              max_total_log_size_mb: 50

            audit:
              enabled: true
            """,
        )
        config = ConfigManager(str(cfg)).data
        collector = LogCollector(_fake_controller(config))

        self.assertEqual(list(collector.captures.keys()), [Protocol.SMB])
        (cap,) = collector.captures.values()
        self.assertIsInstance(cap, TcpdumpCapture)
        self.assertEqual(cap.user_args, ["-C", "1", "-W", "2"])
        # Both anomaly keys must dispatch to the same capture object.
        latency_key = AnomalyKey(Protocol.SMB, AnomalyType.LATENCY)
        sockconn_key = AnomalyKey(Protocol.SMB, AnomalyType.SOCKCONN)
        self.assertIn(latency_key, config.anomalies)
        self.assertIn(sockconn_key, config.anomalies)
        self.assertIs(
            collector.captures[latency_key.protocol],
            collector.captures[sockconn_key.protocol],
        )


class TestAllHandlersDedup(unittest.TestCase):
    """`all_handlers` (used by snapshot dispatch) must contain one instance
    per unique QuickAction class, even when several anomalies list the
    same action."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="aodv2_lc_dedup_")
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_overlapping_quick_actions_collapse_to_unique_classes(self):
        cfg = self.tmp / "config.yaml"
        _write_config(
            cfg,
            f"""
            watch_interval_sec: 1
            aod_output_dir: {self.tmp}

            anomalies:
              smb:
                latency:
                  tool: "smbslower"
                  mode: "all"
                  acceptable_count: 10
                  default_threshold_ms: 20
                  actions:
                    mounts:
                    journalctl:
                sockconn:
                  tool: "ss"
                  actions:
                    mounts:
                    dmesg:

            cleanup:
              cleanup_interval_sec: 60
              max_log_age_days: 2
              max_total_log_size_mb: 50

            audit:
              enabled: true
            """,
        )
        config = ConfigManager(str(cfg)).data
        collector = LogCollector(_fake_controller(config))

        classes = {type(h) for h in collector.all_handlers}
        self.assertEqual(
            classes,
            {MountsQuickAction, JournalctlQuickAction, DmesgQuickAction},
        )
        # No duplicates: union of 4 declarations (mounts x2, journalctl,
        # dmesg) -> 3 instances.
        self.assertEqual(len(collector.all_handlers), 3)


class TestE2eMultipleEvents(unittest.TestCase):
    """Drive LogCollector.run() end-to-end with multiple queued events and
    verify each produces its own quick-action bundle populated by the real
    QuickAction subprocesses."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="aodv2_lc_e2e_")
        self.tmp = Path(self._td.name)
        self.batches = self.tmp / "batches"
        cfg = self.tmp / "config.yaml"
        # mounts is the deterministic non-empty source (cat /proc/mounts);
        # journalctl is included to exercise an asyncio.create_subprocess_exec
        # path in addition to the cat path.
        _write_config(
            cfg,
            f"""
            watch_interval_sec: 1
            aod_output_dir: {self.tmp}

            anomalies:
              nfs:
                error:
                  tool: "nfsiosnoop"
                  acceptable_count: 5
                  track_errors:
                    - NFS4ERR_BAD_STATEID
                  actions:
                    mounts:
                    journalctl:

            cleanup:
              cleanup_interval_sec: 60
              max_log_age_days: 2
              max_total_log_size_mb: 50

            audit:
              enabled: true
            """,
        )
        self.config = ConfigManager(str(cfg)).data
        self.controller = _fake_controller(self.config)
        self.collector = LogCollector(self.controller)

    def tearDown(self):
        # Belt-and-braces: tell any leftover supervisor to stop.
        self.controller.stop_event.set()
        self._td.cleanup()

    def test_five_queued_events_produce_five_bundles(self):
        events = [
            _make_event(Protocol.NFS, AnomalyType.ERROR, ts=10_000 + i)
            for i in range(5)
        ]
        for e in events:
            self.controller.anomalyActionQueue.put(e)
        # Sentinel terminates the drain loop after the events have been
        # picked up.
        self.controller.anomalyActionQueue.put(None)

        t = threading.Thread(
            target=self.collector.run, name="lc-e2e-events", daemon=True
        )
        t.start()
        _drain_join(t, timeout=60)

        bundles = sorted(self.batches.glob("aod_quick_*_nfs_error.tar.zst"))
        self.assertEqual(
            len(bundles),
            5,
            f"expected 5 quick-action bundles, got {[p.name for p in bundles]}",
        )

        for b in bundles:
            members = _bundle_members(b)
            self.assertIn(
                "mounts.log",
                members,
                f"{b.name} missing mounts.log; members={members}",
            )
            self.assertGreater(
                members["mounts.log"],
                0,
                f"{b.name} mounts.log is empty",
            )

        # No staging dirs left behind after bundling.
        staging = [p for p in self.batches.iterdir() if p.is_dir()]
        self.assertEqual(staging, [], f"leftover staging dirs: {staging}")


class TestE2eSnapshotFansOut(unittest.TestCase):
    """A single Protocol.AOD/SNAPSHOT event must run the deduplicated union
    of all configured handlers, each exactly once, into one bundle.

    Config mixes SMB and NFS with two anomalies each so handler dedup is
    exercised both within a protocol (smb.latency + smb.sockconn both list
    `mounts`) and across protocols (smb.* and nfs.* both list `mounts`)."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="aodv2_lc_snap_")
        self.tmp = Path(self._td.name)
        self.batches = self.tmp / "batches"
        cfg = self.tmp / "config.yaml"
        _write_config(
            cfg,
            f"""
            watch_interval_sec: 1
            aod_output_dir: {self.tmp}

            anomalies:
              smb:
                latency:
                  tool: "smbslower"
                  mode: "all"
                  acceptable_count: 10
                  default_threshold_ms: 20
                  actions:
                    mounts:
                    journalctl:
                sockconn:
                  tool: "ss"
                  actions:
                    mounts:
                    dmesg:
              nfs:
                latency:
                  tool: "nfsslower"
                  mode: "all"
                  acceptable_count: 10
                  default_threshold_ms: 50
                  actions:
                    mounts:
                    journalctl:
                error:
                  tool: "nfsiosnoop"
                  acceptable_count: 5
                  track_errors:
                    - NFS4ERR_BAD_STATEID
                  actions:
                    mounts:
                    dmesg:

            cleanup:
              cleanup_interval_sec: 60
              max_log_age_days: 2
              max_total_log_size_mb: 50

            audit:
              enabled: true
            """,
        )
        self.config = ConfigManager(str(cfg)).data
        self.controller = _fake_controller(self.config)
        self.collector = LogCollector(self.controller)

    def tearDown(self):
        self.controller.stop_event.set()
        self._td.cleanup()

    def test_snapshot_bundle_contains_unique_handler_outputs(self):
        self.controller.anomalyActionQueue.put(
            _make_event(Protocol.AOD, AnomalyType.SNAPSHOT, ts=42_000)
        )
        self.controller.anomalyActionQueue.put(None)

        t = threading.Thread(
            target=self.collector.run, name="lc-e2e-snap", daemon=True
        )
        t.start()
        _drain_join(t, timeout=60)

        bundles = sorted(self.batches.glob("aod_quick_*_aod_snapshot.tar.zst"))
        self.assertEqual(
            len(bundles),
            1,
            f"expected 1 snapshot bundle, got {[p.name for p in bundles]}",
        )
        entries = _bundle_entries(bundles[0])
        names = [n for n, _ in entries]

        # mounts.log is the deterministic-non-empty marker; dmesg and
        # journalctl are skipped silently by QuickAction.execute when their
        # subprocess yields no output (unprivileged caller), so only assert
        # presence on best-effort outputs but require that each output
        # filename appears at most once (i.e. handler dedup worked). The
        # check uses the raw entry list so a duplicate write to the same
        # filename is detectable (a dict would silently collapse it).
        self.assertEqual(
            len(names), len(set(names)), f"duplicate members: {names}"
        )
        members = dict(entries)
        self.assertIn("mounts.log", members)
        self.assertGreater(members["mounts.log"], 0)


class TestE2eSharedCaptureProcess(unittest.TestCase):
    """Two anomalies under one protocol share a single LongCapture instance.
    Two distinct snapshot requests must still yield two distinct capture
    bundles (one per request) while only one capture instance was ever
    constructed."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="aodv2_lc_cap_")
        self.tmp = Path(self._td.name)
        self.batches = self.tmp / "batches"
        self.fake_tcpdump = _install_fake_tcpdump(self.tmp)
        self._prev_env = os.environ.get("AOD_TCPDUMP_BIN")
        os.environ["AOD_TCPDUMP_BIN"] = str(self.fake_tcpdump)

        cfg = self.tmp / "config.yaml"
        _write_config(
            cfg,
            f"""
            watch_interval_sec: 1
            aod_output_dir: {self.tmp}

            anomalies:
              smb:
                latency:
                  tool: "smbslower"
                  mode: "all"
                  acceptable_count: 10
                  default_threshold_ms: 20
                  actions:
                    mounts:
                    tcpdump: ["-C", "1", "-W", "2"]
                sockconn:
                  tool: "ss"
                  actions:
                    mounts:
                    tcpdump: ["-C", "1", "-W", "2"]

            cleanup:
              cleanup_interval_sec: 60
              max_log_age_days: 2
              max_total_log_size_mb: 50

            audit:
              enabled: true
            """,
        )
        self.config = ConfigManager(str(cfg)).data
        self.controller = _fake_controller(self.config)
        self.collector = LogCollector(self.controller)

    def tearDown(self):
        self.controller.stop_event.set()
        if self._prev_env is None:
            os.environ.pop("AOD_TCPDUMP_BIN", None)
        else:
            os.environ["AOD_TCPDUMP_BIN"] = self._prev_env
        self._td.cleanup()

    def test_one_capture_instance_serves_per_event_bundles(self):
        # Sanity: only one LongCapture object exists for Protocol.SMB even
        # though two anomalies declared tcpdump.
        self.assertEqual(list(self.collector.captures.keys()), [Protocol.SMB])
        (cap_inst,) = self.collector.captures.values()

        t = threading.Thread(
            target=self.collector.run, name="lc-e2e-cap", daemon=True
        )
        t.start()

        live_cap = self.tmp / "captures" / "smb" / "cap.pcap"
        latency_bundle = self.batches / "aod_capture_1000_smb_latency.tar.zst"
        sockconn_bundle = (
            self.batches / "aod_capture_2000_smb_sockconn.tar.zst"
        )

        # Serialise the snapshot requests so each one is fully bundled
        # before the next is enqueued. This isolates the invariant under
        # test ("one shared capture object, one bundle per request") from
        # supervisor scheduling races that aren't part of the contract.
        self.assertTrue(
            _wait_for(live_cap.exists, timeout=15),
            "stub recorder never wrote initial cap.pcap",
        )
        self.controller.anomalyActionQueue.put(
            _make_event(Protocol.SMB, AnomalyType.LATENCY, ts=1_000)
        )
        self.assertTrue(
            _wait_for(latency_bundle.exists, timeout=20),
            f"latency bundle never appeared; have "
            f"{sorted(p.name for p in self.batches.iterdir())}",
        )

        self.assertTrue(
            _wait_for(live_cap.exists, timeout=15),
            "stub recorder never re-wrote cap.pcap after first snapshot",
        )
        self.controller.anomalyActionQueue.put(
            _make_event(Protocol.SMB, AnomalyType.SOCKCONN, ts=2_000)
        )
        self.assertTrue(
            _wait_for(sockconn_bundle.exists, timeout=20),
            f"sockconn bundle never appeared; have "
            f"{sorted(p.name for p in self.batches.iterdir())}",
        )

        self.controller.anomalyActionQueue.put(None)
        _drain_join(t, timeout=60)

        # Each bundle must contain a non-empty cap.pcap produced by the
        # stub recorder.
        for b in (latency_bundle, sockconn_bundle):
            members = _bundle_members(b)
            self.assertIn("cap.pcap", members, f"{b.name} members={members}")
            self.assertGreater(
                members["cap.pcap"], 0, f"{b.name} cap.pcap empty"
            )

        # Both anomalies declared the `mounts` quick action, so each event
        # must also have produced its sibling aod_quick_*.tar.zst alongside
        # the capture bundle.
        for batch_id, anomaly_name in (
            (1_000, "smb_latency"),
            (2_000, "smb_sockconn"),
        ):
            quick = self.batches / f"aod_quick_{batch_id}_{anomaly_name}.tar.zst"
            self.assertTrue(
                quick.exists(),
                f"missing sibling quick bundle {quick.name}; have "
                f"{sorted(p.name for p in self.batches.iterdir())}",
            )
            quick_members = _bundle_members(quick)
            self.assertIn("mounts.log", quick_members)
            self.assertGreater(quick_members["mounts.log"], 0)

        # The shared capture object survived the whole run.
        self.assertIs(self.collector.captures[Protocol.SMB], cap_inst)


class TestLongCaptureStopEvent(unittest.TestCase):
    """The LongCapture supervisor must exit when stop_event fires and must
    leave no stray child process behind (SIGINT path via _stop)."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="aodv2_lc_stop_")
        self.tmp = Path(self._td.name)
        self.fake_tcpdump = _install_fake_tcpdump(self.tmp)
        self._prev_env = os.environ.get("AOD_TCPDUMP_BIN")
        os.environ["AOD_TCPDUMP_BIN"] = str(self.fake_tcpdump)

    def tearDown(self):
        if self._prev_env is None:
            os.environ.pop("AOD_TCPDUMP_BIN", None)
        else:
            os.environ["AOD_TCPDUMP_BIN"] = self._prev_env
        self._td.cleanup()

    def test_stop_event_exits_supervisor_within_grace(self):
        cap = TcpdumpCapture(
            Protocol.SMB,
            ["-C", "1", "-W", "2"],
            capture_dir=str(self.tmp / "captures" / "smb"),
            bundle_dir=str(self.tmp / "batches"),
            restart_delay_sec=0.05,
        )
        stop_event = threading.Event()

        async def driver():
            task = asyncio.create_task(cap.run(stop_event))
            # Give the supervisor time to spawn the stub recorder.
            for _ in range(50):
                await asyncio.sleep(0.05)
                if cap._proc is not None and cap._proc.returncode is None:
                    break
            else:
                task.cancel()
                raise AssertionError(
                    "capture process never reached running state"
                )

            child_pid = cap._proc.pid
            stop_event.set()

            # Supervisor must exit cleanly within stop_grace_sec + bundling
            # headroom. tcpdump stop_grace_sec is the LongCapture default of
            # 5s; give a generous margin for CI noise.
            await asyncio.wait_for(task, timeout=cap.stop_grace_sec + 10)
            return child_pid

        child_pid = asyncio.run(driver())

        # The supervisor's _stop path must have reaped the child.
        self.assertIsNotNone(cap._proc)
        self.assertIsNotNone(
            cap._proc.returncode,
            "child process was not reaped after stop_event",
        )
        # And the kernel must agree the pid is gone (no zombie / no still-running).
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)


class TestE2eSharedTraceCmdCapture(unittest.TestCase):
    """Same shared-capture invariant as the tcpdump test, but for
    TraceCmdCapture. Its lifecycle is different: no long-running recorder,
    just oneshot `reset` / `start` / `stop` / `extract`. The stub honors
    `extract -o <path>` so each snapshot still produces a non-empty bundle."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="aodv2_lc_trace_")
        self.tmp = Path(self._td.name)
        self.batches = self.tmp / "batches"
        self.fake_tracecmd = _install_fake_tracecmd(self.tmp)
        self._prev_env = os.environ.get("AOD_TRACECMD_BIN")
        os.environ["AOD_TRACECMD_BIN"] = str(self.fake_tracecmd)

        cfg = self.tmp / "config.yaml"
        # Two NFS anomalies share a single trace-cmd recorder
        # (ConfigManager requires args to agree).
        _write_config(
            cfg,
            f"""
            watch_interval_sec: 1
            aod_output_dir: {self.tmp}

            anomalies:
              nfs:
                latency:
                  tool: "nfsslower"
                  mode: "all"
                  acceptable_count: 10
                  default_threshold_ms: 50
                  actions:
                    mounts:
                    trace-cmd: ["-e", "nfs", "-b", "1024"]
                error:
                  tool: "nfsiosnoop"
                  acceptable_count: 5
                  track_errors:
                    - NFS4ERR_BAD_STATEID
                  actions:
                    mounts:
                    trace-cmd: ["-e", "nfs", "-b", "1024"]

            cleanup:
              cleanup_interval_sec: 60
              max_log_age_days: 2
              max_total_log_size_mb: 50

            audit:
              enabled: true
            """,
        )
        self.config = ConfigManager(str(cfg)).data
        self.controller = _fake_controller(self.config)
        self.collector = LogCollector(self.controller)

    def tearDown(self):
        self.controller.stop_event.set()
        if self._prev_env is None:
            os.environ.pop("AOD_TRACECMD_BIN", None)
        else:
            os.environ["AOD_TRACECMD_BIN"] = self._prev_env
        self._td.cleanup()

    def test_one_tracecmd_instance_serves_per_event_bundles(self):
        # Only one LongCapture instance exists for Protocol.NFS even though
        # two anomalies declared trace-cmd.
        self.assertEqual(list(self.collector.captures.keys()), [Protocol.NFS])
        (cap_inst,) = self.collector.captures.values()
        self.assertIsInstance(cap_inst, TraceCmdCapture)

        t = threading.Thread(
            target=self.collector.run, name="lc-e2e-trace", daemon=True
        )
        t.start()

        latency_bundle = self.batches / "aod_capture_1000_nfs_latency.tar.zst"
        error_bundle = self.batches / "aod_capture_2000_nfs_error.tar.zst"

        self.controller.anomalyActionQueue.put(
            _make_event(Protocol.NFS, AnomalyType.LATENCY, ts=1_000)
        )
        self.assertTrue(
            _wait_for(latency_bundle.exists, timeout=20),
            f"latency bundle never appeared; have "
            f"{sorted(p.name for p in self.batches.iterdir())}",
        )

        self.controller.anomalyActionQueue.put(
            _make_event(Protocol.NFS, AnomalyType.ERROR, ts=2_000)
        )
        self.assertTrue(
            _wait_for(error_bundle.exists, timeout=20),
            f"error bundle never appeared; have "
            f"{sorted(p.name for p in self.batches.iterdir())}",
        )

        self.controller.anomalyActionQueue.put(None)
        _drain_join(t, timeout=60)

        for b in (latency_bundle, error_bundle):
            members = _bundle_members(b)
            self.assertIn("cap.dat", members, f"{b.name} members={members}")
            self.assertGreater(
                members["cap.dat"], 0, f"{b.name} cap.dat empty"
            )

        # The shared capture object survived the whole run.
        self.assertIs(self.collector.captures[Protocol.NFS], cap_inst)


class TestLongCaptureSpawnDisable(unittest.TestCase):
    """When _spawn fails _MAX_SPAWN_FAILURES times in a row the supervisor
    must disable itself and exit, and subsequent snapshot() calls must be
    dropped with a warning instead of silently accumulating in the queue.

    Exercised once per backend (tcpdump+SMB and trace-cmd+NFS) because the
    two backends fail to spawn through different code paths:
      * TcpdumpCapture: the base class _spawn does
        asyncio.create_subprocess_exec which raises FileNotFoundError when
        AOD_TCPDUMP_BIN points nowhere.
      * TraceCmdCapture: overrides _spawn to run `trace-cmd reset` and
        `trace-cmd start` via _oneshot, which catches FileNotFoundError
        and returns None, then _spawn returns False on the non-zero rc.
    Both must converge on the same _disabled-after-_MAX_SPAWN_FAILURES
    invariant."""

    def _run_disable_assertions(self, cap) -> None:
        stop_event = threading.Event()
        # Capture the supervisor-set _disabled flag and confirm the
        # supervisor exited without anyone else setting stop_event. The
        # outer driver() sets stop_event in its finally block purely to
        # release the to_thread waiter, so we record state via a marker
        # that's set strictly when the supervisor returns.
        result: dict = {}

        async def driver():
            task = asyncio.create_task(cap.run(stop_event))
            try:
                await asyncio.wait_for(task, timeout=10)
                result["stop_set_before_exit"] = stop_event.is_set()
                result["disabled"] = cap._disabled
                result["spawn_failures"] = cap._spawn_failures
            finally:
                # LongCapture.run holds a persistent asyncio.to_thread waiter
                # on stop_event. Cancelling it does not interrupt the
                # underlying executor thread, so we must signal the event
                # here to let asyncio.run's default-executor shutdown
                # finish without blocking.
                stop_event.set()

        asyncio.run(driver())

        self.assertTrue(
            result.get("disabled"),
            "supervisor exited without setting _disabled",
        )
        self.assertGreaterEqual(result.get("spawn_failures", 0), 3)
        self.assertFalse(
            result.get("stop_set_before_exit", True),
            "supervisor must not require stop_event to disable itself",
        )
        # Post-disable snapshot() calls are dropped: queue stays empty and
        # a warning is logged.
        with self.assertLogs("base.LongCapture", level="WARNING") as cm:
            cap.snapshot("post_disable_42")
        self.assertEqual(cap._snap_q.qsize(), 0)
        self.assertTrue(
            any("DROPPED snapshot" in m for m in cm.output),
            f"expected DROPPED snapshot warning; got {cm.output}",
        )

    def test_tcpdump_three_spawn_failures_disable_supervisor(self):
        with tempfile.TemporaryDirectory(prefix="aodv2_lc_disable_td_") as td:
            tmp = Path(td)
            # Point tcpdump at a path that cannot be exec'd so every _spawn
            # raises FileNotFoundError. restart_delay_sec is kept tiny so
            # the three failures elapse well within the test timeout.
            prev = os.environ.get("AOD_TCPDUMP_BIN")
            os.environ["AOD_TCPDUMP_BIN"] = str(tmp / "does_not_exist")
            try:
                cap = TcpdumpCapture(
                    Protocol.SMB,
                    ["-C", "1", "-W", "2"],
                    capture_dir=str(tmp / "captures" / "smb"),
                    bundle_dir=str(tmp / "batches"),
                    restart_delay_sec=0.01,
                )
                self._run_disable_assertions(cap)
            finally:
                if prev is None:
                    os.environ.pop("AOD_TCPDUMP_BIN", None)
                else:
                    os.environ["AOD_TCPDUMP_BIN"] = prev

    def test_tracecmd_three_spawn_failures_disable_supervisor(self):
        with tempfile.TemporaryDirectory(prefix="aodv2_lc_disable_tc_") as td:
            tmp = Path(td)
            # Same idea for trace-cmd: a non-existent binary causes
            # _oneshot("start", ...) to return None, which makes _spawn
            # return False and increments the failure counter.
            prev = os.environ.get("AOD_TRACECMD_BIN")
            os.environ["AOD_TRACECMD_BIN"] = str(tmp / "does_not_exist")
            try:
                cap = TraceCmdCapture(
                    Protocol.NFS,
                    ["-e", "nfs", "-b", "1024"],
                    capture_dir=str(tmp / "captures" / "nfs"),
                    bundle_dir=str(tmp / "batches"),
                    restart_delay_sec=0.01,
                )
                self._run_disable_assertions(cap)
            finally:
                if prev is None:
                    os.environ.pop("AOD_TRACECMD_BIN", None)
                else:
                    os.environ["AOD_TRACECMD_BIN"] = prev


class TestLongCaptureShutdownDropsQueuedSnapshots(unittest.TestCase):
    """Snapshots that are still queued when the supervisor finishes its
    main loop must be reported as dropped (the finally-block warning path
    in LongCapture.run). The data is lost; the warning is the only signal
    the operator has.

    Exercised once per backend (tcpdump+SMB and trace-cmd+NFS) since the
    two have different supervisor lifecycles:
      * TcpdumpCapture has a long-running recorder process the supervisor
        sits on via proc.wait().
      * TraceCmdCapture has no recorder; _spawn assigns a _NoProc sentinel
        whose wait() blocks forever.
    Both must drain-and-warn pending snap_q entries in the finally block."""

    def _run_drop_assertions(self, cap) -> None:
        stop_event = threading.Event()

        async def driver():
            task = asyncio.create_task(cap.run(stop_event))
            # Wait until the supervisor is sitting in its wait() on
            # snap/proc/stop. For tcpdump _proc is the running tcpdump
            # subprocess; for trace-cmd _proc is the _NoProc sentinel.
            for _ in range(100):
                await asyncio.sleep(0.05)
                if cap._proc is not None and cap._proc.returncode is None:
                    break
            else:
                task.cancel()
                raise AssertionError("supervisor never reached running state")

            # Enqueue several snapshots back-to-back, then immediately
            # request shutdown. At most one snapshot can be processed
            # between the put and the stop_event firing; the rest stay in
            # the queue and must be drained-and-warned by the finally block.
            cap.snapshot("drop_a")
            cap.snapshot("drop_b")
            cap.snapshot("drop_c")
            stop_event.set()
            await asyncio.wait_for(task, timeout=cap.stop_grace_sec + 10)

        with self.assertLogs("base.LongCapture", level="WARNING") as cm:
            asyncio.run(driver())

        dropped_logs = [m for m in cm.output if "DROPPED" in m]
        self.assertTrue(
            dropped_logs,
            f"expected at least one DROPPED warning; got {cm.output}",
        )
        # At least one of the queued ids must appear in the warning. We
        # don't pin which ones because the supervisor may legitimately
        # bundle one before observing stop_event.
        self.assertTrue(
            any(
                any(tag in m for tag in ("drop_a", "drop_b", "drop_c"))
                for m in dropped_logs
            ),
            f"DROPPED warning did not name any queued batch_id: {dropped_logs}",
        )

    def test_tcpdump_queued_snapshots_at_shutdown_emit_dropped_warning(self):
        with tempfile.TemporaryDirectory(prefix="aodv2_lc_dropq_td_") as td:
            tmp = Path(td)
            fake = _install_fake_tcpdump(tmp)
            prev = os.environ.get("AOD_TCPDUMP_BIN")
            os.environ["AOD_TCPDUMP_BIN"] = str(fake)
            try:
                cap = TcpdumpCapture(
                    Protocol.SMB,
                    ["-C", "1", "-W", "2"],
                    capture_dir=str(tmp / "captures" / "smb"),
                    bundle_dir=str(tmp / "batches"),
                    restart_delay_sec=0.05,
                )
                self._run_drop_assertions(cap)
            finally:
                if prev is None:
                    os.environ.pop("AOD_TCPDUMP_BIN", None)
                else:
                    os.environ["AOD_TCPDUMP_BIN"] = prev

    def test_tracecmd_queued_snapshots_at_shutdown_emit_dropped_warning(self):
        with tempfile.TemporaryDirectory(prefix="aodv2_lc_dropq_tc_") as td:
            tmp = Path(td)
            fake = _install_fake_tracecmd(tmp)
            prev = os.environ.get("AOD_TRACECMD_BIN")
            os.environ["AOD_TRACECMD_BIN"] = str(fake)
            try:
                cap = TraceCmdCapture(
                    Protocol.NFS,
                    ["-e", "nfs", "-b", "1024"],
                    capture_dir=str(tmp / "captures" / "nfs"),
                    bundle_dir=str(tmp / "batches"),
                    restart_delay_sec=0.05,
                )
                self._run_drop_assertions(cap)
            finally:
                if prev is None:
                    os.environ.pop("AOD_TRACECMD_BIN", None)
                else:
                    os.environ["AOD_TRACECMD_BIN"] = prev


if __name__ == "__main__":
    unittest.main()
