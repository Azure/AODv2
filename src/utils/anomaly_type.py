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
