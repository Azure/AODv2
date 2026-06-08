import unittest
from src.handlers import (
    CifsstatsQuickAction,
    DebugDataQuickAction,
    DmesgQuickAction,
    error_anomaly_handler,
    JournalctlQuickAction,
    latency_anomaly_handler,
    MountsQuickAction,
    SmbinfoQuickAction,
    SysLogsQuickAction,
)


class TestHandlers(unittest.TestCase):
    def test_cifsstats(self):
        self.assertTrue(hasattr(CifsstatsQuickAction, "CifsstatsQuickAction"))

    def test_debugdata(self):
        self.assertTrue(hasattr(DebugDataQuickAction, "DebugDataQuickAction"))

    def test_dmesg(self):
        self.assertTrue(hasattr(DmesgQuickAction, "DmesgQuickAction"))

    def test_error_anomaly(self):
        self.assertTrue(hasattr(error_anomaly_handler, "ErrorAnomalyHandler"))

    def test_journalctl(self):
        self.assertTrue(hasattr(JournalctlQuickAction, "JournalctlQuickAction"))

    def test_latency(self):
        self.assertTrue(hasattr(latency_anomaly_handler, "LatencyAnomalyHandler"))

    def test_mounts(self):
        self.assertTrue(hasattr(MountsQuickAction, "MountsQuickAction"))

    def test_smbinfo(self):
        self.assertTrue(hasattr(SmbinfoQuickAction, "SmbinfoQuickAction"))

    def test_syslogs(self):
        self.assertTrue(hasattr(SysLogsQuickAction, "SysLogsQuickAction"))


if __name__ == "__main__":
    unittest.main()
