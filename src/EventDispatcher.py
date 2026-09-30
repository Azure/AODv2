"""Consumer of events from the pinned BPF ringbuf at /sys/fs/bpf/aodrb.
Drains the ringbuffer as fast as possible and forwards event batches to eventQueue.
"""

import ctypes
import errno
import logging
import os

import numpy as np

from utils.shared_data import event_dtype, RB_MAX_RECORDS, RINGBUF_PINNED

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load the ring buffer shim shared library (libringbuf_shim.so)
# ---------------------------------------------------------------------------
_SHIM_PATH = os.path.join(os.path.dirname(__file__), "bin", "libringbuf_shim.so")
_shim = ctypes.CDLL(_SHIM_PATH)

_shim.rb_open.argtypes = [ctypes.c_char_p]
_shim.rb_open.restype = ctypes.c_void_p

_shim.rb_poll_into.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
]
_shim.rb_poll_into.restype = ctypes.c_int

_shim.rb_close.argtypes = [ctypes.c_void_p]
_shim.rb_close.restype = None


class EventDispatcher:
    """Polls BPF ring buffer via C shim and drains all events.
    Uses libringbuf_shim.so to bulk-read events from the kernel ring buffer
    directly into a numpy array.
    """

    POLL_TIMEOUT_MS = 1000  # only affects shutdown responsiveness

    def __init__(self, controller):
        """Initialize the EventDispatcher."""
        self.controller = controller
        self._ctx = _shim.rb_open(RINGBUF_PINNED)
        if not self._ctx:
            raise RuntimeError(
                f"Failed to open pinned ring buffer at {RINGBUF_PINNED.decode()}"
            )
        # Scratch buffer reused across polls. Sized to the kernel ringbuf
        self._scratch = np.empty(RB_MAX_RECORDS, dtype=event_dtype)
        if __debug__:
            logger.info(
                "EventDispatcher initialized, ring buffer: %s, scratch capacity: %d",
                RINGBUF_PINNED.decode(),
                RB_MAX_RECORDS,
            )

    def run(self) -> None:
        """Poll the BPF ring buffer, drain events, and forward to eventQueue."""
        total_events_processed = 0
        batch_count = 0
        if __debug__:
            logger.info("EventDispatcher started running")

        scratch = self._scratch
        capacity = scratch.shape[0]
        scratch_ptr = scratch.ctypes.data

        try:
            while not self.controller.stop_event.is_set():
                count = _shim.rb_poll_into(
                    self._ctx, scratch_ptr, capacity, self.POLL_TIMEOUT_MS
                )

                if count < 0:
                    if count == -errno.EINTR:
                        continue
                    logger.error("ring_buffer__poll error: %d", count)
                    break

                if count == 0:
                    continue

                # Copy out only the live slice; the scratch buffer is reused on the
                # next poll, so we must not hand a view of it to the queue.
                self.controller.eventQueue.put(scratch[:count].copy())

                if __debug__:
                    batch_count += 1
                    total_events_processed += count
                    if batch_count % 10 == 0:
                        logger.debug(
                            "EventDispatcher metrics: batches=%d, total_events=%d, "
                            "avg_per_batch=%.1f",
                            batch_count,
                            total_events_processed,
                            total_events_processed / batch_count,
                        )

            if __debug__:
                logger.info(
                    "EventDispatcher stopping. Final metrics: batches=%d, total_events=%d",
                    batch_count,
                    total_events_processed,
                )
        finally:
            # Sentinel for downstream consumers. cleanup() is owned by Controller
            # so the supervisor can restart run() without losing the ring buffer ctx.
            self.controller.eventQueue.put(None)

    def cleanup(self) -> None:
        """Release the ring buffer context."""
        if self._ctx:
            _shim.rb_close(self._ctx)
            self._ctx = None
            if __debug__:
                logger.info("Ring buffer context closed")
