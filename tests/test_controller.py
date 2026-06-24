import os
import signal
import subprocess
import unittest
from unittest.mock import MagicMock, call, patch

import Controller as controller_mod
from Controller import Controller, handle_signal, handle_snapshot_signal
from utils.anomaly_type import AnomalyType, Protocol
from utils.config_schema import AnomalyKey
from utils.pdeathsig_wrapper import pdeathsig_preexec
from utils.shared_data import ALL_NFS_CMDS, ALL_NFS_ERRS, ALL_SMB_CMDS

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "../config/config.yaml")
SRC_BIN_DIR = os.path.join(os.path.dirname(controller_mod.__file__), "bin")


def _make_controller():
    """Construct a Controller with no-op sub-components."""
    with (
        patch("Controller.EventDispatcher"),
        patch("Controller.AnomalyWatcher"),
        patch("Controller.LogCollector"),
        patch("Controller.SpaceWatcher"),
    ):
        return Controller(CONFIG_PATH)


class TestController(unittest.TestCase):
    def setUp(self):
        self.controller = _make_controller()

    def test_init(self):
        self.assertIsNotNone(self.controller.config)
        self.assertTrue(hasattr(self.controller, "event_dispatcher"))
        self.assertTrue(hasattr(self.controller, "anomaly_watcher"))
        self.assertTrue(hasattr(self.controller, "log_collector_manager"))
        self.assertTrue(hasattr(self.controller, "space_watcher"))


class TestSnapshotAndShutdown(unittest.TestCase):
    """Cover trigger_snapshot() and stop() semantics: enqueueing the
    SNAPSHOT/SHUTDOWN dump requests, the stop_event flag, the sentinels
    on both queues, and the double-stop / post-stop guards."""

    def setUp(self):
        self.controller = _make_controller()

    def _drain(self, q):
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        return items

    def test_trigger_snapshot_enqueues_aod_snapshot(self):
        self.controller.trigger_snapshot()

        items = self._drain(self.controller.anomalyActionQueue)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(
            item["anomaly_key"],
            AnomalyKey(Protocol.AOD, AnomalyType.SNAPSHOT),
        )
        self.assertIsInstance(item["timestamp"], int)
        self.assertGreater(item["timestamp"], 0)
        self.assertFalse(self.controller.stop_event.is_set())

    def test_stop_enqueues_shutdown_then_sentinels(self):
        self.controller.stop()

        self.assertTrue(self.controller.stop_event.is_set())

        anomaly_items = self._drain(self.controller.anomalyActionQueue)
        # SHUTDOWN dump request must be enqueued BEFORE the None sentinel
        # so LogCollector picks it up before draining.
        self.assertEqual(len(anomaly_items), 2)
        self.assertEqual(
            anomaly_items[0]["anomaly_key"],
            AnomalyKey(Protocol.AOD, AnomalyType.SHUTDOWN),
        )
        self.assertIsNone(anomaly_items[1])

        event_items = self._drain(self.controller.eventQueue)
        self.assertEqual(event_items, [None])

    def test_double_stop_emits_single_shutdown_snapshot(self):
        self.controller.stop()
        self.controller.stop()

        anomaly_items = self._drain(self.controller.anomalyActionQueue)
        shutdown_dumps = [
            i
            for i in anomaly_items
            if i is not None
            and i["anomaly_key"].anomaly_type == AnomalyType.SHUTDOWN
        ]
        self.assertEqual(len(shutdown_dumps), 1)
        self.assertEqual(sum(1 for i in anomaly_items if i is None), 2)
        event_items = self._drain(self.controller.eventQueue)
        self.assertEqual(event_items, [None, None])

    def test_snapshot_noop_after_stop(self):
        self.controller.stop()
        self._drain(self.controller.anomalyActionQueue)

        self.controller.trigger_snapshot()

        self.assertTrue(self.controller.anomalyActionQueue.empty())

    def test_shutdown_snapshot_still_allowed_after_stop(self):
        # Once stop() has run, the public trigger_snapshot() rejects
        # SNAPSHOT but must still accept an explicit SHUTDOWN request
        # (the path stop() itself takes).
        self.controller.stop()
        self._drain(self.controller.anomalyActionQueue)

        self.controller.trigger_snapshot(AnomalyType.SHUTDOWN)

        items = self._drain(self.controller.anomalyActionQueue)
        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0]["anomaly_key"],
            AnomalyKey(Protocol.AOD, AnomalyType.SHUTDOWN),
        )


