"""QuotaSweeper: keeps the diagnostics container under its size budget.

Azure Blob has no container quota and emits no "full" event, so enforcement
has to run somewhere. This is that somewhere (decision D2).

Design notes that are easy to get wrong:

* The running total is an approximation. Event Grid is at-least-once and
  unordered, so it drifts - but only upward, and every sweep recomputes from a
  real listing and resets it. Over-counting just causes a harmless early sweep.
* Listing is the source of truth. A counter alone would drift forever.
* Deletion is oldest-first with no protection for recent data: AODv2 v1 has no
  minimum-age guard and no host prioritisation (D3, D10).
"""

import json
import logging
import os

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

app = func.FunctionApp()
logger = logging.getLogger("aod.sweeper")

ACCOUNT = os.environ["AOD_STORAGE_ACCOUNT"]
CONTAINER = os.environ["AOD_CONTAINER"]
ENDPOINT_SUFFIX = os.environ.get("AOD_ENDPOINT_SUFFIX", "core.windows.net")
PREFIX = os.environ.get("AOD_PREFIX", "aodv2/")
QUOTA_BYTES = int(os.environ.get("AOD_QUOTA_BYTES", 50 * 1024 ** 3))
HIGH_WATER = float(os.environ.get("AOD_HIGH_WATER", "0.9"))
LOW_WATER = float(os.environ.get("AOD_LOW_WATER", "0.7"))

COUNTER_BLOB = f"{PREFIX}.sweeper/estimated_bytes.json"

_credential = DefaultAzureCredential()
_service = BlobServiceClient(
    f"https://{ACCOUNT}.blob.{ENDPOINT_SUFFIX}", credential=_credential
)
_container = _service.get_container_client(CONTAINER)


def _read_estimate() -> int:
    try:
        blob = _container.get_blob_client(COUNTER_BLOB)
        return int(json.loads(blob.download_blob().readall())["estimated_bytes"])
    except Exception:
        # No counter yet, or it is unreadable: a full listing will rebuild it.
        return 0


def _write_estimate(value: int) -> None:
    try:
        blob = _container.get_blob_client(COUNTER_BLOB)
        blob.upload_blob(
            json.dumps({"estimated_bytes": max(0, value)}).encode(), overwrite=True
        )
    except Exception as e:
        logger.warning("Could not persist the size estimate: %s", e)


def _list_packages():
    """Every package blob with its size and creation time, newest last.

    The sweeper's own counter blob is excluded so it can never delete it.
    """
    items = []
    for blob in _container.list_blobs(name_starts_with=PREFIX):
        if blob.name == COUNTER_BLOB:
            continue
        items.append((blob.name, blob.size or 0, blob.creation_time))
    items.sort(key=lambda item: (item[2] is None, item[2]))
    return items


def sweep(trigger: str) -> dict:
    """Measure for real, delete oldest-first until under the low-water mark."""
    packages = _list_packages()
    total = sum(size for _, size, _ in packages)
    target = int(QUOTA_BYTES * LOW_WATER)

    deleted, freed = 0, 0
    if total > QUOTA_BYTES * HIGH_WATER:
        for name, size, _ in packages:
            if total <= target:
                break
            try:
                _container.delete_blob(name)
            except Exception as e:
                logger.warning("Could not delete %s: %s", name, e)
                continue
            total -= size
            deleted += 1
            freed += size

            # Manifests are siblings of the package; drop them together so
            # metadata does not outlive the evidence it describes.
            if name.endswith(".tar.zst"):
                sibling = name.replace(".tar.zst", ".manifest.json")
                try:
                    _container.delete_blob(sibling)
                    freed += 0
                except Exception:
                    pass

    _write_estimate(total)
    result = {
        "trigger": trigger,
        "total_bytes": total,
        "quota_bytes": QUOTA_BYTES,
        "utilisation": round(total / QUOTA_BYTES, 3) if QUOTA_BYTES else None,
        "packages": len(packages),
        "deleted": deleted,
        "freed_bytes": freed,
    }
    if deleted:
        # Customers must be able to see evidence being evicted, not discover it later.
        logger.warning("Sweep deleted %d package(s), freeing %.1f MB", deleted, freed / 1048576)
    logger.info("Sweep result: %s", json.dumps(result))
    return result


@app.event_grid_trigger(arg_name="event")
def on_blob_created(event: func.EventGridEvent) -> None:
    """Cheap path: add to the estimate, and only list when it crosses the mark."""
    try:
        size = int((event.get_json() or {}).get("contentLength", 0))
    except (ValueError, TypeError):
        size = 0

    estimate = _read_estimate() + size
    if estimate <= QUOTA_BYTES * HIGH_WATER:
        _write_estimate(estimate)
        return

    logger.info("Estimate %d crossed the high-water mark; sweeping", estimate)
    sweep(trigger="blob_created")


@app.timer_trigger(schedule="0 0 3 * * *", arg_name="timer", run_on_startup=False)
def daily_reconcile(timer: func.TimerRequest) -> None:
    """Backstop for dropped events and for lifecycle deletions the counter never sees."""
    sweep(trigger="daily")
