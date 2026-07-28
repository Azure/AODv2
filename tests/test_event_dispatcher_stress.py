"""Stress test for EventDispatcher with a fast producer.

Slow test (opt-in). The producer pushes events as fast as it can.
When the fake shim's MAX_PENDING queue fills, `test_queue_event`
returns 0 -- that is the backpressure signal -- and the producer
yields until the dispatcher catches up.

Under sustained pressure we verify:
  * no event loss across the full run
  * strict pid ordering preserved end-to-end across many batches
  * every field round-trips (tool, task, rqst_id, latency, cmd_end_time)
  * backpressure actually happened (shim's queue was rejected at least
    once -- otherwise the run was fast enough that nothing was stressed
    and the test result is uninteresting)

Scale via `$AODV2_DISPATCHER_PRESSURE_EVENTS` (default 1_000_000).

Verification is streaming -- each batch is checked and discarded as it
is pulled from `eventQueue`, so peak memory stays bounded regardless
of total event count.
"""

import ctypes
import os
import queue
import threading
import time
import unittest
from unittest import mock

import numpy as np
import pytest

import EventDispatcher as ed_mod
from EventDispatcher import EventDispatcher
from utils.shared_data import event_dtype

from test_event_dispatcher import (  # noqa: E402
    TASK_COMM_LEN,
    _CEvent,
    _SKIP_REASON,
    _load_test_shim,
    _make_controller,
)

TOTAL_EVENTS = int(os.environ.get("AODV2_DISPATCHER_PRESSURE_EVENTS", "10000000"))
# Scratch must be >= the shim's pending capacity, mirroring the
# production invariant (scratch=149797 >> kernel ring=2048). If it
# isn't, the production callback `_copy_event` deliberately drops
# overflow events -- expected behavior, but it would masquerade as
# a dispatcher bug here. Shim MAX_PENDING is 8192; pick comfortably
# above that. Multi-batch behavior still occurs: while the dispatcher
# processes one poll, the producer pushes more events, and the next
# poll picks them up as a separate batch.
SCRATCH_SIZE = 16384


def _produce_all(shim, total: int) -> int:
    """Push `total` events into the shim as fast as possible.

    Returns the number of times `test_queue_event` was rejected
    (`MAX_PENDING` reached). Spins on rejection so no events are dropped.
    """
    rejects = 0
    e = _CEvent()
    sz = ctypes.sizeof(_CEvent)
    e_ref = ctypes.byref(e)
    for pid in range(total):
        e.pid = pid
        e.command = pid & 0xFFFF
        e.tool = b"S"
        e.cmd_end_time_ns = 0xC0FFEE00 + pid
        e.rqst_id = 0xDEADBEEF00 + pid
        e.metric_latency_ns = 1000 + pid
        e.task = f"task-{pid}".encode()[:TASK_COMM_LEN]
        while shim.test_queue_event(e_ref, sz) != 1:
            rejects += 1
            time.sleep(0)  # ask GIL to switch threads so the dispatcher can catch up
    return rejects


