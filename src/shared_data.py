"""Data shared between multiple aod componenets."""

import ctypes
import errno
from types import MappingProxyType
import numpy as np

SHM_NAME = "/bpf_shm"
TASK_COMM_LEN = 16

# Assuming we are working with x64 architecture for now
HEAD_TAIL_BYTES = 8
MAX_ENTRIES = 2048
PAGE_SIZE = 4096
# Beware before changing page size, shm data size has a condition that is must be a multiple of the page size

SHM_SIZE = (MAX_ENTRIES + 1) * PAGE_SIZE
SHM_DATA_SIZE = SHM_SIZE - 2 * HEAD_TAIL_BYTES

MAX_WAIT = 0.005  # 5 ms, used in event dispatcher and anomaly watcher

ALL_SMB_CMDS = MappingProxyType(
    {
        "SMB2_NEGOTIATE": 0,
        "SMB2_SESSION_SETUP": 1,
        "SMB2_LOGOFF": 2,
        "SMB2_TREE_CONNECT": 3,
        "SMB2_TREE_DISCONNECT": 4,
        "SMB2_CREATE": 5,
        "SMB2_CLOSE": 6,
        "SMB2_FLUSH": 7,
        "SMB2_READ": 8,
        "SMB2_WRITE": 9,
        "SMB2_LOCK": 10,
        "SMB2_IOCTL": 11,
        "SMB2_CANCEL": 12,
        "SMB2_ECHO": 13,
        "SMB2_QUERY_DIRECTORY": 14,
        "SMB2_CHANGE_NOTIFY": 15,
        "SMB2_QUERY_INFO": 16,
        "SMB2_SET_INFO": 17,
        "SMB2_OPLOCK_BREAK": 18,
        "SMB2_SERVER_TO_CLIENT_NOTIFICATION": 19,
    }
)

ALL_ERROR_CODES = list(errno.errorcode.values())


class Metrics(ctypes.Union):
    _fields_ = [("latency_ns", ctypes.c_ulonglong), ("retval", ctypes.c_int)]


class Event(ctypes.Structure):
    """Event c struct."""

    _fields_ = [
        ("pid", ctypes.c_int),
        ("cmd_end_time_ns", ctypes.c_ulonglong),
        ("session_id", ctypes.c_ulonglong),
        ("mid", ctypes.c_ulonglong),
        ("smbcommand", ctypes.c_ushort),
        ("metric", Metrics),
        ("tool", ctypes.c_ubyte),
        ("is_compounded", ctypes.c_ubyte),
        ("task", ctypes.c_char * TASK_COMM_LEN),
    ]


# we need to ensure that event_dtype and event cstruct is of the same size
event_dtype = np.dtype(
    [
        ("pid", np.int32),
        ("cmd_end_time_ns", np.uint64),
        ("session_id", np.uint64),
        ("mid", np.uint64),
        ("smbcommand", np.int16),
        ("metric_latency_ns", np.uint64),
        ("tool", np.uint8),
        ("is_compounded", np.uint8),
        ("task", f"S{TASK_COMM_LEN}"),
    ],
    align=True,
)
