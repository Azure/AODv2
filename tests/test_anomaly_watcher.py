import unittest
from src.AnomalyWatcher import AnomalyWatcher
from src.Controller import Controller
import os


class TestAnomalyWatcher(unittest.TestCase):
    def setUp(self):
        config_path = os.path.join(
            os.path.dirname(__file__), "../config/config.yaml"
        )
        self.controller = Controller(config_path)
        self.watcher = AnomalyWatcher(self.controller)

    def test_init(self):
        self.assertIsNotNone(self.watcher)


if __name__ == "__main__":
    unittest.main()