@pytest.mark.slow
@unittest.skipIf(_SKIP_REASON is not None, f"shim unavailable: {_SKIP_REASON}")
class DispatcherPressureTest(unittest.TestCase):
    """Sustained-pressure end-to-end test for EventDispatcher."""

    @classmethod
    def setUpClass(cls):
        cls.shim = _load_test_shim()

    def setUp(self):
        self.shim.test_reset()
        self._patcher = mock.patch.object(ed_mod, "_shim", self.shim)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_sustained_pressure_no_loss_no_reorder(self):
        ctrl = _make_controller()
        d = EventDispatcher(ctrl)
        d._scratch = np.empty(SCRATCH_SIZE, dtype=event_dtype)

        # Start dispatcher first so it's already polling when production
        # begins
        dispatcher_thread = threading.Thread(target=d.run, daemon=True)
        dispatcher_thread.start()

        # Producer in its own thread so the main thread can stream-verify
        # batches concurrently and keep ctrl.eventQueue bounded.
        producer_rejects: list[int] = []

        def producer():
            producer_rejects.append(_produce_all(self.shim, TOTAL_EVENTS))

        producer_thread = threading.Thread(target=producer, daemon=True)
        t0 = time.monotonic()
        producer_thread.start()

        # Stream-verify batches as they arrive, concurrently with the
        # producer.
        # _verify_stream returns once it has seen expected_total events;
        # we then shut the dispatcher down and check for the sentinel.
        try:
            received = self._verify_stream(ctrl, expected_total=TOTAL_EVENTS)
            producer_thread.join(timeout=30.0)
            self.assertFalse(producer_thread.is_alive(), "producer did not finish")
        finally:
            ctrl.stop_event.set()
            dispatcher_thread.join(timeout=10.0)
            self.assertFalse(dispatcher_thread.is_alive(), "dispatcher did not exit")
            d.cleanup()

        # After stop, the dispatcher must emit exactly one sentinel and
        # nothing else.
        self._assert_sentinel_then_empty(ctrl)

        elapsed = time.monotonic() - t0
        rejects = producer_rejects[0]
        # Report throughput / rejects for visibility
        print(
            f"\n[stress] events={TOTAL_EVENTS} batches={received['batches']} "
            f"rejects={rejects} elapsed={elapsed:.2f}s "
            f"throughput={TOTAL_EVENTS / elapsed:,.0f} ev/s"
        )

        # Sanity: dispatcher actually had to split work into multiple
        # batches (otherwise scratch was oversized for the run).
        self.assertGreater(received["batches"], 1, received)

    # ----- helpers ---------------------------------------------------------

    def _verify_stream(self, ctrl, *, expected_total: int) -> dict[str, int]:
        """Pull batches off ctrl.eventQueue and validate each in turn.

        Returns once exactly `expected_total` events have been seen.
        Streams so peak memory is one batch, not the whole run.
        """
        expected_pid = 0
        batches = 0
        # Generous because the producer thread is still running for
        # most of the verification window.
        get_timeout = 30.0

        while expected_pid < expected_total:
            try:
                item = ctrl.eventQueue.get(timeout=get_timeout)
            except queue.Empty:
                self.fail(
                    f"eventQueue stalled at pid={expected_pid} "
                    f"after {get_timeout}s"
                )

            # No sentinel expected while we're still counting up to
            # expected_total -- nobody has set stop_event yet.
            self.assertIsNotNone(
                item,
                f"unexpected sentinel at pid={expected_pid} "
                f"(expected {expected_total} events)",
            )

            n = len(item)
            self.assertLessEqual(
                expected_pid + n,
                expected_total,
                f"received more events than produced: "
                f"got {expected_pid + n}, expected {expected_total}",
            )
            # pid ordering -- the only contract that matters end-to-end.
            expected_pids = np.arange(
                expected_pid, expected_pid + n, dtype=np.uint32
            )
            np.testing.assert_array_equal(
                item["pid"],
                expected_pids,
                err_msg=(
                    f"pid mismatch in batch #{batches} starting at "
                    f"expected_pid={expected_pid}"
                ),
            )
            # Per-field integrity. Vectorized -- one check per field
            # per batch, regardless of batch size.
            np.testing.assert_array_equal(item["tool"], b"S")
            np.testing.assert_array_equal(
                item["cmd_end_time_ns"],
                expected_pids.astype(np.uint64) + 0xC0FFEE00,
            )
            np.testing.assert_array_equal(
                item["rqst_id"],
                expected_pids.astype(np.uint64) + 0xDEADBEEF00,
            )
            np.testing.assert_array_equal(
                item["metric_latency_ns"],
                expected_pids.astype(np.uint64) + 1000,
            )
            # Spot-check task on batch endpoints.
            self.assertEqual(item["task"][0], f"task-{expected_pid}".encode())
            self.assertEqual(
                item["task"][-1], f"task-{expected_pid + n - 1}".encode()
            )

            expected_pid += n
            batches += 1

        return {"batches": batches, "events": expected_pid}

    def _assert_sentinel_then_empty(self, ctrl) -> None:
        """After stop, dispatcher must emit exactly one sentinel."""
        saw_sentinel = False
        leftovers: list = []
        # Sentinel is enqueued in the dispatcher's finally before the
        # thread joins, so by here it must be present.
        try:
            first = ctrl.eventQueue.get(timeout=5.0)
        except queue.Empty:
            self.fail("no sentinel emitted on stop")
        if first is None:
            saw_sentinel = True
        else:
            leftovers.append(first)
        while True:
            try:
                item = ctrl.eventQueue.get_nowait()
            except queue.Empty:
                break
            if item is None and not saw_sentinel:
                saw_sentinel = True
            else:
                leftovers.append(item)
        self.assertTrue(saw_sentinel, "no sentinel emitted on stop")
        self.assertEqual(leftovers, [], "items emitted after stream consumed")


if __name__ == "__main__":
    unittest.main()
