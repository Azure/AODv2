"""Main controller module for the AODv2 service.

Responsible for orchestrating startup, configuration, process
supervision, and graceful shutdown of all service components.
"""

import threading
import queue
import subprocess
import os
import signal
from functools import partial
import time
import ctypes
import ctypes.util
import logging

from ConfigManager import ConfigManager
from EventDispatcher import EventDispatcher
from AnomalyWatcher import AnomalyWatcher
from LogCollector import LogCollector
from SpaceWatcher import SpaceWatcher
from utils.anomaly_type import AnomalyType, Protocol
from utils.config_schema import AnomalyKey
from utils.pdeathsig_wrapper import pdeathsig_preexec
from utils.syslogger import setup_logging

logger = logging.getLogger(__name__)


def set_thread_name(name):
    """Set thread name visible in htop when pressing H to show threads."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        # Limit name to 15 characters (Linux kernel limit)
        name = name[:15].encode("utf-8")
        libc.prctl(15, name, 0, 0, 0)  # PR_SET_NAME = 15
    except Exception:
        pass


class Controller:
    """Main controller class for the AODv2 service."""

    def __init__(self, config_path: str):
        """Manages configuration, starts and supervises all service components,
        and coordinates graceful shutdown."""

        if __debug__:
            logger.info("Initializing Controller with config: %s", config_path)
        self.stop_event = threading.Event()
        self.config = ConfigManager(config_path).data
        self.threads = []

        # Metrics tracking
        if __debug__:
            self.thread_restarts = 0
            self.process_restarts = 0
        self.eventQueue = queue.Queue()
        self.anomalyActionQueue = queue.Queue()
        self.tool_processes = {}
        self.tool_cmd_builders = {
            "smbslower": self._get_latency_tool_cmd,
            "nfsslower": self._get_latency_tool_cmd,
        }

        # Initialize all components
        if __debug__:
            logger.info("Initializing service components")
        self.event_dispatcher = EventDispatcher(self)
        self.anomaly_watcher = AnomalyWatcher(self)
        self.log_collector_manager = LogCollector(self)
        self.space_watcher = SpaceWatcher(self)
        if __debug__:
            logger.info("Controller initialization complete")

    def _supervise_thread(
        self,
        thread_name: str,
        target: callable,
        *args,
        fatal_on_exc: bool = False,
        **kwargs,
    ) -> None:
        """Start and supervise a thread, restarting it if it dies
        unexpectedly. If fatal_on_exc is True, an unhandled exception
        instead escalates to a full service shutdown -- use this for
        components that own non-reentrant state (e.g. an asyncio loop with
        attached subprocesses) where restart-in-place would leak resources."""

        def runner():
            set_thread_name(thread_name)  # only to view thread name in top
            while not self.stop_event.is_set():
                try:
                    target(*args, **kwargs)
                except Exception as e:
                    logger.exception(
                        "%s thread died unexpectedly", thread_name, exc_info=True
                    )
                    if fatal_on_exc:
                        logger.error(
                            "%s cannot be restarted in-place; shutting down service",
                            thread_name,
                        )
                        self.stop()
                        return
                    if __debug__:
                        self.thread_restarts += 1
                    time.sleep(1)  # Wait before restarting
                    logger.warning(
                        "AOD component %s restarted due to unexpected exit",
                        thread_name,
                    )

        num_consecutive_failures = 0
        t = threading.Thread(target=runner, name=thread_name, daemon=True)
        t.start()
        if __debug__:
            logger.info("Started thread %s with ID %d", thread_name, t.ident)
        self.threads.append(t)

    def _supervise_process(self, process_name: str, cmd_builder: callable) -> None:
        """Supervise a process, restarting it if it exits unexpectedly."""
        set_thread_name("ProcessSupervisor")  # only to view thread name in top
        while not self.stop_event.is_set():
            cmd = cmd_builder()
            process = subprocess.Popen(
                cmd, start_new_session=True, preexec_fn=pdeathsig_preexec
            )
            self.tool_processes[process_name] = process
            if __debug__:
                logger.info(
                    "Started %s process with PID %d", process_name, process.pid
                )
            while True:
                if self.stop_event.wait(timeout=1):
                    break
                if process.poll() is not None:
                    logger.warning(
                        "AOD component %s exited unexpectedly with code %d, restarting...",
                        process_name,
                        process.returncode,
                    )
                    if __debug__:
                        self.process_restarts += 1
                    break
            if self.stop_event.is_set():
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGINT)
                    process.wait(timeout=5)
                    if __debug__:
                        logger.info("%s process stopped gracefully", process_name)
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "%s process did not stop in time; sending SIGKILL",
                        process_name,
                    )
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                except ProcessLookupError:
                    pass
                break
            time.sleep(1)

    def _get_latency_tool_cmd(self, tool_name: str) -> list[str]:
        """Get command array for a latency eBPF tool based on its anomaly config."""
        anomaly_cfg = None
        for cfg in self.config.anomalies.values():
            if cfg.tool == tool_name:
                anomaly_cfg = cfg
                break

        min_threshold = min(list(anomaly_cfg.track.values()))
        track_cmds = ",".join(str(cmd_id) for cmd_id in anomaly_cfg.track.keys())

        ebpf_binary_path = os.path.join(os.path.dirname(__file__), "bin", tool_name)
        return [ebpf_binary_path, "-m", str(min_threshold), "-c", track_cmds]

    def trigger_snapshot(
        self, anomaly_type: AnomalyType = AnomalyType.SNAPSHOT
    ) -> None:
        """Enqueue a full-system dump request. Safe to call from a signal
        handler. No-op once shutdown is in progress so we don't race with
        the sentinel pushed by stop()."""
        if self.stop_event.is_set() and anomaly_type != AnomalyType.SHUTDOWN:
            return
        self.anomalyActionQueue.put(
            {
                "anomaly_key": AnomalyKey(Protocol.AOD, anomaly_type),
                "timestamp": int(time.time() * 1e9),
            }
        )

    def stop(self) -> None:
        """Signal all threads and processes to stop."""
        # Enqueue a shutdown dump BEFORE the sentinel so LogCollector picks
        # it up before draining. Guard against double-stop (SIGTERM during
        # SIGINT, etc.) so we don't emit two shutdown dumps.
        if not self.stop_event.is_set():
            self.trigger_snapshot(AnomalyType.SHUTDOWN)
        self.stop_event.set()
        # Wake AnomalyWatcher. It will propagate its own sentinel to
        # anomalyActionQueue, but we also push one here as a backup in case
        # AnomalyWatcher died before reaching its post-loop sentinel push;
        # otherwise LogCollector would block forever in queue.get().
        self.eventQueue.put(None)
        self.anomalyActionQueue.put(None)

    def _shutdown(self) -> None:
        """Shutdown all threads and components gracefully."""
        for thread in self.threads:
            thread.join(timeout=5)
            if __debug__:
                if thread.is_alive():
                    logger.warning(
                        "Thread %s with ID %d did not exit within timeout",
                        thread.name,
                        thread.ident,
                    )
                else:
                    logger.info(
                        "Thread %s with ID %d has been shut down",
                        thread.name,
                        thread.ident,
                    )

        if hasattr(self, "event_dispatcher"):
            self.event_dispatcher.cleanup()
        # if hasattr(self, "space_watcher"):
        #     self.space_watcher.cleanup_by_size()

    def _extract_tools(self) -> set[str]:
        """Extract the set of ebpf tools to run from the config."""
        tool_names = set()
        for anomaly in self.config.anomalies.values():
            tool_names.add(anomaly.tool)
        return tool_names

    def run(self) -> None:
        """Start all supervisor threads and wait for shutdown."""
        if __debug__:
            logger.info("Starting AOD service")
        set_thread_name("Controller")  # only to view thread name in top
        tool_names = self._extract_tools()
        if __debug__:
            logger.info("Starting tools: %s", tool_names)
        for tool_name in tool_names:
            cmd_builder = self.tool_cmd_builders.get(tool_name)
            if cmd_builder:
                t = threading.Thread(
                    target=self._supervise_process,
                    args=(tool_name, lambda tn=tool_name: cmd_builder(tn)),
                    name=f"{tool_name}_Supervisor",
                    daemon=True,
                )
                t.start()
                self.threads.append(t)
            else:
                logger.warning("No command builder defined for tool '%s'", tool_name)

        self._supervise_thread("EventDispatcher", self.event_dispatcher.run)
        self._supervise_thread("AnomalyWatcher", self.anomaly_watcher.run)
        # LogCollector owns an asyncio loop with attached subprocess handles
        # (LongCapture). A fresh loop on restart would orphan those handles
        # and leak their underlying processes, so escalate to a full shutdown
        # and let the service supervisor restart us cleanly.
        self._supervise_thread(
            "LogCollector", self.log_collector_manager.run, fatal_on_exc=True
        )
        self._supervise_thread("SpaceWatcher", self.space_watcher.run)
        logger.info("AODv2 service started successfully")
        self.stop_event.wait()
        self._shutdown()


def handle_signal(controller, signum, frame):
    """Handle termination signals to gracefully shut down the controller."""
    if __debug__:
        logger.info("Received signal %d, shutting down...", signum)
    controller.stop()


def handle_snapshot_signal(controller, signum, frame):
    """Handle SIGUSR1 by enqueuing a full-system snapshot."""
    if __debug__:
        logger.info("Received signal %d, triggering snapshot...", signum)
    controller.trigger_snapshot()


def main():
    """Main entry point for the AODv2 controller daemon."""
    log_level = os.getenv("AOD_LOG_LEVEL", "INFO").upper()
    syslog_level = os.getenv("AOD_SYSLOG_LEVEL", "WARNING").upper()
    stderr = os.getenv("AOD_LOG_STDERR", "0") == "1"
    setup_logging(
        getattr(logging, log_level, logging.INFO),
        stderr=stderr,
        syslog_level=getattr(logging, syslog_level, logging.WARNING),
    )

    # Check if script is running as root
    if os.geteuid() != 0:
        raise RuntimeError("Controller daemon must be run as root.")

    # add arguments later

    config_path = os.environ.get(
        "AOD_CONFIG",
        os.path.join(os.path.dirname(__file__), "../config/config.yaml"),
    )
    controller = Controller(config_path)
    signal.signal(signal.SIGTERM, partial(handle_signal, controller))
    signal.signal(signal.SIGINT, partial(handle_signal, controller))
    signal.signal(signal.SIGUSR1, partial(handle_snapshot_signal, controller))
    controller.run()


if __name__ == "__main__":
    # Performance optimized: Verbose logger.info calls wrapped in if __debug__
    # Use python -O for production to remove all debug overhead

    try:
        main()
    except Exception as e:
        logger.exception("Fatal error in main():", exc_info=True)
