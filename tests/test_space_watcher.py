import unittest
from src.SpaceWatcher import SpaceWatcher
from src.Controller import Controller
import os


class TestSpaceWatcher(unittest.TestCase):
    def setUp(self):
        config_path = os.path.join(
            os.path.dirname(__file__), "../config/config.yaml"
        )
        self.controller = Controller(config_path)
        self.watcher = SpaceWatcher(self.controller)

    def test_init(self):
        self.assertIsNotNone(self.watcher)


if __name__ == "__main__":
    unittest.main()
