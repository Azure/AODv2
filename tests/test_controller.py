import unittest
from unittest.mock import Mock, patch
import threading
import queue
import asyncio
from src.Controller import Controller
import os
import time


class TestController(unittest.TestCase):
    def setUp(self):
        config_path = os.path.join(
            os.path.dirname(__file__), "../config/config.yaml"
        )
        self.controller = Controller(config_path)

    def test_init(self):
        self.assertIsNotNone(self.controller.config)
        self.assertTrue(hasattr(self.controller, "event_dispatcher"))
        self.assertTrue(hasattr(self.controller, "anomaly_watcher"))
        self.assertTrue(hasattr(self.controller, "log_collector_manager"))
        self.assertTrue(hasattr(self.controller, "space_watcher"))

    if __name__ == "__main__":
        unittest.main()
