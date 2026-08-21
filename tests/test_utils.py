import unittest
from utils import anomaly_type, config_schema


class TestUtils(unittest.TestCase):
    def test_anomaly_type(self):
        self.assertTrue(hasattr(anomaly_type, "AnomalyType"))

    def test_config_schema(self):
        self.assertTrue(hasattr(config_schema, "Config"))


if __name__ == "__main__":
    unittest.main()
