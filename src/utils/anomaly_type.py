from enum import Enum
from utils.shared_data import ALL_SMB_CMDS, ALL_NFS_CMDS, ALL_NFS_ERRS


class Protocol(Enum):
    """Enumeration for different protocols."""

    SMB = "smb"
    NFS = "nfs"
    # Synthetic protocol used internally to tag full-system snapshot events
    # (manual SIGUSR1 trigger or service shutdown). Not valid in user config.
    AOD = "aod"
    # Add more protocols as needed


class AnomalyType(Enum):
    """Enumeration for different types of anomalies that can be detected."""

    LATENCY = "latency"
    ERROR = "error"
    SOCKCONN = "sockconn"
    # Synthetic types paired with Protocol.AOD for full-system dumps.
    SNAPSHOT = "snapshot"
    SHUTDOWN = "shutdown"
    # Add more types as needed


# Maps eBPF tool name to the tool ID byte it writes into events
TOOL_NAME_TO_ID = {
    "smbslower": 0,
    "nfsslower": 10,
    "nfsiosnoop": 11,
    # Add more as needed
}

# Null args in config for quick actions
KNOWN_QUICK_ACTIONS = frozenset(
    {"dmesg", "journalctl", "debugdata", "stats", "mounts", "smbinfo", "syslogs"}
)

# Long-running capture tools recognized in `actions:`.
# Value in the mapping must be a list of CLI args.
# AOD owns the output file (-w/-o) and the protocol filter; these are
# rejected if the user supplies them.
CAPTURE_TOOLS = frozenset({"tcpdump", "trace-cmd"})

# Flags that AOD reserves for itself per capture tool.
CAPTURE_RESERVED_FLAGS = {
    "tcpdump": frozenset({"-w", "--write-file"}),
    "trace-cmd": frozenset({"-o"}),
}

# Flags the user MUST supply per capture tool. Each listed flag must appear at
# least once in the user's args list. AOD does not invent values for these
# because they materially change the capture's footprint (rotation size,
# rotation count, traced subsystems).
CAPTURE_REQUIRED_FLAGS = {
    "tcpdump": frozenset({"-C", "-W"}),
    "trace-cmd": frozenset({"-e"}),
}

# Protocol -> server-side TCP port. Single source of truth.
PROTOCOL_SERVER_PORT = {
    Protocol.SMB: 445,
    Protocol.NFS: 2049,
}

# This is the single source of truth for:
#   - which protocols AOD knows
#   - which anomaly types each protocol supports
#   - which tools can source each anomaly type
#   - which filter axes each tool accepts and the lookup table for each axis
PROTOCOL_SPEC = {
    Protocol.SMB: {
        AnomalyType.LATENCY: {
            "smbslower": {"track_commands": ALL_SMB_CMDS},
        },
        AnomalyType.SOCKCONN: {
            "ss": {},
        },
    },
    Protocol.NFS: {
        AnomalyType.LATENCY: {
            "nfsslower": {"track_commands": ALL_NFS_CMDS},
        },
        AnomalyType.ERROR: {
            "nfsiosnoop": {
                "track_commands": ALL_NFS_CMDS,
                "track_errors": ALL_NFS_ERRS,
            },
        },
        AnomalyType.SOCKCONN: {
            "ss": {},
        },
    },
}

# Maps enum to anomaly handler classes. Imports live at the bottom so this
# module finishes defining `get_tool_axes` (consumed by handlers) before the
# handler modules are loaded, breaking what would otherwise be a circular
# import.


def get_tool_axes(protocol, anomaly_type, tool: str) -> dict:
    """Return the axes mapping for (protocol, anomaly_type, tool).

    Accepts either Enum members or their string values for `protocol` and
    `anomaly_type` to keep call sites (config parser, handlers) terse.
    Raises KeyError with the offending triple if the combination is not
    declared in PROTOCOL_SPEC.
    """
    if isinstance(protocol, str):
        protocol = Protocol(protocol)
    if isinstance(anomaly_type, str):
        anomaly_type = AnomalyType(anomaly_type)
    return PROTOCOL_SPEC[protocol][anomaly_type][tool]


from handlers.LatencyAnomalyHandler import LatencyAnomalyHandler  # noqa: E402
from handlers.ErrorAnomalyHandler import ErrorAnomalyHandler  # noqa: E402
from handlers.SockconnAnomalyHandler import SockconnAnomalyHandler  # noqa: E402

ANOMALY_HANDLER_REGISTRY = {
    AnomalyType.LATENCY: LatencyAnomalyHandler,
    AnomalyType.ERROR: ErrorAnomalyHandler,
    AnomalyType.SOCKCONN: SockconnAnomalyHandler,
    # Add more types here as needed
}
