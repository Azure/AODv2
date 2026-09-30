import unittest
from src import shared_data
from collections.abc import Mapping


class TestSharedData(unittest.TestCase):
    def test_all_smb_cmds(self):
        self.assertIsInstance(shared_data.ALL_SMB_CMDS, Mapping)


if __name__ == "__main__":
    unittest.main()