class TestExtractTools(unittest.TestCase):
    """_extract_tools() should return the unique set of tool names
    referenced by the configured anomalies."""

    def test_returns_unique_tool_set_from_production_config(self):
        controller = _make_controller()
        # Mirrors config/config.yaml: smb.latency + smb.sockconn,
        # nfs.latency + nfs.sockconn + nfs.error.
        self.assertEqual(
            controller._extract_tools(),
            {"smbslower", "ss", "nfsslower", "nfsiosnoop"},
        )


class TestLatencyToolCmd(unittest.TestCase):
    """_get_latency_tool_cmd() must translate an anomaly's track_commands
    map into the eBPF CLI: [<binary>, -m <min_ms>, -c <csv cmd ids>]."""

    def setUp(self):
        self.controller = _make_controller()

    def test_smbslower_cmd_uses_min_threshold_and_all_smb_cmd_ids(self):
        cmd = self.controller._get_latency_tool_cmd("smbslower")

        # Production config: default 20 ms, SMB2_WRITE overridden to 50 ms
        # in 'all' mode -> all SMB cmd ids tracked, min threshold is 20.
        self.assertEqual(cmd[0], os.path.join(SRC_BIN_DIR, "smbslower"))
        self.assertEqual(cmd[1:4], ["-m", "20", "-c"])

        ids = [int(x) for x in cmd[4].split(",")]
        self.assertEqual(set(ids), set(ALL_SMB_CMDS.values()))

    def test_nfsslower_cmd_uses_default_threshold(self):
        cmd = self.controller._get_latency_tool_cmd("nfsslower")

        # Production config: default 50 ms, no per-command overrides ->
        # every NFS cmd tracked at 50 ms, so min threshold is 50.
        self.assertEqual(cmd[0], os.path.join(SRC_BIN_DIR, "nfsslower"))
        self.assertEqual(cmd[1:4], ["-m", "50", "-c"])

        ids = [int(x) for x in cmd[4].split(",")]
        self.assertEqual(set(ids), set(ALL_NFS_CMDS.values()))


class TestErrorToolCmd(unittest.TestCase):
    """_get_error_tool_cmd() emits only the axes (track_commands /
    track_errors) that the user actually populated. Empty axes are
    omitted -- the kernel side treats a missing allowlist as 'no
    filter'."""

    def setUp(self):
        self.controller = _make_controller()

    def test_nfsiosnoop_cmd_omits_empty_track_commands(self):
        cmd = self.controller._get_error_tool_cmd("nfsiosnoop")

        # Production config: track_commands is empty, track_errors lists
        # NFS4ERR_OLD_STATEID (10024) and NFS4ERR_BAD_STATEID (10025).
        self.assertEqual(cmd[0], os.path.join(SRC_BIN_DIR, "nfsiosnoop"))
        self.assertNotIn("-c", cmd)
        self.assertIn("-e", cmd)

        err_csv = cmd[cmd.index("-e") + 1]
        err_ids = [int(x) for x in err_csv.split(",")]
        self.assertEqual(
            set(err_ids),
            {
                ALL_NFS_ERRS["NFS4ERR_OLD_STATEID"],
                ALL_NFS_ERRS["NFS4ERR_BAD_STATEID"],
            },
        )
        # Sorted form, since the CLI builder sorts for stable output.
        self.assertEqual(err_ids, sorted(err_ids))


