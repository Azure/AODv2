import unittest
from src.LogCollector import LogCollector
from src.Controller import Controller
import os


class TestLogCollector(unittest.TestCase):
    def setUp(self):
        config_path = os.path.join(
            os.path.dirname(__file__), "../config/config.yaml"
        )
        self.controller = Controller(config_path)
        self.collector = LogCollector(self.controller)

    def test_init(self):
        self.assertIsNotNone(self.collector)


if __name__ == "__main__":
    unittest.main()
