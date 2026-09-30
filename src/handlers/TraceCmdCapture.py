"""trace-cmd-based long-running event capture.

Uses the start/stop/extract/reset lifecycle, so there is no long-running recorder
process to supervise. The kernel keeps tracing into its ring buffer until snapshot time,
when we stop, extract to a .dat, reset, and re-enable for the next anomaly.
"""

import asyncio
import logging
import os
import shutil

from base.LongCapture import LongCapture, _STOP_GRACE_SEC


def _bin() -> str:
    return (
        os.environ.get("AOD_TRACECMD_BIN")
        or shutil.which("trace-cmd")
        or "trace-cmd"
    )


logger = logging.getLogger(__name__)


class _NoProc:
    """Sentinel for tools without a long-running foreground process. Its
    wait() blocks forever so the LongCapture supervisor only reacts to
    snapshot or stop_event, never to a spurious 'proc exited' signal."""

    returncode: int | None = None

    def __init__(self) -> None:
        self._fut: asyncio.Future = asyncio.get_running_loop().create_future()

    async def wait(self) -> int:
        await self._fut
        return 0

    def release(self) -> None:
        if not self._fut.done():
            self._fut.set_result(0)


class TraceCmdCapture(LongCapture):
    """trace-cmd start/stop/extract/reset capture. Events and buffer size
    come from user_args (passed straight to `trace-cmd start`)."""

    tool_name = "trace-cmd"
    output_extension = ".dat"
    # Bound for each trace-cmd sub-invocation (start/stop/extract/reset).
    stop_grace_sec = _STOP_GRACE_SEC * 1.5

    def build_argv(self, output_path: str) -> list[str]:
        # Unused by our overridden _spawn
        return [_bin(), "start", *self.user_args]

    async def _spawn(self, live_path: str) -> bool:
        # Clear any leftover trace state from a prior aborted run, then enable.
        await self._oneshot("reset")
        if __debug__:
            logger.info(
                "Enabling trace-cmd for %s: %s",
                self.protocol.value,
                " ".join([_bin(), "start", *self.user_args]),
            )
        rc = await self._oneshot("start", *self.user_args)
        if rc != 0:
            return False
        self._live_path = live_path
        self._proc = _NoProc()
        return True

    async def _stop(self) -> None:
        if self._proc is None:
            return
        await self._oneshot("stop")
        # Extract to the supervisor's live path so _bundle() picks it up.
        await self._oneshot("extract", "-o", self._live_path)
        await self._oneshot("reset")
        if isinstance(self._proc, _NoProc):
            self._proc.release()
        self._proc = None

    async def _oneshot(self, *args: str) -> int | None:
        """Run a single trace-cmd subcommand. Returns rc, or None on failure
        to launch / timeout."""
        try:
            proc = await asyncio.create_subprocess_exec(
                _bin(),
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("trace-cmd binary not found while running '%s'", args[0])
            return None
        try:
            _, err = await asyncio.wait_for(
                proc.communicate(), timeout=self.stop_grace_sec
            )
        except asyncio.TimeoutError:
            logger.warning("trace-cmd %s timed out; killing", args[0])
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return None
        if proc.returncode != 0:
            logger.warning(
                "trace-cmd %s rc=%d: %s",
                args[0],
                proc.returncode,
                err.decode(errors="replace").strip() if err else "",
            )
        return proc.returncode