class TestRunStartsComponents(unittest.TestCase):
    """run() is the orchestration entry point. Verify it spins up a
    process supervisor per eBPF tool, supervisor threads for the four
    long-lived components, and applies fatal_on_exc only to
    LogCollector (which owns the asyncio loop)."""

    def setUp(self):
        self.controller = _make_controller()

    def _invoke_run(self):
        # Pre-set stop_event so the final stop_event.wait() returns
        # immediately and run() doesn't block.
        self.controller.stop_event.set()
        with (
            patch("Controller.threading.Thread") as mock_thread_cls,
            patch.object(self.controller, "_supervise_thread") as mock_sup_thread,
            patch.object(self.controller, "_shutdown"),
        ):
            self.controller.run()
        return mock_thread_cls, mock_sup_thread

    def test_starts_one_process_supervisor_per_ebpf_tool(self):
        mock_thread_cls, _ = self._invoke_run()

        names = sorted(c.kwargs["name"] for c in mock_thread_cls.call_args_list)
        # 'ss' is userspace and is driven by AnomalyWatcher, not the
        # process supervisor. The three eBPF tools each get one.
        self.assertEqual(
            names,
            sorted(
                [
                    "smbslower_Supervisor",
                    "nfsslower_Supervisor",
                    "nfsiosnoop_Supervisor",
                ]
            ),
        )
        for c in mock_thread_cls.call_args_list:
            # Bound methods are equal-but-not-identical across attribute
            # accesses, so compare with ==.
            self.assertEqual(c.kwargs["target"], self.controller._supervise_process)
            self.assertTrue(c.kwargs["daemon"])
        mock_thread_cls.return_value.start.assert_called()

    def test_each_supervisor_thread_uses_its_own_cmd_builder(self):
        # Regression test for the late-binding bug guarded by the
        # `tn=tool_name, cb=cmd_builder` default-arg trick in run().
        mock_thread_cls, _ = self._invoke_run()

        builders_by_tool = {}
        for c in mock_thread_cls.call_args_list:
            tool_name, builder_lambda = c.kwargs["args"]
            builders_by_tool[tool_name] = builder_lambda

        self.assertEqual(
            builders_by_tool["smbslower"](),
            self.controller._get_latency_tool_cmd("smbslower"),
        )
        self.assertEqual(
            builders_by_tool["nfsslower"](),
            self.controller._get_latency_tool_cmd("nfsslower"),
        )
        self.assertEqual(
            builders_by_tool["nfsiosnoop"](),
            self.controller._get_error_tool_cmd("nfsiosnoop"),
        )

    def test_supervises_four_component_threads_with_logcollector_fatal(self):
        _, mock_sup_thread = self._invoke_run()

        by_name = {c.args[0]: c for c in mock_sup_thread.call_args_list}
        self.assertEqual(
            set(by_name),
            {"EventDispatcher", "AnomalyWatcher", "LogCollector", "SpaceWatcher"},
        )

        # LogCollector owns an asyncio loop with attached subprocess
        # handles; restart-in-place would orphan them, so it must be
        # marked fatal_on_exc.
        self.assertTrue(by_name["LogCollector"].kwargs.get("fatal_on_exc"))
        for name in ("EventDispatcher", "AnomalyWatcher", "SpaceWatcher"):
            self.assertFalse(by_name[name].kwargs.get("fatal_on_exc", False))

        # Each component is supervised by its run() method, not some other
        # entry point.
        self.assertIs(
            by_name["EventDispatcher"].args[1], self.controller.event_dispatcher.run
        )
        self.assertIs(
            by_name["AnomalyWatcher"].args[1], self.controller.anomaly_watcher.run
        )
        self.assertIs(
            by_name["LogCollector"].args[1],
            self.controller.log_collector_manager.run,
        )
        self.assertIs(
            by_name["SpaceWatcher"].args[1], self.controller.space_watcher.run
        )


class TestSuperviseThread(unittest.TestCase):
    """The thread supervisor must transparently restart a target that
    raises, unless fatal_on_exc is set -- in which case it must escalate
    to a full service shutdown."""

    def setUp(self):
        self.controller = _make_controller()

    def test_restarts_target_on_exception(self):
        call_count = 0

        def target():
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                # Stop after the 3rd attempt so the supervisor exits.
                self.controller.stop_event.set()
                return
            raise RuntimeError("boom")

        # Patch stop() so we can prove the non-fatal branch never
        # escalates -- a sentinel-based check on the queues would also
        # pass if a future regression set stop_event some other way.
        with (
            patch("Controller.time.sleep"),
            patch.object(self.controller, "stop") as mock_stop,
        ):
            self.controller._supervise_thread("flaky", target)
            thread = self.controller.threads[-1]
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(call_count, 3)
        if __debug__:
            self.assertEqual(self.controller.thread_restarts, 2)
        mock_stop.assert_not_called()

    def test_fatal_on_exc_escalates_to_full_shutdown(self):
        def target():
            raise RuntimeError("non-recoverable")

        with patch("Controller.time.sleep"):
            self.controller._supervise_thread(
                "fatal_component", target, fatal_on_exc=True
            )

        thread = self.controller.threads[-1]
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertTrue(self.controller.stop_event.is_set())
        # fatal_on_exc returns immediately without bumping the restart
        # counter -- the whole service is going down.
        if __debug__:
            self.assertEqual(self.controller.thread_restarts, 0)
        # stop() must have been called: it pushes sentinels onto the
        # consumer queues.
        self.assertFalse(self.controller.eventQueue.empty())
        self.assertFalse(self.controller.anomalyActionQueue.empty())

    def test_clean_exit_does_not_count_as_restart(self):
        # A target that returns normally without raising loops back to
        # the while condition. With stop_event set, the loop exits.
        calls = 0

        def target():
            nonlocal calls
            calls += 1
            self.controller.stop_event.set()

        self.controller._supervise_thread("clean", target)
        self.controller.threads[-1].join(timeout=5)

        self.assertEqual(calls, 1)
        if __debug__:
            self.assertEqual(self.controller.thread_restarts, 0)

    def test_supervised_thread_is_daemon_and_registered(self):
        self.controller.stop_event.set()  # exit immediately
        self.controller._supervise_thread("regd", lambda: None)
        thread = self.controller.threads[-1]
        thread.join(timeout=5)

        self.assertIn(thread, self.controller.threads)
        self.assertTrue(thread.daemon)
        self.assertEqual(thread.name, "regd")


