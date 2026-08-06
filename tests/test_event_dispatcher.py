import unittest
from src.EventDispatcher import EventDispatcher
from src.Controller import Controller
import os


class TestEventDispatcher(unittest.TestCase):
    def setUp(self):
        config_path = os.path.join(
            os.path.dirname(__file__), "../config/config.yaml"
        )
        self.controller = Controller(config_path)
        self.dispatcher = EventDispatcher(self.controller)

    def test_init(self):
        self.assertIsNotNone(self.dispatcher)


if __name__ == "__main__":
    unittest.main()
