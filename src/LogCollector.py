import asyncio
import logging
import tarfile
import shutil
import time
import os
import queue
from datetime import datetime, timezone
from pathlib import Path
import zstandard as zstd

from handlers.JournalctlQuickAction import JournalctlQuickAction
from handlers.CifsstatsQuickAction import CifsstatsQuickAction
from handlers.DmesgQuickAction import DmesgQuickAction
from handlers.DebugDataQuickAction import DebugDataQuickAction
from handlers.MountsQuickAction import MountsQuickAction
from handlers.SmbinfoQuickAction import SmbinfoQuickAction
from handlers.SysLogsQuickAction import SysLogsQuickAction
from utils.anomaly_type import AnomalyType
from utils import manifest as manifest_utils
from utils import paths
from utils.host_id import get_host_id

logger = logging.getLogger(__name__)

class LogCollector:
    
    def __init__(self, controller):
        self.loop = asyncio.new_event_loop()
        self.max_concurrent_tasks = 4
        self.controller = controller
        self.anomaly_interval = getattr(self.controller.config, "watch_interval_sec", 1)  # 1 second default
        self.aod_output_dir = str(paths.batches_dir(controller.config))
        try:
            paths.ensure_dir(Path(self.aod_output_dir))
        except OSError as e:
            # Collectors create it on first write; startup must not fail here.
            logger.warning("Could not pre-create %s: %s", self.aod_output_dir, e)
        self.host_id = getattr(controller, "host_id", None) or get_host_id(
            paths.output_dir(controller.config)
        )
        self.anomaly_tools = {
            cfg.type.strip().lower(): cfg.tool
            for cfg in controller.config.guardian.anomalies.values()
        }
        
        # Metrics tracking
        if __debug__:
            self.tasks_processed = 0
            self.tasks_failed = 0
            logger.info("LogCollector initialized, output dir: %s", self.aod_output_dir)
        self.action_factory = {
            "journalctl": lambda: JournalctlQuickAction(self.aod_output_dir, self.anomaly_interval),
            "stats": lambda: CifsstatsQuickAction(self.aod_output_dir),
            "debugdata": lambda: DebugDataQuickAction(self.aod_output_dir),
            "dmesg": lambda: DmesgQuickAction(self.aod_output_dir, self.anomaly_interval),
            "mounts": lambda: MountsQuickAction(self.aod_output_dir),
            "smbinfo": lambda: SmbinfoQuickAction(self.aod_output_dir),
            "syslogs": lambda: SysLogsQuickAction(self.aod_output_dir, num_lines=100),
        }
        self.handlers = self.get_anomaly_events(controller.config)

    def get_anomaly_events(self, config) -> dict:
        """
        Build a mapping from anomaly type to a list of action instances,
        using the 'actions' field from each anomaly config in the loaded config.
        """
        anomaly_events = {}
        for anomaly_name, anomaly_cfg in config.guardian.anomalies.items():
            actions = []
            for action_name in getattr(anomaly_cfg, "actions", []):
                factory = self.action_factory.get(action_name)
                if factory is not None:
                    actions.append(factory())
                else:
                    logger.warning("No factory for action '%s' in anomaly '%s'", action_name, anomaly_name)
            try:
                anomaly_type_enum = AnomalyType(anomaly_cfg.type.strip().lower())
                anomaly_events[anomaly_type_enum] = actions
            except ValueError:
                logger.warning("Unknown anomaly type '%s' for '%s'", anomaly_cfg.type, anomaly_name)
        return anomaly_events

    async def _create_log_collection_task(self, anomaly_event) -> None:
        """Collect logs, write the manifest, and publish the package atomically.

        The package only becomes visible under its final `.tar.zst` name once it
        is complete, so a scanner can never read a truncated archive.
        """
        if __debug__:
            logger.info("Collecting logs for anomaly event %s", anomaly_event)
        anomaly_type = anomaly_event["anomaly"]
        batch_id = f"{anomaly_type.value}_{anomaly_event['timestamp']}"

        handlers = self.handlers[anomaly_type]
        if not handlers:  # Check if empty
            if __debug__:
                logger.warning("No handlers configured for anomaly type %s, skipping collection", anomaly_type)
            return

        started_at = datetime.now(timezone.utc)
        results = await asyncio.gather(
            *[handler.execute(batch_id) for handler in handlers]
        )
        ended_at = datetime.now(timezone.utc)

        staging_dir = handlers[0].get_output_dir(batch_id)
        os.makedirs(staging_dir, exist_ok=True)

        package_manifest = manifest_utils.build_manifest(
            host_id=self.host_id,
            anomaly_type=anomaly_type.value,
            anomaly_tool=self.anomaly_tools.get(anomaly_type.value, "unknown"),
            collectors=list(results),
            started_at=started_at.isoformat().replace("+00:00", "Z"),
            ended_at=ended_at.isoformat().replace("+00:00", "Z"),
        )
        manifest_utils.write_manifest(
            package_manifest, os.path.join(staging_dir, manifest_utils.MANIFEST_FILENAME)
        )

        final_path = f"{staging_dir}{paths.PACKAGE_EXTENSION}"
        partial_path = f"{final_path}{paths.PARTIAL_EXTENSION}"

        # Compress the logs using tar + zstd (faster than gzip)
        with open(partial_path, 'wb') as f:
            cctx = zstd.ZstdCompressor(level=3)  # Level 3 for good speed/compression balance
            with cctx.stream_writer(f) as writer:
                with tarfile.open(fileobj=writer, mode='w|') as tar:
                    tar.add(staging_dir, arcname=os.path.basename(staging_dir))

        # Sibling manifest lands before the package so it is never seen alone.
        manifest_utils.write_manifest(
            manifest_utils.finalize_manifest(package_manifest, partial_path),
            f"{staging_dir}{manifest_utils.MANIFEST_EXTENSION}",
        )
        os.replace(partial_path, final_path)

        shutil.rmtree(staging_dir, ignore_errors=True)
        self._enqueue_for_upload(os.path.basename(final_path))

    def _enqueue_for_upload(self, package_id: str) -> None:
        """Fast path only; the Uploader's directory scan is what guarantees delivery."""
        upload_queue = getattr(self.controller, "uploadQueue", None)
        if upload_queue is None:
            return
        try:
            upload_queue.put_nowait(package_id)
        except queue.Full:
            if __debug__:
                logger.debug("uploadQueue full, leaving %s for the scan", package_id)

    async def _create_log_collection_task_with_limit(self, anomaly_event, semaphore: asyncio.Semaphore) -> None:
        # use the with ... statement so that we do not have to manually release the semaphore
        async with semaphore:
            try:
                await self._create_log_collection_task(anomaly_event)
                if __debug__:
                    self.tasks_processed += 1
            except Exception as e:
                logger.error("Error %s while collecting logs for anomaly action %s", e, anomaly_event)
                if __debug__:
                    self.tasks_failed += 1
            finally:
                # send a task done signal to the queue
                await asyncio.to_thread(self.controller.anomalyActionQueue.task_done)
                
                # Log metrics every 10 tasks
                if __debug__ and (self.tasks_processed + self.tasks_failed) % 10 == 0:
                    success_rate = (self.tasks_processed / (self.tasks_processed + self.tasks_failed) * 100) if (self.tasks_processed + self.tasks_failed) > 0 else 0
                    logger.debug("LogCollector metrics: processed=%d, failed=%d, success_rate=%.1f%%", 
                               self.tasks_processed, self.tasks_failed, success_rate)

    async def _run(self):
        currently_running_tasks = set()
        semaphore = asyncio.Semaphore(self.max_concurrent_tasks)

        while True:
            try:
                anomaly_event = await asyncio.to_thread(self.controller.anomalyActionQueue.get) # we can afford to block here since we send a poison pill when the script stops
                if anomaly_event is None:  # Sentinel to stop the loop
                    self.controller.anomalyActionQueue.task_done()
                    # send sentinal to LogCompressor queue when integrated
                    break
                task = asyncio.create_task(self._create_log_collection_task_with_limit(anomaly_event, semaphore))
                currently_running_tasks.add(task)
                # remove task from the set when done
                task.add_done_callback(currently_running_tasks.discard)
            except Exception as e:
                logger.error("Error while processing anomaly event: %s", e)
            
        if currently_running_tasks:
            await asyncio.gather(*currently_running_tasks) # wait for all tasks to finish

    def run(self):
        # run forever
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._run()) # Runner.run() is meant for the main thread, so we use run_until_complete()
        self.loop.close()