import logging
import logging.handlers
import sys


def setup_logging(
    level: int = logging.INFO,
    app_name: str = "aodv2",
    stderr: bool = False,
    syslog_level: int = logging.WARNING,
) -> None:
    """Configure root logging. Always logs to syslog; stderr only if requested."""
    root = logging.getLogger()
    if getattr(root, "_aod_configured", False):
        return
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(threadName)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if stderr:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(fmt)
        root.addHandler(stderr_handler)

    try:
        syslog_handler = logging.handlers.SysLogHandler(
            address="/dev/log", facility=logging.handlers.SysLogHandler.LOG_DAEMON
        )
        syslog_handler.ident = f"{app_name}: "
        syslog_handler.setFormatter(fmt)
        syslog_handler.setLevel(syslog_level)
        root.addHandler(syslog_handler)
    except OSError as e:
        root.warning(f"Failed to set up syslog handler: {e}")

    root._aod_configured = True
