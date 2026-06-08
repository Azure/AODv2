"""Anomaly Watcher Module Monitors events and triggers anomaly detection
handlers."""

import logging
import queue
import time
import syslog
import numpy as np

from shared_data import MAX_WAIT
from utils.anomaly_type import AnomalyType, ANOMALY_TYPE_TO_TOOL_ID
from handlers.latency_anomaly_handler import LatencyAnomalyHandler
from handlers.error_anomaly_handler import ErrorAnomalyHandler
from base.AnomalyHandlerBase import AnomalyHandler

logger = logging.getLogger(__name__)

# Maps enum to anomaly handler classes.
ANOMALY_HANDLER_REGISTRY = {
    AnomalyType.LATENCY: LatencyAnomalyHandler,
    AnomalyType.ERROR: ErrorAnomalyHandler,
    # Add more types here as needed
}


class AnomalyWatcher:
    """Registers its own tail in eventQueue.

    It sleeps for an interval (specified in the config), wakes up and
    drains the queue. It computes the masks to separate events for each
    anomaly type and conducts anomaly analysis. Queues the anomaly
    action type to the anomalyActionQueue.
    """

    def __init__(self, controller):
        """Initialize the AnomalyWatcher with the controller instance."""
        self.controller = controller
        self.interval = getattr(
            self.controller.config, "watch_interval_sec", 1
        )  # 1 second default
        self.handlers: dict[AnomalyType, AnomalyHandler] = (
            self._load_anomaly_handlers(controller.config)
        )

        # Initialize metrics tracking attributes
        if __debug__:
            self.total_count = 0
            self.events_by_tool = {}
            self.anomaly_counts = {
                anomaly_type: 0 for anomaly_type in ANOMALY_HANDLER_REGISTRY.keys()
            }

    def _load_anomaly_handlers(self, config) -> dict[AnomalyType, AnomalyHandler]:
        handler_map = {}
        for anomaly_name, anomaly_cfg in config.guardian.anomalies.items():
            try:
                anomaly_type_enum = AnomalyType(anomaly_cfg.type.strip().lower())
            except ValueError:
                print(
                    f"[AnomalyWatcher] Warning: Unknown anomaly type '{anomaly_cfg.type}' for '{anomaly_name}'"
                )
                continue

            handler_class = ANOMALY_HANDLER_REGISTRY.get(anomaly_type_enum)
            if handler_class:
                handler_map[anomaly_type_enum] = handler_class(anomaly_cfg)
            else:
                print(
                    f"[AnomalyWatcher] Warning: No handler registered for anomaly type '{anomaly_cfg.type}'"
                )
        return handler_map

    def run(self) -> None:
        """Loop: poll eventQueue, detect anomalies, and put actions into anomalyActionQueue"""
        if __debug__:
            total_anomalies_detected = 0
            batch_count = 0
            total_latency = 0

        while True:
            batch = self.controller.eventQueue.get(True)
            if batch is None:
                self.controller.eventQueue.task_done()
                self.controller.anomalyActionQueue.put(None)
                break  # Exit loop on sentinel

            end_time = time.time() + MAX_WAIT
            sentinal_found = False
            while time.time() < end_time:
                try:
                    next_batch = self.controller.eventQueue.get_nowait()
                    if next_batch is None:
                        self.controller.eventQueue.task_done()
                        sentinal_found = True
                        break  # Exit inner loop immediately on sentinel
                    batch = np.concatenate((batch, next_batch))
                    self.controller.eventQueue.task_done()
                except queue.Empty:
                    break

            if __debug__:
                self.total_count += len(batch)
                batch_count += 1

                # Calculate average latency for this batch
                if len(batch) > 0 and "latency_ns" in batch.dtype.names:
                    batch_latency = batch["latency_ns"].sum()
                    total_latency += batch_latency

                logger.debug(
                    "Processing batch of %d events, total count: %d",
                    len(batch),
                    self.total_count,
                )

            for anomaly_type, handler in self.handlers.items():
                tool_id = ANOMALY_TYPE_TO_TOOL_ID[anomaly_type]
                masked_batch = batch[batch["tool"] == tool_id]

                if __debug__:
                    # Track events per tool type
                    if tool_id not in self.events_by_tool:
                        self.events_by_tool[tool_id] = 0
                    self.events_by_tool[tool_id] += len(masked_batch)

                if len(masked_batch) > 0 and handler.detect(masked_batch):
                    action = self._generate_action(anomaly_type)
                    syslog.syslog(
                        syslog.LOG_ALERT,
                        f"AOD detected anomaly: {anomaly_type.value} with {len(masked_batch)} events",
                    )
                    self.controller.anomalyActionQueue.put(action)
                    if __debug__:
                        total_anomalies_detected += 1
                        self.anomaly_counts[anomaly_type] += 1
                        logger.info(
                            "Anomaly detected: %s (%d events analyzed)",
                            anomaly_type.value,
                            len(masked_batch),
                        )

            self.controller.eventQueue.task_done()
            if sentinal_found:
                self.controller.anomalyActionQueue.put(None)
                break
            time.sleep(self.interval)

        if __debug__:
            avg_latency_ms = (
                (float(total_latency) / float(self.total_count) / 1_000_000)
                if self.total_count > 0
                else 0
            )
            logger.info(
                "AnomalyWatcher stopping. Final metrics: batches=%d, total_events=%d, total_anomalies=%d, avg_latency=%.2fms",
                batch_count,
                self.total_count,
                total_anomalies_detected,
                avg_latency_ms,
            )

    def _generate_action(self, anomaly_type: AnomalyType) -> dict:
        """Generate an action based on the detected anomaly."""
        timestamp_ns = int(time.time() * 1e9)  # nanoseconds since epoch
        return {
            "anomaly": anomaly_type,
            "timestamp": timestamp_ns,
        }
