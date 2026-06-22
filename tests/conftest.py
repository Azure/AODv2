"""Shared pytest fixtures for the AODv2 test suite.

Centralizes where tests write durable artifacts (CSV traces, plots, logs,
sparse fake-bundle directories) so they land in one predictable place.

Also exposes a couple of plain helpers used by multiple test files:
  * make_fake_controller -- minimal Controller stand-in for any
    component that reads .config.<section> and .stop_event.
  * make_batch -- create a sparse aod_*.tar.zst file with optional
    mtime backdating, used by both unit and soak tests.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# Repo root = parent of this tests/ directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUTPUT_ROOT = _REPO_ROOT / "tests" / "_output"


# --- Shared helpers ----------------------------------------------------------


def make_fake_controller(**config_sections) -> SimpleNamespace:
    """Build a minimal Controller stand-in for tests.

    Each kwarg becomes a section on `controller.config`:

        make_fake_controller(cleanup={"max_log_age_days": 1})
        # -> controller.config.cleanup == {"max_log_age_days": 1}

        make_fake_controller(cleanup={...}, anomalies={...})
        # -> controller.config.cleanup, controller.config.anomalies

    The returned object also has a real `threading.Event` at
    `.stop_event`. Components under test pull config sections via
    `.get()`, so plain dicts are sufficient.
    """
    return SimpleNamespace(
        config=SimpleNamespace(**config_sections),
        stop_event=threading.Event(),
    )


def make_batch(
    batches_dir: Path,
    name: str,
    size_bytes: int,
    age_seconds: float = 0.0,
) -> Path:
    """Create a sparse `aod_*.tar.zst`-style file under `batches_dir`.

    Uses sparse-file creation (seek + 1-byte write) so `stat().st_size`
    reports `size_bytes` while real disk usage stays ~4 KB. This lets
    the soak test mint 100 MB+ "files" cheaply; for the unit suite the
    sparse-vs-dense distinction is invisible since it only inspects
    stat() and `exists()`.

    Optionally backdates both atime and mtime by `age_seconds`.
    Creates `batches_dir` (with parents) if missing.
    """
    batches_dir.mkdir(parents=True, exist_ok=True)
    path = batches_dir / name
    with open(path, "wb") as f:
        if size_bytes > 0:
            f.seek(size_bytes - 1)
            f.write(b"\0")
    if age_seconds:
        mt = time.time() - age_seconds
        os.utime(path, (mt, mt))
    return path


# --- Output directory fixtures -----------------------------------------------


@pytest.fixture(scope="session")
def test_output_root() -> Path:
    """Session-scoped root for all test artifacts.

    Resolution order:
      1. $AODV2_TEST_OUTPUT_DIR if set (absolute or repo-relative path).
      2. <repo>/tests/_output/

    The directory is created if missing and is NOT cleaned up at the end
    of the session -- artifacts are kept for forensic review.
    """
    env = os.environ.get("AODV2_TEST_OUTPUT_DIR")
    root = Path(env).expanduser() if env else _DEFAULT_OUTPUT_ROOT
    if not root.is_absolute():
        root = (_REPO_ROOT / root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture(scope="session")
def test_output_run_dir(test_output_root: Path) -> Path:
    """Per-pytest-invocation subdirectory under test_output_root.

    Named with a wall-clock timestamp + pid so concurrent / repeated runs
    don't clobber each other. Tests should write under here.
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = test_output_root / f"run_{stamp}_pid{os.getpid()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir
