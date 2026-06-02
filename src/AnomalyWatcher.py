"""Anomaly Watcher Module Monitors events and triggers anomaly detection
handlers."""

import logging
import queue
import time
import numpy as np

from utils.anomaly_type import AnomalyType, TOOL_NAME_TO_ID
from handlers.latency_anomaly_handler import LatencyAnomalyHandler
from handlers.error_anomaly_handler import ErrorAnomalyHandler

logger = logging.getLogger(__name__)

# Maps enum to anomaly handler classes.
ANOMALY_HANDLER_REGISTRY = {
    AnomalyType.LATENCY: LatencyAnomalyHandler,
    AnomalyType.ERROR: ErrorAnomalyHandler,
    # Add more types here as needed
}

total_count = 0
events_by_tool = {}


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
            self.controller.config, "watch_interval_sec", 60
        )  # 60 seconds default
        # name -> (handler, cfg).
        self.handlers: dict = self._load_anomaly_handlers(controller.config)

    def _load_anomaly_handlers(self, config) -> dict:
        handler_map = {}
        for anomaly_name, anomaly_cfg in config.anomalies.items():
            try:
                anomaly_type_enum = AnomalyType(anomaly_cfg.type.strip().lower())
            except ValueError:
                logger.warning(
                    "Unknown anomaly type '%s' for '%s'",
                    anomaly_cfg.type,
                    anomaly_name,
                )
                continue

            handler_class = ANOMALY_HANDLER_REGISTRY.get(anomaly_type_enum)
            if handler_class:
                handler_map[anomaly_name] = (handler_class(anomaly_cfg), anomaly_cfg)
            else:
                logger.warning(
                    "No handler registered for anomaly type '%s'",
                    anomaly_cfg.type,
                )
        return handler_map

    def run(self) -> None:
        """Loop: poll eventQueue, detect anomalies, and put actions into anomalyActionQueue"""
        if __debug__:
            total_anomalies_detected = 0
            batch_count = 0
            total_latency = 0
            latency_event_count = 0
            global total_count
            global events_by_tool

        while not self.controller.stop_event.is_set():
            batch = self.controller.eventQueue.get(True)
            if batch is None:
                self.controller.eventQueue.task_done()
                break  # sentinel

            batches = [batch]
            sentinel_found = False
            try:
                while True:
                    nxt = self.controller.eventQueue.get_nowait()
                    self.controller.eventQueue.task_done()
                    if nxt is None:
                        sentinel_found = True
                        break
                    batches.append(nxt)
            except queue.Empty:
                pass

            batch = batches[0] if len(batches) == 1 else np.concatenate(batches)

            if __debug__:
                total_count += len(batch)
                batch_count += 1
                if len(batch) > 0 and "latency_ns" in batch.dtype.names:
                    total_latency += int(batch["latency_ns"].sum())
                    latency_event_count += len(batch)
                logger.debug(
                    "Processing batch of %d events, total count: %d",
                    len(batch),
                    total_count,
                )

            for anomaly_name, (handler, anomaly_cfg) in self.handlers.items():
                tool_id = TOOL_NAME_TO_ID[anomaly_cfg.tool]
                masked_batch = batch[batch["tool"] == bytes([tool_id])]

                if __debug__:
                    events_by_tool[tool_id] = events_by_tool.get(tool_id, 0) + len(
                        masked_batch
                    )

                if len(masked_batch) == 0:
                    continue

                try:
                    detected = handler.detect(masked_batch)
                except Exception:
                    logger.exception(
                        "Handler for '%s' raised; skipping this batch",
                        anomaly_name,
                    )
                    continue

                if detected:
                    action = self._generate_action(anomaly_name, anomaly_cfg)
                    logger.critical(
                        "AOD detected anomaly: %s with %d events, at UTC time %s",
                        anomaly_name,
                        len(masked_batch),
                        time.strftime(
                            "%Y-%m-%d %H:%M:%S",
                            time.gmtime(action["timestamp"] / 1e9),
                        ),
                    )
                    self.controller.anomalyActionQueue.put(action)
                    if __debug__:
                        total_anomalies_detected += 1
                        logger.info(
                            "Anomaly detected: %s (%d events analyzed)",
                            anomaly_name,
                            len(masked_batch),
                        )

            self.controller.eventQueue.task_done()
            if sentinel_found:
                break
            # Sleep on stop_event so shutdown isn't blocked for `interval` seconds.
            if self.controller.stop_event.wait(self.interval):
                break

        self.controller.anomalyActionQueue.put(None)

        if __debug__:
            avg_latency_ms = (
                (total_latency / latency_event_count / 1_000_000)
                if latency_event_count > 0
                else 0
            )
            logger.info(
                "AnomalyWatcher stopping. Final metrics: batches=%d, total_events=%d, "
                "total_anomalies=%d, avg_latency=%.2fms",
                batch_count,
                total_count,
                total_anomalies_detected,
                avg_latency_ms,
            )

    def _generate_action(self, anomaly_name: str, anomaly_cfg) -> dict:
        """Generate an action based on the detected anomaly."""
        timestamp_ns = int(time.time() * 1e9)  # nanoseconds since epoch
        anomaly_type = AnomalyType(anomaly_cfg.type.strip().lower())
        return {
            "anomaly": anomaly_type,
            "anomaly_name": anomaly_name,
            "timestamp": timestamp_ns,
        }
