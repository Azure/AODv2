"""Fail-closed checks embedded in the opt-in external-venv package hooks."""

import importlib
import importlib.metadata
import re
import sys
from pathlib import Path


def validate_runtime(requirements, minimum_python):
    errors = []
    if sys.version_info[:2] < tuple(minimum_python):
        errors.append(
            f"Python >= {'.'.join(map(str, minimum_python))} is required; "
            f"got {sys.version.split()[0]}"
        )
    if sys.version_info.releaselevel != "final":
        errors.append("a stable, final Python release is required")
    if sys.prefix == sys.base_prefix or not (Path(sys.prefix) / "pyvenv.cfg").is_file():
        errors.append("AOD_PYTHON must select a real virtual environment")
    managed = Path("/opt/aodv2").resolve()
    if any(
        resolved == managed or managed in resolved.parents
        for resolved in (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve())
    ):
        errors.append("the venv and base interpreter must live outside package-managed /opt/aodv2")
    if errors:
        for error in errors:
            print(f"aodv2 external-venv: {error}", file=sys.stderr)
        return 1

    for distribution, module, minimum in requirements:
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"missing distribution: {distribution}>={minimum}")
            continue
        match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:\.post\d+)?(?:\+[A-Za-z0-9.]+)?", version)
        if match is None:
            errors.append(f"{distribution}: unsupported or prerelease version {version!r}")
            continue
        have = tuple(int(part) for part in match[1].split("."))
        need = tuple(int(part) for part in minimum.split("."))
        width = max(len(have), len(need))
        if have + (0,) * (width - len(have)) < need + (0,) * (width - len(need)):
            errors.append(f"{distribution} {version} does not satisfy >= {minimum}")
            continue
        try:
            importlib.import_module(module)
        except (ImportError, OSError) as error:
            errors.append(f"cannot import {module}: {error}")
    if errors:
        for error in errors:
            print(f"aodv2 external-venv: {error}", file=sys.stderr)
        return 1
    print(f"aodv2 external-venv: validated {sys.executable} (Python {sys.version.split()[0]})")
    return 0
