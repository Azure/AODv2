"""End-to-end checks across LogCollector, Uploader and SpaceWatcher.

These exercise the interactions that the unit tests cannot: that a package is
only published once complete, that it reaches the destination, and that disk
cleanup does not destroy evidence that has not shipped yet.
"""

import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import UploadStateStore as ustate  # noqa: E402
from LogCollector import LogCollector  # noqa: E402
from SpaceWatcher import SpaceWatcher  # noqa: E402
from UploadStateStore import UploadStateStore  # noqa: E402
from Uploader import Uploader  # noqa: E402
from base.UploadTransport import (  # noqa: E402
    AlreadyUploaded,
    AuthError,
    DestinationError,
    PayloadError,
    ThrottledError,
    TransientError,
)
from transports.LocalTransport import LocalTransport  # noqa: E402
from utils import manifest as manifest_utils  # noqa: E402
from utils import paths  # noqa: E402
from utils.anomaly_type import AnomalyType  # noqa: E402
from utils.config_schema import (  # noqa: E402
    AnomalyConfig,
    Config,
    GuardianConfig,
    UploadConfig,
    UploadLimits,
    WatcherConfig,
)
from utils.host_id import get_host_id  # noqa: E402
import queue  # noqa: E402


def build_config(output_dir, *, upload_enabled=True, spool_max_mb=256, max_total_mb=256):
    return Config(
        watch_interval_sec=1,
        aod_output_dir=str(output_dir),
        watcher=WatcherConfig(actions=["dmesg"]),
        guardian=GuardianConfig(anomalies={
            "latency": AnomalyConfig(
                type="Latency", tool="smbslower", acceptable_count=10,
                default_threshold_ms=20, track={9: 50}, actions=["mounts"],
            )
        }),
        cleanup={"max_log_age_days": 2, "max_total_log_size_mb": max_total_mb,
                 "cleanup_interval_sec": 60},
        audit={"enabled": True},
        upload=UploadConfig(
            enabled=upload_enabled,
            protocol="local",
            limits=UploadLimits(upload_spool_max_mb=spool_max_mb),
        ),
    )


class FakeController:
    """Minimal stand-in for Controller with the attributes components read."""

    def __init__(self, output_dir, **config_kwargs):
        self.config = build_config(output_dir, **config_kwargs)
        self.stop_event = threading.Event()
        self.uploadQueue = queue.Queue(maxsize=64)
        self.anomalyActionQueue = queue.Queue()
        self.host_id = get_host_id(paths.output_dir(self.config))
        self.upload_store = UploadStateStore(paths.upload_dir(self.config) / "state.db")
        self.upload_transport = LocalTransport(paths.upload_dir(self.config) / "local-blobs")


class UploadPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.tmp.name)
        self.controller = FakeController(self.output_dir)
        self.batches = paths.batches_dir(self.controller.config)
        self.batches.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.controller.upload_store.close()
        self.tmp.cleanup()

    def _make_package(self, name="aod_quick_latency_1", size=512, mtime=None):
        package = self.batches / f"{name}{paths.PACKAGE_EXTENSION}"
        package.write_bytes(b"x" * size)
        manifest_utils.write_manifest(
            {
                "schema_version": 1,
                "anomaly": {"type": "latency", "tool": "smbslower"},
                "collection": {"started_at": "2026-09-21T12:00:00Z",
                               "ended_at": "2026-09-21T12:00:01Z"},
            },
            self.batches / f"{name}{manifest_utils.MANIFEST_EXTENSION}",
        )
        if mtime is not None:
            os.utime(package, (mtime, mtime))
        return package

    # ------------------------------------------------------------------ collector

    def test_collector_publishes_atomically_with_a_manifest(self):
        collector = LogCollector(self.controller)
        event = {"anomaly": AnomalyType.LATENCY, "timestamp": 1_700_000_000_000}
        asyncio.run(collector._create_log_collection_task(event))

        packages = list(self.batches.glob(f"*{paths.PACKAGE_EXTENSION}"))
        self.assertEqual(len(packages), 1, "exactly one package should be published")
        self.assertFalse(
            list(self.batches.glob(f"*{paths.PARTIAL_EXTENSION}")),
            "no .part file may survive a successful publish",
        )

        sibling = packages[0].with_name(
            packages[0].name.replace(paths.PACKAGE_EXTENSION, manifest_utils.MANIFEST_EXTENSION)
        )
        self.assertTrue(sibling.exists(), "sibling manifest must accompany the package")

        import json
        manifest = json.loads(sibling.read_text())
        self.assertEqual(manifest["anomaly"]["type"], "latency")
        self.assertIn("sha256", manifest["package"])
        self.assertTrue(manifest["collectors"], "collector outcomes must be recorded")

    # ------------------------------------------------------------------- uploader

    def test_scan_uploads_pending_package(self):
        self._make_package()
        uploader = Uploader(self.controller)
        uploader.backfill_cutoff = 0  # accept pre-existing packages

        uploader._scan()
        uploader._drain_due()

        blobs = list((paths.upload_dir(self.controller.config) / "local-blobs").rglob("*.tar.zst"))
        self.assertEqual(len(blobs), 1)
        self.assertIn("2026-09-21", str(blobs[0]), "blob path uses the manifest date")
        self.assertIn("latency", blobs[0].name)

        row = self.controller.upload_store.get(f"aod_quick_latency_1{paths.PACKAGE_EXTENSION}")
        self.assertEqual(row["state"], ustate.STATE_UPLOADED)

    def test_reupload_after_crash_is_safe(self):
        self._make_package()
        uploader = Uploader(self.controller)
        uploader.backfill_cutoff = 0
        uploader._scan()
        uploader._drain_due()

        # Simulate a crash that lost the UPLOADED record but not the blob.
        package_id = f"aod_quick_latency_1{paths.PACKAGE_EXTENSION}"
        self.controller.upload_store.mark_blocked(package_id, "reset", time.time() - 1)
        uploader._drain_due()

        blobs = list((paths.upload_dir(self.controller.config) / "local-blobs").rglob("*.tar.zst"))
        self.assertEqual(len(blobs), 1, "retry must not create a duplicate blob")
        self.assertEqual(
            self.controller.upload_store.get(package_id)["state"], ustate.STATE_UPLOADED
        )

    def test_backfill_window_skips_old_packages(self):
        self._make_package(name="aod_quick_latency_old", mtime=time.time() - 90000)
        uploader = Uploader(self.controller)
        uploader.backfill_cutoff = time.time() - 86400

        uploader._scan()

        row = self.controller.upload_store.get(
            f"aod_quick_latency_old{paths.PACKAGE_EXTENSION}"
        )
        self.assertEqual(row["state"], ustate.STATE_SKIPPED)
        self.assertEqual(row["reason"], ustate.REASON_BACKFILL_WINDOW)

    # ---------------------------------------------------------------- spacewatcher

    def test_cleanup_never_deletes_an_in_flight_package(self):
        package = self._make_package(size=2 * 1024 * 1024)
        store = self.controller.upload_store
        store.register(package.name, str(package), package.stat().st_size)
        store.claim(package.name)  # IN_PROGRESS

        self.controller.config = build_config(self.output_dir, max_total_mb=1, spool_max_mb=1)
        watcher = SpaceWatcher(self.controller)
        watcher.upload_store = store
        watcher.cleanup_by_size()

        self.assertTrue(package.exists(), "a package being uploaded must survive cleanup")

    def test_cleanup_reclaims_uploaded_before_pending(self):
        old = time.time() - 600
        uploaded = self._make_package(name="aod_quick_latency_uploaded",
                                      size=1024 * 1024, mtime=old + 100)
        pending = self._make_package(name="aod_quick_latency_pending",
                                     size=1024 * 1024, mtime=old)

        store = self.controller.upload_store
        for pkg in (uploaded, pending):
            store.register(pkg.name, str(pkg), pkg.stat().st_size)
        store.mark_uploaded(uploaded.name, "blob/uploaded")

        self.controller.config = build_config(self.output_dir, max_total_mb=1, spool_max_mb=64)
        watcher = SpaceWatcher(self.controller)
        watcher.upload_store = store
        watcher.cleanup_by_size()

        # The pending package is newer AND protected, so the uploaded one goes
        # even though ordering by mtime alone would have chosen the other.
        self.assertFalse(uploaded.exists(), "uploaded copies are reclaimed first")
        self.assertTrue(pending.exists(), "un-uploaded evidence is protected by the spool reserve")

    def test_pending_is_evicted_once_past_the_spool_reserve(self):
        packages = [
            self._make_package(name=f"aod_quick_latency_{i}", size=1024 * 1024,
                               mtime=time.time() - 1000 + i)
            for i in range(3)
        ]
        store = self.controller.upload_store
        for pkg in packages:
            store.register(pkg.name, str(pkg), pkg.stat().st_size)

        # Reserve smaller than the pending total, so the oldest must give way.
        self.controller.config = build_config(self.output_dir, max_total_mb=1, spool_max_mb=1)
        watcher = SpaceWatcher(self.controller)
        watcher.upload_store = store
        watcher.cleanup_by_size()

        surviving = [p for p in packages if p.exists()]
        self.assertLess(len(surviving), 3, "spool reserve must be bounded, not absolute")
        self.assertIn(packages[-1], surviving, "the newest evidence is kept longest")

    def test_failing_uploads_reclaim_as_much_as_no_upload_at_all(self):
        # Pinning must not make cleanup weaker than it was before the upload
        # feature existed: a fleet whose uploads are all failing must still get
        # its disk back.
        def run(register_as_pending):
            for stale in self.batches.glob("*"):
                stale.unlink()
            names = []
            for i in range(16):
                pkg = self._make_package(name=f"aod_quick_latency_{i:02d}",
                                         size=1024 * 1024,
                                         mtime=time.time() - 1000 + i)
                names.append(pkg.name)
                if register_as_pending:
                    self.controller.upload_store.register(
                        pkg.name, str(pkg), pkg.stat().st_size)

            self.controller.config = build_config(
                self.output_dir, spool_max_mb=8, max_total_mb=16)
            watcher = SpaceWatcher(self.controller)
            watcher.upload_store = (
                self.controller.upload_store if register_as_pending else None)
            watcher.cleanup_by_size()

            survivors = sorted(f.name for f in self.batches.glob("*"))
            for name in names:
                self.controller.upload_store.delete(name)
            return survivors

        baseline = run(register_as_pending=False)
        with_failing_uploads = run(register_as_pending=True)

        self.assertEqual(with_failing_uploads, baseline,
                         "failing uploads must not stop cleanup reclaiming disk")
        self.assertTrue(baseline, "cleanup should not delete everything")

    def test_age_cleanup_still_runs_while_uploads_are_failing(self):
        # The age limit is the backstop that bounds disk use when nothing can
        # upload; the spool reserve must not defeat it.
        stale = self._make_package(name="aod_quick_latency_stale",
                                   mtime=time.time() - 10 * 86400)
        store = self.controller.upload_store
        store.register(stale.name, str(stale), stale.stat().st_size)  # PENDING, never uploaded

        self.controller.config = build_config(self.output_dir, spool_max_mb=4096)
        watcher = SpaceWatcher(self.controller)
        watcher.upload_store = store
        watcher.cleanup_by_age()

        self.assertFalse(stale.exists(),
                         "an un-uploaded package past the age limit must still be deleted")

    def test_spool_reserve_cannot_consume_the_whole_size_budget(self):
        # If the reserve equals the total cap, cleanup can never reach its
        # target and disk sits at the limit indefinitely.
        from ConfigManager import ConfigManager

        shipped = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
        config = ConfigManager(str(shipped)).data
        self.assertLess(
            config.upload.limits.upload_spool_max_mb,
            config.cleanup["max_total_log_size_mb"],
            "upload_spool_max_mb must leave room for cleanup to free space",
        )


