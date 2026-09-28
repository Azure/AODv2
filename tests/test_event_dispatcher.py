"""Integration tests for EventDispatcher + the C ringbuf shim.

These tests build a parallel `libringbuf_shim_test.so` from the
production ringbuf_shim.c source linked against libbpf stubs
(see tests/c/). That gives end-to-end coverage of:

  Python EventDispatcher.run  →  ctypes
                              →  C rb_poll_into
                              →  C _copy_event (production code)
                              →  numpy scratch buffer
                              →  controller.eventQueue

Scenarios covered (deliberately small):
  * struct event layout matches numpy event_dtype (catches dtype drift)
  * _open_ringbuf success / timeout / stop-event paths
  * Bursty end-to-end stress: many events across multiple polls land
    on eventQueue in order, sentinel emitted on stop
  * ring_buffer__poll error breaks the loop and still emits the sentinel
  * cleanup() releases ctx and is idempotent / re-openable
"""

import ctypes
import os
import queue
import shutil
import subprocess
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

import EventDispatcher as ed_mod
from EventDispatcher import EventDispatcher
from utils.shared_data import RB_MAX_RECORDS, event_dtype

# --- Test shim build --------------------------------------------------------


_THIS_DIR = Path(__file__).resolve().parent
_C_DIR = _THIS_DIR / "c"
_SO_PATH = _C_DIR / "libringbuf_shim_test.so"


def _build_test_shim() -> str | None:
    """Build libringbuf_shim_test.so. Returns a skip reason or None."""
    if shutil.which("make") is None:
        return "make not available"
    if shutil.which(os.environ.get("CC", "cc")) is None:
        return "C compiler not available"
    result = subprocess.run(
        ["make", "-s"], cwd=_C_DIR, capture_output=True, text=True
    )
    if result.returncode != 0:
        return f"build failed: {result.stderr.strip()}"
    if not _SO_PATH.is_file():
        return f"build succeeded but {_SO_PATH} is missing"
    return None


_SKIP_REASON = _build_test_shim()


# --- ctypes mirror of struct event -----------------------------------------


TASK_COMM_LEN = 16


class _CEvent(ctypes.Structure):
    """Mirror of `struct event` in aod_diag.h. Verified at runtime."""

    _fields_ = [
        ("pid", ctypes.c_uint32),
        ("command", ctypes.c_uint16),
        ("tool", ctypes.c_char),
        ("_pad", ctypes.c_char),
        ("cmd_end_time_ns", ctypes.c_uint64),
        ("rqst_id", ctypes.c_uint64),
        ("metric_latency_ns", ctypes.c_uint64),
        ("task", ctypes.c_char * TASK_COMM_LEN),
    ]


def _load_test_shim() -> ctypes.CDLL:
    shim = ctypes.CDLL(str(_SO_PATH))
    shim.rb_open.argtypes = [ctypes.c_char_p]
    shim.rb_open.restype = ctypes.c_void_p
    shim.rb_poll_into.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    shim.rb_poll_into.restype = ctypes.c_int
    shim.rb_close.argtypes = [ctypes.c_void_p]
    shim.rb_close.restype = None

    shim.test_reset.argtypes = []
    shim.test_reset.restype = None
    for name in (
        "test_set_bpf_obj_get_returns",
        "test_set_ring_buffer_new_fails",
        "test_set_poll_returns",
    ):
        getattr(shim, name).argtypes = [ctypes.c_int]
        getattr(shim, name).restype = None
    shim.test_queue_event.argtypes = [ctypes.POINTER(_CEvent), ctypes.c_size_t]
    shim.test_queue_event.restype = ctypes.c_int
    shim.test_pending_count.argtypes = []
    shim.test_pending_count.restype = ctypes.c_int
    shim.test_sizeof_event.argtypes = []
    shim.test_sizeof_event.restype = ctypes.c_size_t
    return shim


# --- Builders ---------------------------------------------------------------


def _make_controller() -> SimpleNamespace:
    return SimpleNamespace(
        eventQueue=queue.Queue(),
        stop_event=threading.Event(),
    )


def _queue_event(shim, *, pid: int, command: int = 0) -> None:
    e = _CEvent()
    e.pid = pid
    e.command = command
    e.tool = b"S"
    e.cmd_end_time_ns = 0xC0FFEE00 + pid
    e.rqst_id = 0xDEADBEEF00 + pid
    e.metric_latency_ns = 1000 + pid
    e.task = f"task-{pid}".encode()[:TASK_COMM_LEN]
    ok = shim.test_queue_event(ctypes.byref(e), ctypes.sizeof(_CEvent))
    if ok != 1:
        raise RuntimeError("test queue full")


# --- Test base --------------------------------------------------------------


@unittest.skipIf(_SKIP_REASON is not None, f"shim unavailable: {_SKIP_REASON}")
class _ShimTestCase(unittest.TestCase):
    """Loads the test shim once and patches `ed_mod._shim` for each test."""

    @classmethod
    def setUpClass(cls):
        cls.shim = _load_test_shim()

    def setUp(self):
        self.shim.test_reset()
        self._patcher = mock.patch.object(ed_mod, "_shim", self.shim)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)


# --- Layout sanity ----------------------------------------------------------


