"""Integration-style unit tests for AnomalyWatcher.

These tests wire the real handlers (LatencyAnomalyHandler,
ErrorAnomalyHandler, SockconnAnomalyHandler) into a fake Controller and
push synthetic events shaped like the real eBPF batch dtype. No fake
handler classes are used: the registry is exactly the production one.

AnomalyWatcher surface exercised:
  - controller.config.watch_interval_sec
  - controller.config.anomalies
  - controller.stop_event
  - controller.eventQueue
  - controller.anomalyActionQueue

SockconnAnomalyHandler reads /proc/net/tcp{,6}; we point it at temp
files via the module-level `_PROC_FILES` tuple.
"""

import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

import AnomalyWatcher as aw_mod
from AnomalyWatcher import AnomalyWatcher
from handlers import SockconnAnomalyHandler as sockconn_mod
from utils.anomaly_type import (
    AnomalyType,
    PROTOCOL_SERVER_PORT,
    Protocol,
    TOOL_NAME_TO_ID,
)
from utils.config_schema import AnomalyConfig, AnomalyKey
from utils.shared_data import ALL_NFS_CMDS, ALL_NFS_ERRS, ALL_SMB_CMDS, event_dtype

from conftest import make_fake_controller

# --- Builders for real configs / batches / fake controllers -----------------


def _make_anomaly_config(
    *,
    protocol: Protocol,
    anomaly_type: AnomalyType,
    tool: str,
    acceptable_count: int,
    track: dict | None = None,
) -> AnomalyConfig:
    """Construct a real AnomalyConfig the way ConfigManager would."""
    return AnomalyConfig(
        tool=tool,
        key=AnomalyKey(protocol, anomaly_type),
        acceptable_count=acceptable_count,
        track=track or {},
    )


def _smb_latency_config(
    *, acceptable_count: int = 3, threshold_ms: int = 50
) -> AnomalyConfig:
    """A complete SMB-latency config tracking every SMB command at one
    common threshold."""
    return _make_anomaly_config(
        protocol=Protocol.SMB,
        anomaly_type=AnomalyType.LATENCY,
        tool="smbslower",
        acceptable_count=acceptable_count,
        track={
            "track_commands": {
                cmd_id: threshold_ms for cmd_id in ALL_SMB_CMDS.values()
            }
        },
    )


def _nfs_latency_config(
    *, acceptable_count: int = 3, threshold_ms: int = 50
) -> AnomalyConfig:
    """A complete NFS-latency config tracking every NFS command at one
    common threshold."""
    return _make_anomaly_config(
        protocol=Protocol.NFS,
        anomaly_type=AnomalyType.LATENCY,
        tool="nfsslower",
        acceptable_count=acceptable_count,
        track={
            "track_commands": {
                cmd_id: threshold_ms for cmd_id in ALL_NFS_CMDS.values()
            }
        },
    )


def _nfs_error_config(*, acceptable_count: int = 2) -> AnomalyConfig:
    return _make_anomaly_config(
        protocol=Protocol.NFS,
        anomaly_type=AnomalyType.ERROR,
        tool="nfsiosnoop",
        acceptable_count=acceptable_count,
        track={
            "track_commands": frozenset(ALL_NFS_CMDS.values()),
            "track_errors": frozenset(ALL_NFS_ERRS.values()),
        },
    )


def _smb_sockconn_config() -> AnomalyConfig:
    return _make_anomaly_config(
        protocol=Protocol.SMB,
        anomaly_type=AnomalyType.SOCKCONN,
        tool="ss",
        acceptable_count=1,
    )


def _nfs_sockconn_config() -> AnomalyConfig:
    return _make_anomaly_config(
        protocol=Protocol.NFS,
        anomaly_type=AnomalyType.SOCKCONN,
        tool="ss",
        acceptable_count=1,
    )


def _make_controller(
    *,
    anomalies: dict[AnomalyKey, AnomalyConfig] | None = None,
    watch_interval_sec: int | float | None = None,
) -> SimpleNamespace:
    """Fake controller carrying the two queues AnomalyWatcher uses."""
    kwargs = {}
    if anomalies is not None:
        kwargs["anomalies"] = anomalies
    if watch_interval_sec is not None:
        kwargs["watch_interval_sec"] = watch_interval_sec
    ctrl = make_fake_controller(**kwargs)
    ctrl.eventQueue = queue.Queue()
    ctrl.anomalyActionQueue = queue.Queue()
    return ctrl


