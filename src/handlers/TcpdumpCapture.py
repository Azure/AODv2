"""tcpdump-based long-running packet capture."""

import os
import shutil

from base.LongCapture import LongCapture
from utils.anomaly_type import Protocol

# Protocol -> tcpdump BPF filter tokens. Kept as a list so subprocess receives
# them as discrete argv entries (tcpdump concatenates trailing non-flag args
# into the filter expression).
_PROTOCOL_FILTER = {
    Protocol.SMB: ["port", "445"],
    Protocol.NFS: ["port", "2049"],
}

_DEFAULT_IFACE = "any"


class TcpdumpCapture(LongCapture):
    """tcpdump capture. AOD owns -w (output) and the trailing filter
    expression. User args carry rotation knobs (-C, -W) and any other tuning
    (-s, -B, -i)."""

    tool_name = "tcpdump"
    output_extension = ".pcap"

    def build_argv(self, output_path: str) -> list[str]:
        binary = (
            os.environ.get("AOD_TCPDUMP_BIN") or shutil.which("tcpdump") or "tcpdump"
        )
        argv = [binary, *self.user_args]
        if "-i" not in self.user_args:
            argv += ["-i", _DEFAULT_IFACE]
        # tcpdump drops privileges to user `tcpdump` after opening the socket,
        # which breaks file rotation (-C) because the rotated files cannot be
        # created in a root-owned dir. AOD already runs as root, so keep root
        # unless the operator explicitly chose a different user.
        if "-Z" not in self.user_args:
            argv += ["-Z", "root"]
        argv += ["-w", output_path]
        argv += _PROTOCOL_FILTER[self.protocol]
        return argv
