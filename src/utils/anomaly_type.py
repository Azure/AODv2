from enum import Enum


class Protocol(Enum):
    """Enumeration for different protocols."""

    SMB = "smb"
    NFS = "nfs"
    # Add more protocols as needed


class AnomalyType(Enum):
    """Enumeration for different types of anomalies that can be detected."""

    LATENCY = "latency"
    ERROR = "error"
    # Add more types as needed


# Maps eBPF tool name to the tool ID byte it writes into events
TOOL_NAME_TO_ID = {
    "smbslower": 0,
    "nfsslower": 1,
    "smbiosnoop": -1,  # fill correct value
    "nfsiosnoop": -1,  # fill correct value
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
