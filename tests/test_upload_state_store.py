"""End-to-end upload pipeline tests using the local filesystem transport.

Covers the behaviours that are easy to get wrong and expensive to discover in
production: crash recovery, idempotency, and SpaceWatcher's interaction with
un-uploaded evidence.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import UploadStateStore as ustate  # noqa: E402
from UploadStateStore import UploadStateStore  # noqa: E402
from base.UploadTransport import AlreadyUploaded  # noqa: E402
from transports.LocalTransport import LocalTransport  # noqa: E402


class TestUploadStateStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UploadStateStore(Path(self.tmp.name) / "state.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_register_is_idempotent(self):
        self.assertTrue(self.store.register("pkg1", "/tmp/pkg1", 100))
        self.assertFalse(self.store.register("pkg1", "/tmp/pkg1", 100))

    def test_claim_happens_once(self):
        self.store.register("pkg1", "/tmp/pkg1", 100)
        self.assertTrue(self.store.claim("pkg1"))
        self.assertFalse(self.store.claim("pkg1"))

    def test_crash_recovery_does_not_consume_an_attempt(self):
        self.store.register("pkg1", "/tmp/pkg1", 100)
        self.store.claim("pkg1")
        self.assertEqual(self.store.get("pkg1")["state"], ustate.STATE_IN_PROGRESS)

        self.store.reset_in_progress()

        row = self.store.get("pkg1")
        self.assertEqual(row["state"], ustate.STATE_PENDING)
        self.assertEqual(row["attempts"], 0)

    def test_retry_increments_attempts_but_blocked_does_not(self):
        self.store.register("pkg1", "/tmp/pkg1", 100)
        self.store.mark_retry("pkg1", "timeout", time.time())
        self.assertEqual(self.store.get("pkg1")["attempts"], 1)

        self.store.mark_blocked("pkg1", "blocked", time.time())
        self.assertEqual(self.store.get("pkg1")["attempts"], 1)

    def test_due_packages_respects_backoff(self):
        self.store.register("pkg1", "/tmp/pkg1", 100)
        self.store.mark_retry("pkg1", "timeout", time.time() + 3600)
        self.assertEqual(self.store.due_packages(), [])

        self.store.mark_retry("pkg1", "timeout", time.time() - 1)
        self.assertEqual(len(self.store.due_packages()), 1)

    def test_pending_bytes_excludes_uploaded(self):
        self.store.register("pkg1", "/tmp/pkg1", 100)
        self.store.register("pkg2", "/tmp/pkg2", 250)
        self.assertEqual(self.store.pending_bytes(), 350)

        self.store.mark_uploaded("pkg1", "blob/pkg1")
        self.assertEqual(self.store.pending_bytes(), 250)

    def test_prune_removes_only_old_terminal_rows(self):
        self.store.register("done", "/tmp/done", 10)
        self.store.mark_uploaded("done", "blob/done")
        self.store.register("waiting", "/tmp/waiting", 10)

        self.assertEqual(self.store.prune(time.time() + 60), 1)
        self.assertIsNone(self.store.get("done"))
        self.assertIsNotNone(self.store.get("waiting"))


class TestLocalTransport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "blobs"
        self.transport = LocalTransport(self.root)
        self.package = Path(self.tmp.name) / "pkg.tar.zst"
        self.package.write_bytes(b"diagnostic payload")

    def tearDown(self):
        self.tmp.cleanup()

    def test_upload_then_reupload_signals_already_uploaded(self):
        self.transport.preflight()
        self.transport.upload(self.package, "aodv2/v1/host/2026-09-21/120000Z-latency.tar.zst")

        target = self.root / "aodv2/v1/host/2026-09-21/120000Z-latency.tar.zst"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"diagnostic payload")

        with self.assertRaises(AlreadyUploaded):
            self.transport.upload(self.package, "aodv2/v1/host/2026-09-21/120000Z-latency.tar.zst")

    def test_no_partial_objects_left_behind(self):
        self.transport.preflight()
        self.transport.upload(self.package, "a/b/c.tar.zst")
        leftovers = list(self.root.rglob("*.uploading"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