def _make_event_batch(
    tool_name: str,
    commands: list[int],
    metric_values: list[int] | None = None,
) -> np.ndarray:
    """Allocate a batch matching the real `event_dtype`.

    `metric_values` is written into the polymorphic `metric_latency_ns`
    field: latency in ns for LATENCY events, errno for ERROR events.
    Defaults to zeros, which is fine for ErrorAnomalyHandler tests
    """
    n = len(commands)
    if metric_values is None:
        metric_values = [0] * n
    assert len(metric_values) == n

    batch = np.zeros(n, dtype=event_dtype)
    batch["tool"] = bytes([TOOL_NAME_TO_ID[tool_name]])
    batch["command"] = np.asarray(commands, dtype=np.uint16)
    batch["metric_latency_ns"] = np.asarray(metric_values, dtype=np.uint64)
    return batch


# --- Fake /proc/net/tcp helpers ---------------------------------------------


def _proc_net_tcp_line(
    sl: int,
    local_hex: str,
    remote_hex: str,
    state_hex: str = "01",
) -> str:
    """One whitespace-delimited /proc/net/tcp row. Only sl, local, remote,
    state matter for SockconnAnomalyHandler; pad the rest with zeros."""
    return (
        f"  {sl}: {local_hex} {remote_hex} {state_hex} "
        "00000000:00000000 00:00000000 00000000     0        0 0 0 0 0 0 0 0\n"
    )


def _write_proc_tcp(path: Path, rows: list[str]) -> None:
    """Header line + rows. Mirrors /proc/net/tcp layout."""
    header = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
        "retrnsmt   uid  timeout inode\n"
    )
    path.write_text(header + "".join(rows))


def _smb_remote_endpoint() -> str:
    """`local_address`-style hex string with the SMB server port."""
    port = PROTOCOL_SERVER_PORT[Protocol.SMB]
    return f"0100007F:{port:04X}"  # 127.0.0.1:445


# --- Init -------------------------------------------------------------------


class AnomalyWatcherInitTests(unittest.TestCase):
    def test_interval_defaults_to_60_when_missing(self):
        ctrl = _make_controller(anomalies={})
        w = AnomalyWatcher(ctrl)
        self.assertEqual(w.interval, 60)

    def test_interval_read_from_config(self):
        ctrl = _make_controller(anomalies={}, watch_interval_sec=7)
        w = AnomalyWatcher(ctrl)
        self.assertEqual(w.interval, 7)

    def test_real_handlers_split_by_userspace_vs_ebpf(self):
        latency = _smb_latency_config()
        sockconn = _smb_sockconn_config()
        ctrl = _make_controller(
            anomalies={latency.key: latency, sockconn.key: sockconn}
        )
        w = AnomalyWatcher(ctrl)
        self.assertEqual(len(w.ebpf_handlers), 1)
        self.assertEqual(len(w.userspace_handlers), 1)
        self.assertIn(latency.key, w.ebpf_handlers)
        self.assertNotIn(latency.key, w.userspace_handlers)
        self.assertIn(sockconn.key, w.userspace_handlers)
        self.assertNotIn(sockconn.key, w.ebpf_handlers)

    def test_unknown_anomaly_type_logs_and_skipped(self):
        latency = _smb_latency_config()
        ctrl = _make_controller(anomalies={latency.key: latency})
        with mock.patch.dict(aw_mod.ANOMALY_HANDLER_REGISTRY, {}, clear=True):
            with self.assertLogs(aw_mod.logger, level="WARNING") as cm:
                w = AnomalyWatcher(ctrl)
        self.assertEqual(w.ebpf_handlers, {})
        self.assertEqual(w.userspace_handlers, {})
        self.assertTrue(
            any("No handler registered" in msg for msg in cm.output),
            cm.output,
        )


# --- Userspace dispatch with the real Sockconn handler ----------------------


