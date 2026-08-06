"""Handles reading events from shared memory, batching them, and dispatching to
the controller's event queue."""

# import ctypes
import logging
import os
import mmap
import time
import struct
import numpy as np

from shared_data import (
    SHM_NAME,
    SHM_SIZE,
    SHM_DATA_SIZE,
    HEAD_TAIL_BYTES,
    event_dtype,
    MAX_WAIT,
)

logger = logging.getLogger(__name__)

# Ensure that the size of Event and event_dtype is same
# assert ctypes.sizeof(Event) == event_dtype.itemsize, (
#     f"Size mismatch: ctypes Event is {ctypes.sizeof(Event)} bytes, "
#     f"numpy event_dtype is {event_dtype.itemsize} bytes"
# )


class EventDispatcher:
    """Polls C ring buffer and drains all events.

    Parses the raw c struct to Python numpy struct array and sends it to
    eventQueue.
    """

    def __init__(self, controller):
        """Initialize the EventDispatcher."""
        self.controller = controller
        self.head_tail_fmt = "<Q" if HEAD_TAIL_BYTES == 8 else "<I"
        if __debug__:
            logger.info("EventDispatcher initialized, shared memory: %s", SHM_NAME)
        self.shm_fd, self.shm_map = self._setup_shared_memory()

    def _setup_shared_memory(self) -> tuple[int, mmap.mmap]:
        """Open, create, size, and memory-map the shared memory segment.

        Returns:
            Tuple[int, mmap.mmap]: The file descriptor and mmap object.
        """
        shm_created = False
        shm_file_path = f"/dev/shm{SHM_NAME}"
        try:
            shm_fd = os.open(shm_file_path, os.O_RDWR)
            if __debug__:
                logger.info("Opened existing shared memory: %s", shm_file_path)
        except FileNotFoundError:
            # This is expected on first startup - create new shared memory
            if __debug__:
                logger.info(
                    "Shared memory not found, creating new: %s", shm_file_path
                )
            shm_fd = os.open(shm_file_path, os.O_RDWR | os.O_CREAT, 0o666)
            shm_created = True
        except Exception as e:
            logger.error("Failed to open shared memory: %s", e)
            raise

        if shm_created:
            try:
                os.ftruncate(shm_fd, SHM_SIZE)
            except Exception as e:
                logger.error("Failed to set size of shared memory: %s", e)
                os.close(shm_fd)
                raise

        try:
            shm_map = mmap.mmap(
                shm_fd,
                SHM_SIZE,
                flags=mmap.MAP_SHARED,
                prot=mmap.PROT_READ | mmap.PROT_WRITE,
            )
        except Exception as e:
            logger.error("Failed to map shared memory: %s", e)
            os.close(shm_fd)
            raise

        return shm_fd, shm_map

    def _get_buffer_size(self) -> int:
        """Tells how much data is available in the shared memory buffer."""
        self.shm_map.seek(0)
        head = struct.unpack_from(self.head_tail_fmt, self.shm_map, 0)[0]
        tail = struct.unpack_from(self.head_tail_fmt, self.shm_map, HEAD_TAIL_BYTES)[
            0
        ]
        if tail == head:
            return 0
        if tail < head:
            return head - tail
        # Wrap-around case
        return (SHM_DATA_SIZE - tail) + head

    def run(self) -> None:
        """Loop: poll eventQueue, detect anomalies, and put actions into anomalyActionQueue"""
        if __debug__:
            logger.info("EventDispatcher started running")
        timer = 3
        if __debug__:
            total_events_processed = 0
            batch_count = 0
            total_latency = 0

        while not self.controller.stop_event.is_set():
            no_of_events = self._get_buffer_size() // event_dtype.itemsize
            if no_of_events >= 10 or timer == 0:
                timer = 3  # reset timer
                if no_of_events == 0:
                    continue

                if __debug__:
                    logger.debug(
                        "Processing %d events from shared memory", no_of_events
                    )

                time.sleep(MAX_WAIT)
                raw_events = self._poll_shm_buffer()
                parsed_events = self._parse(raw_events)
                self.controller.eventQueue.put(parsed_events)

                # Metrics tracking
                if __debug__:
                    batch_count += 1
                    batch_size = len(parsed_events)
                    total_events_processed += batch_size

                    # Calculate average latency for this batch
                    if batch_size > 0 and "latency_ns" in parsed_events.dtype.names:
                        batch_latency = parsed_events["latency_ns"].sum()
                        total_latency += batch_latency

                    if batch_count % 10 == 0:  # Log metrics every 10 batches
                        avg_events_per_batch = total_events_processed / batch_count
                        avg_latency_ms = (
                            (total_latency / total_events_processed / 1_000_000)
                            if total_events_processed > 0
                            else 0
                        )
                        logger.debug(
                            "EventDispatcher metrics: batches=%d, total_events=%d, avg_per_batch=%.1f, avg_latency=%.2fms",
                            batch_count,
                            total_events_processed,
                            avg_events_per_batch,
                            avg_latency_ms,
                        )
            else:
                time.sleep(1)
                timer -= 1

        if __debug__:
            avg_latency_ms = (
                (total_latency / total_events_processed / 1_000_000)
                if total_events_processed > 0
                else 0
            )
            logger.info(
                "EventDispatcher stopping. Final metrics: batches=%d, total_events=%d, avg_latency=%.2fms",
                batch_count,
                total_events_processed,
                avg_latency_ms,
            )
        self.controller.eventQueue.put(None)  # send sentinal to the queue

    def _poll_shm_buffer(self) -> bytes:
        """Fetch a batch of raw events from shared memory."""

        self.shm_map.seek(0)
        head = struct.unpack_from(self.head_tail_fmt, self.shm_map, 0)[0]
        tail = struct.unpack_from(self.head_tail_fmt, self.shm_map, HEAD_TAIL_BYTES)[
            0
        ]

        if tail == head:
            # no events to read
            return b""

        if tail < head:
            available_bytes = head - tail
            self.shm_map.seek(2 * HEAD_TAIL_BYTES + tail)
            raw = self.shm_map.read(available_bytes)
            tail = (tail + available_bytes) % SHM_DATA_SIZE
            self._update_tail(tail)
            return raw

        # Wrap-around case
        available_bytes = (SHM_DATA_SIZE - tail) + head
        bytes_to_end = SHM_DATA_SIZE - tail
        self.shm_map.seek(2 * HEAD_TAIL_BYTES + tail)
        raw1 = self.shm_map.read(bytes_to_end)
        self.shm_map.seek(2 * HEAD_TAIL_BYTES)
        raw2 = self.shm_map.read(head)
        tail = (tail + available_bytes) % SHM_DATA_SIZE
        self._update_tail(tail)
        return raw1 + raw2

    def _update_tail(self, tail) -> None:
        self.shm_map.seek(HEAD_TAIL_BYTES)
        self.shm_map.write(struct.pack(self.head_tail_fmt, tail))
        self.shm_map.flush()

    def _parse(self, raw: bytes) -> np.ndarray | None:
        """Convert raw struct bytes to a numpy array of events (batch)."""
        if not raw:
            return None
        return np.frombuffer(raw, dtype=event_dtype)

    def cleanup(self) -> None:
        """Clean up resources used by the EventDispatcher."""

        # Shared memory cleanup
        try:
            # Read head and tail before closing mmap
            self.shm_map.seek(0)
            head = struct.unpack_from(self.head_tail_fmt, self.shm_map, 0)[0]
            tail = struct.unpack_from(self.head_tail_fmt, self.shm_map, 8)[0]
            if head != tail:
                logger.warning(
                    "Head and tail are not equal, indicating potential data loss (head=%d, tail=%d)",
                    head,
                    tail,
                )

            self.shm_map.close()
            os.close(self.shm_fd)
            os.unlink(f"/dev/shm{SHM_NAME}")
            if __debug__:
                logger.info("Shared memory cleaned up")
        except OSError as e:
            logger.error("Error cleaning up shared memory: %s", e)
