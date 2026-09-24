"""Top level configuration for the AOD."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True, frozen=True)
class AnomalyConfig:
    """AnomalyConfig is a dataclass that defines the configuration for an
    anomaly detection tool."""

    type: str
    tool: str
    acceptable_count: int
    default_threshold_ms: Optional[int] = None
    track: dict[int, Optional[int]] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class GuardianConfig:
    """GuardianConfig will tell which anomalies to detect and how to handle
    them."""

    anomalies: dict[str, AnomalyConfig]


@dataclass(slots=True, frozen=True)
class WatcherConfig:
    """WatcherConfig will tell which actions to be taken."""

    actions: list[str]


@dataclass(slots=True, frozen=True)
class UploadDestination:
    """Customer-owned blob container that receives diagnostic packages."""

    account: Optional[str] = None
    container: Optional[str] = None
    endpoint_suffix: str = "core.windows.net"
    prefix: str = "aodv2/v1"

    @property
    def endpoint(self) -> str:
        return f"https://{self.account}.blob.{self.endpoint_suffix}"


@dataclass(slots=True, frozen=True)
class UploadIdentity:
    """How the daemon proves who it is. No secrets are ever stored on disk."""

    kind: str = "managed"  # managed | arc
    client_id: Optional[str] = None


@dataclass(slots=True, frozen=True)
class UploadLimits:
    """Bounds on upload behaviour.

    `multipart_threshold_mb` is not a cap: packages above it are uploaded in
    blocks rather than rejected.
    """

    multipart_threshold_mb: int = 64
    block_size_mb: int = 4
    upload_spool_max_mb: int = 100
    max_upload_bandwidth_kbps: int = 0  # 0 = unlimited
    upload_timeout_sec: int = 300
    # 0 = upload every package still on disk when upload is first enabled.
    # Safe because SpaceWatcher already caps local storage.
    backfill_max_age_hours: int = 0


@dataclass(slots=True, frozen=True)
class UploadRetry:
    """Backoff and circuit-breaker timing."""

    max_attempts: int = 8
    base_backoff_sec: int = 30
    max_backoff_sec: int = 3600
    circuit_probe_interval_sec: int = 900


@dataclass(slots=True, frozen=True)
class UploadBehavior:
    """Operational switches that do not affect the transport itself."""

    delete_after_upload: bool = False
    require_https: bool = True
    preflight_write_check: bool = True


@dataclass(slots=True, frozen=True)
class UploadConfig:
    """Auto-upload settings. Absent from config.yaml means disabled."""

    enabled: bool = False
    protocol: str = "blob"
    destination: UploadDestination = field(default_factory=UploadDestination)
    identity: UploadIdentity = field(default_factory=UploadIdentity)
    limits: UploadLimits = field(default_factory=UploadLimits)
    retry: UploadRetry = field(default_factory=UploadRetry)
    scan_interval_sec: int = 300
    behavior: UploadBehavior = field(default_factory=UploadBehavior)


@dataclass(slots=True, frozen=True)
class Config:
    """Top level configuration for the AOD."""

    watch_interval_sec: int
    aod_output_dir: str
    watcher: WatcherConfig
    guardian: GuardianConfig
    cleanup: dict  # could make a dataclass if desired
    audit: dict  # could make a dataclass if desired
    upload: UploadConfig = field(default_factory=UploadConfig)