class TestSuperviseProcess(unittest.TestCase):
    """The process supervisor: builds the command per-iteration from
    cmd_builder, launches it under a fresh session with PR_SET_PDEATHSIG,
    restarts it on unexpected exit, and on shutdown escalates from
    SIGINT to SIGKILL if the child doesn't exit in time."""

    def setUp(self):
        self.controller = _make_controller()

    def _common_patches(self, popen_side_effect, wait_returns):
        """Build the standard patch set for _supervise_process tests.

        wait_returns is the sequence of values stop_event.wait() should
        yield. When wait returns True the real stop_event is set, so the
        supervisor naturally exits via its own is_set() check on the
        next iteration -- no need to mock is_set separately. The
        production code path can grow additional is_set() checks without
        invalidating these tests.
        """
        wait_iter = iter(wait_returns)

        def wait_side_effect(timeout=None):
            try:
                val = next(wait_iter)
            except StopIteration:
                return self.controller.stop_event.is_set()
            if val:
                self.controller.stop_event.set()
            return val

        return {
            "popen": patch(
                "Controller.subprocess.Popen",
                side_effect=popen_side_effect,
            ),
            "killpg": patch("Controller.os.killpg"),
            "getpgid": patch("Controller.os.getpgid", return_value=4242),
            "sleep": patch("Controller.time.sleep"),
            "set_name": patch("Controller.set_thread_name"),
            "wait": patch.object(
                self.controller.stop_event, "wait", side_effect=wait_side_effect
            ),
        }

    def test_popen_kwargs_and_graceful_sigint_on_shutdown(self):
        cmd_builder = MagicMock(return_value=["/bin/tool", "-x"])
        fake_proc = MagicMock(pid=1234, returncode=0)

        # Single outer iteration: stop_event.wait() returns True (signal
        # received) which also sets the real stop_event, so the
        # post-break is_set() check enters the shutdown branch and the
        # outer loop exits.
        patches = self._common_patches(
            popen_side_effect=[fake_proc],
            wait_returns=[True],
        )
        with (
            patches["popen"] as mock_popen,
            patches["killpg"] as mock_killpg,
            patches["getpgid"] as mock_getpgid,
            patches["sleep"],
            patches["set_name"],
            patches["wait"],
        ):
            self.controller._supervise_process("smbslower", cmd_builder)

        cmd_builder.assert_called_once()
        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        self.assertEqual(args[0], ["/bin/tool", "-x"])
        self.assertTrue(kwargs.get("start_new_session"))
        self.assertIs(kwargs.get("preexec_fn"), pdeathsig_preexec)

        # Graceful SIGINT goes to the whole process group (the child was
        # launched with start_new_session=True).
        mock_getpgid.assert_called_with(1234)
        mock_killpg.assert_called_once_with(4242, signal.SIGINT)
        fake_proc.wait.assert_called_once_with(timeout=5)

        # The live handle is registered so trigger_* paths can find it.
        self.assertIs(self.controller.tool_processes["smbslower"], fake_proc)

    def test_sigkill_escalation_when_child_ignores_sigint(self):
        cmd_builder = MagicMock(return_value=["/bin/tool"])
        fake_proc = MagicMock(pid=1234, returncode=None)
        fake_proc.wait.side_effect = subprocess.TimeoutExpired(
            cmd="x", timeout=5
        )

        patches = self._common_patches(
            popen_side_effect=[fake_proc],
            wait_returns=[True],
        )
        with (
            patches["popen"],
            patches["killpg"] as mock_killpg,
            patches["getpgid"],
            patches["sleep"],
            patches["set_name"],
            patches["wait"],
        ):
            self.controller._supervise_process("smbslower", cmd_builder)

        # First SIGINT, then -- because wait() raised TimeoutExpired --
        # SIGKILL to the same pgid.
        mock_killpg.assert_has_calls(
            [call(4242, signal.SIGINT), call(4242, signal.SIGKILL)]
        )
        self.assertEqual(mock_killpg.call_count, 2)

    def test_restarts_process_on_unexpected_exit(self):
        cmd_builder = MagicMock(side_effect=[["/bin/tool"], ["/bin/tool"]])
        # Iteration 1: poll() reports exited (returncode=1) -> restart path.
        proc1 = MagicMock(pid=1001, returncode=1)
        proc1.poll.return_value = 1
        # Iteration 2: still running when we shut down -> graceful path.
        proc2 = MagicMock(pid=1002, returncode=0)
        proc2.poll.return_value = None

        # Iteration 1: wait returns False, poll triggers restart, stop_event
        # still clear so we loop. Iteration 2: wait returns True, which sets
        # stop_event, so the shutdown branch runs and we break.
        patches = self._common_patches(
            popen_side_effect=[proc1, proc2],
            wait_returns=[False, True],
        )
        with (
            patches["popen"] as mock_popen,
            patches["killpg"] as mock_killpg,
            patches["getpgid"],
            patches["sleep"],
            patches["set_name"],
            patches["wait"],
        ):
            self.controller._supervise_process("smbslower", cmd_builder)

        self.assertEqual(mock_popen.call_count, 2)
        if __debug__:
            self.assertEqual(self.controller.process_restarts, 1)
        # The live handle is the most recently started process.
        self.assertIs(self.controller.tool_processes["smbslower"], proc2)
        # Only the second process was alive at shutdown time, so killpg
        # is called once.
        mock_killpg.assert_called_once()

    def test_swallows_processlookuperror_on_shutdown(self):
        # If the child happens to exit between SIGINT and wait()
        # returning, ProcessLookupError must not propagate.
        cmd_builder = MagicMock(return_value=["/bin/tool"])
        fake_proc = MagicMock(pid=1234, returncode=0)

        patches = self._common_patches(
            popen_side_effect=[fake_proc],
            wait_returns=[True],
        )
        with (
            patches["popen"],
            patch(
                "Controller.os.killpg", side_effect=ProcessLookupError
            ) as mock_killpg,
            patches["getpgid"],
            patches["sleep"],
            patches["set_name"],
            patches["wait"],
        ):
            # Must not raise.
            self.controller._supervise_process("smbslower", cmd_builder)

        mock_killpg.assert_called_once()


