"""Command line entry point for AODv2.

Exists mainly for `upload validate`: misconfigured upload is the most likely
support case, and the failure must name the specific cause rather than showing
a generic error after a silent retry loop.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import yaml

from ConfigManager import ConfigManager
from base.UploadTransport import AuthError, DestinationError, TransportError
from utils import paths

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "config.yaml")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

VALIDATE_TIMEOUT_SEC = 10

_TICK = "[ ok ]"
_CROSS = "[fail]"
_SKIP = "[skip]"


def _self_invocation() -> str:
    """How the operator should re-invoke us, which differs before packaging."""
    if Path(sys.argv[0]).name == "aodv2":
        return "aodv2"
    return f"PYTHONPATH={Path(__file__).resolve().parent} python3 -m cli"


def _restart_hint() -> str:
    # Packaging decides where the unit lands, so check every standard location
    # rather than assuming the daemon was installed from source.
    unit_dirs = ("/etc/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system")
    if Path("/run/systemd/system").exists() and any(
        (Path(d) / "linux_diagnostics.service").exists() for d in unit_dirs
    ):
        return "sudo systemctl restart linux_diagnostics.service"
    return "restart the daemon for this to take effect"


def _load_config(path):
    try:
        return ConfigManager(path).data
    except (RuntimeError, ValueError) as e:
        print(f"{_CROSS} configuration: {e}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)


def _build_transport(config, fast: bool = False):
    upload = config.upload
    if upload.protocol == "local":
        from transports.LocalTransport import LocalTransport
        return LocalTransport(paths.upload_dir(config) / "local-blobs")

    from transports.BlobTransport import BlobTransport
    return BlobTransport(
        destination=upload.destination,
        identity=upload.identity,
        limits=upload.limits,
        timeout_sec=upload.limits.upload_timeout_sec,
        preflight_write_check=upload.behavior.preflight_write_check,
        # An interactive check must answer quickly rather than retry for minutes.
        retry_total=0 if fast else None,
        connect_timeout_sec=VALIDATE_TIMEOUT_SEC if fast else None,
    )


def cmd_upload_validate(args) -> int:
    """Check identity, network path and permissions, reporting each separately."""
    config = _load_config(args.config)
    upload = config.upload

    print(f"config          : {args.config}")
    print(f"upload.enabled  : {upload.enabled}")
    if not upload.enabled:
        print(f"{_SKIP} upload is disabled; nothing to validate")
        return EXIT_OK

    print(f"destination     : {upload.destination.account}/{upload.destination.container}")
    print(f"identity        : {upload.identity.kind}"
          + (f" (client_id {upload.identity.client_id})" if upload.identity.client_id else ""))
    print(f"prefix          : {upload.destination.prefix}")
    print()

    try:
        transport = _build_transport(config, fast=True)
    except ImportError as e:
        print(f"{_CROSS} SDK missing: {e}", file=sys.stderr)
        print("        install azure-storage-blob and azure-identity", file=sys.stderr)
        return EXIT_FAILED
    except Exception as e:
        print(f"{_CROSS} could not build transport: {e}", file=sys.stderr)
        return EXIT_FAILED

    print(f"{_TICK} transport built: {transport.describe()}")

    try:
        transport.preflight()
    except AuthError as e:
        print(f"{_CROSS} authentication/permission: {e}", file=sys.stderr)
        print("        the VM's managed identity needs write access on the container.",
              file=sys.stderr)
        print("        Assigning it requires Owner or User Access Administrator.",
              file=sys.stderr)
        return EXIT_FAILED
    except DestinationError as e:
        print(f"{_CROSS} destination: {e}", file=sys.stderr)
        print("        check the account name, container name and network path.",
              file=sys.stderr)
        return EXIT_FAILED
    except TransportError as e:
        print(f"{_CROSS} transport: {e}", file=sys.stderr)
        if "resolve" in str(e).lower():
            # A wrong account name and a DNS outage look identical here.
            print(f"        '{upload.destination.account}' did not resolve - check the"
                  " account name and any private-endpoint DNS.", file=sys.stderr)
        return EXIT_FAILED

    checks = "container readable and writable" if upload.behavior.preflight_write_check \
        else "container readable (write probe disabled)"
    print(f"{_TICK} preflight: {checks}")
    print()
    print("upload is correctly configured")
    return EXIT_OK


def cmd_upload_status(args) -> int:
    """Print the status file support asks a customer for."""
    config = _load_config(args.config)
    status_path = paths.upload_dir(config) / "status.json"
    try:
        print(status_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"{_SKIP} no status yet at {status_path}"
              " (the uploader writes it once it has run)")
        return EXIT_OK
    except OSError as e:
        print(f"{_CROSS} could not read {status_path}: {e}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def cmd_upload_show_config(args) -> int:
    """Dump the effective upload settings, including defaults."""
    config = _load_config(args.config)
    upload = config.upload
    print(json.dumps({
        "enabled": upload.enabled,
        "protocol": upload.protocol,
        "destination": {
            "account": upload.destination.account,
            "container": upload.destination.container,
            "endpoint": upload.destination.endpoint,
            "prefix": upload.destination.prefix,
        },
        "identity": {"kind": upload.identity.kind, "client_id": upload.identity.client_id},
        "limits": {
            "multipart_threshold_mb": upload.limits.multipart_threshold_mb,
            "block_size_mb": upload.limits.block_size_mb,
            "upload_spool_max_mb": upload.limits.upload_spool_max_mb,
            "backfill_max_age_hours": upload.limits.backfill_max_age_hours,
        },
        "scan_interval_sec": upload.scan_interval_sec,
    }, indent=2))
    return EXIT_OK


DROP_IN_NAME = "10-upload.yaml"


def _drop_in_path(config_path) -> Path:
    return Path(config_path).resolve().parent / "config.d" / DROP_IN_NAME


def cmd_upload_enable(args) -> int:
    """Turn on upload by writing a drop-in beside config.yaml.

    A drop-in rather than an edit, so a hand-maintained config.yaml keeps its
    comments and structure.
    """
    settings = {
        "upload": {
            "enabled": True,
            "protocol": "blob",
            "destination": {
                "account": args.account,
                "container": args.container,
                "endpoint_suffix": args.endpoint_suffix,
                "prefix": args.prefix,
            },
            "identity": {"kind": args.identity_kind},
        }
    }
    if args.client_id:
        settings["upload"]["identity"]["client_id"] = args.client_id

    destination = _drop_in_path(args.config)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(destination.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("# Written by 'aodv2 upload enable'. Edits here override config.yaml.\n")
            yaml.safe_dump(settings, handle, default_flow_style=False, sort_keys=False)
        os.replace(tmp, destination)
    except OSError as e:
        print(f"{_CROSS} could not write {destination}: {e}", file=sys.stderr)
        print("        this command needs root", file=sys.stderr)
        return EXIT_FAILED

    print(f"{_TICK} wrote {destination}")

    # Reject a bad destination now rather than at the first anomaly.
    config = _load_config(args.config)
    print(f"{_TICK} config reloads cleanly (upload.enabled={config.upload.enabled})")
    print()
    print("Next:")
    print(f"  sudo {_self_invocation()} upload validate   # check identity and permissions")
    print(f"  {_restart_hint()}")
    return EXIT_OK


def cmd_upload_disable(args) -> int:
    """Stop uploading, leaving already-collected packages on disk."""
    destination = _drop_in_path(args.config)
    try:
        if destination.exists():
            destination.unlink()
            print(f"{_TICK} removed {destination}")
        else:
            print(f"{_SKIP} no drop-in at {destination}")
    except OSError as e:
        print(f"{_CROSS} could not remove {destination}: {e}", file=sys.stderr)
        return EXIT_FAILED
    print(f"  {_restart_hint()}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aodv2", description="AODv2 diagnostics daemon tooling")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    upload = sub.add_parser("upload", help="auto-upload commands").add_subparsers(
        dest="subcommand", required=True
    )

    enable = upload.add_parser("enable", help="turn on upload to a storage account")
    enable.add_argument("--account", required=True, help="storage account name")
    enable.add_argument("--container", required=True, help="container name")
    enable.add_argument("--prefix", default="aodv2/v1")
    enable.add_argument("--endpoint-suffix", default="core.windows.net")
    enable.add_argument("--identity-kind", default="managed", choices=["managed", "arc"])
    enable.add_argument("--client-id", default=None, help="for a user-assigned identity")
    enable.set_defaults(func=cmd_upload_enable)

    upload.add_parser("disable", help="turn off upload") \
        .set_defaults(func=cmd_upload_disable)
    upload.add_parser("validate", help="check identity, destination and permissions") \
        .set_defaults(func=cmd_upload_validate)
    upload.add_parser("status", help="print the current upload status") \
        .set_defaults(func=cmd_upload_status)
    upload.add_parser("show-config", help="print effective upload settings") \
        .set_defaults(func=cmd_upload_show_config)
    return parser


def main(argv=None) -> int:
    logging.basicConfig(level=os.getenv("AOD_LOG_LEVEL", "WARNING").upper(),
                        format="%(name)s - %(levelname)s - %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
