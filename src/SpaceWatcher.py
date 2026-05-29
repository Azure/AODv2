"""Space Watcher is responsible for monitoring disk space usage in the AOD output directory."""

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)
SIZE_DELETE_THRESHOLD = 0.85
MAX_HOT_THRESHOLD = 0.97


class SpaceWatcher:
    """Wake up every 10 minutes and check the size of the output dir.
    If it grows over a certain threshold, clean up older logs to bring the usage down to a safe threshold.
    Every N days, clean up log bundles older than N days.

    Only counts completed .tar.zst files to prevent race conditions with LogCollector.
    """

    def __init__(self, controller):
        """Initialize the SpaceWatcher."""
        self.controller = controller
        cleanup_config = controller.config.cleanup
        self.max_log_age_days = cleanup_config.get(
            "max_log_age_days", 2
        )  # Default to 2 days if not set
        self.max_total_log_size_mb = cleanup_config.get(
            "max_total_log_size_mb", 500
        )  # Default to 500 MB if not set
        self.cleanup_interval = cleanup_config.get(
            "cleanup_interval_sec", 600
        )  # Default to 600 sec if not set
        self.aod_output_dir = cleanup_config.get(
            "aod_output_dir", "/var/log/aod"
        )  # Default to /var/log/aod if not set
        self.batches_dir = Path(self.aod_output_dir) / "batches"
        self.last_full_cleanup = (
            time.time() - self.max_log_age_days * 24 * 60 * 60
        )  # Initialize to ensure first cleanup runs immediately

        # Compression configuration - matches LogCollector's compression method
        self.compression_extension = (
            ".tar.zst"  # Could be made configurable via config file
        )

        # Metrics tracking
        if __debug__:
            self.cleanup_runs = 0
            self.total_files_deleted = 0
            self.total_space_freed_mb = 0
            logger.info(
                "SpaceWatcher initialized: max_size=%.2fMB, max_age=%d days, cleanup_interval=%ds",
                self.max_total_log_size_mb,
                self.max_log_age_days,
                self.cleanup_interval,
            )

    def run(self) -> None:
        """Periodically checks disk space and triggers cleanup if needed."""
        if __debug__:
            logger.info("SpaceWatcher started running")
        while not self.controller.stop_event.is_set():
            try:
                entries_data = self._get_compressed_file_stat(
                    self.batches_dir, self.compression_extension
                )
                if self._check_space(entries_data):
                    self.cleanup_by_size(entries_data)
                if self._full_cleanup_needed():
                    self.cleanup_by_age(entries_data)
            except Exception as e:
                logger.error("SpaceWatcher cleanup failed: %s", e)
                if __debug__:
                    logger.debug("Full traceback:", exc_info=True)
            time.sleep(self.cleanup_interval)

    def _get_compressed_file_stat(
        self, directory: Path, extension: str
    ) -> list[tuple[Path, int, float]]:
        """Return the total size of compressed files in the directory."""
        entries = list(directory.glob(f"aod_*{extension}"))
        entries_data = []
        for e in entries:
            if e.is_file():
                s = e.stat()
                sz = s.st_size
                mt = s.st_mtime
                entries_data.append((e, sz, mt))
        entries_data.sort(key=lambda x: x[2])  # Sort by modification time
        return entries_data

    def _full_cleanup_needed(self) -> bool:
        """Check if current time  > last_full_cleanup + max_log_age_days."""
        current_time = time.time()
        if (
            current_time - self.last_full_cleanup
            > self.max_log_age_days * 24 * 60 * 60
        ):  # Convert days to seconds
            self.last_full_cleanup = current_time
            return True
        return False

    def _check_space(self, entries_data: list[tuple[Path, int, float]]) -> bool:
        """Check if disk space is below a threshold using pathlib, only counting compressed files."""
        try:
            total_bytes = sum(sz for _, sz, _ in entries_data)
            limit_bytes = self.max_total_log_size_mb * 1024 * 1024

            # Normal warning mode: disk approaching full (97% of limit)
            if total_bytes > MAX_HOT_THRESHOLD * limit_bytes:
                logger.warning(
                    "Total log size %.2f MB exceeds %.0f%% of max %.2f MB",
                    total_bytes / (1024 * 1024),
                    MAX_HOT_THRESHOLD * 100,
                    self.max_total_log_size_mb,
                )
                return True

            return False

        except (FileNotFoundError, PermissionError, OSError) as e:
            logger.warning("Error checking space: %s", e)
            return False

    def cleanup_by_age(self, entries_data: list[tuple[Path, int, float]]) -> None:
        """Delete batch directories or files older than max_log_age_days days"""
        if not entries_data:
            if __debug__:
                logger.warning("No eligible AOD entries to cleanup by age")
            return

        cutoff = time.time() - self.max_log_age_days * 24 * 60 * 60
        to_delete = [(entry, sz) for entry, sz, mt in entries_data if mt < cutoff]

        if len(to_delete) == 0:
            if __debug__:
                logger.debug("No AOD batch entries to cleanup by age")
            return

        if __debug__:
            deleted_count = 0
            space_freed_bytes = 0

        for entry, sz in to_delete:
            try:
                entry.unlink()
                if __debug__:
                    deleted_count += 1
                    space_freed_bytes += sz
                    logger.debug(
                        "Deleted old batch entry %s (%.1f KB)",
                        entry,
                        sz / 1024,
                    )
            except (FileNotFoundError, PermissionError, OSError) as e:
                logger.warning("Failed to delete %s: %s", entry, e)

        if __debug__:
            self.cleanup_runs += 1
            self.total_files_deleted += deleted_count
            self.total_space_freed_mb += space_freed_bytes / (1024 * 1024)
            logger.info(
                "Age-based cleanup complete. Deleted %d batch entries (%.1f MB freed).",
                deleted_count,
                space_freed_bytes / (1024 * 1024),
            )

    def cleanup_by_size(self, entries_data: list[tuple[Path, int, float]]) -> None:
        """Delete oldest files or directories starting with aod_ until total size is under max_total_log_size_mb."""
        if not entries_data:
            if __debug__:
                logger.warning("No eligible AOD entries to cleanup by size")
            return
        try:
            total_size = sum(sz for _, sz, _ in entries_data)
            target_threshold = (
                self.max_total_log_size_mb * 1024 * 1024 * SIZE_DELETE_THRESHOLD
            )

            if __debug__:
                logger.info(
                    "Total size of AOD entries: %.2f MB, max allowed: %.2f MB. Pruning to %.2f MB if needed.",
                    total_size / (1024 * 1024),
                    self.max_total_log_size_mb,
                    target_threshold / (1024 * 1024),
                )

            if __debug__:
                deleted_count = 0
                space_freed_bytes = 0

            if total_size <= target_threshold:
                return

            for entry, size, _ in entries_data:
                if total_size <= target_threshold:
                    break
                try:
                    entry.unlink()
                    total_size -= size
                    if __debug__:
                        deleted_count += 1
                        space_freed_bytes += size
                        logger.debug(
                            "Deleted batch entry %s (%.1f KB) to free space",
                            entry,
                            size / 1024,
                        )
                except (FileNotFoundError, PermissionError, OSError) as e:
                    logger.warning("Failed to delete %s: %s", entry, e)

            if __debug__:
                self.cleanup_runs += 1
                self.total_files_deleted += deleted_count
                self.total_space_freed_mb += space_freed_bytes / (1024 * 1024)

                # Log comprehensive metrics every 5 cleanup runs
                if self.cleanup_runs % 5 == 0:
                    logger.debug(
                        "SpaceWatcher metrics: runs=%d, files_deleted=%d, space_freed=%.1fMB",
                        self.cleanup_runs,
                        self.total_files_deleted,
                        self.total_space_freed_mb,
                    )

                logger.info(
                    "Size-based cleanup complete. Deleted %d entries (%.1f MB freed). Total size now: %.2f MB",
                    deleted_count,
                    space_freed_bytes / (1024 * 1024),
                    total_size / (1024 * 1024),
                )
        except Exception as e:
            logger.error("Size-based cleanup failed completely: %s", e)
            if __debug__:
                logger.debug("Full traceback:", exc_info=True)
