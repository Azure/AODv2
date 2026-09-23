"""Azure Blob transport using the Azure SDK.

The SDK performs the size-based switch decided in D5: blobs under
`max_single_put_size` go in one request, larger ones are staged as blocks and
committed, with per-block retry. We therefore configure those thresholds and
let the SDK own the mechanics.

The SDK is imported lazily so a daemon without it installed degrades to
"upload disabled" rather than failing to start.
"""

import logging
from pathlib import Path

from base.UploadTransport import (
    AlreadyUploaded,
    AuthError,
    DestinationError,
    PayloadError,
    ThrottledError,
    TransientError,
    TransportError,
    UploadTransport,
)

logger = logging.getLogger(__name__)

PREFLIGHT_BLOB = ".aod-preflight"
_RETRYABLE_STATUS = {408, 500, 502, 503, 504}


class BlobTransport(UploadTransport):
    """Uploads packages to a customer-owned blob container."""

    def __init__(self, destination, identity, limits, timeout_sec: int = 300,
                 preflight_write_check: bool = True, retry_total: int | None = None,
                 connect_timeout_sec: int | None = None):
        self.destination = destination
        self.timeout_sec = timeout_sec
        self.preflight_write_check = preflight_write_check

        from azure.identity import ManagedIdentityCredential
        from azure.storage.blob import BlobServiceClient

        # Off an Azure VM, IMDS is unreachable and the default retry policy can
        # block for minutes. Callers that need a fast answer pass retry_total=0.
        credential_kwargs = {}
        if retry_total is not None:
            credential_kwargs["retry_total"] = retry_total
        if connect_timeout_sec is not None:
            credential_kwargs["connection_timeout"] = connect_timeout_sec
        if identity.client_id:
            credential_kwargs["client_id"] = identity.client_id

        # Also covers Arc-enabled machines, which expose an IMDS-compatible endpoint.
        self._credential = ManagedIdentityCredential(**credential_kwargs)

        client_kwargs = {}
        if retry_total is not None:
            client_kwargs["retry_total"] = retry_total

        self._service = BlobServiceClient(
            account_url=destination.endpoint,
            credential=self._credential,
            max_single_put_size=limits.multipart_threshold_mb * 1024 * 1024,
            max_block_size=limits.block_size_mb * 1024 * 1024,
            connection_timeout=connect_timeout_sec or timeout_sec,
            **client_kwargs,
        )
        self._container = self._service.get_container_client(destination.container)

    def describe(self) -> str:
        return f"{self.destination.endpoint}/{self.destination.container}"

    def check_credential(self) -> None:
        """Fail with a specific message when no managed identity is available.

        Without this, 'not running on an Azure VM' surfaces as an opaque
        connection error after a long retry loop.
        """
        try:
            self._credential.get_token("https://storage.azure.com/.default")
        except Exception as e:
            raise AuthError(f"no usable managed identity on this host: {e}") from e

    def preflight(self) -> None:
        """Distinguish 'cannot authenticate' from 'cannot write' from 'no container'."""
        self.check_credential()
        try:
            self._container.get_container_properties(timeout=self.timeout_sec)
        except Exception as e:
            raise self._classify(e, context="container properties") from e

        if not self.preflight_write_check:
            return

        try:
            probe = self._container.get_blob_client(PREFLIGHT_BLOB)
            probe.upload_blob(b"aod", overwrite=True, timeout=self.timeout_sec)
        except Exception as e:
            raise self._classify(e, context="write probe") from e

    def upload(self, local_path, blob_name: str, metadata: dict | None = None) -> None:
        local_path = Path(local_path)
        try:
            size = local_path.stat().st_size
        except OSError as e:
            raise PayloadError(f"package unreadable: {e}") from e

        from azure.storage.blob import ContentSettings

        blob = self._container.get_blob_client(blob_name)
        try:
            with open(local_path, "rb") as stream:
                blob.upload_blob(
                    stream,
                    blob_type="BlockBlob",
                    # The blob name is the idempotency key: a conflict means an
                    # earlier attempt already succeeded.
                    overwrite=False,
                    length=size,
                    max_concurrency=1,
                    content_settings=ContentSettings(content_type="application/zstd"),
                    metadata=_sanitize_metadata(metadata),
                    timeout=self.timeout_sec,
                )
        except Exception as e:
            classified = self._classify(e, context=f"upload {blob_name}")
            if isinstance(classified, AlreadyUploaded):
                raise classified from None
            raise classified from e

    @staticmethod
    def _classify(error: Exception, context: str = "") -> Exception:
        """Map SDK errors onto the reactions the Uploader knows how to take."""
        from azure.core.exceptions import (
            ClientAuthenticationError,
            HttpResponseError,
            ResourceExistsError,
            ResourceNotFoundError,
            ServiceRequestError,
            ServiceResponseError,
        )

        if isinstance(error, ResourceExistsError):
            return AlreadyUploaded(context)
        if isinstance(error, ClientAuthenticationError):
            return AuthError(f"{context}: {error}")
        if isinstance(error, ResourceNotFoundError):
            return DestinationError(f"{context}: container or account not found ({error})")
        if isinstance(error, (ServiceRequestError, ServiceResponseError)):
            # Connection-level: DNS, reset, timeout.
            return TransientError(f"{context}: {error}")

        if isinstance(error, HttpResponseError):
            status = getattr(error, "status_code", None)
            if status == 429:
                return ThrottledError(f"{context}: throttled", retry_after=_retry_after(error))
            if status == 403:
                return AuthError(f"{context}: forbidden - check the role assignment ({error})")
            if status == 404:
                return DestinationError(f"{context}: not found ({error})")
            if status in _RETRYABLE_STATUS:
                return TransientError(f"{context}: HTTP {status}")
            if status is not None and 400 <= status < 500:
                # Other 4xx are request problems that retrying cannot fix.
                return PayloadError(f"{context}: HTTP {status} ({error})")
            return TransientError(f"{context}: {error}")

        if isinstance(error, OSError):
            return PayloadError(f"{context}: {error}")
        return TransportError(f"{context}: {error}")


def _retry_after(error) -> int:
    try:
        value = error.response.headers.get("Retry-After")
        return max(1, int(value))
    except (AttributeError, TypeError, ValueError):
        return 60


def _sanitize_metadata(metadata: dict | None) -> dict | None:
    """Blob metadata keys must be valid C# identifiers and ASCII values."""
    if not metadata:
        return None
    clean = {}
    for key, value in metadata.items():
        if value is None:
            continue
        safe_key = "".join(ch if ch.isalnum() else "_" for ch in str(key))
        if safe_key and not safe_key[0].isalpha():
            safe_key = f"x{safe_key}"
        clean[safe_key] = str(value).encode("ascii", "ignore").decode("ascii")[:256]
    return clean or None
