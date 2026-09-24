"""Stable, non-reversible host identifier used in manifests and blob paths.

The raw machine-id is never exported. A locally generated salt means the hash
cannot be correlated back to a machine without access to that host.

NOTE: the identifier format is pending privacy review; only this module needs to
change if a different scheme is chosen.
"""

import hashlib
import logging
import os
import socket
from pathlib import Path

logger = logging.getLogger(__name__)

SALT_FILENAME = "host_salt"
_MACHINE_ID_PATHS = ("/etc/machine-id", "/var/lib/dbus/machine-id")


def _read_machine_id() -> str:
    for candidate in _MACHINE_ID_PATHS:
        try:
            value = Path(candidate).read_text(encoding="utf-8").strip()
            if value:
                return value
        except (FileNotFoundError, PermissionError, OSError):
            continue
    # Containers and some minimal images have no machine-id.
    return f"hostname:{socket.gethostname()}"


def _load_or_create_salt(state_dir: Path) -> bytes:
    salt_path = state_dir / SALT_FILENAME
    try:
        data = salt_path.read_bytes()
        if data:
            return data
    except (FileNotFoundError, PermissionError, OSError):
        pass

    salt = os.urandom(32)
    try:
        os.makedirs(state_dir, exist_ok=True)
        tmp_path = salt_path.with_suffix(".tmp")
        tmp_path.write_bytes(salt)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, salt_path)
    except OSError as e:
        # A non-persisted salt changes the host_id across restarts; log loudly
        # because it breaks correlation of a host's packages over time.
        logger.error("Could not persist host salt to %s: %s", salt_path, e)
    return salt


def get_host_id(state_dir) -> str:
    """Return `sha256:<hex>` derived from the salted machine-id."""
    state_dir = Path(state_dir)
    digest = hashlib.sha256(_load_or_create_salt(state_dir) + _read_machine_id().encode()).hexdigest()
    return f"sha256:{digest}"


def short_host_id(host_id: str, length: int = 16) -> str:
    """Path-safe abbreviation used as the `<host-id>` blob path segment."""
    return host_id.removeprefix("sha256:")[:length]
