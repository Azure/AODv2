"""Parses the config YAML into python dataclass."""

import logging
import warnings
import yaml
from utils.shared_data import ALL_SMB_CMDS, ALL_NFS_CMDS
from utils.anomaly_type import (
    AnomalyType,
    Protocol,
    KNOWN_QUICK_ACTIONS,
    CAPTURE_TOOLS,
    CAPTURE_RESERVED_FLAGS,
    CAPTURE_REQUIRED_FLAGS,
)
from utils.config_schema import Config, AnomalyConfig, AnomalyKey

logger = logging.getLogger(__name__)

# Maps tool name to the command set it operates on
TOOL_TO_CMDS = {
    "smbslower": ALL_SMB_CMDS,
    "smbiosnoop": ALL_SMB_CMDS,
    "nfsslower": ALL_NFS_CMDS,
    "nfsiosnoop": ALL_NFS_CMDS,
    "ss": None,  # ss doesn't have a fixed command set like the SMB/NFS tools
}
VALID_TRACKING_MODES = {"all", "trackonly", "excludeonly"}


class ConfigManager:
    """Loads and parses the YAML configuration file, validates anomaly and
    watcher settings, and constructs the top-level configuration object for the
    diagnostics service."""

    def __init__(self, config_path: str):
        """Initializes the ConfigManager by loading and parsing the
        configuration file."""
        if __debug__:
            logger.info("Loading configuration from: %s", config_path)
        config_data = self._load_yaml(config_path)
        self.data = self._build_config(config_data)
        if __debug__:
            import pprint

            logger.debug("Loaded config object:\n%s", pprint.pformat(self.data))
        logger.info("Configuration loaded successfully")

    def _load_yaml(self, config_path: str):
        """Load the YAML configuration file."""
        try:
            with open(config_path, "r", encoding="utf-8") as file:
                return yaml.safe_load(file)
        except FileNotFoundError as exc:
            logger.error("Config file not found: %s", config_path)
            raise RuntimeError(f"Config file not found: {config_path}") from exc
        except yaml.YAMLError as exc:
            logger.error("Invalid YAML in config file: %s", exc)
            raise RuntimeError(f"Invalid YAML in config file: {exc}") from exc

    def _build_config(self, config_data: dict):
        """Build the top-level config object."""
        anomalies = self._parse_anomalies(config_data)
        return Config(
            watch_interval_sec=config_data["watch_interval_sec"],
            aod_output_dir=config_data["aod_output_dir"],
            anomalies=anomalies,
            cleanup=config_data["cleanup"],
            audit=config_data["audit"],
        )

    def _parse_actions(
        self, raw_actions, anomaly_key
    ) -> tuple[list[str], dict[str, list[str]]]:
        """Split the `actions:` mapping into quick-action names and capture specs.

        Expected shape:
            actions:
              dmesg:                    # quick action -> null value
              tcpdump: ["-s", "0"]      # capture -> list of CLI args
        """
        if raw_actions is None:
            return [], {}
        if not isinstance(raw_actions, dict):
            raise ValueError(
                f"'actions' for {anomaly_key} must be a mapping of "
                f"action_name -> null|list, got {type(raw_actions).__name__}"
            )

        quick_actions: list[str] = []
        captures: dict[str, list[str]] = {}
        for name, value in raw_actions.items():
            if name in KNOWN_QUICK_ACTIONS:
                if value not in (None, [], {}):
                    raise ValueError(
                        f"Quick action '{name}' for {anomaly_key} takes no "
                        f"parameters; got {value!r}"
                    )
                quick_actions.append(name)
            elif name in CAPTURE_TOOLS:
                if value is None:
                    value = []
                if not isinstance(value, list) or not all(
                    isinstance(a, str) for a in value
                ):
                    raise ValueError(
                        f"Capture '{name}' for {anomaly_key} must be a list of "
                        f"string CLI args, got {value!r}"
                    )
                reserved = CAPTURE_RESERVED_FLAGS.get(name, frozenset())
                for arg in value:
                    if arg in reserved:
                        raise ValueError(
                            f"Capture '{name}' for {anomaly_key} may not specify "
                            f"reserved flag '{arg}' (AOD controls the output file "
                            f"and the protocol filter)"
                        )
                required = CAPTURE_REQUIRED_FLAGS.get(name, frozenset())
                missing = sorted(f for f in required if f not in value)
                if missing:
                    raise ValueError(
                        f"Capture '{name}' for {anomaly_key} is missing required "
                        f"flag(s): {missing}. AOD does not default these because "
                        f"they control the capture footprint (rotation size/count, "
                        f"traced events)."
                    )
                captures[name] = list(value)
            else:
                raise ValueError(
                    f"Unknown action '{name}' for {anomaly_key}. "
                    f"Known quick actions: {sorted(KNOWN_QUICK_ACTIONS)}; "
                    f"capture tools: {sorted(CAPTURE_TOOLS)}"
                )
        return quick_actions, captures

    def _validate_capture_exclusivity(
        self, anomalies: dict[AnomalyKey, AnomalyConfig]
    ) -> None:
        """Each capture tool may be bound to at most one protocol across all
        anomalies. Within the same protocol multiple anomalies may share a
        capture tool, but they must agree on the CLI args since one process
        serves them all."""
        # tool -> (protocol, args)
        tool_binding: dict[str, tuple[str, list[str]]] = {}
        for key, cfg in anomalies.items():
            for tool, args in cfg.captures.items():
                bound = tool_binding.get(tool)
                if bound is None:
                    tool_binding[tool] = (cfg.protocol, args)
                    continue
                bound_proto, bound_args = bound
                if bound_proto != cfg.protocol:
                    raise ValueError(
                        f"Capture tool '{tool}' is configured for both "
                        f"protocol '{bound_proto}' and '{cfg.protocol}'. Each "
                        f"capture tool may be bound to only one protocol at a time."
                    )
                if bound_args != args:
                    raise ValueError(
                        f"Capture tool '{tool}' for protocol '{cfg.protocol}' "
                        f"has conflicting args across anomalies: {bound_args!r} "
                        f"vs {args!r}. A single capture process serves all "
                        f"anomalies of one protocol, so args must match."
                    )

    def _parse_anomalies(self, config_data: dict) -> dict[AnomalyKey, AnomalyConfig]:
        """Parse the two-level anomalies section: protocol -> anomaly_type -> config.
        Produces a flat dict keyed by AnomalyKey(protocol, anomaly_type).
        """
        anomalies = {}
        for protocol_name, anomaly_types in config_data["anomalies"].items():
            try:
                protocol = Protocol(protocol_name.strip().lower())
            except ValueError as exc:
                raise ValueError(
                    f"Unknown protocol '{protocol_name}'. Must be one of: {[p.value for p in Protocol]}"
                ) from exc
            for anomaly_name, anomaly in anomaly_types.items():
                anomaly_type = AnomalyType(anomaly_name.strip().lower())
                key = AnomalyKey(protocol, anomaly_type)
                track = self._get_track_for_anomaly(anomaly_type, anomaly, key)
                quick_actions, captures = self._parse_actions(
                    anomaly.get("actions"), key
                )
                anomalies[key] = AnomalyConfig(
                    type=anomaly_name,
                    tool=anomaly["tool"],
                    protocol=protocol.value,
                    acceptable_count=anomaly.get("acceptable_count", 0),
                    default_threshold_ms=anomaly.get("default_threshold_ms"),
                    track=track,
                    quick_actions=quick_actions,
                    captures=captures,
                )
                # TODO: separate userspace and eBPF anomaly configs or unify them in some way
                if __debug__:
                    logger.debug(
                        "Parsed anomaly config for '%s': %s",
                        key,
                        anomalies[key],
                    )
        self._validate_capture_exclusivity(anomalies)
        return anomalies

    def _check_codes(self, codes, all_codes, code_type):
        """Check that codes are present in all_codes, not duplicated, and not
        empty."""
        seen = set()
        for code in codes:
            if code not in all_codes:
                raise ValueError(f"Code {code} not found in {code_type}.")
            if code in seen:
                warnings.warn(
                    f"Code {code} is duplicated in {code_type}.", UserWarning
                )
            seen.add(code)

    def _validate_cmds(self, all_codes, track_codes, exclude_codes):
        """Validate that track and exclude codes/cmds are present, not
        duplicated, and not overlapping."""

        # check if any track_codes are duplicated
        self._check_codes(track_codes, all_codes, "track codes")

        # check if any exclude_codes are duplicated
        self._check_codes(exclude_codes, all_codes, "exclude codes")

        # check if any track_codes are in exclude_codes
        for code in track_codes:
            if code in exclude_codes:
                raise ValueError(
                    f"Code {code} is duplicated in track and exclude codes. It is unclear if Code {code} should be tracked or excluded."
                )

    def _validate_thresholds(self, track_commands):
        """Check that all thresholds in track_commands are valid (int/float and
        >= 0)."""
        for command in track_commands or []:
            if "threshold" in command:
                threshold = command["threshold"]
                if not isinstance(threshold, (int, float)) or threshold < 0:
                    raise ValueError(
                        f"Invalid threshold value in track command: {command}"
                    )

    def _validate_latency_commands(self, track_commands, exclude_commands, all_cmds):
        """Validate latency commands for tracking and exclusion.
        Checks for duplicates and presence using validate_cmds, and
        checks threshold validity.
        """
        track_cmd_names = [
            cmd["command"] for cmd in (track_commands or []) if "command" in cmd
        ]
        exclude_cmd_names = exclude_commands or []

        self._validate_cmds(
            all_codes=list(all_cmds.keys()),
            track_codes=track_cmd_names,
            exclude_codes=exclude_cmd_names,
        )

        self._validate_thresholds(track_commands)

    def _normalize_track_and_exclude(
        self, mode: str, track_items, exclude_items, anomaly_type: str = "anomaly"
    ):
        """Normalize track and exclude items based on the mode.
        Validates mode and warns/clears the irrelevant list if needed.
        """
        if mode not in VALID_TRACKING_MODES:
            raise ValueError(
                f"Invalid mode '{mode}' for {anomaly_type}. Must be one of: {', '.join(VALID_TRACKING_MODES)}"
            )
        if mode == "trackonly" and exclude_items:
            warnings.warn(
                f"{anomaly_type.capitalize()} exclude items will be ignored in trackonly mode."
            )
            exclude_items = []
        elif mode == "excludeonly" and track_items:
            warnings.warn(
                f"{anomaly_type.capitalize()} track items will be ignored in excludeonly mode."
            )
            track_items = []
        return track_items, exclude_items

    def _build_latency_command_map(
        self, mode, track_commands, exclude_commands, default_threshold, cmd_lookup
    ):
        """Build the command map for latency anomaly detection.
        cmd_lookup: the protocol's monitoring command name->id mapping (e.g. ALL_SMB_CMDS).
        """

        def get_threshold(cmd_dict):
            return cmd_dict.get("threshold", default_threshold)

        all_cmds = list(cmd_lookup.values())
        command_map = {}
        exclude_command_ids = [cmd_lookup[cmd] for cmd in exclude_commands]

        if mode == "trackonly":
            for cmd_dict in track_commands:
                cmd_id = cmd_lookup[cmd_dict["command"]]
                command_map[cmd_id] = get_threshold(cmd_dict)
        elif mode == "excludeonly":
            for cmd_id in all_cmds:
                if cmd_id not in exclude_command_ids:
                    command_map[cmd_id] = default_threshold
        else:  # mode == "all"
            for cmd_id in all_cmds:
                command_map[cmd_id] = default_threshold
            for cmd_dict in track_commands:
                cmd_id = cmd_lookup[cmd_dict["command"]]
                command_map[cmd_id] = get_threshold(cmd_dict)
            for cmd_id in exclude_command_ids:
                command_map.pop(cmd_id, None)
        return command_map

    def _get_latency_track_cmds(self, anomaly, cmd_lookup):
        """Parse and validate latency anomaly tracking commands from the
        config."""
        track_commands = anomaly.get("track_commands", []) or []
        exclude_commands = anomaly.get("exclude_commands", []) or []
        latency_mode = anomaly.get("mode", "all")
        default_threshold = anomaly.get("default_threshold_ms", 10)

        # Validate latency mode constraints
        track_commands, exclude_commands = self._normalize_track_and_exclude(
            latency_mode, track_commands, exclude_commands, "latency"
        )

        # Validate commands and thresholds
        self._validate_latency_commands(track_commands, exclude_commands, cmd_lookup)

        # Build command map
        return self._build_latency_command_map(
            latency_mode,
            track_commands,
            exclude_commands,
            default_threshold,
            cmd_lookup,
        )

    def _get_track_for_anomaly(
        self, anomaly_type: AnomalyType, anomaly: dict, key: AnomalyKey
    ):
        """Dispatch to the correct track extraction function based on anomaly type.
        """
        tool = anomaly["tool"]
        if tool not in TOOL_TO_CMDS:
            raise ValueError(f"Unknown tool '{tool}' — no command set mapped.")
        cmd_lookup = TOOL_TO_CMDS[tool]

        if anomaly_type == AnomalyType.LATENCY:
            track = self._get_latency_track_cmds(anomaly, cmd_lookup)
            if not track:
                raise ValueError(
                    f"No items to track for anomaly '{key}' after applying config logic."
                )
            return track
        elif anomaly_type == AnomalyType.SOCKCONN:
            # Userspace probe; AnomalyWatcher polls ss/proc each tick and
            # the handler diffs the state. No per-command tracking knobs.
            return {}
        elif anomaly_type == AnomalyType.ERROR:
            # return self._get_error_track_cmds(anomaly) --- IGNORE ---
            raise NotImplementedError("Error anomaly type is not supported yet.")
        else:
            raise ValueError(f"No handler for anomaly type: {anomaly_type.value}")
