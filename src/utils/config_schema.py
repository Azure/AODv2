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
    quick_actions: list[str] = field(default_factory=list)
    # tool name -> raw CLI args (AOD adds -w/-o and the protocol filter at runtime)
    captures: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class Config:
    """Top level configuration for the AOD."""

    watch_interval_sec: int
    aod_output_dir: str
    anomalies: dict[AnomalyKey, AnomalyConfig]
    cleanup: dict
    audit: dict
