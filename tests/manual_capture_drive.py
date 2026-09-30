"""Manually drive LogCollector + captures with synthetic anomaly events.

Run:
    sudo -E PYTHONPATH=src .venv/bin/python tests/manual_capture_drive.py

Pushes synthetic events on the same queue the real eBPF pipeline uses,
so the capture-snapshot path (and quick actions, if their tools are present)
fires exactly as it would in production.
"""

import logging
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ConfigManager import ConfigManager
from LogCollector import LogCollector
from utils.anomaly_type import AnomalyType, Protocol
from utils.config_schema import AnomalyKey


class StubController:
    """Just enough of Controller for LogCollector to function."""

    def __init__(self, config_path):
        self.config = ConfigManager(config_path).data
        self.anomalyActionQueue = queue.Queue()
        self.stop_event = threading.Event()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    ctrl = StubController(os.path.join("config", "config.yaml"))
    lc = LogCollector(ctrl)

    t = threading.Thread(target=lc.run, name="LogCollector", daemon=True)
    t.start()

    # Let captures spawn.
    time.sleep(2.0)

    # Fire two synthetic SMB latency anomalies, then one NFS.
    events = [
        (Protocol.SMB, AnomalyType.LATENCY),
        (Protocol.SMB, AnomalyType.LATENCY),
        (Protocol.NFS, AnomalyType.LATENCY),
    ]
    for i, (proto, atype) in enumerate(events):
        ctrl.anomalyActionQueue.put(
            {
                "anomaly_key": AnomalyKey(proto, atype),
                "timestamp": str(int(time.time()) + i),
            }
        )
        time.sleep(8.0)  # real trace-cmd flush can take several seconds

    # Let any in-flight snapshot finish before signalling stop.
    time.sleep(2.0)

    # Shut down.
    ctrl.stop_event.set()
    ctrl.anomalyActionQueue.put(None)  # sentinel
    t.join(timeout=60)
    print("done; bundles:")
    bundles = os.path.join(ctrl.config.aod_output_dir, "batches")
    for f in sorted(os.listdir(bundles)):
        print(" ", f)


if __name__ == "__main__":
    main()
