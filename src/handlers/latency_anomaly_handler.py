"""Latency Anomaly Handler This handler detects latency anomalies based on
predefined thresholds for SMB commands."""

import logging
import numpy as np
from base.AnomalyHandlerBase import AnomalyHandler
from shared_data import ALL_SMB_CMDS

logger = logging.getLogger(__name__)


# works only if ebpf code does filtering as per config file (i.e. ignore excluded cmds)
class LatencyAnomalyHandler(AnomalyHandler):
    """Checks if a batch of events has any latency anomalies based on the
    thresholds defined in the config."""

    def __init__(self, latency_config):
        super().__init__(latency_config)
        self.acceptable_count = self.config.acceptable_count
        # bcos im iterating over an array of size 20, using for loop wont affect the performance
        self.threshold_lookup = np.full(len(ALL_SMB_CMDS) + 1, 0, dtype=np.uint64)
        for smb_cmd_id, threshold in self.config.track.items():
            self.threshold_lookup[smb_cmd_id] = threshold * 1000000
        logger.debug(
            "LatencyAnomalyHandler initialized with %d thresholds",
            len(self.config.track),
        )

    # works only if ebpf code does filtering as per config file (i.e. ignore excluded cmds)
    def detect(self, events_batch: np.ndarray) -> bool:
        """Returns true if we detect many cmds crossing thresholds or a single
        cmd crossing 1 second."""
        anomaly_count = np.sum(
            (
                events_batch["metric_latency_ns"]
                >= self.threshold_lookup[events_batch["smbcommand"]]
            )
        )
        max_latency = np.max(events_batch["metric_latency_ns"])

        if __debug__:
            logger.debug(
                "Detected %d latency anomalies for %s, max_latency=%.2fms",
                anomaly_count,
                self.config.tool,
                max_latency / 1e6,
            )
        return (
            anomaly_count >= self.acceptable_count or max_latency >= 1e9
        )  # 1 second
