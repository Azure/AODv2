"""Sock-conn Anomaly Handler. Detects changes in the set of established
client sockets to the SMB/NFS server port between watch_interval_seconds ticks.

Userspace probe: each tick the handler reads /proc/net/tcp{,6} and builds
a frozenset of (local, remote) pairs whose state is ESTABLISHED and whose
remote port matches the protocol's server port. Anomaly fires when that
set differs from the previous tick.

/proc parsing is used in preference to `ss` because it avoids a fork/exec
per tick.
"""

import logging
from base.AnomalyHandlerBase import UserspaceAnomalyHandler
from utils.anomaly_type import Protocol, PROTOCOL_SERVER_PORT

logger = logging.getLogger(__name__)

# /proc/net/tcp{,6} st column value for TCP_ESTABLISHED (kernel enum 1).
_ST_ESTABLISHED = b"01"

_PROC_FILES = ("/proc/net/tcp", "/proc/net/tcp6")


class SockconnAnomalyHandler(UserspaceAnomalyHandler):
    """Fires when the set of established client sockets to the server port
    changes between watch_interval_seconds ticks."""

    def __init__(self, sockconn_config):
        super().__init__(sockconn_config)
        try:
            protocol = Protocol(self.config.protocol)
        except ValueError as exc:
            raise ValueError(
                f"SockconnAnomalyHandler: unknown protocol "
                f"'{self.config.protocol}'"
            ) from exc
        if protocol not in PROTOCOL_SERVER_PORT:
            raise ValueError(
                f"SockconnAnomalyHandler: no server port mapped for "
                f"protocol '{protocol.value}'"
            )
        port_hex = f"{PROTOCOL_SERVER_PORT[protocol]:04X}"
        self._port_filter = f":{port_hex} ".encode("ascii")
        self._port_suffix = f":{port_hex}".encode("ascii")
        self._prev: frozenset[tuple[bytes, bytes]] | None = None
        logger.debug(
            "SockconnAnomalyHandler initialized for protocol=%s port_hex=%s",
            protocol.value,
            port_hex,
        )

    def tick(self) -> bool:
        """Snapshot established sockets to the server port; return True if
        the set differs from the previous tick. First tick always returns
        False."""
        curr = self._snapshot()
        if self._prev is None:
            self._prev = curr
            return False
        changed = curr != self._prev
        if changed and __debug__:
            added = curr - self._prev
            removed = self._prev - curr
            logger.info(
                "Sockconn change for %s: +%d -%d (prev=%d curr=%d)",
                self.config.protocol,
                len(added),
                len(removed),
                len(self._prev),
                len(curr),
            )
        self._prev = curr
        return changed

    def _snapshot(self) -> frozenset[tuple[bytes, bytes]]:
        """Read /proc/net/tcp{,6} and return the set of (local, remote)
        endpoint byte strings whose state is ESTABLISHED and whose remote
        port matches the configured server port."""
        sockets: set[tuple[bytes, bytes]] = set()
        port_filter = self._port_filter
        suffix = self._port_suffix
        for path in _PROC_FILES:
            try:
                with open(path, "rb") as fh:
                    # Skip header line; defensively handle empty files.
                    next(fh, None)
                    for line in fh:
                        if port_filter not in line:
                            continue
                        fields = line.split()
                        # Layout: sl local_address rem_address st tx_queue ...
                        if len(fields) < 4 or fields[3] != _ST_ESTABLISHED:
                            continue
                        # Strict check: ensure the port_filter hit was actually
                        # the remote-port suffix, not a coincidental match.
                        if not fields[2].endswith(suffix):
                            continue
                        sockets.add((fields[1], fields[2]))
            except FileNotFoundError:
                # /proc/net/tcp6 may not exist on IPv6-disabled kernels.
                continue
            except OSError:
                logger.exception("SockconnAnomalyHandler: failed to read %s", path)
                continue
        return frozenset(sockets)
