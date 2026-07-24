from base.QuickAction import QuickAction


class DmesgQuickAction(QuickAction):

    def __init__(self, batches_root: str, anomaly_interval: int = 1):
        """Args:
        batches_root (str): Root directory for log batches.
        anomaly_interval (int): Time interval in seconds to filter logs.
        """
        super().__init__(batches_root, "dmesg.log")
        self.anomaly_interval = anomaly_interval

    def get_command(self) -> tuple[list[str], str]:
        return [
            "journalctl",
            "-k",
            "--since",
            f"{self.anomaly_interval} seconds ago",
        ], "cmd"
