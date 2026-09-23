"""Single place that resolves AOD output directories.

Every component must resolve paths through here. Previously SpaceWatcher read
`aod_output_dir` out of the `cleanup` section, where it does not exist, and
silently fell back to the default.
"""

import os
from pathlib import Path

DEFAULT_OUTPUT_DIR = "/var/log/aod"
BATCHES_SUBDIR = "batches"
UPLOAD_SUBDIR = "upload"
PACKAGE_EXTENSION = ".tar.zst"
PARTIAL_EXTENSION = ".part"


def output_dir(config) -> Path:
    """Root AOD output directory, from the top-level `aod_output_dir` key."""
    return Path(getattr(config, "aod_output_dir", None) or DEFAULT_OUTPUT_DIR)


def batches_dir(config) -> Path:
    """Directory holding completed diagnostic packages."""
    return output_dir(config) / BATCHES_SUBDIR


def upload_dir(config) -> Path:
    """Directory holding upload state and status.

    Deliberately a sibling of `batches/`: SpaceWatcher globs `aod_*` there and
    deletes matches outright, which would destroy upload state kept alongside
    the packages.
    """
    return output_dir(config) / UPLOAD_SUBDIR


def ensure_dir(path: Path) -> Path:
    """Create a directory (and parents) if missing, returning it."""
    os.makedirs(path, exist_ok=True)
    return path


# The eBPF helpers sit beside the source in a checkout, but packaging installs
# them outside the Python module tree.
_TOOL_SEARCH_DIRS = (
    os.environ.get("AOD_TOOL_DIR"),
    str(Path(__file__).resolve().parent.parent / "bin"),
    "/usr/libexec/linux_diagnostics",
    "/usr/lib/linux_diagnostics",
)


def find_tool(name: str):
    """Absolute path to a bundled helper binary, or None if it is not installed."""
    for directory in _TOOL_SEARCH_DIRS:
        if not directory:
            continue
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None
