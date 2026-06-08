"""Abstract base class for quick actions in the log collection process."""

import asyncio
import logging
import time
from pathlib import Path
import os
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class QuickAction(ABC):
    """Base class for quick actions in the log collection process."""

    def __init__(self, batches_root: str, log_filename: str):
        self.batches_root = batches_root
        self.log_filename = log_filename

        # Metrics tracking
        if __debug__:
            self.executions = 0
            self.total_execution_time = 0
            self.failures = 0

    def get_output_path(self, batch_id: str) -> str:
        """Return the output path for the quick action."""
        return os.path.join(
            self.batches_root, f"aod_quick_{batch_id}", self.log_filename
        )

    def get_output_dir(self, batch_id: str) -> str:
        """Return the output directory for the quick action."""
        return os.path.join(self.batches_root, f"aod_quick_{batch_id}")

    @abstractmethod
    def get_command(self) -> tuple[list[str], str]:
        """Return the command to run as a list.
        FOR CAT CMDS, RETURN A LIST OF SIZE 2: ["cat", "/path/to/file"]"""

    async def execute(self, batch_id: str) -> None:
        """Run process to collect logs."""
        if __debug__:
            start_time = time.time()

        try:
            output_path = self.get_output_path(batch_id)
            cmd, cmd_type = self.get_command()

            if cmd_type == "cat":
                # Expecting: ["cat", "/path/to/file"]
                _, in_path = cmd
                await self.collect_cat_output(in_path, output_path)
            elif cmd_type == "cmd":
                await self.collect_cmd_output(cmd, output_path)

            if __debug__:
                self.executions += 1
        except Exception as e:
            # Fail gracefully - log error but don't raise to avoid performance impact
            if __debug__:
                self.failures += 1
            logger.warning(
                "QuickAction %s failed for batch %s: %s",
                self.__class__.__name__,
                batch_id,
                e,
            )
            # Don't raise - continue processing other actions
        finally:
            if __debug__:
                self.total_execution_time += time.time() - start_time
                avg_time = self.total_execution_time / max(
                    1, self.executions + self.failures
                )
                if (
                    self.executions + self.failures
                ) % 10 == 0:  # Log metrics every 10 attempts
                    success_rate = (
                        (self.executions / (self.executions + self.failures) * 100)
                        if (self.executions + self.failures) > 0
                        else 0
                    )
                    logger.debug(
                        "%s metrics: success=%d, failures=%d, success_rate=%.1f%%, avg_time=%.2fs",
                        self.__class__.__name__,
                        self.executions,
                        self.failures,
                        success_rate,
                        avg_time,
                    )

    async def collect_cat_output(self, in_path: str, out_path: str) -> None:
        in_path = Path(in_path)
        out_path = Path(out_path)
        if __debug__:
            logger.debug("Collecting proc fs output from: %s", in_path)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            data = in_path.read_bytes()
            out_path.write_bytes(data)
            if __debug__:
                logger.debug("Output written to: %s", out_path)
        except Exception as e:
            # Don't raise - let execute() handle the failure gracefully
            logger.debug("Failed to collect cat output from %s: %s", in_path, e)
            raise  # Re-raise to be caught by execute()

    async def collect_cmd_output(self, cmd: list, out_path: str) -> None:
        out_path = Path(out_path)
        if __debug__:
            logger.debug("Collecting command output for: %s", " ".join(cmd))
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            if stdout:
                out_path.write_bytes(stdout)
            if __debug__:
                logger.debug("Command output written to: %s", out_path)
        except Exception as e:
            # Don't raise - let execute() handle the failure gracefully
            logger.debug("Failed to execute command '%s': %s", " ".join(cmd), e)
            raise  # Re-raise to be caught by execute()
