"""Builds the manifest that accompanies every diagnostic package.

Written twice: once inside the archive so it is self-describing, and once
beside it so a consumer can identify a package without downloading and
decompressing it. The in-archive copy has no `package` section, since an
archive cannot contain its own checksum.
"""

import hashlib
import json
import logging
import os
import platform
from pathlib import Path

logger = logging.getLogger(__name__)

AOD_VERSION = "2.0.0"
MANIFEST_SCHEMA_VERSION = 1

MANIFEST_FILENAME = "manifest.json"
MANIFEST_EXTENSION = ".manifest.json"
_NETWORK_FSTYPES = {"cifs", "smb3", "nfs", "nfs4"}


def _distro() -> str:
    try:
        fields = {}
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                fields[key] = value.strip().strip('"')
        return f"{fields.get('ID', 'unknown')} {fields.get('VERSION_ID', '')}".strip()
    except (FileNotFoundError, PermissionError, OSError):
        return "unknown"


def _mounts() -> list[dict]:
    """Azure Files-relevant mounts only; local filesystems are not reported."""
    found = []
    try:
        for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[2] not in _NETWORK_FSTYPES:
                continue
            device, mountpoint, fstype = parts[0], parts[1], parts[2]
            server, share = _split_device(device, fstype)
            found.append({
                "type": fstype,
                "server": server,
                "share": share,
                "mountpoint": mountpoint,
            })
    except (FileNotFoundError, PermissionError, OSError) as e:
        logger.debug("Could not read /proc/mounts: %s", e)
    return found


def _split_device(device: str, fstype: str) -> tuple[str, str]:
    if fstype in ("cifs", "smb3"):
        trimmed = device.lstrip("/")
        server, _, share = trimmed.partition("/")
    else:
        server, _, share = device.partition(":")
        share = share.lstrip("/")
    return server, share


def build_manifest(
    *,
    host_id: str,
    anomaly_type: str,
    anomaly_tool: str,
    collectors: list,
    started_at: str,
    ended_at: str,
) -> dict:
    """Manifest without the `package` section; see `finalize_manifest`."""
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "aod_version": AOD_VERSION,
        "host_id": host_id,
        "hostname": platform.node(),
        "anomaly": {"type": anomaly_type, "tool": anomaly_tool},
        "collection": {"started_at": started_at, "ended_at": ended_at},
        "collectors": [c.to_dict() for c in collectors],
        "environment": {
            "kernel": platform.release(),
            "distro": _distro(),
            "arch": platform.machine(),
        },
        "mounts": _mounts(),
    }


def sha256_of(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finalize_manifest(manifest: dict, package_path) -> dict:
    """Return a copy with the `package` section filled in from the archive."""
    package_path = Path(package_path)
    finalized = dict(manifest)
    finalized["package"] = {
        "size_bytes": package_path.stat().st_size,
        "sha256": sha256_of(package_path),
        "compression": "tar.zst",
    }
    return finalized


def write_manifest(manifest: dict, destination) -> None:
    """Write a manifest atomically so readers never see a partial file."""
    destination = Path(destination)
    tmp_path = destination.with_name(destination.name + ".tmp")
    tmp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, destination)
