"""Outcome of a single collector run inside one diagnostic package."""

from dataclasses import dataclass
from typing import Optional

STATUS_OK = "ok"
STATUS_FAILED = "failed"


@dataclass(slots=True, frozen=True)
class CollectorResult:
    """Per-collector outcome, surfaced in the package manifest.

    Exists so a package can report partial collection instead of silently
    shipping incomplete evidence.
    """

    name: str
    status: str
    bytes: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_dict(self) -> dict:
        out = {"name": self.name, "status": self.status, "bytes": self.bytes}
        if self.error:
            out["error"] = self.error
        return out
