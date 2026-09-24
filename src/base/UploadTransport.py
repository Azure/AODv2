"""Transport contract for shipping a diagnostic package to its destination.

Exceptions are grouped by how the Uploader must react, not by what went wrong
on the wire. That keeps retry policy in one place and stops a misconfigured
destination from burning per-package retries.
"""

from abc import ABC, abstractmethod
from pathlib import Path


class TransportError(Exception):
    """Base for all transport failures."""


class TransientError(TransportError):
    """Temporary: timeouts, connection resets, 5xx. Retry with backoff."""


class ThrottledError(TransientError):
    """Service asked us to slow down. Does not consume a retry attempt."""

    def __init__(self, message: str, retry_after: int = 60):
        super().__init__(message)
        self.retry_after = retry_after


class AuthError(TransportError):
    """Credentials missing, expired beyond recovery, or lacking permission.

    Not a per-package problem, so it opens the circuit instead of retrying.
    """


class DestinationError(TransportError):
    """Container or account does not exist / is unreachable by configuration."""


class PayloadError(TransportError):
    """The package itself cannot be sent. Permanent; no retry."""


class AlreadyUploaded(Exception):
    """Blob already exists, so an earlier attempt succeeded.

    Raised rather than returned because it short-circuits the upload, and it
    is what makes at-least-once retry safe after a crash.
    """


class UploadTransport(ABC):
    """Minimal surface the Uploader depends on."""

    @abstractmethod
    def describe(self) -> str:
        """Human-readable destination, for the status file and logs."""

    @abstractmethod
    def preflight(self) -> None:
        """Validate identity and destination. Raises on failure."""

    @abstractmethod
    def upload(self, local_path: Path, blob_name: str, metadata: dict | None = None) -> None:
        """Send one package. Raises AlreadyUploaded or a TransportError.

        `metadata` mirrors key manifest fields so a consumer can filter with a
        listing alone, without downloading anything.
        """
