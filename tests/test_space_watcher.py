"""Unit tests for SpaceWatcher.

SpaceWatcher only touches:
  - controller.config.cleanup
  - controller.stop_event

So these tests use a fake Controller (see conftest.make_fake_controller)
and a tmp aod_output_dir instead of constructing a real Controller. The
cleanup values used here are deliberately chosen to differ from
config/config.yaml so the suite does not silently drift with the
production config.
"""

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import SpaceWatcher as space_watcher_mod
from SpaceWatcher import SpaceWatcher, MAX_HOT_THRESHOLD, SIZE_DELETE_THRESHOLD
from conftest import make_batch, make_fake_controller


def _make_controller(tmpdir: Path, **overrides) -> SimpleNamespace:
    """SpaceWatcher-specific wrapper around `make_fake_controller`.

    Defaults are chosen to differ from `config/config.yaml` so the suite
    does not silently drift with the production config.
    """
    cleanup = {
        "cleanup_interval_sec": 1,
        "max_log_age_days": 1,
        "max_total_log_size_mb": 1,
        "aod_output_dir": str(tmpdir),
    }
    cleanup.update(overrides)
    return make_fake_controller(cleanup=cleanup)


class SpaceWatcherInitTests(unittest.TestCase):
    def test_reads_values_from_cleanup_config(self):
        with TemporaryDirectory() as td:
            ctrl = _make_controller(Path(td))
            w = SpaceWatcher(ctrl)
            self.assertEqual(w.cleanup_interval, 1)
            self.assertEqual(w.max_log_age_days, 1)
            self.assertEqual(w.max_total_log_size_mb, 1)
            self.assertEqual(w.aod_output_dir, td)
            self.assertEqual(w.batches_dir, Path(td) / "batches")
            self.assertEqual(w.compression_extension, ".tar.zst")

    def test_falls_back_to_defaults_when_keys_missing(self):
        ctrl = make_fake_controller(cleanup={})
        w = SpaceWatcher(ctrl)
        self.assertEqual(w.cleanup_interval, 600)
        self.assertEqual(w.max_log_age_days, 2)
        self.assertEqual(w.max_total_log_size_mb, 500)
        self.assertEqual(w.aod_output_dir, "/var/log/aod")

    def test_initial_last_full_cleanup_triggers_immediately(self):
        with TemporaryDirectory() as td:
            ctrl = _make_controller(Path(td))
            w = SpaceWatcher(ctrl)
            # Constructor backdates last_full_cleanup by max_log_age_days, so
            # the first call to _full_cleanup_needed() should return True.
            self.assertTrue(w._full_cleanup_needed())


class GetCompressedFileStatTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.batches = self.tmp / "batches"
        self.ctrl = _make_controller(self.tmp)
        self.w = SpaceWatcher(self.ctrl)

    def tearDown(self):
        self._td.cleanup()

    def test_returns_empty_when_dir_missing(self):
        # batches dir was never created
        result = self.w._get_compressed_file_stat(self.batches, ".tar.zst")
        self.assertEqual(result, [])

    def test_picks_only_aod_prefixed_zst_files(self):
        make_batch(self.batches, "aod_001.tar.zst", 10)
        make_batch(self.batches, "aod_002.tar.zst", 20)
        # Non-matching: wrong prefix, wrong extension, partial upload.
        (self.batches / "other_001.tar.zst").write_bytes(b"x")
        (self.batches / "aod_003.tar.gz").write_bytes(b"x")
        (self.batches / "aod_004.tar.zst.tmp").write_bytes(b"x")
        result = self.w._get_compressed_file_stat(self.batches, ".tar.zst")
        names = sorted(p.name for p, _, _ in result)
        self.assertEqual(names, ["aod_001.tar.zst", "aod_002.tar.zst"])

    def test_sorted_by_mtime_ascending(self):
        make_batch(self.batches, "aod_new.tar.zst", 10, age_seconds=10)
        make_batch(self.batches, "aod_old.tar.zst", 10, age_seconds=1000)
        make_batch(self.batches, "aod_mid.tar.zst", 10, age_seconds=100)
        result = self.w._get_compressed_file_stat(self.batches, ".tar.zst")
        ordered = [p.name for p, _, _ in result]
        self.assertEqual(
            ordered, ["aod_old.tar.zst", "aod_mid.tar.zst", "aod_new.tar.zst"]
        )


class CheckSpaceTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.ctrl = _make_controller(self.tmp)
        self.w = SpaceWatcher(self.ctrl)
        self.limit_bytes = self.w.max_total_log_size_mb * 1024 * 1024

    def tearDown(self):
        self._td.cleanup()

    def _entries_totalling(self, total: int):
        return [(Path("dummy"), total, 0.0)] if total else []

    def test_empty_returns_false(self):
        self.assertFalse(self.w._check_space([]))

    def test_below_hot_threshold_returns_false(self):
        # Half the limit -> well under MAX_HOT_THRESHOLD (0.97)
        entries = self._entries_totalling(self.limit_bytes // 2)
        self.assertFalse(self.w._check_space(entries))

    def test_just_under_hot_threshold_returns_false(self):
        entries = self._entries_totalling(
            int(self.limit_bytes * MAX_HOT_THRESHOLD) - 1
        )
        self.assertFalse(self.w._check_space(entries))

    def test_above_hot_threshold_returns_true(self):
        entries = self._entries_totalling(
            int(self.limit_bytes * MAX_HOT_THRESHOLD) + 1024
        )
        self.assertTrue(self.w._check_space(entries))


class FullCleanupNeededTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.ctrl = _make_controller(Path(self._td.name))
        self.w = SpaceWatcher(self.ctrl)

    def tearDown(self):
        self._td.cleanup()

    def test_first_call_returns_true_and_updates_stamp(self):
        before = self.w.last_full_cleanup
        self.assertTrue(self.w._full_cleanup_needed())
        self.assertGreater(self.w.last_full_cleanup, before)

    def test_second_call_within_window_returns_false(self):
        self.assertTrue(self.w._full_cleanup_needed())
        self.assertFalse(self.w._full_cleanup_needed())

    def test_returns_true_again_once_window_elapses(self):
        self.assertTrue(self.w._full_cleanup_needed())
        # Pretend the previous run happened more than max_log_age_days ago.
        self.w.last_full_cleanup = time.time() - (
            self.w.max_log_age_days * 24 * 60 * 60 + 1
        )
        self.assertTrue(self.w._full_cleanup_needed())


class CleanupByAgeTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.batches = self.tmp / "batches"
        self.ctrl = _make_controller(self.tmp)
        self.w = SpaceWatcher(self.ctrl)
        self.day_sec = 24 * 60 * 60

    def tearDown(self):
        self._td.cleanup()

    def test_empty_is_noop(self):
        # Should not raise even with no entries.
        self.w.cleanup_by_age([])

    def test_deletes_only_files_older_than_max_age(self):
        # max_log_age_days = 1
        old = make_batch(
            self.batches, "aod_old.tar.zst", 10, age_seconds=2 * self.day_sec
        )
        fresh = make_batch(self.batches, "aod_fresh.tar.zst", 10, age_seconds=60)
        entries = self.w._get_compressed_file_stat(self.batches, ".tar.zst")
        self.w.cleanup_by_age(entries)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_survives_missing_file_during_unlink(self):
        # File is in entries_data but unlink will fail.
        make_batch(self.batches, "aod_a.tar.zst", 10, age_seconds=2 * self.day_sec)
        entries = self.w._get_compressed_file_stat(self.batches, ".tar.zst")
        # Race: file vanishes before we delete it.
        with mock.patch.object(
            Path, "unlink", side_effect=FileNotFoundError("gone")
        ):
            # Must not raise.
            self.w.cleanup_by_age(entries)


class CleanupBySizeTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.batches = self.tmp / "batches"
        self.ctrl = _make_controller(self.tmp)
        self.w = SpaceWatcher(self.ctrl)
        # 1 MB limit -> 0.85 MB target after pruning
        self.limit_bytes = self.w.max_total_log_size_mb * 1024 * 1024
        self.target = int(self.limit_bytes * SIZE_DELETE_THRESHOLD)

    def tearDown(self):
        self._td.cleanup()

    def test_empty_is_noop(self):
        self.w.cleanup_by_size([])

    def test_no_delete_when_under_target(self):
        f = make_batch(self.batches, "aod_a.tar.zst", 1024)
        entries = self.w._get_compressed_file_stat(self.batches, ".tar.zst")
        self.w.cleanup_by_size(entries)
        self.assertTrue(f.exists())

    def test_deletes_oldest_first_until_under_target(self):
        # Three ~400 KB files = 1.2 MB total > 1 MB limit; target is 0.85 MB,
        # so we expect the oldest file to be deleted (two ~400 KB remain).
        size = 400 * 1024
        old = make_batch(self.batches, "aod_old.tar.zst", size, age_seconds=3000)
        mid = make_batch(self.batches, "aod_mid.tar.zst", size, age_seconds=2000)
        new = make_batch(self.batches, "aod_new.tar.zst", size, age_seconds=1000)
        entries = self.w._get_compressed_file_stat(self.batches, ".tar.zst")
        self.w.cleanup_by_size(entries)
        self.assertFalse(old.exists())
        self.assertTrue(mid.exists())
        self.assertTrue(new.exists())

    def test_continues_after_unlink_failure(self):
        size = 400 * 1024
        a = make_batch(self.batches, "aod_a.tar.zst", size, age_seconds=3000)
        b = make_batch(self.batches, "aod_b.tar.zst", size, age_seconds=2000)
        c = make_batch(self.batches, "aod_c.tar.zst", size, age_seconds=1000)
        entries = self.w._get_compressed_file_stat(self.batches, ".tar.zst")

        real_unlink = Path.unlink

        def flaky_unlink(self, *args, **kwargs):
            if self.name == "aod_a.tar.zst":
                raise PermissionError("denied")
            return real_unlink(self, *args, **kwargs)

        with mock.patch.object(Path, "unlink", flaky_unlink):
            self.w.cleanup_by_size(entries)

        # 'a' could not be removed; loop should have moved on to 'b' to keep
        # freeing space toward the target.
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())
        self.assertTrue(c.exists())


