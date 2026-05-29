"""Top level configuration for the AOD."""

from dataclasses import dataclass, field
from typing import NamedTuple, Optional
from utils.anomaly_type import AnomalyType, Protocol


class AnomalyKey(NamedTuple):
    """Composite key for anomaly config: (protocol, anomaly_type)."""

    protocol: Protocol
    anomaly_type: AnomalyType


@dataclass(slots=True, frozen=True)
class AnomalyConfig:
    """AnomalyConfig is a dataclass that defines the configuration for an
    anomaly detection tool."""

    type: str
    tool: str
    protocol: str
    acceptable_count: int
    default_threshold_ms: Optional[int] = None
    track: dict[int, Optional[int]] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class Config:
    """Top level configuration for the AOD."""

    watch_interval_sec: int
    aod_output_dir: str
    anomalies: dict[AnomalyKey, AnomalyConfig]
    cleanup: dict
    audit: dict
