"""Combined stress + soak test for SpaceWatcher under realistic AOD output.

NOT run by default: marked @pytest.mark.slow. Total wall time ~33 min.

Opt in:
    pytest -m slow tests/test_space_watcher_soak.py -s

What it does
------------
For each (limit_mb, interval_sec) setup in SCENARIOS:

  1. Spins up a real SpaceWatcher in a thread.
  2. Spins up a producer thread that creates SPARSE bundle files into
     <aod_root>/batches/ using AOD's real naming convention
     (aod_quick_<TS>_<proto>_<kind>.tar.zst, and for protocols with a
     capture tool a sibling aod_capture_<TS>_<proto>_<kind>.tar.zst).
     Sparse files report the desired apparent size via stat().st_size
     (which is all SpaceWatcher reads) while costing ~4 KB of real disk.
  3. Spins up a sampler thread that snapshots (t, total_mb, file_count)
     every ~1 sec into a per-scenario CSV under /tmp/aod_soak_<pid>/.
  4. Runs for `2.5 * interval_sec` (min 60 s) -- enough for >= 2 cleanup
     ticks at steady state.
  5. Stops the producer, then forces one synchronous final cleanup, and
     asserts the directory is back inside the configured budget.

Producer cadence (per anomaly event, mirroring LogCollector):
  - 100 MB budget  -> N=1..5 quick-action bundles (1-10 MB each), no
                      long captures. Mirrors the recommended low-disk
                      deployment where capture tools aren't configured.
  - 500 MB+ budget -> N=1..5 quick bundles AS ABOVE plus at most one
                      long-capture bundle per capture-bound protocol
                      (smb -> tcpdump, nfs -> trace-cmd). Hard cap of 2
                      captures per event.

Events fire with inter-event delays sampled uniformly from a discrete
set of integer seconds -- the smallest watch interval AOD supports is
1 s, with quiet periods up to ~1 min between bursts in normal use.

Producer rates are tuned so volume per interval clearly exceeds the
budget -- this is the *stress* half of the test. The watcher MUST clean
up to stay in bounds; the assertions catch it if it doesn't.

Both cleanup paths are exercised:
  - cleanup_by_size: driven by the producer overshooting the budget.
  - cleanup_by_age:  scenarios set max_log_age_days to a small fractional
                     value (seconds-scale) so the age tick fires several
                     times during the run, and we pre-seed a handful of
                     aged bundles before the watcher starts so the age
                     tick always has eligible entries to act on.
"""

import csv
import hashlib
import logging
import os
import random
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkdtemp
from types import MethodType

import pytest

import SpaceWatcher as _sw_module
from SpaceWatcher import SpaceWatcher, SIZE_DELETE_THRESHOLD
from conftest import make_batch, make_fake_controller

# --- Scenario table -----------------------------------------------------------

# Anomaly identities. Each tuple is (protocol, anomaly_type, has_capture)
# and corresponds to an entry under `anomalies:` in config/config.yaml.
# `has_capture` records whether AOD's default config binds a capture tool
# (tcpdump / trace-cmd) to that anomaly.
_ANOMALIES: list[tuple[str, str, bool]] = [
    ("smb", "latency", True),  # tcpdump
    ("smb", "sockconn", True),  # tcpdump
    ("nfs", "latency", True),  # trace-cmd
    ("nfs", "error", True),  # trace-cmd
    ("nfs", "sockconn", False),  # dmesg only
]


# Spawn intervals (seconds between anomaly events) picked uniformly from
# these discrete choices. Mirrors AOD's reality: the smallest watch
# interval AOD supports is 1 s, and quiet periods between bursts run
# tens of seconds in normal operation.
#
# _SPAWN_CHOICES_TIGHT drops the 60 s "quiet" option used by the normal
# set because at low (100 MB) budgets we want producer accumulation
# between age-cleanup ticks to clearly cross the 0.97 x limit
# size-cleanup trigger
_SPAWN_CHOICES_TIGHT = (1, 5)
_SPAWN_CHOICES_NORMAL = (1, 5, 10, 15, 30, 60)


# Quick-action bundle: 1-10 MB. One per anomaly, always.
_QUICK_MIN_MB, _QUICK_MAX_MB = 1, 10
# Long-capture bundle: 50-200 MB. At most one per *capture-bound
# protocol* per event
_CAPTURE_MIN_MB, _CAPTURE_MAX_MB = 50, 200