class RunLoopTests(unittest.TestCase):
    def test_run_exits_on_stop_event(self):
        with TemporaryDirectory() as td:
            ctrl = _make_controller(Path(td))
            w = SpaceWatcher(ctrl)
            # Pre-arm the stop_event so the first wait() returns immediately.
            ctrl.stop_event.set()
            t0 = time.monotonic()
            w.run()
            elapsed = time.monotonic() - t0
            # Should return almost instantly, well under cleanup_interval (1s).
            self.assertLess(elapsed, 0.5)

    def test_run_invokes_cleanups_when_warranted(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            ctrl = _make_controller(tmp)
            w = SpaceWatcher(ctrl)
            # Populate enough data to cross the hot threshold AND have one
            # aged-out file.
            size = 400 * 1024
            make_batch(
                tmp / "batches", "aod_old.tar.zst", size, age_seconds=3 * 86400
            )
            make_batch(tmp / "batches", "aod_mid.tar.zst", size, age_seconds=2000)
            make_batch(tmp / "batches", "aod_new.tar.zst", size, age_seconds=1000)

            with (
                mock.patch.object(
                    SpaceWatcher, "cleanup_by_size", autospec=True
                ) as m_size,
                mock.patch.object(
                    SpaceWatcher, "cleanup_by_age", autospec=True
                ) as m_age,
            ):
                # Stop after one iteration via a fake wait().
                ctrl.stop_event = mock.Mock()
                ctrl.stop_event.is_set.side_effect = [False, True]
                ctrl.stop_event.wait.return_value = True
                w.controller = ctrl
                w.run()

            self.assertTrue(m_size.called)
            self.assertTrue(m_age.called)


class ModuleConstantsTests(unittest.TestCase):
    def test_thresholds_are_sane(self):
        self.assertGreater(space_watcher_mod.MAX_HOT_THRESHOLD, 0.9)
        self.assertLess(space_watcher_mod.MAX_HOT_THRESHOLD, 1.0)
        self.assertGreater(space_watcher_mod.SIZE_DELETE_THRESHOLD, 0.5)
        self.assertLess(
            space_watcher_mod.SIZE_DELETE_THRESHOLD,
            space_watcher_mod.MAX_HOT_THRESHOLD,
        )


if __name__ == "__main__":
    unittest.main()
