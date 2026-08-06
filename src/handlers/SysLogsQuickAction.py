import logging
from base.QuickAction import QuickAction

logger = logging.getLogger(__name__)


class SysLogsQuickAction(QuickAction):
    def __init__(self, batches_root: str, num_lines: int = 100):
        """Args:
        batches_root (str): Root directory for log batches.
        num_lines (int): Number of lines to fetch from the syslog.
        """
        super().__init__(batches_root, "syslogs.log")
        self.num_lines = num_lines
        if __debug__:
            logger.debug(
                "SysLogsQuickAction initialized with num_lines=%d", num_lines
            )

    def get_command(self) -> list:
        return [
            "tail",
            f"-n{self.num_lines}",
            "/var/log/syslog",
        ], "cmd"
