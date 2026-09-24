"""Uploads completed diagnostic packages to the customer's storage account.

Driven by two inputs: a queue fed by LogCollector for the fast path, and a
periodic directory scan that recovers anything the queue missed - packages
created while the service was down, while upload was disabled, or while the
queue was full. The state store, not the queue, decides what still needs
sending.
"""

import json
import hashlib
import logging
import os
import queue
import random
import syslog
import time
from datetime import datetime, timezone
from pathlib import Path

import UploadStateStore as upload_state
from base.UploadTransport import (
    AlreadyUploaded,
    AuthError,
    DestinationError,
    PayloadError,
    ThrottledError,
    TransientError,
    TransportError,
)
from utils import manifest as manifest_utils
from utils import paths
from utils.host_id import short_host_id

logger = logging.getLogger(__name__)

STATUS_FILENAME = "status.json"

STATE_IDLE = "IDLE"
STATE_RUNNING = "RUNNING"
STATE_DISABLED = "DISABLED"
BLOCKED_AUTH = "BLOCKED_AUTH"
BLOCKED_DESTINATION = "BLOCKED_DESTINATION"


class Uploader:
    """Supervised component; `run()` is restarted by Controller if it dies."""

    def __init__(self, controller):
        self.controller = controller
        self.config = controller.config.upload
        self.store = controller.upload_store
        self.transport = controller.upload_transport
        self.batches_dir = paths.batches_dir(controller.config)
        self.upload_dir = paths.upload_dir(controller.config)
        self.status_path = self.upload_dir / STATUS_FILENAME
        self.host_segment = short_host_id(controller.host_id)

        self.scan_interval = max(1, self.config.scan_interval_sec)
        self.retry = self.config.retry
        self.backfill_cutoff = None
        self._next_scan = 0.0
        self._circuit_until = 0.0
        self._circuit_reason = None
        self._circuit_detail = None
        self._preflight_done = False

        self.uploaded = 0
        self.failed = 0
        self.bytes_uploaded = 0
        self.last_success_at = None

    # ---------------------------------------------------------------- lifecycle

    def run(self) -> None:
        """Blocking loop. Re-entrant: Controller restarts it after a crash."""
        if not self.config.enabled or self.store is None:
            logger.info("Upload disabled; Uploader idle")
            self.controller.stop_event.wait()
            return

        paths.ensure_dir(self.upload_dir)
        self.store.reset_in_progress()
        # 0 means no cutoff: ship everything still on disk, which the local
        # size cap already bounds.
        backfill_hours = self.config.limits.backfill_max_age_hours
        self.backfill_cutoff = (
            time.time() - backfill_hours * 3600 if backfill_hours > 0 else None
        )

        while not self.controller.stop_event.is_set():
            timeout = max(0.0, self._next_scan - time.monotonic())
            package_id = None
            try:
                package_id = self.controller.uploadQueue.get(timeout=timeout)
            except queue.Empty:
                pass

            if package_id is None:
                # Either the scan timer fired or shutdown put a sentinel in.
                if self.controller.stop_event.is_set():
                    break
            else:
                self._register_package(self.batches_dir / package_id)

            if time.monotonic() >= self._next_scan:
                self._scan()
                self._next_scan = time.monotonic() + self.scan_interval

            self._drain_due()
            self._write_status(STATE_IDLE)

    # ------------------------------------------------------------------- scanning

    def _scan(self) -> None:
        """Reconcile the batches directory against the state store."""
        try:
            packages = [
                p for p in self.batches_dir.glob(f"aod_*{paths.PACKAGE_EXTENSION}")
                if not p.name.endswith(paths.PARTIAL_EXTENSION)
            ]
        except OSError as e:
            logger.warning("Could not scan %s: %s", self.batches_dir, e)
            return

        for package in packages:
            self._register_package(package)

        self._prune_missing({p.name for p in packages})
        self.store.prune(time.time() - self._retention_seconds())

    def _register_package(self, package: Path) -> None:
        try:
            size = package.stat().st_size
            mtime = package.stat().st_mtime
        except OSError:
            return

        if not self.store.register(package.name, str(package), size):
            return  # already known

        if self.backfill_cutoff is not None and mtime < self.backfill_cutoff:
            self.store.mark_skipped(package.name, upload_state.REASON_BACKFILL_WINDOW)
            logger.info("Skipping %s: older than the backfill window", package.name)

    def _prune_missing(self, present: set) -> None:
        """Drop rows whose package is gone, e.g. reclaimed by SpaceWatcher."""
        for package_id, state in self.store.states().items():
            if package_id not in present and state != upload_state.STATE_IN_PROGRESS:
                self.store.delete(package_id)

    def _retention_seconds(self) -> int:
        max_age_days = self.controller.config.cleanup.get("max_log_age_days", 2)
        return int(max_age_days * 2 * 24 * 3600)

    # ------------------------------------------------------------------ uploading

    def _drain_due(self) -> None:
        if self._circuit_blocked():
            return
        if not self._ensure_preflight():
            return

        for row in self.store.due_packages():
            if self.controller.stop_event.is_set() or self._circuit_blocked():
                return
            self._upload_one(row)

    def _upload_one(self, row) -> None:
        package_id = row["package_id"]
        local_path = Path(row["local_path"])

        if not self.store.claim(package_id):
            return
        if not local_path.exists():
            self.store.delete(package_id)
            return

        blob_name = ""
        try:
            blob_name = self._blob_name(local_path)
            metadata = self._blob_metadata(local_path)
            self.transport.upload(local_path, blob_name, metadata)
            self._on_success(package_id, blob_name, row["size_bytes"], local_path)
        except AlreadyUploaded:
            # A previous attempt landed before we recorded it.
            self._on_success(package_id, blob_name, row["size_bytes"], local_path)
        except ThrottledError as e:
            self.store.mark_blocked(package_id, "throttled", time.time() + e.retry_after)
        except (AuthError, DestinationError) as e:
            self._open_circuit(e)
            self.store.mark_blocked(package_id, "blocked", time.time() + self.retry.circuit_probe_interval_sec)
        except PayloadError as e:
            self.failed += 1
            self.store.mark_failed(package_id, str(e)[:200])
            logger.error("Permanently failed to upload %s: %s", package_id, e)
        except TransientError as e:
            self._on_transient(package_id, row["attempts"], e)
        except TransportError as e:
            self._on_transient(package_id, row["attempts"], e)
        except Exception as e:
            # Letting this escape kills the thread; the supervisor restarts it,
            # reset_in_progress requeues the package, and because claim() does
            # not count attempts a poison package would loop for ever. Treat it
            # as transient so the retry limit applies.
            logger.exception("Unexpected error uploading %s", package_id)
            self._on_transient(package_id, row["attempts"], e)

    def _on_success(self, package_id: str, blob_name: str, size_bytes: int, local_path: Path) -> None:
        self.store.mark_uploaded(package_id, blob_name)
        self.uploaded += 1
        self.bytes_uploaded += int(size_bytes or 0)
        self.last_success_at = time.time()
        if __debug__:
            logger.info("Uploaded %s -> %s", package_id, blob_name)

        if self.config.behavior.delete_after_upload:
            try:
                local_path.unlink()
                sibling = local_path.with_name(
                    local_path.name.replace(paths.PACKAGE_EXTENSION, manifest_utils.MANIFEST_EXTENSION)
                )
                sibling.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("Could not remove uploaded package %s: %s", local_path, e)

    def _on_transient(self, package_id: str, attempts: int, error: Exception) -> None:
        attempts = int(attempts or 0)
        if attempts + 1 >= self.retry.max_attempts:
            self.failed += 1
            self.store.mark_failed(package_id, f"max_attempts: {error}"[:200])
            syslog.syslog(
                syslog.LOG_WARNING,
                f"AOD gave up uploading {package_id} after {attempts + 1} attempts",
            )
            return
        self.store.mark_retry(package_id, str(error)[:200], time.time() + self._backoff(attempts))

    def _backoff(self, attempts: int) -> float:
        """Exponential with full jitter, so a fleet does not retry in lockstep."""
        ceiling = min(self.retry.base_backoff_sec * (2 ** attempts), self.retry.max_backoff_sec)
        return random.uniform(0, ceiling)

    # ------------------------------------------------------------------- circuit

    def _circuit_blocked(self) -> bool:
        return self._circuit_reason is not None and time.monotonic() < self._circuit_until

    def _open_circuit(self, error: Exception) -> None:
        reason = BLOCKED_AUTH if isinstance(error, AuthError) else BLOCKED_DESTINATION
        if self._circuit_reason != reason:
            # One alert per transition, not one per package.
            syslog.syslog(syslog.LOG_ERR, f"AOD upload blocked ({reason}): {error}")
            logger.error("Upload circuit opened (%s): %s", reason, error)
        self._circuit_reason = reason
        self._circuit_detail = str(error)[:200]
        self._circuit_until = time.monotonic() + self.retry.circuit_probe_interval_sec
        self._preflight_done = False

    def _ensure_preflight(self) -> bool:
        if self._preflight_done:
            return True
        try:
            self.transport.preflight()
        except (AuthError, DestinationError) as e:
            self._open_circuit(e)
            return False
        except TransportError as e:
            logger.warning("Preflight failed, will retry: %s", e)
            return False

        if self._circuit_reason is not None:
            logger.info("Upload destination reachable again; resuming")
            self._circuit_reason = None
        self._preflight_done = True
        return True

    # -------------------------------------------------------------- blob naming

    def _blob_name(self, package: Path) -> str:
        """`<prefix>/<host>/<date>/<time>Z-<anomaly>-<digest>.tar.zst` (D6).

        The digest is what makes the name unique. Collection timestamps have
        one-second resolution, so two packages from the same second would
        otherwise share a name - and since an existing blob is treated as
        "already uploaded", the second one would be silently discarded.
        Deriving it from content also keeps retries of the same package
        idempotent.
        """
        anomaly, collected_at = self._package_metadata(package)
        date_part = collected_at.strftime("%Y-%m-%d")
        time_part = collected_at.strftime("%H%M%S")
        prefix = self.config.destination.prefix.strip("/")
        digest = self._package_digest(package)
        return (f"{prefix}/{self.host_segment}/{date_part}/"
                f"{time_part}Z-{anomaly}-{digest}{paths.PACKAGE_EXTENSION}")

    def _package_digest(self, package: Path, length: int = 12) -> str:
        """Short content digest, from the manifest when available."""
        sha = (self._read_manifest(package).get("package") or {}).get("sha256")
        if sha:
            return sha[:length]
        # No manifest: hash the file itself rather than risk a colliding name.
        try:
            return manifest_utils.sha256_of(package)[:length]
        except OSError:
            return hashlib.sha256(package.name.encode()).hexdigest()[:length]

    def _read_manifest(self, package: Path) -> dict:
        sibling = package.with_name(
            package.name.replace(paths.PACKAGE_EXTENSION, manifest_utils.MANIFEST_EXTENSION)
        )
        try:
            return json.loads(sibling.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _package_metadata(self, package: Path) -> tuple:
        data = self._read_manifest(package)
        try:
            anomaly = data["anomaly"]["type"]
            started = data["collection"]["started_at"].replace("Z", "+00:00")
            return anomaly, datetime.fromisoformat(started)
        except (KeyError, ValueError, TypeError):
            logger.debug("No usable manifest for %s; naming from mtime", package.name)
            mtime = datetime.fromtimestamp(package.stat().st_mtime, tz=timezone.utc)
            return "unknown", mtime

    def _blob_metadata(self, package: Path) -> dict:
        """Fields a consumer can filter on without downloading the package."""
        data = self._read_manifest(package)
        if not data:
            return {}
        return {
            "schema_version": data.get("schema_version"),
            "aod_version": data.get("aod_version"),
            "anomaly_type": data.get("anomaly", {}).get("type"),
            "kernel": data.get("environment", {}).get("kernel"),
            "sha256": data.get("package", {}).get("sha256"),
        }

    # -------------------------------------------------------------------- status

    def _write_status(self, state: str) -> None:
        """Single file support can ask a customer for when upload misbehaves."""
        status = {
            "state": self._circuit_reason or state,
            "destination": self.transport.describe() if self.transport else None,
            "identity_kind": self.config.identity.kind,
            "enabled": self.config.enabled,
            "last_success_at": self.last_success_at,
            "uploaded": self.uploaded,
            "failed": self.failed,
            "bytes_uploaded": self.bytes_uploaded,
            "counts_by_state": self.store.counts_by_state() if self.store else {},
            "pending_bytes": self.store.pending_bytes() if self.store else 0,
            "written_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if self._circuit_reason:
            status["blocked_reason"] = getattr(self, "_circuit_detail", None)

        try:
            tmp = self.status_path.with_name(self.status_path.name + ".tmp")
            tmp.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.status_path)
        except OSError as e:
            logger.debug("Could not write upload status: %s", e)
