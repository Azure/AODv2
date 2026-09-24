"""Error-classification tests for the Azure Blob transport.

No emulator is available here, so these drive the real SDK exception types
through the classifier. That is the part worth testing anyway: a
misclassified error either retries forever or gives up on recoverable data.
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from azure.core.exceptions import (
        ClientAuthenticationError,
        HttpResponseError,
        ResourceExistsError,
        ResourceNotFoundError,
        ServiceRequestError,
        ServiceResponseError,
    )
    from transports.BlobTransport import BlobTransport, _sanitize_metadata
    SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - SDK is an optional dependency
    SDK_AVAILABLE = False

from base.UploadTransport import (  # noqa: E402
    AlreadyUploaded,
    AuthError,
    DestinationError,
    PayloadError,
    ThrottledError,
    TransientError,
    TransportError,
)


class _Response:
    """Minimal stand-in for the pieces azure-core reads off a response."""

    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.reason = "test"
        self.content_type = "application/xml"

    def text(self, *args, **kwargs):
        return ""


def http_error(status, headers=None):
    err = HttpResponseError(response=_Response(status, headers))
    err.status_code = status
    return err


@unittest.skipUnless(SDK_AVAILABLE, "azure-storage-blob not installed")
class BlobErrorClassificationTest(unittest.TestCase):
    """Each mapping decides whether evidence is retried, dropped, or blocks the queue."""

    def classify(self, error):
        return BlobTransport._classify(error, context="test")

    def test_existing_blob_counts_as_success(self):
        # This is what makes retry-after-crash safe: the blob name is the key.
        self.assertIsInstance(self.classify(ResourceExistsError("exists")), AlreadyUploaded)

    def test_auth_failures_open_the_circuit(self):
        self.assertIsInstance(self.classify(ClientAuthenticationError("no token")), AuthError)
        self.assertIsInstance(self.classify(http_error(403)), AuthError)

    def test_missing_container_is_a_destination_problem(self):
        self.assertIsInstance(self.classify(ResourceNotFoundError("gone")), DestinationError)
        self.assertIsInstance(self.classify(http_error(404)), DestinationError)

    def test_throttling_carries_retry_after(self):
        classified = self.classify(http_error(429, {"Retry-After": "17"}))
        self.assertIsInstance(classified, ThrottledError)
        self.assertEqual(classified.retry_after, 17)

    def test_throttling_without_header_falls_back(self):
        self.assertEqual(self.classify(http_error(429)).retry_after, 60)

    def test_server_and_network_errors_are_retryable(self):
        for status in (408, 500, 502, 503, 504):
            self.assertIsInstance(self.classify(http_error(status)), TransientError,
                                  f"HTTP {status} should be retryable")
        self.assertIsInstance(self.classify(ServiceRequestError("reset")), TransientError)
        self.assertIsInstance(self.classify(ServiceResponseError("no response")), TransientError)

    def test_other_client_errors_are_permanent(self):
        # Retrying a malformed request just burns attempts.
        self.assertIsInstance(self.classify(http_error(400)), PayloadError)
        self.assertIsInstance(self.classify(http_error(413)), PayloadError)

    def test_throttling_is_a_transient_subtype(self):
        # The Uploader catches ThrottledError first; this guards that ordering.
        self.assertTrue(issubclass(ThrottledError, TransientError))

    def test_unknown_errors_stay_generic(self):
        self.assertIsInstance(self.classify(ValueError("surprise")), TransportError)


@unittest.skipUnless(SDK_AVAILABLE, "azure-storage-blob not installed")
class BlobMetadataTest(unittest.TestCase):
    def test_keys_are_made_identifier_safe(self):
        cleaned = _sanitize_metadata({"schema-version": 1, "anomaly.type": "latency"})
        self.assertEqual(cleaned, {"schema_version": "1", "anomaly_type": "latency"})

    def test_none_values_are_dropped(self):
        self.assertEqual(_sanitize_metadata({"a": None, "b": "x"}), {"b": "x"})

    def test_empty_metadata_becomes_none(self):
        self.assertIsNone(_sanitize_metadata({}))
        self.assertIsNone(_sanitize_metadata(None))

    def test_non_ascii_is_stripped_not_rejected(self):
        self.assertEqual(_sanitize_metadata({"k": "caf\u00e9"}), {"k": "caf"})


@unittest.skipUnless(SDK_AVAILABLE, "azure-storage-blob not installed")
class ChunkedUploadTest(unittest.TestCase):
    """A package over the threshold is staged as blocks (D5).

    This also pins the permissions the custom role grants: if the block path
    ever needed to read, the role would have to grant blobs/read, which would
    let any host read every other host's evidence.
    """

    def _upload(self, payload_size, threshold):
        from azure.core.credentials import AccessToken
        from azure.core.pipeline.transport import HttpTransport
        from azure.storage.blob import BlobServiceClient

        calls = []

        class Response:
            def __init__(self, request):
                self.request = request
                self.status_code = 201
                self.headers = {"ETag": '"0x1"', "x-ms-request-id": "r",
                                "Last-Modified": "Wed, 24 Sep 2026 00:00:00 GMT"}
                self.reason = "Created"
                self.content_type = "application/xml"
                self.block_size = 4096

            def body(self):
                return b""

            def text(self, *args, **kwargs):
                return ""

        class Recorder(HttpTransport):
            def send(self, request, **kwargs):
                calls.append((request.method, request.url.split("?")[-1]))
                return Response(request)

            def open(self):
                pass

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class Credential:
            def get_token(self, *args, **kwargs):
                return AccessToken("token", 9999999999)

        service = BlobServiceClient(
            "https://acct.blob.core.windows.net", credential=Credential(),
            max_single_put_size=threshold, max_block_size=threshold // 2,
            transport=Recorder(),
        )
        blob = service.get_container_client("c").get_blob_client("pkg.tar.zst")
        blob.upload_blob(io.BytesIO(b"x" * payload_size), overwrite=True,
                         length=payload_size)
        return calls

    def test_large_package_is_staged_as_blocks(self):
        calls = self._upload(payload_size=3 * 1024 * 1024, threshold=1024 * 1024)
        self.assertGreater(len([q for _, q in calls if "comp=block&" in q]), 1)
        self.assertEqual(len([q for _, q in calls if q == "comp=blocklist"]), 1)

    def test_small_package_goes_in_one_request(self):
        calls = self._upload(payload_size=1024, threshold=1024 * 1024)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("comp=block", calls[0][1])

    def test_upload_never_reads(self):
        # The custom role grants no blobs/read; a GET here would mean 403.
        for size in (1024, 3 * 1024 * 1024):
            calls = self._upload(payload_size=size, threshold=1024 * 1024)
            self.assertEqual([m for m, _ in calls if m != "PUT"], [],
                             f"non-PUT request issued for a {size} byte package")


if __name__ == "__main__":
    unittest.main()
