"""Filesystem transport: writes packages into a local directory tree.

Lets the whole upload pipeline - scanning, state transitions, retry, eviction -
be exercised without any cloud account. Not for production use.
"""

import json
import logging
import os
import shutil
from pathlib import Path

from base.UploadTransport import (
    AlreadyUploaded,
    DestinationError,
    PayloadError,
    TransportError,
    UploadTransport,
)

logger = logging.getLogger(__name__)


class LocalTransport(UploadTransport):
    """Mirrors blob names onto disk under a root directory."""

    def __init__(self, root):
        self.root = Path(root)

    def describe(self) -> str:
        return f"local:{self.root}"

    def preflight(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / ".aod-preflight"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            raise DestinationError(f"local destination {self.root} unusable: {e}") from e

    def upload(self, local_path: Path, blob_name: str, metadata: dict | None = None) -> None:
        local_path = Path(local_path)
        if not local_path.exists():
            raise PayloadError(f"package missing: {local_path}")

        target = self.root / blob_name
        if target.exists():
            raise AlreadyUploaded(blob_name)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write then rename so a reader never observes a partial object,
            # mirroring the atomicity a single blob PUT provides.
            staging = target.with_name(target.name + ".uploading")
            shutil.copyfile(local_path, staging)
            os.replace(staging, target)
            if metadata:
                target.with_name(target.name + ".meta.json").write_text(
                    json.dumps(metadata, sort_keys=True), encoding="utf-8"
                )
        except OSError as e:
            raise TransportError(f"failed writing {target}: {e}") from e