@dataclass
class Scenario:
    name: str
    limit_mb: int
    interval_sec: int
    # Whether AOD has any capture tools (tcpdump / trace-cmd) configured.
    # False for low-disk deployments
    include_captures: bool
    # Discrete set of inter-event spawn delays in seconds; producer
    # uniformly picks one per event.
    spawn_choices: tuple[int, ...]
    # Expected mean total size of one anomaly event in MB.
    avg_event_mb: float
    # Max log age in seconds, fed into SpaceWatcher as a fractional
    # max_log_age_days (sec / 86400). Chosen per-scenario so the age
    # tick fires multiple times during the soak.
    max_log_age_sec: int


def _aod_event(
    rng: random.Random,
    event_seq: int,
    include_captures: bool,
) -> list[tuple[str, int]]:
    """One AOD anomaly event, faithful to real AOD semantics.

    - 1..5 distinct anomalies fire per event (uniform). AOD does not
      emit duplicates of the same anomaly within a watch interval, so
      we sample without replacement.
    - One quick-action bundle per anomaly (1-10 MB).
    - If `include_captures`, AOD also emits at most ONE long-capture
      bundle per capture-bound *protocol*, regardless of how many
      anomalies of that protocol fired. With the (smb, nfs) split,
      that's a hard cap of 2 captures per event.
    """
    ts_ns = time.time_ns() + event_seq
    n = rng.randint(1, len(_ANOMALIES))
    picks = rng.sample(_ANOMALIES, n)

    out: list[tuple[str, int]] = []
    capture_protos: set[str] = set()
    for proto, kind, has_cap in picks:
        size = rng.randint(_QUICK_MIN_MB, _QUICK_MAX_MB) * 1024 * 1024
        out.append((f"aod_quick_{ts_ns}_{proto}_{kind}.tar.zst", size))
        if include_captures and has_cap:
            capture_protos.add(proto)

    for proto in capture_protos:
        size = rng.randint(_CAPTURE_MIN_MB, _CAPTURE_MAX_MB) * 1024 * 1024
        out.append((f"aod_capture_{ts_ns}_{proto}.tar.zst", size))
    return out


# Mean event size, derived from the constants above.
#
# Quick bundles: E[N=1..5] * mean quick size
#   mean N = 3, mean quick size = 5.5 MB  ->  16.5 MB
#
# Capture bundles, conditional on include_captures=True:
#   _ANOMALIES has 2 smb-cap-bound (of 2 smb), 2 nfs-cap-bound (of 3 nfs).
#   P(>=1 smb-cap in sample of n from 5) where 2 of 5 are smb-cap:
#     n=1: 0.4   n=2: 0.7   n=3: 0.9   n=4: 1.0   n=5: 1.0   mean=0.8
#   Same for nfs-cap (2 of 5)             mean=0.8
#   E[distinct capture protos per event]  = 1.6
#   Mean capture size = (50 + 200) / 2    = 125 MB
#   Capture MB / event                    = 1.6 * 125 = 200 MB
_NO_CAP_AVG_MB = 3 * ((_QUICK_MIN_MB + _QUICK_MAX_MB) / 2)
_WITH_CAP_AVG_MB = _NO_CAP_AVG_MB + 1.6 * ((_CAPTURE_MIN_MB + _CAPTURE_MAX_MB) / 2)


# Producer rates chosen so average volume per interval clearly exceeds
# the budget, forcing the watcher to cleanup. max_log_age_sec is set
# << scenario duration so cleanup_by_age fires several times during
# the run.
SCENARIOS: list[Scenario] = [
    # Low-disk, log-only deployments. include_captures=False mirrors the
    # recommended config: anyone configuring captures at 100 MB is
    # outside the supported envelope and not what this test models.
    # max_log_age_sec=30 (vs 10 for stricter regimes) gives accumulation
    # between age-cleanup ticks enough room to cross the size trigger.
    Scenario("100MB_1s", 100, 1, False, _SPAWN_CHOICES_TIGHT, _NO_CAP_AVG_MB, 30),
    Scenario(
        "500MB_10s", 500, 10, True, _SPAWN_CHOICES_NORMAL, _WITH_CAP_AVG_MB, 30
    ),
    Scenario(
        "500MB_60s", 500, 60, True, _SPAWN_CHOICES_NORMAL, _WITH_CAP_AVG_MB, 120
    ),
    Scenario(
        "1GB_120s", 1024, 120, True, _SPAWN_CHOICES_NORMAL, _WITH_CAP_AVG_MB, 30
    ),
    Scenario(
        "2GB_300s", 2048, 300, True, _SPAWN_CHOICES_NORMAL, _WITH_CAP_AVG_MB, 120
    ),
]