class TestShutdownSequence(unittest.TestCase):
    """_shutdown() joins every registered thread and tears down the
    EventDispatcher's resources."""

    def setUp(self):
        self.controller = _make_controller()

    def test_joins_all_threads_and_cleans_up_dispatcher(self):
        t1 = MagicMock(ident=1)
        t1.name = "t1"
        t1.is_alive.return_value = False
        t2 = MagicMock(ident=2)
        t2.name = "t2"
        t2.is_alive.return_value = False
        self.controller.threads = [t1, t2]

        self.controller._shutdown()

        t1.join.assert_called_once_with(timeout=5)
        t2.join.assert_called_once_with(timeout=5)
        self.controller.event_dispatcher.cleanup.assert_called_once()

    def test_tolerates_thread_that_does_not_exit(self):
        stuck = MagicMock(ident=99)
        stuck.name = "stuck"
        stuck.is_alive.return_value = True  # never exits
        self.controller.threads = [stuck]

        # Should log a warning but must not raise; remaining cleanup
        # still runs.
        self.controller._shutdown()

        stuck.join.assert_called_once_with(timeout=5)
        self.controller.event_dispatcher.cleanup.assert_called_once()


class TestSignalHandlers(unittest.TestCase):
    """The module-level signal handlers must be safe to call from a
    signal context and must delegate to the Controller."""

    def setUp(self):
        self.controller = _make_controller()

    def test_handle_signal_invokes_stop(self):
        with patch.object(self.controller, "stop") as mock_stop:
            handle_signal(self.controller, signal.SIGTERM, None)
        mock_stop.assert_called_once_with()

    def test_handle_snapshot_signal_invokes_trigger_snapshot(self):
        with patch.object(self.controller, "trigger_snapshot") as mock_trigger:
            handle_snapshot_signal(self.controller, signal.SIGUSR1, None)
        # Don't pin the call arg list: trigger_snapshot()'s default is
        # AnomalyType.SNAPSHOT, and a future cleanup that passes it
        # explicitly would be behaviour-equivalent.
        mock_trigger.assert_called_once()
        passed_type = (
            mock_trigger.call_args.args[0]
            if mock_trigger.call_args.args
            else mock_trigger.call_args.kwargs.get(
                "anomaly_type", AnomalyType.SNAPSHOT
            )
        )
        self.assertEqual(passed_type, AnomalyType.SNAPSHOT)


if __name__ == "__main__":
    unittest.main()
