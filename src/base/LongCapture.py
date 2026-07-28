"""Abstract base class for long-running packet/event capture tools.

Each LongCapture owns one continuously-running process bound to a protocol.
The capture handles its own lifecycle: spawn, supervise, snapshot on demand,
 restart after the configured watch_interval_sec and shutdown.

Visible to LogCollector:
    await capture.run(stop_event)          # supervisor coroutine
    capture.snapshot(batch_id)             # fire-and-forget request
"""

import asyncio
import glob
import logging
import os
import signal
import tarfile
from abc import ABC, abstractmethod

import zstandard as zstd

from utils.anomaly_type import Protocol
from utils.pdeathsig_wrapper import pdeathsig_preexec

logger = logging.getLogger(__name__)

# Grace period for SIGINT to flush before we SIGKILL.
_STOP_GRACE_SEC = 5

# Consecutive _spawn failures before the supervisor disables itself.
_MAX_SPAWN_FAILURES = 3


class LongCapture(ABC):
    """Owns the lifecycle of one long-running capture process bound to one
    protocol. Spawns/respawns the process, and on demand stops it, bundles
    its output files into a sibling tarball, and restarts it on the next tick.
    """

    # Subclasses must set these class attributes
    tool_name: str = ""
    output_extension: str = ""
    # Seconds to wait for the recorder to exit after _request_stop() returns,
    # before SIGKILL. Subclasses may override.
    stop_grace_sec: float = _STOP_GRACE_SEC

    def __init__(
        self,
        protocol: Protocol,
        user_args: list[str],
        capture_dir: str,
        bundle_dir: str,
        restart_delay_sec: float = 1.0,
    ):
        self.protocol = protocol
        self.user_args = list(user_args)
        self.capture_dir = capture_dir
        self.bundle_dir = bundle_dir
        self.restart_delay_sec = restart_delay_sec
        self._proc: asyncio.subprocess.Process | None = None
        self._snap_q: asyncio.Queue = asyncio.Queue()
        self._disabled = False
        self._is_cooldown = False
        self._spawn_failures = 0

    @abstractmethod
    def build_argv(self, output_path: str) -> list[str]:
        """Return the full argv to spawn the capture process writing to
        output_path."""

    def snapshot(self, batch_id: str) -> None:
        """Fire-and-forget snapshot request. Non-blocking: the unbounded
        queue accepts the request immediately and the supervisor coroutine
        handles stop/bundle/restart in the background. The resulting tarball
        path is logged by the supervisor.

        batch_id is the same identifier LogCollector uses for the sibling
        quick-action tarball ("<ts>_<proto>_<anomaly_type>").

        Requests are dropped if the capture is disabled (3 consecutive
        spawn failures => no recorder available) or in cooldown (the
        previous snapshot is still warming up the fresh recorder).
        SHUTDOWN snapshots are subject to the same gates; callers that
        need the shutdown bundle must wait for cooldown to clear before
        triggering the stop."""
        if self._disabled:
            logger.warning(
                "%s capture for %s DROPPED snapshot %s: capture disabled. "
                "There were problems spawning the capture process %d times in a row; ",
                self.tool_name,
                self.protocol.value,
                batch_id,
                self._spawn_failures,
            )
            return
        if self._is_cooldown:
            logger.warning(
                "%s capture for %s DROPPED snapshot %s: capture is in cooldown. "
                "for capture context check the aod_capture_*_%s.tar.zst bundle closest in time to these batch_ids.",
                self.tool_name,
                self.protocol.value,
                batch_id,
                self.protocol.value,
            )
            return
        self._snap_q.put_nowait(batch_id)

    async def run(self, stop_event) -> None:
        """Supervise the capture process: spawn -> wait on (process exit or
        snapshot request) -> on snapshot, drain queue, stop+bundle, respawn and
        warmup; on unexpected exit, restart after restart_delay_sec; on
        stop_event, stop and return.

        Coalesce + cooldown semantics: when a snapshot request fires, any
        other requests already queued for this protocol are drained into the
        same bundle (same recorder state, same tarball). _is_cooldown is set
        before any await so subsequent snapshot() calls during stop/bundle
        AND during the post-respawn warmup are dropped with a log line
        pointing the operator at the bundle they share."""
        os.makedirs(self.capture_dir, exist_ok=True)
        os.makedirs(self.bundle_dir, exist_ok=True)
        live_path = os.path.join(self.capture_dir, f"cap{self.output_extension}")

        # Persistent waiter on the threading.Event. Created once and reused
        # every iteration: cancelling an asyncio.to_thread task does NOT
        # interrupt the underlying Event.wait, so per-iteration recreation
        # would leak executor threads. The waiter naturally completes when
        # stop_event is set and is awaited during cleanup below.
        stop_wait = asyncio.create_task(asyncio.to_thread(stop_event.wait))

        try:
            while not stop_event.is_set():
                if not await self._spawn(live_path):
                    self._spawn_failures += 1
                    if self._spawn_failures >= _MAX_SPAWN_FAILURES:
                        logger.exception(
                            "%s capture for %s failed to spawn %d times; "
                            "disabling captures for this protocol",
                            self.tool_name,
                            self.protocol.value,
                            self._spawn_failures,
                        )
                        self._disabled = True
                        break
                    await asyncio.sleep(self.restart_delay_sec)
                    continue
                self._spawn_failures = 0

                # If we just respawned after a bundle, hold off on accepting
                # new snapshot requests until the fresh recorder has had
                # restart_delay_sec to accumulate data. Without this, a
                # snapshot landing immediately after respawn would produce a
                # near-empty bundle. snapshot() drops requests during this
                # window because _is_cooldown is still True.
                if self._is_cooldown:
                    await asyncio.sleep(self.restart_delay_sec)
                    self._is_cooldown = False
                    if __debug__:
                        logger.info(
                            "%s capture for %s ready after cooldown",
                            self.tool_name,
                            self.protocol.value,
                        )

                proc_wait = asyncio.create_task(self._proc.wait())
                snap_wait = asyncio.create_task(self._snap_q.get())
                try:
                    done, _ = await asyncio.wait(
                        {proc_wait, snap_wait, stop_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    proc_wait.cancel()
                    snap_wait.cancel()
                    # Do NOT cancel stop_wait; it must persist across iterations.
                    await asyncio.gather(
                        proc_wait, snap_wait, return_exceptions=True
                    )

                # If a snapshot request was pulled in the same tick as stop_event,
                # bundle it before shutting down so the data isn't lost.
                pending_snap = snap_wait.result() if snap_wait in done else None

                if stop_wait in done:
                    # Controller.stop() enqueues the SHUTDOWN snapshot on
                    # LogCollector's anomalyActionQueue and immediately
                    # sets stop_event. The to_thread waiter for stop_event
                    # completes the moment .set() is called, but
                    # LogCollector still has to dequeue the event and call
                    # capture.snapshot() before snap_wait can fire. Give
                    # that pipeline a brief grace window so the shutdown
                    # capture context isn't lost just because stop_event
                    # outraced the queue.
                    if pending_snap is None:
                        try:
                            pending_snap = await asyncio.wait_for(
                                self._snap_q.get(), timeout=2.0
                            )
                        except asyncio.TimeoutError:
                            pending_snap = None
                    if pending_snap is not None:
                        coalesced = self._drain_queue(pending_snap)
                        await self._stop()
                        try:
                            tar_path = await asyncio.to_thread(
                                self._bundle, coalesced[0]
                            )
                            logger.info(
                                "%s snapshot bundled at shutdown: %s "
                                "(coalesced %d request(s): %s)",
                                self.tool_name,
                                tar_path,
                                len(coalesced),
                                ", ".join(coalesced),
                            )
                        except Exception:
                            logger.exception("Snapshot bundling failed at shutdown")
                    break

                if pending_snap is not None:
                    # Drain synchronously because snapshot should not enqueue more
                    # between the drain and until _is_cooldown.
                    self._is_cooldown = True
                    coalesced = self._drain_queue(pending_snap)
                    await self._stop()
                    try:
                        tar_path = await asyncio.to_thread(
                            self._bundle, coalesced[0]
                        )
                        logger.info(
                            "%s snapshot bundled: %s "
                            "(coalesced %d request(s): %s)",
                            self.tool_name,
                            tar_path,
                            len(coalesced),
                            ", ".join(coalesced),
                        )
                    except Exception:
                        logger.exception("Snapshot bundling failed")

                else:
                    logger.warning(
                        "%s capture for %s exited unexpectedly (rc=%s); "
                        "restarting in %.1fs",
                        self.tool_name,
                        self.protocol.value,
                        self._proc.returncode,
                        self.restart_delay_sec,
                    )
                    await asyncio.sleep(self.restart_delay_sec)

        except asyncio.CancelledError:
            raise
        finally:
            await self._stop()
            dropped_ids: list[str] = []
            while not self._snap_q.empty():
                dropped_ids.append(self._snap_q.get_nowait())
            if dropped_ids:
                # These snapshots arrived after we'd already committed to
                # shutting down (e.g. SHUTDOWN dump enqueued right after a
                # SIGUSR1 SNAPSHOT). The capture data for them is lost; the
                # quick-action tarball with the matching batch_id is still
                # written. Point the operator at the nearest aod_capture_*
                # bundle for this protocol to recover context.
                logger.warning(
                    "%s capture for %s DROPPED %d queued snapshot(s) at shutdown: %s. "
                    "Quick-action tarballs aod_quick_<batch_id>.tar.zst still exist; "
                    "for capture context check the aod_capture_*_%s.tar.zst bundle "
                    "closest in time to these batch_ids.",
                    self.tool_name,
                    self.protocol.value,
                    len(dropped_ids),
                    ", ".join(dropped_ids),
                    self.protocol.value,
                )
            # Reap the stop_event waiter. If stop_event was set normally it
            # has already completed; if we're exiting via cancellation the
            # underlying executor thread will release as soon as the event
            # is set (which Controller.stop() guarantees on shutdown).
            stop_wait.cancel()
            await asyncio.gather(stop_wait, return_exceptions=True)
            # Remove any unbundled capture files so the next run starts clean.
            for p in glob.glob(os.path.join(self.capture_dir, "cap*")):
                try:
                    os.remove(p)
                except OSError:
                    pass

    async def _spawn(self, live_path: str) -> bool:
        argv = self.build_argv(live_path)
        if __debug__:
            logger.info(
                "Starting %s capture for %s: %s",
                self.tool_name,
                self.protocol.value,
                " ".join(argv),
            )
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                preexec_fn=pdeathsig_preexec,
            )
            return True
        except Exception as e:
            logger.exception(
                "Failed to spawn %s capture for %s",
                self.tool_name,
                self.protocol.value,
            )
            return False

    async def _request_stop(self) -> None:
        """Politely ask the capture process to flush and exit. Default is
        SIGINT to the process group. Subclasses may override."""
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGINT)
        except ProcessLookupError:
            pass

    async def _stop(self) -> None:
        if self._proc is None or self._proc.returncode is not None:
            return
        await self._request_stop()
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=self.stop_grace_sec)
        except asyncio.TimeoutError:
            logger.warning(
                "%s capture for %s did not exit after stop request; sending SIGKILL",
                self.tool_name,
                self.protocol.value,
            )
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self._proc.wait()

    def _drain_queue(self, first: str) -> list[str]:
        """Pop everything currently queued into one list, including `first`
        which was already pulled from snap_wait. Synchronous so it stays
        atomic w.r.t. snapshot() calls in the same event loop."""
        coalesced = [first]
        while not self._snap_q.empty():
            coalesced.append(self._snap_q.get_nowait())
        return coalesced

    def _bundle(self, batch_id: str) -> str | None:
        files = glob.glob(os.path.join(self.capture_dir, "cap*"))
        if not files:
            logger.warning(
                "No %s capture files to bundle for %s",
                self.tool_name,
                self.protocol.value,
            )
            return None
        tar_path = os.path.join(
            self.bundle_dir,
            f"aod_capture_{batch_id}.tar.zst",
        )
        cctx = zstd.ZstdCompressor(level=3)
        try:
            with (
                open(tar_path, "wb") as f,
                cctx.stream_writer(f) as writer,
                tarfile.open(fileobj=writer, mode="w|") as tar,
            ):
                for p in files:
                    tar.add(p, arcname=os.path.basename(p))
        finally:
            # Always drop the source cap* files so the next snapshot doesn't
            # tar them again on top of fresh data. The supervisor's outer
            # except logs the bundling failure separately.
            for p in files:
                try:
                    os.remove(p)
                except OSError:
                    pass
        return tar_path