class LayoutTests(_ShimTestCase):
    def test_struct_event_matches_numpy_dtype(self):
        """If C `struct event` and numpy `event_dtype` drift, every
        batch the shim produces is garbage."""
        self.assertEqual(ctypes.sizeof(_CEvent), event_dtype.itemsize)
        self.assertEqual(ctypes.sizeof(_CEvent), self.shim.test_sizeof_event())


# --- Init / open / cleanup --------------------------------------------------


class LifecycleTests(_ShimTestCase):
    def test_init_state(self):
        d = EventDispatcher(_make_controller())
        self.assertEqual(d._scratch.shape, (RB_MAX_RECORDS,))
        self.assertEqual(d._scratch.dtype, event_dtype)
        self.assertIsNone(d._ctx)

    def test_open_ringbuf_success(self):
        d = EventDispatcher(_make_controller())
        d._open_ringbuf(timeout_sec=1)
        self.assertTrue(d._ctx)
        d.cleanup()

    def test_open_ringbuf_raises_timeout_when_bpf_obj_get_fails(self):
        self.shim.test_set_bpf_obj_get_returns(-1)
        d = EventDispatcher(_make_controller())
        with self.assertRaises(TimeoutError):
            d._open_ringbuf(timeout_sec=0)

    def test_open_ringbuf_respects_stop_event(self):
        """A persistent open failure must not block shutdown."""
        self.shim.test_set_bpf_obj_get_returns(-1)
        ctrl = _make_controller()
        ctrl.stop_event.set()
        d = EventDispatcher(ctrl)
        d._open_ringbuf(timeout_sec=10)  # would otherwise loop until deadline
        self.assertIsNone(d._ctx)

    def test_cleanup_releases_and_is_reopenable(self):
        d = EventDispatcher(_make_controller())
        d._open_ringbuf(timeout_sec=1)
        d.cleanup()
        self.assertIsNone(d._ctx)
        # Calling cleanup with no ctx must be a no-op, not a crash.
        d.cleanup()
        # Reopen after cleanup -- the Controller supervisor relies on this.
        d._open_ringbuf(timeout_sec=1)
        self.assertTrue(d._ctx)
        d.cleanup()


# --- End-to-end bursty stress ----------------------------------------------


class EndToEndStressTests(_ShimTestCase):
    """Drives EventDispatcher.run() through the real C shim across many
    polls, with events injected in bursts from the main thread."""

    def test_bursts_arrive_in_order_through_dispatcher(self):
        ctrl = _make_controller()
        d = EventDispatcher(ctrl)
        # Shrink scratch so we can easily observe multiple batches without
        # tripping the production shim's capacity-overflow (event-eaten)
        # path: each burst stays comfortably below this.
        d._scratch = np.empty(256, dtype=event_dtype)

        num_bursts = 1000
        burst_size = 250
        total = num_bursts * burst_size

        t = threading.Thread(target=d.run, daemon=True)
        t.start()
        try:
            for b in range(num_bursts):
                for i in range(burst_size):
                    _queue_event(self.shim, pid=b * burst_size + i)
                # Give the dispatcher a window to drain this burst before
                # we queue the next. The fake poll idles ~1ms when empty.
                deadline = time.monotonic() + 1.0
                while (
                    self.shim.test_pending_count() > 0
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.005)
                self.assertEqual(
                    self.shim.test_pending_count(),
                    0,
                    f"burst {b} not drained in time",
                )

            ctrl.stop_event.set()
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "dispatcher did not exit")
        finally:
            ctrl.stop_event.set()
            d.cleanup()

        # Drain everything the dispatcher emitted, including the sentinel.
        batches: list[np.ndarray] = []
        saw_sentinel = False
        while True:
            try:
                item = ctrl.eventQueue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                saw_sentinel = True
                continue
            batches.append(item)

        self.assertTrue(saw_sentinel, "no sentinel emitted on stop")
        # All events present, in order, across however many batches.
        all_pids = [int(p) for batch in batches for p in batch["pid"]]
        self.assertEqual(all_pids, list(range(total)))
        # Spot-check non-pid fields to catch a partial-memcpy regression
        # in _copy_event
        flat = np.concatenate(batches)
        self.assertTrue(np.all(flat["tool"] == b"S"))
        self.assertTrue(
            np.array_equal(
                flat["rqst_id"],
                np.arange(total, dtype=np.uint64) + 0xDEADBEEF00,
            )
        )
        self.assertTrue(
            np.array_equal(
                flat["metric_latency_ns"],
                np.arange(total, dtype=np.uint64) + 1000,
            )
        )
        self.assertEqual(flat["task"][0], b"task-0")
        self.assertEqual(flat["task"][-1], f"task-{total - 1}".encode())
        # And the work was actually split into multiple polls/batches
        # (otherwise the per-burst drain wait above would be meaningless).
        self.assertGreater(len(batches), 1, batches)

    def test_poll_error_breaks_loop_and_emits_sentinel(self):
        ctrl = _make_controller()
        d = EventDispatcher(ctrl)
        # No events queued; shim's poll will return this error code.
        self.shim.test_set_poll_returns(-5)  # -EIO
        with self.assertLogs(ed_mod.logger, level="ERROR") as cm:
            d.run()
        self.assertIsNone(ctrl.eventQueue.get_nowait())  # sentinel
        self.assertTrue(ctrl.eventQueue.empty())
        self.assertTrue(
            any("ring_buffer__poll error" in m for m in cm.output), cm.output
        )
        d.cleanup()


if __name__ == "__main__":
    unittest.main()
