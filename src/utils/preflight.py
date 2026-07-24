"""Runtime dependency preflight for the AODv2 daemon.

Verifies that the running interpreter meets utils._deps.MIN_PYTHON and
that every Python distribution listed in utils._deps.REQUIRED is
installed and meets its version specifier from pyproject.toml.
"""

import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from utils._deps import REQUIRED

try:
    from utils._deps import MIN_PYTHON
except ImportError:
    # Fresh checkout that never ran `make deps` -- fall back to pyproject floor.
    MIN_PYTHON = (3, 11)


_INSTALL_HINT = (
    "Install the deps into the interpreter AOD_PYTHON points at:\n"
    "  - System-wide: dnf/apt install python3-numpy "
    "python3-pyyaml (python3-yaml on Debian) python3-zstandard\n"
    "  - Venv:        pip install numpy PyYAML zstandard, then set\n"
    "                 AOD_PYTHON=/path/to/venv/bin/python "
    "in /etc/aodv2/aodv2.env"
)

def _parse_ver(s: str) -> tuple[int, ...]:
    """Parse a version string into a tuple that compares correctly for
    the >= / > / == checks we do. Non-numeric suffixes on a segment
    (e.g. '1.0rc1') are truncated to their numeric prefix."""
    parts: list[int] = []
    for seg in s.split("."):
        n = ""
        for ch in seg:
            if ch.isdigit():
                n += ch
            else:
                break
        parts.append(int(n) if n else 0)
    return tuple(parts)


def _satisfies(have: str, spec: str) -> bool:
    spec = spec.strip()
    if spec.startswith(">="):
        return _parse_ver(have) >= _parse_ver(spec[2:].strip())
    if spec.startswith(">"):
        return _parse_ver(have) > _parse_ver(spec[1:].strip())
    if spec.startswith("=="):
        return _parse_ver(have) == _parse_ver(spec[2:].strip())
    if spec.startswith("<="):
        return _parse_ver(have) <= _parse_ver(spec[2:].strip())
    if spec.startswith("<"):
        return _parse_ver(have) < _parse_ver(spec[1:].strip())
    # Unknown / empty spec: assume satisfied rather than raise a false alarm.
    return True


def verify_runtime_deps() -> None:
    """Exits the process with status 1 on a too-old interpreter or any
    missing / version-mismatched dep. Returns normally otherwise."""

    have_py = sys.version_info[:2]
    if have_py < MIN_PYTHON:
        need = ".".join(str(x) for x in MIN_PYTHON)
        got = ".".join(str(x) for x in have_py)
        print(
            f"aodv2: interpreter too old -- need Python >= {need}, got {got}\n"
            f"  running under: {sys.executable}\n"
            f"Point AOD_PYTHON at a newer interpreter in "
            f"/etc/aodv2/aodv2.env, e.g.\n"
            f"  AOD_PYTHON=/usr/bin/python3.11",
            file=sys.stderr,
        )
        sys.exit(1)

    if not REQUIRED:
        # Fresh checkout that never ran `make deps` -- nothing to enforce.
        return

    missing: list[str] = []
    wrong: list[str] = []
    for name, spec in REQUIRED:
        try:
            have = _pkg_version(name)
        except PackageNotFoundError:
            missing.append(f"{name}{spec}")
            continue
        if spec and not _satisfies(have, spec):
            wrong.append(f"{name} {have} (need {spec})")

    if not (missing or wrong):
        return

    print("aodv2: dependency check failed", file=sys.stderr)
    for m in missing:
        print(f"  missing: {m}", file=sys.stderr)
    for w in wrong:
        print(f"  bad version: {w}", file=sys.stderr)
    print(_INSTALL_HINT, file=sys.stderr)
    sys.exit(1)
