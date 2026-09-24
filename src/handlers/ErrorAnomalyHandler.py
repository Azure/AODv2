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
        rules = self.config.track.get("rules")
        if rules:
            commands = events_batch["command"]
            errors = np.bitwise_and(
                events_batch["metric_latency_ns"], np.uint64(0xFFFFFFFF)
            )
            for rule in rules:
                matches = np.ones(len(events_batch), dtype=bool)
                if rule["track_commands"]:
                    matches &= np.isin(commands, tuple(rule["track_commands"]))
                if rule["track_errors"]:
                    matches &= np.isin(errors, tuple(rule["track_errors"]))
                count = int(np.count_nonzero(matches))
                if __debug__:
                    logger.debug(
                        "Error rule %s for %s matched %d events (threshold=%d)",
                        rule["name"],
                        self.config.tool,
                        count,
                        rule["acceptable_count"],
                    )
                if count >= rule["acceptable_count"]:
                    return True
            return False

        count = len(events_batch)
        if __debug__:
            logger.debug(
                "ErrorAnomalyHandler %s: %d events in batch (threshold=%d)",
                self.config.tool,
                count,
                self.acceptable_count,
            )
        return count >= self.acceptable_count