class FailingTransport(LocalTransport):
    """Raises a chosen error on upload, to drive the Uploader's reactions."""

    def __init__(self, root, error):
        super().__init__(root)
        self.error = error
        self.attempts = 0

    def upload(self, local_path, blob_name, metadata=None):
        self.attempts += 1
        raise self.error


class UploaderErrorHandlingTest(unittest.TestCase):
    """Classification is only useful if the reaction to it is right."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.tmp.name)
        self.controller = FakeController(self.output_dir)
        self.batches = paths.batches_dir(self.controller.config)
        self.batches.mkdir(parents=True, exist_ok=True)
        self.package = self.batches / f"aod_quick_latency_1{paths.PACKAGE_EXTENSION}"
        self.package.write_bytes(b"payload")
        self.package_id = self.package.name

    def tearDown(self):
        self.controller.upload_store.close()
        self.tmp.cleanup()

    def _uploader(self, error):
        self.controller.upload_transport = FailingTransport(
            paths.upload_dir(self.controller.config) / "blobs", error
        )
        uploader = Uploader(self.controller)
        uploader.backfill_cutoff = 0
        uploader._scan()
        return uploader

    def _row(self):
        return self.controller.upload_store.get(self.package_id)

    def test_transient_error_retries_and_consumes_an_attempt(self):
        uploader = self._uploader(TransientError("connection reset"))
        uploader._drain_due()

        row = self._row()
        self.assertEqual(row["state"], ustate.STATE_PENDING)
        self.assertEqual(row["attempts"], 1)
        self.assertGreater(row["next_attempt_at"], time.time() - 1)

    def test_an_unexpected_error_does_not_escape_or_loop_for_ever(self):
        # claim() does not count attempts, so an exception that escapes here
        # kills the thread, gets requeued by reset_in_progress on restart, and
        # retries without limit. It must be treated as transient instead.
        uploader = self._uploader(RuntimeError("something the SDK never documented"))
        uploader._drain_due()

        row = self._row()
        self.assertEqual(row["state"], ustate.STATE_PENDING)
        self.assertEqual(row["attempts"], 1, "an unexpected error must consume an attempt")

    def test_an_unexpected_error_eventually_fails_permanently(self):
        uploader = self._uploader(RuntimeError("poison package"))
        for _ in range(uploader.retry.max_attempts):
            # Make it due again without waiting out the backoff.
            self.controller.upload_store.mark_retry(self.package_id, "test", 0)
            uploader._drain_due()

        self.assertEqual(self._row()["state"], ustate.STATE_FAILED,
                         "a package that always explodes must stop being retried")

    def test_throttling_does_not_consume_an_attempt(self):
        uploader = self._uploader(ThrottledError("slow down", retry_after=42))
        uploader._drain_due()

        row = self._row()
        self.assertEqual(row["state"], ustate.STATE_PENDING)
        self.assertEqual(row["attempts"], 0, "throttling is not the package's fault")
        self.assertGreater(row["next_attempt_at"], time.time() + 30)

    def test_auth_error_opens_circuit_without_consuming_attempts(self):
        uploader = self._uploader(AuthError("403 forbidden"))
        uploader._drain_due()

        row = self._row()
        self.assertEqual(row["state"], ustate.STATE_PENDING)
        self.assertEqual(row["attempts"], 0, "a misconfigured destination must not burn retries")
        self.assertEqual(uploader._circuit_reason, "BLOCKED_AUTH")

        # While the circuit is open no further transfers are attempted.
        before = uploader.transport.attempts
        uploader._drain_due()
        self.assertEqual(uploader.transport.attempts, before)

    def test_destination_error_opens_circuit(self):
        uploader = self._uploader(DestinationError("container missing"))
        uploader._drain_due()
        self.assertEqual(uploader._circuit_reason, "BLOCKED_DESTINATION")

    def test_payload_error_fails_permanently(self):
        uploader = self._uploader(PayloadError("unreadable"))
        uploader._drain_due()
        self.assertEqual(self._row()["state"], ustate.STATE_FAILED)

    def test_attempts_are_exhausted_into_failed(self):
        uploader = self._uploader(TransientError("nope"))
        for _ in range(self.controller.config.upload.retry.max_attempts + 1):
            self.controller.upload_store.mark_blocked(self.package_id, "ready", time.time() - 1)
            uploader._drain_due()

        self.assertEqual(self._row()["state"], ustate.STATE_FAILED)

    def test_already_uploaded_is_treated_as_success(self):
        uploader = self._uploader(AlreadyUploaded("blob exists"))
        uploader._drain_due()
        self.assertEqual(self._row()["state"], ustate.STATE_UPLOADED)

    def test_status_file_reports_blocked_state(self):
        uploader = self._uploader(AuthError("403"))
        uploader._drain_due()
        uploader._write_status("IDLE")

        import json
        status = json.loads((paths.upload_dir(self.controller.config) / "status.json").read_text())
        self.assertEqual(status["state"], "BLOCKED_AUTH")
        self.assertIn("403", status["blocked_reason"])


class ConfigDropInTest(unittest.TestCase):
    """`aodv2 upload enable` must not disturb a hand-maintained config.yaml."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name)
        self.source = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
        self.config_path = self.config_dir / "config.yaml"
        self.config_path.write_text(self.source.read_text())

    def tearDown(self):
        self.tmp.cleanup()

    def test_drop_in_overrides_without_touching_config_yaml(self):
        import cli

        original = self.config_path.read_text()
        exit_code = cli.main([
            "--config", str(self.config_path), "upload", "enable",
            "--account", "acct", "--container", "cont",
        ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.config_path.read_text(), original,
                         "config.yaml must be left byte-identical")

        from ConfigManager import ConfigManager
        config = ConfigManager(str(self.config_path)).data
        self.assertTrue(config.upload.enabled)
        self.assertEqual(config.upload.destination.account, "acct")
        self.assertEqual(config.upload.destination.container, "cont")

    def test_disable_removes_the_drop_in(self):
        import cli

        cli.main(["--config", str(self.config_path), "upload", "enable",
                  "--account", "a", "--container", "c"])
        cli.main(["--config", str(self.config_path), "upload", "disable"])

        from ConfigManager import ConfigManager
        self.assertFalse(ConfigManager(str(self.config_path)).data.upload.enabled)

    def test_shipped_config_uploads_everything_on_disk(self):
        # Turning upload on after an incident must still ship that incident.
        from ConfigManager import ConfigManager
        config = ConfigManager(str(self.config_path)).data
        self.assertEqual(config.upload.limits.backfill_max_age_hours, 0)

    def test_client_id_is_persisted_for_a_shared_identity(self):
        # Without this, IMDS cannot choose between a VM's identities once a
        # user-assigned one is attached alongside the system-assigned one.
        import cli

        exit_code = cli.main([
            "--config", str(self.config_path), "upload", "enable",
            "--account", "acct", "--container", "cont",
            "--client-id", "11111111-2222-3333-4444-555555555555",
        ])
        self.assertEqual(exit_code, 0)

        from ConfigManager import ConfigManager
        identity = ConfigManager(str(self.config_path)).data.upload.identity
        self.assertEqual(identity.client_id, "11111111-2222-3333-4444-555555555555")


class OperatorHintTest(unittest.TestCase):
    """The commands we tell the operator to run must match how they installed."""

    def test_disabled_upload_creates_nothing_on_disk(self):
        # Creating the upload tree unconditionally breaks startup for a daemon
        # that cannot write there, on hosts that never enable upload.
        with tempfile.TemporaryDirectory() as tmp:
            config = build_config(Path(tmp), upload_enabled=False)
            self.assertFalse(
                paths.upload_dir(config).exists(),
                "upload state must not exist while upload is disabled",
            )

    def test_invocation_matches_how_we_were_called(self):
        import cli

        with unittest.mock.patch.object(sys, "argv", ["/usr/bin/aodv2"]):
            self.assertEqual(cli._self_invocation(), "aodv2")
        with unittest.mock.patch.object(sys, "argv", ["/repo/src/cli.py"]):
            self.assertIn("python3 -m cli", cli._self_invocation())

    def test_restart_hint_finds_the_unit_wherever_packaging_put_it(self):
        import cli

        for unit_dir in ("/etc/systemd/system", "/usr/lib/systemd/system",
                         "/lib/systemd/system"):
            present = {"/run/systemd/system", f"{unit_dir}/linux_diagnostics.service"}
            with unittest.mock.patch.object(
                cli.Path, "exists", lambda self: str(self) in present
            ):
                self.assertIn("systemctl restart", cli._restart_hint(),
                              f"unit in {unit_dir} was not detected")

    def test_restart_hint_without_systemd(self):
        import cli

        with unittest.mock.patch.object(cli.Path, "exists", lambda self: False):
            self.assertNotIn("systemctl", cli._restart_hint())


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.controller = FakeController(Path(self.tmp.name))
        self.batches = paths.batches_dir(self.controller.config)
        self.batches.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.controller.upload_store.close()
        self.tmp.cleanup()

    def test_zero_backfill_hours_uploads_old_packages(self):
        old = self.batches / f"aod_quick_latency_old{paths.PACKAGE_EXTENSION}"
        old.write_bytes(b"old evidence")
        ancient = time.time() - 30 * 86400
        os.utime(old, (ancient, ancient))

        uploader = Uploader(self.controller)
        uploader.backfill_cutoff = None  # what limits.backfill_max_age_hours=0 produces
        uploader._scan()
        uploader._drain_due()

        row = self.controller.upload_store.get(old.name)
        self.assertEqual(row["state"], ustate.STATE_UPLOADED,
                         "a month-old package must still be uploaded")


class BlobNamingTest(unittest.TestCase):
    """Blob names must be unique per package.

    Collection timestamps have one-second resolution. If two packages share a
    name, the second is rejected as "already uploaded" and silently lost - the
    upload is reported as successful while the data never arrives.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.controller = FakeController(Path(self.tmp.name))
        self.batches = paths.batches_dir(self.controller.config)
        self.batches.mkdir(parents=True, exist_ok=True)
        self.uploader = Uploader(self.controller)

    def tearDown(self):
        self.controller.upload_store.close()
        self.tmp.cleanup()

    def _package(self, name, payload):
        package = self.batches / f"{name}{paths.PACKAGE_EXTENSION}"
        package.write_bytes(payload)
        manifest_utils.write_manifest(
            manifest_utils.finalize_manifest(
                {
                    "schema_version": 1,
                    "anomaly": {"type": "latency", "tool": "smbslower"},
                    # Identical second for both packages - the colliding case.
                    "collection": {"started_at": "2026-09-23T10:00:00Z",
                                   "ended_at": "2026-09-23T10:00:01Z"},
                },
                package,
            ),
            self.batches / f"{name}{manifest_utils.MANIFEST_EXTENSION}",
        )
        return package

    def test_same_second_packages_get_distinct_names(self):
        first = self._package("aod_quick_latency_1", b"first package")
        second = self._package("aod_quick_latency_2", b"second package")

        self.assertNotEqual(
            self.uploader._blob_name(first),
            self.uploader._blob_name(second),
            "packages collected in the same second must not share a blob name",
        )

    def test_same_package_keeps_a_stable_name(self):
        # Retry after a crash must target the same blob, or it duplicates.
        package = self._package("aod_quick_latency_1", b"payload")
        self.assertEqual(
            self.uploader._blob_name(package), self.uploader._blob_name(package)
        )

    def test_two_same_second_packages_both_reach_the_destination(self):
        self._package("aod_quick_latency_1", b"first package")
        self._package("aod_quick_latency_2", b"second package")

        self.uploader.backfill_cutoff = None
        self.uploader._scan()
        self.uploader._drain_due()

        blobs = list((paths.upload_dir(self.controller.config) / "local-blobs").rglob("*.tar.zst"))
        self.assertEqual(len(blobs), 2, "both packages must be written, not deduplicated away")
        self.assertEqual({b.read_bytes() for b in blobs},
                         {b"first package", b"second package"})

    def test_missing_manifest_still_yields_a_unique_name(self):
        bare_one = self.batches / f"aod_bare_1{paths.PACKAGE_EXTENSION}"
        bare_two = self.batches / f"aod_bare_2{paths.PACKAGE_EXTENSION}"
        bare_one.write_bytes(b"one")
        bare_two.write_bytes(b"two")

        self.assertNotEqual(
            self.uploader._blob_name(bare_one), self.uploader._blob_name(bare_two)
        )


if __name__ == "__main__":
    unittest.main()
