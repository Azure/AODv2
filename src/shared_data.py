"""Data shared between multiple aod componenets."""

import ctypes
import errno
from types import MappingProxyType
import numpy as np

TASK_COMM_LEN = 16
RINGBUF_PINNED = b"/sys/fs/bpf/aodrb"

# Upper bound on records the kernel ringbuf can hold at once.
# Kernel ringbuf is MAX_ENTRIES * 4096 = 8 MiB (see aod_diag.h); each record
# costs 8 B header + roundup_8(sizeof(struct event)=48) = 56 B.
# Sizing the consumer scratch to this value lets us do single-poll without
# fearing overflow
RB_MAX_RECORDS = (2048 * 4096) // 56  # 149797

MAX_WAIT = 0.005  # 5 ms, used in anomaly watcher

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


ALL_NFS_CMDS = MappingProxyType(
    {
        "NFSPROC4_CLNT_NULL": 0,
        "NFSPROC4_CLNT_READ": 1,
        "NFSPROC4_CLNT_WRITE": 2,
        "NFSPROC4_CLNT_COMMIT": 3,
        "NFSPROC4_CLNT_OPEN": 4,
        "NFSPROC4_CLNT_OPEN_CONFIRM": 5,
        "NFSPROC4_CLNT_OPEN_NOATTR": 6,
        "NFSPROC4_CLNT_OPEN_DOWNGRADE": 7,
        "NFSPROC4_CLNT_CLOSE": 8,
        "NFSPROC4_CLNT_SETATTR": 9,
        "NFSPROC4_CLNT_FSINFO": 10,
        "NFSPROC4_CLNT_RENEW": 11,
        "NFSPROC4_CLNT_SETCLIENTID": 12,
        "NFSPROC4_CLNT_SETCLIENTID_CONFIRM": 13,
        "NFSPROC4_CLNT_LOCK": 14,
        "NFSPROC4_CLNT_LOCKT": 15,
        "NFSPROC4_CLNT_LOCKU": 16,
        "NFSPROC4_CLNT_ACCESS": 17,
        "NFSPROC4_CLNT_GETATTR": 18,
        "NFSPROC4_CLNT_LOOKUP": 19,
        "NFSPROC4_CLNT_LOOKUP_ROOT": 20,
        "NFSPROC4_CLNT_REMOVE": 21,
        "NFSPROC4_CLNT_RENAME": 22,
        "NFSPROC4_CLNT_LINK": 23,
        "NFSPROC4_CLNT_SYMLINK": 24,
        "NFSPROC4_CLNT_CREATE": 25,
        "NFSPROC4_CLNT_PATHCONF": 26,
        "NFSPROC4_CLNT_STATFS": 27,
        "NFSPROC4_CLNT_READLINK": 28,
        "NFSPROC4_CLNT_READDIR": 29,
        "NFSPROC4_CLNT_SERVER_CAPS": 30,
        "NFSPROC4_CLNT_DELEGRETURN": 31,
        "NFSPROC4_CLNT_GETACL": 32,
        "NFSPROC4_CLNT_SETACL": 33,
        "NFSPROC4_CLNT_FS_LOCATIONS": 34,
        "NFSPROC4_CLNT_RELEASE_LOCKOWNER": 35,
        "NFSPROC4_CLNT_SECINFO": 36,
        "NFSPROC4_CLNT_FSID_PRESENT": 37,
        "NFSPROC4_CLNT_EXCHANGE_ID": 38,
        "NFSPROC4_CLNT_CREATE_SESSION": 39,
        "NFSPROC4_CLNT_DESTROY_SESSION": 40,
        "NFSPROC4_CLNT_SEQUENCE": 41,
        "NFSPROC4_CLNT_GET_LEASE_TIME": 42,
        "NFSPROC4_CLNT_RECLAIM_COMPLETE": 43,
        "NFSPROC4_CLNT_LAYOUTGET": 44,
        "NFSPROC4_CLNT_GETDEVICEINFO": 45,
        "NFSPROC4_CLNT_LAYOUTCOMMIT": 46,
        "NFSPROC4_CLNT_LAYOUTRETURN": 47,
        "NFSPROC4_CLNT_SECINFO_NO_NAME": 48,
        "NFSPROC4_CLNT_TEST_STATEID": 49,
        "NFSPROC4_CLNT_FREE_STATEID": 50,
        "NFSPROC4_CLNT_GETDEVICELIST": 51,
        "NFSPROC4_CLNT_BIND_CONN_TO_SESSION": 52,
        "NFSPROC4_CLNT_DESTROY_CLIENTID": 53,
        "NFSPROC4_CLNT_SEEK": 54,
        "NFSPROC4_CLNT_ALLOCATE": 55,
        "NFSPROC4_CLNT_DEALLOCATE": 56,
        "NFSPROC4_CLNT_LAYOUTSTATS": 57,
        "NFSPROC4_CLNT_CLONE": 58,
        "NFSPROC4_CLNT_COPY": 59,
        "NFSPROC4_CLNT_OFFLOAD_CANCEL": 60,
        "NFSPROC4_CLNT_LOOKUPP": 61,
        "NFSPROC4_CLNT_LAYOUTERROR": 62,
        "NFSPROC4_CLNT_COPY_NOTIFY": 63,
        "NFSPROC4_CLNT_GETXATTR": 64,
        "NFSPROC4_CLNT_SETXATTR": 65,
        "NFSPROC4_CLNT_LISTXATTRS": 66,
        "NFSPROC4_CLNT_REMOVEXATTR": 67,
        "NFSPROC4_CLNT_READ_PLUS": 68,
    }
)

ALL_ERROR_CODES = list(errno.errorcode.values())


class Metrics(ctypes.Union):
    _fields_ = [
        ("latency_ns", ctypes.c_ulonglong),
        ("retval", ctypes.c_int),
    ]


class Event(ctypes.Structure):
    """Event c struct."""

    _fields_ = [
        ("pid", ctypes.c_uint),
        ("command", ctypes.c_ushort),
        ("tool", ctypes.c_char),
        ("_pad", ctypes.c_char),
        ("cmd_end_time_ns", ctypes.c_ulonglong),
        ("rqst_id", ctypes.c_ulonglong),
        ("metric", Metrics),
        ("task", ctypes.c_char * TASK_COMM_LEN),
    ]


# we need to ensure that event_dtype and event cstruct is of the same size
event_dtype = np.dtype(
    [
        ("pid", np.int32),
        ("command", np.uint16),
        ("tool", "S1"),
        ("_pad", "S1"),
        ("cmd_end_time_ns", np.uint64),
        ("rqst_id", np.uint64),
        (
            "metric_latency_ns",
            np.uint64,
        ),  # This will be used to read latency, but can also be interpreted as retval when needed
        ("task", f"S{TASK_COMM_LEN}"),
    ],
    align=True,
)
