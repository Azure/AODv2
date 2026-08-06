from base.QuickAction import QuickAction


class DebugDataQuickAction(QuickAction):
    """Quick action to collect CIFS debug data."""

    def __init__(self, batches_root: str):
        """Args:
        batches_root (str): Root directory for log batches.
        """
        super().__init__(batches_root, "debug_data.log")

    def get_command(self) -> tuple[list[str], str]:
        """returns cat /proc/fs/cifs/DebugData."""
        return [
            "cat",
            "/proc/fs/cifs/DebugData",
        ], "cat"