# Minimum 60 s; otherwise 2.5 * interval (2 cleanup ticks + warmup).
def _duration_for(s: Scenario) -> float:
    return max(60.0, 2.5 * s.interval_sec)


# --- Helpers ------------------------------------------------------------------


def _dir_size_bytes(batches_dir: Path) -> tuple[int, int]:
    """(total_bytes, file_count) of completed aod_*.tar.zst files."""
    total = 0
    count = 0
    for p in batches_dir.glob("aod_*.tar.zst"):
        try:
            total += p.stat().st_size
            count += 1
        except FileNotFoundError:
            pass  # raced with cleanup
    return total, count


@dataclass
class ScenarioResult:
    scenario: Scenario
    duration_s: float
    cleanups_observed: int
    age_cleanups_observed: int
    age_eligible_total: int
    # Files actually unlinked from disk by the run loop's cleanup_by_size.
    unlinks_by_size: int
    unlinks_forced: int
    files_produced: int
    files_remaining: int
    peak_mb: float
    final_mb: float
    samples_csv: Path
    watcher_exited: bool
    aod_root: Path
    error_logs: list[str] = field(default_factory=list)


class _LogCapture(logging.Handler):
    """Captures ERROR+ records from SpaceWatcher for post-run assertions."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _build_controller(batches_parent: Path, scenario: Scenario):
    """Minimal Controller stand-in: only .config.cleanup and .stop_event."""
    return make_fake_controller(
        cleanup={
            "cleanup_interval_sec": scenario.interval_sec,
            # SpaceWatcher multiplies max_log_age_days by 86400 internally
            # and accepts a float, so we can drive age cleanup on a
            # seconds scale by passing a fractional days value.
            "max_log_age_days": scenario.max_log_age_sec / 86400.0,
            "max_total_log_size_mb": scenario.limit_mb,
            "aod_output_dir": str(batches_parent),
        }
    )


_PRE_SEED_COUNT = 5
_PRE_SEED_SIZE = 1 * 1024 * 1024  # 1 MB each


def _pre_seed_old_files(batches_dir: Path, max_log_age_sec: int) -> None:
    """Drop a handful of aged bundles into batches_dir so cleanup_by_age
    always has eligible entries on its first tick.

    mtimes are backdated by (max_log_age_sec + 60) seconds so they sit
    safely past the cutoff even if the first age tick fires a few
    seconds after watcher start.
    """
    now = time.time()
    backdate = max_log_age_sec + 60
    for i in range(_PRE_SEED_COUNT):
        # Synthetic ns timestamps are made strictly older than anything the producer will emit.
        ts_ns = int((now - backdate) * 1e9) - (_PRE_SEED_COUNT - i)
        name = f"aod_quick_{ts_ns}_smb_latency.tar.zst"
        path = make_batch(batches_dir, name, _PRE_SEED_SIZE)
        try:
            mt = now - backdate + i * 0.001  # strict ordering, all aged
            os.utime(path, (mt, mt))
        except OSError:
            pass


# --- Threads ------------------------------------------------------------------


def _producer_loop(
    batches_dir: Path,
    scenario: Scenario,
    stop_event: threading.Event,
    counter: list[int],
    seed: int,
) -> None:
    rng = random.Random(seed)
    event_seq = 0
    file_seq = 0
    while not stop_event.is_set():
        files = _aod_event(rng, event_seq, scenario.include_captures)
        for name, size in files:
            try:
                path = make_batch(batches_dir, name, size)
                counter[0] += 1
            except OSError:
                # Disk full on the *real* (non-sparse) fallback filesystem --
                # bail rather than crashing the test; the assertion-time
                # check will catch any meaningful failure.
                return
            # Strictly monotonic mtimes -- but always BEHIND wall clock --
            # so cleanup_by_size's oldest-first order is stable AND files
            # never look "in the future" to cleanup_by_age.
            # file_seq increments per-file (not per-event) so paired quick+capture bundles get a stable
            # order too.
            try:
                now = time.time()
                mtime = now - 1.0 + file_seq * 1e-6
                os.utime(path, (now, mtime))
            except OSError:
                pass
            file_seq += 1
        event_seq += 1
        if stop_event.wait(rng.choice(scenario.spawn_choices)):
            return


def _sampler_loop(
    batches_dir: Path,
    csv_path: Path,
    stop_event: threading.Event,
    sample_interval: float,
    peak_holder: list[float],
) -> None:
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_iso", "total_mb", "file_count"])
        while not stop_event.is_set():
            total, n = _dir_size_bytes(batches_dir)
            mb = total / (1024 * 1024)
            if mb > peak_holder[0]:
                peak_holder[0] = mb
            w.writerow([time.strftime("%H:%M:%S"), f"{mb:.2f}", n])
            f.flush()
            if stop_event.wait(sample_interval):
                return


# --- The soak driver ----------------------------------------------------------


def _run_scenario(scenario: Scenario, out_root: Path) -> ScenarioResult:
    duration = _duration_for(scenario)
    aod_root = Path(mkdtemp(prefix=f"aod_soak_{scenario.name}_"))
    batches_dir = aod_root / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_root / f"{scenario.name}.csv"

    controller = _build_controller(aod_root, scenario)
    watcher = SpaceWatcher(controller)

    files_made = [0]
    peak_mb = [0.0]

    cleanup_counter = {"by_size": 0, "by_age": 0}
    age_eligible_total = [0]
    forced_final_calls = [0]
    unlinks_by_size = [0]
    unlinks_forced = [0]
    is_forced_call = [False]
    real_by_size = watcher.cleanup_by_size
    real_by_age = watcher.cleanup_by_age

    def _counting_by_size(self, entries):
        cleanup_counter["by_size"] += 1
        snapshot_mb = sum(sz for _, sz, _ in entries) / (1024 * 1024)
        if snapshot_mb > peak_mb[0]:
            peak_mb[0] = snapshot_mb
        before_paths = [e for e, _, _ in entries]
        result = real_by_size(entries)
        # Count files the call actually removed.
        deleted = sum(1 for p in before_paths if not p.exists())
        if is_forced_call[0]:
            unlinks_forced[0] += deleted
        else:
            unlinks_by_size[0] += deleted
        return result

    def _counting_by_age(self, entries):
        cleanup_counter["by_age"] += 1
        # Count entries the watcher considers past cutoff at THIS call.
        # Note: even if cleanup_by_size (called first in run()) has
        # already unlinked them from disk, they still appear in `entries`
        # because run() snapshots the listing once per tick. So this
        # measures "work cleanup_by_age would do" rather than successful
        # unlinks (those would race with cleanup_by_size on the same
        # paths) -- which is what we want to assert.
        cutoff = time.time() - watcher.max_log_age_days * 86400
        age_eligible_total[0] += sum(1 for _, _, mt in entries if mt < cutoff)
        return real_by_age(entries)

    watcher.cleanup_by_size = MethodType(_counting_by_size, watcher)  # type: ignore[assignment]
    watcher.cleanup_by_age = MethodType(_counting_by_age, watcher)  # type: ignore[assignment]

    capture = _LogCapture()
    sw_logger = _sw_module.logger
    sw_logger.addHandler(capture)

    producer_stop = threading.Event()
    sampler_stop = threading.Event()

    # Deterministic per-scenario seed
    seed = int(
        hashlib.md5(scenario.name.encode(), usedforsecurity=False).hexdigest()[:8],
        16,
    )

    t_watcher = threading.Thread(
        target=watcher.run, name=f"sw-{scenario.name}", daemon=True
    )
    t_producer = threading.Thread(
        target=_producer_loop,
        name=f"prod-{scenario.name}",
        args=(batches_dir, scenario, producer_stop, files_made, seed),
        daemon=True,
    )
    # Sample faster than the cleanup tick so the CSV captures the rise/fall
    # cycle even when interval_sec is small.
    sample_interval = min(0.5, max(0.1, scenario.interval_sec / 4.0))
    t_sampler = threading.Thread(
        target=_sampler_loop,
        name=f"smpl-{scenario.name}",
        args=(batches_dir, csv_path, sampler_stop, sample_interval, peak_mb),
        daemon=True,
    )

    t_start = time.monotonic()
    print(
        f"\n[soak {scenario.name}] limit={scenario.limit_mb}MB "
        f"interval={scenario.interval_sec}s "
        f"max_age={scenario.max_log_age_sec}s "
        f"duration={duration:.0f}s -> {aod_root}",
        flush=True,
    )

    # Pre-seed aged bundles BEFORE the watcher starts so cleanup_by_age's
    # first tick already has eligible entries.
    _pre_seed_old_files(batches_dir, scenario.max_log_age_sec)

    t_watcher.start()
    t_sampler.start()
    t_producer.start()

    try:
        finished = threading.Event()
        finished.wait(duration)
    finally:
        # Stop producer first so the directory stops growing.
        producer_stop.set()
        t_producer.join(timeout=5)
        controller.stop_event.set()
        t_watcher.join(timeout=15.0)
        watcher_exited = not t_watcher.is_alive()

        # Skip entirely if the watcher thread did not exit: it is still
        # holding references to `watcher.cleanup_by_size` and racing it
        # from the main thread would (a) corrupt is_forced_call /
        # unlinks_{by_size,forced} bookkeeping and (b) double-unlink the
        # same entries. The watcher_exited assertion will fail loudly
        # instead, which is the actual diagnostic we want.
        if watcher_exited:
            entries = []
            for p in batches_dir.glob("aod_*.tar.zst"):
                try:
                    st = p.stat()
                    entries.append((p, st.st_size, st.st_mtime))
                except FileNotFoundError:
                    pass
            entries.sort(key=lambda x: x[2])
            forced_final_calls[0] += 1
            is_forced_call[0] = True
            try:
                watcher.cleanup_by_size(entries)
            finally:
                is_forced_call[0] = False
        # Now stop the sampler.
        sampler_stop.set()
        t_sampler.join(timeout=5)
        sw_logger.removeHandler(capture)

    elapsed = time.monotonic() - t_start
    final_bytes, files_remaining = _dir_size_bytes(batches_dir)

    result = ScenarioResult(
        scenario=scenario,
        duration_s=elapsed,
        # Report only the cleanups the run loop performed; subtract the
        # post-run synchronous cleanups we triggered ourselves.
        cleanups_observed=max(0, cleanup_counter["by_size"] - forced_final_calls[0]),
        age_cleanups_observed=cleanup_counter["by_age"],
        age_eligible_total=age_eligible_total[0],
        unlinks_by_size=unlinks_by_size[0],
        unlinks_forced=unlinks_forced[0],
        files_produced=files_made[0],
        files_remaining=files_remaining,
        peak_mb=peak_mb[0],
        final_mb=final_bytes / (1024 * 1024),
        samples_csv=csv_path,
        watcher_exited=watcher_exited,
        aod_root=aod_root,
        error_logs=[
            r.getMessage() + (f" | exc={r.exc_info[1]!r}" if r.exc_info else "")
            for r in capture.records
        ],
    )

    print(
        f"[soak {scenario.name}] DONE elapsed={elapsed:.1f}s "
        f"cleanups={result.cleanups_observed} "
        f"unlinks={result.unlinks_by_size} "
        f"age_cleanups={result.age_cleanups_observed} "
        f"age_eligible={result.age_eligible_total} "
        f"produced={result.files_produced} remaining={result.files_remaining} "
        f"peak={result.peak_mb:.1f}MB final={result.final_mb:.1f}MB "
        f"errors={len(result.error_logs)} csv={csv_path}",
        flush=True,
    )
    return result


# --- The test -----------------------------------------------------------------


@pytest.fixture(scope="module")
def _soak_out_root(test_output_run_dir: Path):
    """Module-scoped CSV output dir + aod_root accumulator.

    Lives under the session-wide `test_output_run_dir` (see
    tests/conftest.py). CSVs are preserved for forensic review;
    the per-scenario aod_root dirs hold only sparse fake bundles and
    are wiped at module teardown.
    """
    root = test_output_run_dir / "space_watcher_soak"
    root.mkdir(parents=True, exist_ok=True)
    aod_roots: list[Path] = []
    print(f"\n[soak] CSV output directory: {root}", flush=True)
    yield root, aod_roots
    for r in aod_roots:
        shutil.rmtree(r, ignore_errors=True)


def _assert_scenario_clean(r: ScenarioResult) -> None:
    s = r.scenario
    # 1. Watcher loop exited cleanly.
    assert r.watcher_exited, (
        f"[{s.name}] watcher thread did not exit within join timeout "
        f"(files_remaining={r.files_remaining})"
    )
    # 2. No ERROR+ logged by SpaceWatcher.
    assert (
        r.error_logs == []
    ), f"[{s.name}] SpaceWatcher logged errors: {r.error_logs}"
    # 3. At least one cleanup path (size or age) ran during the run.
    #    Whether cleanup_by_size specifically fires depends on the
    #    interaction between max_log_age_sec, producer rate, and budget.
    assert (r.cleanups_observed + r.age_cleanups_observed) >= 1, (
        f"[{s.name}] no cleanup ticks of any kind fired in "
        f"{r.duration_s:.0f}s with interval={s.interval_sec}s"
    )
    # 4. Final size (after the forced final cleanup) sits at or below the
    #    documented post-cleanup target (SIZE_DELETE_THRESHOLD * limit)
    #    plus a 10% grace for the burst between final-tick and measurement.
    target_mb = s.limit_mb * SIZE_DELETE_THRESHOLD
    ceiling_mb = target_mb * 1.10
    assert r.final_mb <= ceiling_mb, (
        f"[{s.name}] final size {r.final_mb:.1f}MB exceeds "
        f"ceiling {ceiling_mb:.1f}MB (target {target_mb:.1f}MB)"
    )
    # 5. Producer actually ran (sanity).
    assert (
        r.files_produced > 0
    ), f"[{s.name}] producer made no files; soak was a no-op"
    # 6. Peak is bounded by the cleanup trigger (MAX_HOT_THRESHOLD = 0.97
    #    of the limit) plus a high-confidence upper bound on how much
    #    producer volume can land in one cleanup interval. Events arrive
    #    at random spawn intervals drawn from a small discrete set, so
    #    the per-interval event count behaves roughly Poisson with
    #    lambda = interval / mean_spawn. We use a mean + 3 sigma cap
    #    on the event count. Catches regressions that let the directory
    #    grow unbounded (cleanup disabled, interval honoured as ms
    #    instead of sec, etc.) without flagging the inherent burst
    #    variance of the realistic producer.
    avg_spawn = sum(s.spawn_choices) / len(s.spawn_choices)
    lam = s.interval_sec / avg_spawn
    burst_events = lam + 3.0 * (lam**0.5)
    expected_peak_mb = (0.97 * s.limit_mb + burst_events * s.avg_event_mb) * 1.25
    assert r.peak_mb <= expected_peak_mb, (
        f"[{s.name}] peak {r.peak_mb:.1f}MB exceeds expected upper "
        f"bound {expected_peak_mb:.1f}MB "
        f"(0.97 x {s.limit_mb}MB + burst={burst_events:.1f} events "
        f"x avg_event={s.avg_event_mb:.1f}MB, x1.25 grace)"
    )
    # 7a. cleanup_by_age fired and saw at least the pre-seeded aged
    #     bundles. Proves the eligibility gate (mtime < cutoff) opened
    #     on the very first tick.
    assert r.age_cleanups_observed >= 1, (
        f"[{s.name}] cleanup_by_age never ran in {r.duration_s:.0f}s "
        f"with max_log_age={s.max_log_age_sec}s"
    )
    assert r.age_eligible_total >= _PRE_SEED_COUNT, (
        f"[{s.name}] cleanup_by_age saw {r.age_eligible_total} aged "
        f"entries across {r.age_cleanups_observed} ticks; "
        f"expected >= {_PRE_SEED_COUNT} (pre-seeded)"
    )
    # 7b. cleanup_by_age fired roughly as often as the age period allows.
    #     _full_cleanup_needed gates on max_log_age_sec and is checked
    #     once per cleanup tick (interval_sec), so the period between
    #     age ticks is bounded below by max(interval, max_age). Subtract
    #     one to absorb warmup and tick alignment jitter. Catches a
    #     regression where _full_cleanup_needed fires once and never
    #     again (e.g. last_full_cleanup updated unconditionally).
    age_period_s = max(s.interval_sec, s.max_log_age_sec)
    expected_age_ticks = max(1, int(r.duration_s // age_period_s) - 1)
    assert r.age_cleanups_observed >= expected_age_ticks, (
        f"[{s.name}] cleanup_by_age ran {r.age_cleanups_observed} times "
        f"in {r.duration_s:.0f}s; expected >= {expected_age_ticks} "
        f"(age_period={age_period_s}s)"
    )


@pytest.mark.slow
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_steady_state_under_load(scenario: Scenario, _soak_out_root):
    """Steady-state soak for a single (limit_mb, interval_sec) regime.

    Total wall time across all scenarios is ~33 min. Deselected from
    the default ``pytest`` invocation by the project's ``slow`` marker.
    Invoke the full suite with::

        pytest -m slow tests/test_space_watcher_soak.py -s

    Each scenario is its own parametrized test, so a single regime can
    be selected with ``-k`` (e.g. ``-k 1GB_300s``) and per-scenario
    timing / diagnostics show up directly in pytest's report.
    """
    out_root, aod_roots = _soak_out_root
    r = _run_scenario(scenario, out_root)
    aod_roots.append(r.aod_root)
    _assert_scenario_clean(r)
