"""Error Anomaly Handler to track any given error(s) for any given command(s)."""

import logging
import numpy as np
from base.AnomalyHandlerBase import AnomalyHandler

logger = logging.getLogger(__name__)


class ErrorAnomalyHandler(AnomalyHandler):
    """Fires when the number of kernel-filtered error events in a single
    AnomalyWatcher batch meets `acceptable_count`."""

    def __init__(self, error_config):
        super().__init__(error_config)
        self.acceptable_count = self.config.acceptable_count
        logger.debug(
            "ErrorAnomalyHandler initialized for %s/%s tool=%s "
            "acceptable_count=%d track=%s",
            self.config.key.protocol.value,
            self.config.key.anomaly_type.value,
            self.config.tool,
            self.acceptable_count,
            {axis: len(ids) for axis, ids in self.config.track.items()},
        )

    def detect(self, events_batch: np.ndarray) -> bool:
        count = len(events_batch)
        if __debug__:
            logger.debug(
                "ErrorAnomalyHandler %s: %d events in batch (threshold=%d)",
                self.config.tool,
                count,
                self.acceptable_count,
            )
        return count >= self.acceptable_count
