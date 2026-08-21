import asyncio
import logging
import tarfile
import shutil
import os
import zstandard as zstd

from handlers.JournalctlQuickAction import JournalctlQuickAction
from handlers.CifsstatsQuickAction import CifsstatsQuickAction
from handlers.DmesgQuickAction import DmesgQuickAction
from handlers.DebugDataQuickAction import DebugDataQuickAction
from handlers.MountsQuickAction import MountsQuickAction
from handlers.SmbinfoQuickAction import SmbinfoQuickAction
from handlers.SysLogsQuickAction import SysLogsQuickAction
from handlers.TcpdumpCapture import TcpdumpCapture
from handlers.TraceCmdCapture import TraceCmdCapture
from base.LongCapture import LongCapture
from utils.anomaly_type import Protocol

logger = logging.getLogger(__name__)

# Maps capture tool name in config -> LongCapture class.
_CAPTURE_CLASSES = {
    "tcpdump": TcpdumpCapture,
    "trace-cmd": TraceCmdCapture,
}

LONG_CAPTURE_RESTART_DELAY_SEC = 1  # Time to wait after stopping a capture before restarting it.

class LogCollector:

    def __init__(self, controller):
        self.max_concurrent_tasks = 4
        self.controller = controller
        self.anomaly_interval = getattr(
            self.controller.config, "watch_interval_sec", 60
        )  # 60 second default
        root_output_dir = getattr(
            self.controller.config, "aod_output_dir", "/var/log/aod"
        )
        self.aod_output_dir = os.path.join(root_output_dir, "batches")

        # Metrics tracking
        if __debug__:
            self.tasks_processed = 0
            self.tasks_failed = 0
            logger.info(
                "LogCollector initialized, output dir: %s", self.aod_output_dir
            )
        self.action_factory = {
            "journalctl": lambda: JournalctlQuickAction(
                self.aod_output_dir, self.anomaly_interval
            ),
            "stats": lambda: CifsstatsQuickAction(self.aod_output_dir),
            "debugdata": lambda: DebugDataQuickAction(self.aod_output_dir),
            "dmesg": lambda: DmesgQuickAction(
                self.aod_output_dir, self.anomaly_interval
            ),
            "mounts": lambda: MountsQuickAction(self.aod_output_dir),
            "smbinfo": lambda: SmbinfoQuickAction(self.aod_output_dir),
            "syslogs": lambda: SysLogsQuickAction(
                self.aod_output_dir, num_lines=100
            ),
        }
        self.handlers = self.get_anomaly_events(controller.config)

        # One LongCapture per protocol. Each owns its own process lifecycle.
        self.captures: dict[Protocol, LongCapture] = self._build_captures(
            controller.config,
            capture_root=os.path.join(root_output_dir, "captures"),
            bundle_dir=self.aod_output_dir,
            restart_delay=LONG_CAPTURE_RESTART_DELAY_SEC,
        )
        self._capture_tasks: list[asyncio.Task] = []

    def get_anomaly_events(self, config) -> dict:
        """
        Build a mapping from AnomalyKey (protocol, anomaly_type) to a list of
        action instances, using the 'quick_actions' field from each anomaly
        config. Keyed by the full AnomalyKey so anomalies that share an
        AnomalyType across different protocols (e.g. smb.latency and
        nfs.latency).
        """
        anomaly_events = {}
        for anomaly_key, anomaly_cfg in config.anomalies.items():
            actions = []
            for action_name in getattr(anomaly_cfg, "quick_actions", []):
                factory = self.action_factory.get(action_name)
                if factory is not None:
                    actions.append(factory())
                else:
                    logger.warning(
                        "No factory for action '%s' in anomaly '%s'",
                        action_name,
                        anomaly_key,
                    )
            anomaly_events[anomaly_key] = actions
        return anomaly_events

    def _build_captures(
        self, config, capture_root: str, bundle_dir: str, restart_delay: float
    ) -> dict:
        """One LongCapture per protocol. ConfigManager has already enforced
        that a capture tool is bound to a single protocol with consistent
        args, so we just take the first occurrence per protocol."""
        captures: dict = {}
        for key, cfg in config.anomalies.items():
            for tool_name, args in cfg.captures.items():
                if key.protocol in captures:
                    continue  # already built for this protocol
                cls = _CAPTURE_CLASSES.get(tool_name)
                if cls is None:
                    logger.warning("Unknown capture tool '%s'; skipping", tool_name)
                    continue
                proto_dir = os.path.join(capture_root, key.protocol.value)
                captures[key.protocol] = cls(
                    key.protocol, args, proto_dir, bundle_dir, restart_delay
                )
                if __debug__:
                    logger.info(
                        "Configured %s capture for protocol %s",
                        tool_name,
                        key.protocol.value,
                    )
        return captures

    @staticmethod
    def _bundle_quick_actions(output_path: str) -> str:
        tar_path = f"{output_path}.tar.zst"
        cctx = zstd.ZstdCompressor(level=3)
        try:
            with (
                open(tar_path, "wb") as f,
                cctx.stream_writer(f) as writer,
                tarfile.open(fileobj=writer, mode="w|") as tar,
            ):
                tar.add(output_path, arcname=os.path.basename(output_path))
        finally:
            # Always drop the staging dir so a bundling failure doesn't leave
            # aod_quick_<batch_id>/ behind for SpaceWatcher to clean up later.
            shutil.rmtree(output_path, ignore_errors=True)
        return tar_path

    async def _create_log_collection_task(self, anomaly_event) -> None:
        """Collect quick-action logs and request a capture snapshot in
        parallel, producing one or two sibling tarballs."""
        if __debug__:
            logger.info("Collecting logs for anomaly event %s", anomaly_event)
        anomaly_key = anomaly_event["anomaly_key"]
        anomaly_name_str = (
            f"{anomaly_key.protocol.value}_{anomaly_key.anomaly_type.value}"
        )
        # Include protocol + anomaly type in batch_id so quick-action output
        # dirs and tarballs (aod_quick_<batch_id>...) can't collide across
        # different anomalies that fire at the same ns timestamp.
        batch_id = f"{anomaly_event['timestamp']}_{anomaly_name_str}"

        handlers = self.handlers.get(anomaly_key, [])
        protocol = anomaly_key.protocol
        has_capture = protocol in self.captures

        if not handlers and not has_capture:
            logger.warning(
                "No handlers or captures configured for %s, skipping",
                anomaly_key,
            )
            return

        # Fire-and-forget snapshot request; the capture supervisor handles
        # stop/bundle/restart in the background and logs the resulting
        # tarball path itself.
        capture = self.captures.get(protocol)
        if capture is not None:
            capture.snapshot(batch_id)

        quick_tar_path = None
        if handlers:
            await asyncio.gather(
                *[handler.execute(batch_id) for handler in handlers]
            )
            output_path = handlers[0].get_output_dir(batch_id)
            quick_tar_path = await asyncio.to_thread(
                self._bundle_quick_actions, output_path
            )

        logger.info(
            "Completed log collection for anomaly event %s, quick=%s",
            anomaly_event,
            quick_tar_path,
        )

    async def _create_log_collection_task_with_limit(
        self, anomaly_event, semaphore: asyncio.Semaphore
    ) -> None:
        # use the with ... statement so that we do not have to manually release the semaphore
        async with semaphore:
            try:
                await self._create_log_collection_task(anomaly_event)
                if __debug__:
                    self.tasks_processed += 1
            except Exception as e:
                logger.error(
                    "Error %s while collecting logs for anomaly action %s",
                    e,
                    anomaly_event,
                )
                if __debug__:
                    self.tasks_failed += 1
            finally:
                # send a task done signal to the queue
                self.controller.anomalyActionQueue.task_done()

                # Log metrics every 10 tasks
                if (
                    __debug__
                    and (self.tasks_processed + self.tasks_failed) % 10 == 0
                ):
                    success_rate = (
                        (
                            self.tasks_processed
                            / (self.tasks_processed + self.tasks_failed)
                            * 100
                        )
                        if (self.tasks_processed + self.tasks_failed) > 0
                        else 0
                    )
                    logger.debug(
                        "LogCollector metrics: processed=%d, failed=%d, success_rate=%.1f%%",
                        self.tasks_processed,
                        self.tasks_failed,
                        success_rate,
                    )

    async def _run(self):
        currently_running_tasks = set()
        semaphore = asyncio.Semaphore(self.max_concurrent_tasks)

        # Start one supervisor task per configured capture.
        for protocol, capture in self.captures.items():
            t = asyncio.create_task(
                capture.run(self.controller.stop_event),
                name=f"capture_{protocol.value}_{capture.tool_name}",
            )
            self._capture_tasks.append(t)

        while True:
            try:
                anomaly_event = await asyncio.to_thread(
                    self.controller.anomalyActionQueue.get
                )  # we can afford to block here since we send a poison pill when the script stops
                if anomaly_event is None:  # Sentinel to stop the loop
                    self.controller.anomalyActionQueue.task_done()
                    # send sentinal to LogCompressor queue when integrated
                    break
                task = asyncio.create_task(
                    self._create_log_collection_task_with_limit(
                        anomaly_event, semaphore
                    )
                )
                currently_running_tasks.add(task)
                # remove task from the set when done
                task.add_done_callback(currently_running_tasks.discard)
            except Exception as e:
                logger.error("Error while processing anomaly event: %s", e)

        if currently_running_tasks:
            await asyncio.gather(
                *currently_running_tasks
            )  # wait for all tasks to finish

        # Stop capture supervisors. Their finally-blocks SIGINT/SIGKILL the
        # underlying process. controller.stop_event is already set by this point.
        await self._shutdown_captures()

    async def _shutdown_captures(self) -> None:
        """Wait for capture supervisors to exit on stop_event so any
        in-flight snapshot can finish stopping/bundling the recorder. Only
        cancel as a last resort. Safe to call multiple times."""
        if not self._capture_tasks:
            return
        # Make sure supervisors observe the stop signal even if a caller
        # forgot to set it (e.g. crash path in run()).
        self.controller.stop_event.set()
        pending = [t for t in self._capture_tasks if not t.done()]
        if pending:
            # Give supervisors enough time for the slowest stop_grace_sec
            # (trace-cmd: 20s) plus bundling headroom.
            done, still_pending = await asyncio.wait(pending, timeout=60)
            for t in still_pending:
                logger.warning(
                    "Capture supervisor %s did not exit in grace period; cancelling",
                    t.get_name(),
                )
                t.cancel()
        await asyncio.gather(*self._capture_tasks, return_exceptions=True)

    def run(self):
        # run forever
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        finally:
            # If _run() raised, captures are still pending in this loop and
            # would otherwise be killed via pdeathsig (raw SIGTERM) when the
            # process exits, skipping the graceful stop path. Drive the loop
            # one more time to cancel + gather them so each LongCapture's
            # finally-block can run _stop().
            try:
                loop.run_until_complete(self._shutdown_captures())
            except Exception:
                logger.exception("Capture shutdown failed")
            finally:
                loop.close()
