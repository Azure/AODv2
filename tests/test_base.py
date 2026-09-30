import unittest
from src.base import QuickAction, AnomalyHandlerBase


class TestBase(unittest.TestCase):
    def test_anomaly_handler_base(self):
        self.assertTrue(hasattr(AnomalyHandlerBase, "AnomalyHandler"))

    def test_quick_action(self):
        self.assertTrue(hasattr(QuickAction, "QuickAction"))


if __name__ == "__main__":
    unittest.main()
