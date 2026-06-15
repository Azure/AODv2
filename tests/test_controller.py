import unittest
from unittest.mock import patch
import os

from src.Controller import Controller
from utils.anomaly_type import AnomalyType, Protocol
from utils.config_schema import AnomalyKey

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "../config/config.yaml")


def _make_controller():
    """Construct a Controller with no-op sub-components."""
    with (
        patch("src.Controller.EventDispatcher"),
        patch("src.Controller.AnomalyWatcher"),
        patch("src.Controller.LogCollector"),
        patch("src.Controller.SpaceWatcher"),
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

    def test_snapshot_noop_after_stop(self):
        self.controller.stop()
        self._drain(self.controller.anomalyActionQueue)

        self.controller.trigger_snapshot()  # SNAPSHOT, not SHUTDOWN

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


if __name__ == "__main__":
    unittest.main()
