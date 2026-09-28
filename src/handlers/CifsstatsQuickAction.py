import logging
from base.QuickAction import QuickAction

logger = logging.getLogger(__name__)


class CifsstatsQuickAction(QuickAction):
    def __init__(self, batches_root: str):
        """Args:
        batches_root (str): Root directory for log batches.
        """
        super().__init__(batches_root, "cifsstats.log")
        if __debug__:
            logger.debug("CifsstatsQuickAction initialized")

    def get_command(self) -> tuple[list[str], str]:
        return [
            "cat",
            "/proc/fs/cifs/Stats",
        ], "cat"
