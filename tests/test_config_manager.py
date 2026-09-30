import unittest
from src.ConfigManager import ConfigManager
import os


class TestConfigManager(unittest.TestCase):
    def test_load_config(self):
        config_path = os.path.join(
            os.path.dirname(__file__), "../config/config.yaml"
        )
        cm = ConfigManager(config_path)
        self.assertIsNotNone(cm.data)


if __name__ == "__main__":
    unittest.main()
