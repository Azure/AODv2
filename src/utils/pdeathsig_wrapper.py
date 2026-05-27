"""Process death signal wrapper for ensuring child processes die with parent."""

import ctypes
import ctypes.util


def pdeathsig_preexec():
    """Set PR_SET_PDEATHSIG to SIGTERM so child dies when parent dies.

    This function is designed to be used as the preexec_fn parameter
    in subprocess.Popen() to ensure that child processes are automatically
    terminated when the parent process dies unexpectedly.
    """
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        # PR_SET_PDEATHSIG = 1, SIGTERM = 15
        libc.prctl(1, 15, 0, 0, 0)
    except Exception:
        # Silently fail if prctl is not available or fails
        pass