class DispatchUserspaceHandlersTests(unittest.TestCase):
    """Drives the real SockconnAnomalyHandler by faking /proc/net/tcp."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tcp_path = Path(self.tmp.name) / "tcp"
        self.tcp6_path = Path(self.tmp.name) / "tcp6"
        # Start both files with an empty body so the handler sees no
        # sockets until a test rewrites them.
        _write_proc_tcp(self.tcp_path, [])
        _write_proc_tcp(self.tcp6_path, [])

        self._patcher = mock.patch.object(
            sockconn_mod,
            "_PROC_FILES",
            (str(self.tcp_path), str(self.tcp6_path)),
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _build(self) -> tuple[SimpleNamespace, AnomalyWatcher, AnomalyKey]:
        cfg = _smb_sockconn_config()
        ctrl = _make_controller(anomalies={cfg.key: cfg})
        return ctrl, AnomalyWatcher(ctrl), cfg.key

    def test_first_tick_records_baseline_only(self):
        ctrl, w, _ = self._build()
        w._dispatch_userspace_handlers()
        self.assertTrue(ctrl.anomalyActionQueue.empty())

    def test_socket_set_change_fires(self):
        ctrl, w, key = self._build()
        # Baseline tick: zero sockets.
        w._dispatch_userspace_handlers()
        # Simulate a new ESTABLISHED connection to the SMB server port.
        _write_proc_tcp(
            self.tcp_path,
            [_proc_net_tcp_line(0, "0100007F:C001", _smb_remote_endpoint())],
        )
        w._dispatch_userspace_handlers()
        action = ctrl.anomalyActionQueue.get_nowait()
        self.assertEqual(action["anomaly_key"], key)

    def test_userspace_handler_exception_isolated(self):
        """One handler raising must not block other userspace handlers."""
        cfg_bad = _smb_sockconn_config()
        # A second sockconn-style handler for NFS so two userspace
        # handlers coexist (different AnomalyKey, different handler
        # instance, same registry class).
        cfg_good = _make_anomaly_config(
            protocol=Protocol.NFS,
            anomaly_type=AnomalyType.SOCKCONN,
            tool="ss",
            acceptable_count=1,
        )
        ctrl = _make_controller(
            anomalies={cfg_bad.key: cfg_bad, cfg_good.key: cfg_good}
        )
        w = AnomalyWatcher(ctrl)

        # Prime baseline on both so a delta would fire. tcp6 stays empty
        # throughout, so only the tcp_path rewrites matter below.
        w._dispatch_userspace_handlers()
        # Add an NFS connection so the "good" handler will detect change.
        nfs_remote = f"0100007F:{PROTOCOL_SERVER_PORT[Protocol.NFS]:04X}"
        _write_proc_tcp(
            self.tcp_path,
            [_proc_net_tcp_line(0, "0100007F:C002", nfs_remote)],
        )
        # Force the "bad" handler (SMB) to raise.
        bad_handler = w.userspace_handlers[cfg_bad.key][0]
        with mock.patch.object(
            bad_handler, "tick", side_effect=RuntimeError("boom")
        ):
            w._dispatch_userspace_handlers()

        # The good (NFS) handler should still have produced an action.
        action = ctrl.anomalyActionQueue.get_nowait()
        self.assertEqual(action["anomaly_key"], cfg_good.key)
        self.assertTrue(ctrl.anomalyActionQueue.empty())


# --- run() with real eBPF handlers ------------------------------------------


class RunLoopTests(unittest.TestCase):
    def test_exits_on_stop_event_and_emits_sentinel(self):
        ctrl = _make_controller(anomalies={}, watch_interval_sec=0.05)
        w = AnomalyWatcher(ctrl)
        ctrl.stop_event.set()
        w.run()
        self.assertIsNone(ctrl.anomalyActionQueue.get_nowait())

    def test_exits_on_event_sentinel(self):
        ctrl = _make_controller(anomalies={}, watch_interval_sec=5)
        w = AnomalyWatcher(ctrl)
        ctrl.eventQueue.put(None)
        w.run()
        self.assertIsNone(ctrl.anomalyActionQueue.get_nowait())

    def test_latency_handler_fires_on_threshold_breach(self):
        cfg = _smb_latency_config(acceptable_count=3, threshold_ms=50)
        ctrl = _make_controller(anomalies={cfg.key: cfg}, watch_interval_sec=5)
        w = AnomalyWatcher(ctrl)
        # 4 events all over 50 ms threshold -> >= acceptable_count=3.
        batch = _make_event_batch(
            "smbslower",
            commands=[5, 5, 8, 9],
            metric_values=[60_000_000] * 4,  # 60 ms
        )
        ctrl.eventQueue.put(batch)
        ctrl.eventQueue.put(None)
        w.run()
        action = ctrl.anomalyActionQueue.get_nowait()
        self.assertEqual(action["anomaly_key"], cfg.key)
        self.assertIsNone(ctrl.anomalyActionQueue.get_nowait())

    def test_latency_handler_silent_when_below_threshold(self):
        cfg = _smb_latency_config(acceptable_count=3, threshold_ms=50)
        ctrl = _make_controller(anomalies={cfg.key: cfg}, watch_interval_sec=5)
        w = AnomalyWatcher(ctrl)
        # All events well below threshold, none reach 1s either.
        batch = _make_event_batch(
            "smbslower",
            commands=[5, 5, 8, 9],
            metric_values=[10_000_000] * 4,  # 10 ms
        )
        ctrl.eventQueue.put(batch)
        ctrl.eventQueue.put(None)
        w.run()
        self.assertIsNone(ctrl.anomalyActionQueue.get_nowait())
        self.assertTrue(ctrl.anomalyActionQueue.empty())

    def test_latency_handler_fires_on_single_1s_outlier(self):
        """One >= 1s event trips the handler even if acceptable_count is far
        higher."""
        cfg = _smb_latency_config(acceptable_count=999, threshold_ms=50)
        ctrl = _make_controller(anomalies={cfg.key: cfg}, watch_interval_sec=5)
        w = AnomalyWatcher(ctrl)
        batch = _make_event_batch(
            "smbslower",
            commands=[5],
            metric_values=[1_500_000_000],  # 1.5 s
        )
        ctrl.eventQueue.put(batch)
        ctrl.eventQueue.put(None)
        w.run()
        action = ctrl.anomalyActionQueue.get_nowait()
        self.assertEqual(action["anomaly_key"], cfg.key)

    def test_error_handler_fires_on_count_threshold(self):
        cfg = _nfs_error_config(acceptable_count=2)
        ctrl = _make_controller(anomalies={cfg.key: cfg}, watch_interval_sec=5)
        w = AnomalyWatcher(ctrl)
        # 2 nfsiosnoop events is enough.
        batch = _make_event_batch("nfsiosnoop", commands=[1, 2])
        ctrl.eventQueue.put(batch)
        ctrl.eventQueue.put(None)
        w.run()
        action = ctrl.anomalyActionQueue.get_nowait()
        self.assertEqual(action["anomaly_key"], cfg.key)

    def test_batch_masked_per_handler_by_tool_id(self):
        """A mixed batch must be filtered down to each handler's tool."""
        lat = _smb_latency_config(acceptable_count=1, threshold_ms=50)
        err = _nfs_error_config(acceptable_count=1)
        ctrl = _make_controller(
            anomalies={lat.key: lat, err.key: err}, watch_interval_sec=5
        )
        w = AnomalyWatcher(ctrl)
        mixed = np.concatenate(
            [
                _make_event_batch(
                    "smbslower", commands=[5], metric_values=[60_000_000]
                ),
                _make_event_batch("nfsiosnoop", commands=[1]),
            ]
        )
        ctrl.eventQueue.put(mixed)
        ctrl.eventQueue.put(None)
        w.run()
        # Both should fire, in handler iteration order. We don't depend
        # on order: collect both and assert membership.
        actions = []
        try:
            while True:
                actions.append(ctrl.anomalyActionQueue.get_nowait())
        except queue.Empty:
            pass
        keys = [a["anomaly_key"] for a in actions if a is not None]
        self.assertIn(lat.key, keys)
        self.assertIn(err.key, keys)

    def test_ebpf_handler_exception_isolated(self):
        """If the real Latency handler raises, the real Error handler still
        runs in the same tick."""
        lat = _smb_latency_config(acceptable_count=1, threshold_ms=50)
        err = _nfs_error_config(acceptable_count=1)
        ctrl = _make_controller(
            anomalies={lat.key: lat, err.key: err}, watch_interval_sec=5
        )
        w = AnomalyWatcher(ctrl)
        bad = w.ebpf_handlers[lat.key][0]
        mixed = np.concatenate(
            [
                _make_event_batch(
                    "smbslower", commands=[5], metric_values=[60_000_000]
                ),
                _make_event_batch("nfsiosnoop", commands=[1]),
            ]
        )
        with mock.patch.object(bad, "detect", side_effect=RuntimeError("boom")):
            ctrl.eventQueue.put(mixed)
            ctrl.eventQueue.put(None)
            w.run()
        actions = []
        try:
            while True:
                actions.append(ctrl.anomalyActionQueue.get_nowait())
        except queue.Empty:
            pass
        keys = [a["anomaly_key"] for a in actions if a is not None]
        self.assertNotIn(lat.key, keys)
        self.assertIn(err.key, keys)

    def test_sentinel_mid_drain_stops_after_pending_batches(self):
        """A sentinel in the middle of the drain queue must terminate the
        loop after the first batch group; later batches must not be
        processed."""
        cfg = _smb_latency_config(acceptable_count=1, threshold_ms=50)
        ctrl = _make_controller(anomalies={cfg.key: cfg}, watch_interval_sec=5)
        w = AnomalyWatcher(ctrl)
        # batch1 alone is below threshold; batch2 (after the sentinel)
        # would push us above it. If batch2 leaks in, an action fires.
        ctrl.eventQueue.put(
            _make_event_batch("smbslower", commands=[5], metric_values=[10_000_000])
        )
        ctrl.eventQueue.put(None)
        ctrl.eventQueue.put(
            _make_event_batch("smbslower", commands=[5], metric_values=[60_000_000])
        )
        w.run()
        # Only the shutdown sentinel should be on the action queue.
        self.assertIsNone(ctrl.anomalyActionQueue.get_nowait())
        self.assertTrue(ctrl.anomalyActionQueue.empty())

    def test_empty_masked_batch_skips_detect(self):
        """When the batch contains no events for a handler's tool, detect()
        must not be invoked."""
        cfg = _smb_latency_config(acceptable_count=1, threshold_ms=50)
        ctrl = _make_controller(anomalies={cfg.key: cfg}, watch_interval_sec=5)
        w = AnomalyWatcher(ctrl)
        handler = w.ebpf_handlers[cfg.key][0]
        # Only nfsiosnoop events; the SMB latency handler should see an
        # empty masked batch and short-circuit before detect().
        ctrl.eventQueue.put(_make_event_batch("nfsiosnoop", commands=[1]))
        ctrl.eventQueue.put(None)
        with mock.patch.object(
            handler, "detect", side_effect=AssertionError("should not run")
        ):
            w.run()
        self.assertIsNone(ctrl.anomalyActionQueue.get_nowait())
        self.assertTrue(ctrl.anomalyActionQueue.empty())

    def test_drains_and_concatenates_multiple_batches(self):
        """get() + get_nowait() drain logic must combine pending batches
        into one detect() call per handler."""
        cfg = _smb_latency_config(acceptable_count=5, threshold_ms=50)
        ctrl = _make_controller(anomalies={cfg.key: cfg}, watch_interval_sec=5)
        w = AnomalyWatcher(ctrl)
        # 3 batches, individually below threshold, combined above.
        for n_events in (2, 2, 2):
            ctrl.eventQueue.put(
                _make_event_batch(
                    "smbslower",
                    commands=[5] * n_events,
                    metric_values=[60_000_000] * n_events,
                )
            )
        ctrl.eventQueue.put(None)
        w.run()
        action = ctrl.anomalyActionQueue.get_nowait()
        self.assertEqual(action["anomaly_key"], cfg.key)

    def test_all_anomaly_types_fire_in_one_pass(self):
        """All five (SMB latency, SMB sockconn, NFS latency, NFS error,
        NFS sockconn) detect inside a single run() invocation.

        Sequence:
          1. Prime userspace sockconn baselines on empty /proc.
          2. Mutate /proc to add SMB- and NFS-port connections.
          3. Enqueue one mixed eBPF batch (no sentinel) holding
             smbslower-latency, nfsslower-latency, nfsiosnoop-error events.
          4. Wrap _dispatch_userspace_handlers so the post-batch tick
             runs the real handlers (firing sockconn) and then sets
             stop_event, making the subsequent stop_event.wait() return
             True.
        """
        with tempfile.TemporaryDirectory() as td:
            tcp = Path(td) / "tcp"
            tcp6 = Path(td) / "tcp6"
            _write_proc_tcp(tcp, [])
            _write_proc_tcp(tcp6, [])

            smb_lat = _smb_latency_config(acceptable_count=1, threshold_ms=50)
            smb_sock = _smb_sockconn_config()
            nfs_lat = _nfs_latency_config(acceptable_count=1, threshold_ms=50)
            nfs_err = _nfs_error_config(acceptable_count=1)
            nfs_sock = _nfs_sockconn_config()
            anomalies = {
                cfg.key: cfg
                for cfg in (smb_lat, smb_sock, nfs_lat, nfs_err, nfs_sock)
            }
            ctrl = _make_controller(anomalies=anomalies, watch_interval_sec=5)

            with mock.patch.object(
                sockconn_mod, "_PROC_FILES", (str(tcp), str(tcp6))
            ):
                w = AnomalyWatcher(ctrl)
                # Prime sockconn baselines (first tick records, never fires).
                w._dispatch_userspace_handlers()

                # Now add one connection per protocol so the next userspace
                # tick (inside run()) sees a delta and fires both.
                smb_port = PROTOCOL_SERVER_PORT[Protocol.SMB]
                nfs_port = PROTOCOL_SERVER_PORT[Protocol.NFS]
                _write_proc_tcp(
                    tcp,
                    [
                        _proc_net_tcp_line(
                            0, "0100007F:C001", f"0100007F:{smb_port:04X}"
                        ),
                        _proc_net_tcp_line(
                            1, "0100007F:C002", f"0100007F:{nfs_port:04X}"
                        ),
                    ],
                )

                # Mixed eBPF batch: one slow op per tool, all over threshold.
                ctrl.eventQueue.put(
                    np.concatenate(
                        [
                            _make_event_batch(
                                "smbslower",
                                commands=[5],
                                metric_values=[60_000_000],
                            ),
                            _make_event_batch(
                                "nfsslower",
                                commands=[1],
                                metric_values=[60_000_000],
                            ),
                            _make_event_batch("nfsiosnoop", commands=[1]),
                        ]
                    )
                )

                # Make the post-batch userspace tick fire sockconn, then
                # set stop_event so the loop exits at the next wait().
                real_dispatch = w._dispatch_userspace_handlers

                def stop_after_dispatch():
                    real_dispatch()
                    ctrl.stop_event.set()

                w._dispatch_userspace_handlers = stop_after_dispatch
                w.run()

            actions = []
            try:
                while True:
                    actions.append(ctrl.anomalyActionQueue.get_nowait())
            except queue.Empty:
                pass
            real_actions = [a for a in actions if a is not None]
            keys = [a["anomaly_key"] for a in real_actions]
            self.assertIn(smb_lat.key, keys)
            self.assertIn(smb_sock.key, keys)
            self.assertIn(nfs_lat.key, keys)
            self.assertIn(nfs_err.key, keys)
            self.assertIn(nfs_sock.key, keys)
            # Action-dict contract: exactly {anomaly_key, timestamp:int}.
            for a in real_actions:
                self.assertEqual(set(a), {"anomaly_key", "timestamp"})
                self.assertIsInstance(a["timestamp"], int)

    def test_timeout_dispatches_real_userspace_handler(self):
        """When eventQueue is empty for `interval` seconds, userspace
        handlers must still tick. Uses the real SockconnAnomalyHandler
        pointed at empty fake /proc files."""
        with tempfile.TemporaryDirectory() as td:
            tcp = Path(td) / "tcp"
            tcp6 = Path(td) / "tcp6"
            _write_proc_tcp(tcp, [])
            _write_proc_tcp(tcp6, [])

            cfg = _smb_sockconn_config()
            ctrl = _make_controller(
                anomalies={cfg.key: cfg}, watch_interval_sec=0.01
            )

            with mock.patch.object(
                sockconn_mod, "_PROC_FILES", (str(tcp), str(tcp6))
            ):
                w = AnomalyWatcher(ctrl)
                handler = w.userspace_handlers[cfg.key][0]

                def _stop_soon():
                    time.sleep(0.05)
                    ctrl.stop_event.set()

                t = threading.Thread(target=_stop_soon)
                t.start()
                w.run()
                t.join()

                # First tick set the baseline; later ticks see no change
                # and produce no actions, but `_prev` must have been
                # populated, proving the handler ran.
                self.assertIsNotNone(handler._prev)


if __name__ == "__main__":
    unittest.main()
