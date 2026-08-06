"""Parses the config YAML into python dataclass."""

import logging
import warnings
import yaml
from shared_data import ALL_SMB_CMDS, ALL_ERROR_CODES
from utils.anomaly_type import AnomalyType
from utils.config_schema import Config, WatcherConfig, GuardianConfig, AnomalyConfig

logger = logging.getLogger(__name__)


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
        watcher = self._parse_watcher(config_data)
        guardian = self._parse_guardian(config_data)
        self.data = self._build_config(config_data, watcher, guardian)
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
            raise RuntimeError(f"YAML parsing error in config file: {exc}") from exc

    def _parse_watcher(self, config_data: dict):
        """Parse the watcher section of the config."""
        logger.debug("Parsing watcher configuration")
        return WatcherConfig(actions=config_data["watcher"]["actions"])

    def _parse_guardian(self, config_data: dict):
        """Parse the guardian section and its anomalies."""
        logger.debug("Parsing guardian configuration")
        anomalies = {}
        for name, anomaly in config_data["guardian"]["anomalies"].items():
            track = self._get_track_for_anomaly(anomaly)
            if not track or (isinstance(track, dict) and len(track) == 0):
                logger.error(
                    "No items to track for anomaly '%s' after applying config logic",
                    name,
                )
                raise ValueError(
                    f"No items to track for anomaly '{name}' after applying config logic."
                )
            anomalies[name] = AnomalyConfig(
                type=anomaly["type"],
                tool=anomaly["tool"],
                acceptable_count=anomaly["acceptable_count"],
                default_threshold_ms=anomaly.get("default_threshold_ms"),
                track=track,
                actions=anomaly.get("actions", []),
            )
        return GuardianConfig(anomalies=anomalies)

    def _get_track_for_anomaly(self, anomaly: dict):
        """Dispatch to the correct track extraction function based on anomaly
        type using Enum."""
        anomaly_type_str = anomaly["type"].strip().lower()
        try:
            anomaly_type = AnomalyType(anomaly_type_str)
        except ValueError as exc:
            raise ValueError(f"Unknown anomaly type: {anomaly['type']}") from exc
        dispatch = {
            AnomalyType.LATENCY: self._get_latency_track_cmds,
            AnomalyType.ERROR: self._get_error_track_cmds,
            # Add more types here as needed
        }

        if anomaly_type not in dispatch:
            raise ValueError(f"No handler for anomaly type: {anomaly_type.value}")

        return dispatch[anomaly_type](anomaly)

    def _build_config(self, config_data: dict, watcher, guardian):
        """Build the top-level config object."""
        return Config(
            watch_interval_sec=config_data["watch_interval_sec"],
            aod_output_dir=config_data["aod_output_dir"],
            watcher=watcher,
            guardian=guardian,
            cleanup=config_data["cleanup"],
            audit=config_data["audit"],
        )

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

    def _validate_smb_thresholds(self, track_commands):
        """Check that all thresholds in track_commands are valid (int/float and
        >= 0)."""
        for command in track_commands or []:
            if "threshold" in command:
                threshold = command["threshold"]
                if not isinstance(threshold, (int, float)) or threshold < 0:
                    raise ValueError(
                        f"Invalid threshold value in track command: {command}"
                    )

    def _validate_smb_commands(self, track_commands, exclude_commands):
        """Validate SMB commands for tracking and exclusion.

        Checks for duplicates and presence using validate_cmds, and
        checks threshold validity using a separate function.
        """
        # Extract command names from track_commands (list of dicts)
        track_cmd_names = [
            cmd["command"] for cmd in (track_commands or []) if "command" in cmd
        ]
        exclude_cmd_names = exclude_commands or []

        # Use validate_cmds to check for duplicates and presence
        self._validate_cmds(
            all_codes=list(ALL_SMB_CMDS.keys()),
            track_codes=track_cmd_names,
            exclude_codes=exclude_cmd_names,
        )

        # Check threshold validity for track_commands
        self._validate_smb_thresholds(track_commands)

    def _get_track_codes(self, mode, all_codes, track_codes, exclude_codes):
        """Get the track codes based on the mode and provided codes."""
        if mode == "trackonly":
            return {ALL_ERROR_CODES.index(code): None for code in track_codes}
        exclude_set = set(exclude_codes)
        return {
            idx: None
            for idx, code in enumerate(ALL_ERROR_CODES)
            if code not in exclude_set
        }

    def _normalize_track_and_exclude(
        self, mode: str, track_items, exclude_items, anomaly_type: str = "anomaly"
    ):
        """Normalize track and exclude items based on the mode.

        Warns and clears the irrelevant list if needed.
        """
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
        self, mode, track_commands, exclude_commands, default_threshold
    ):
        """Build the command map for latency anomaly detection."""

        def get_threshold(cmd_dict):
            return cmd_dict.get("threshold", default_threshold)

        all_cmds = list(ALL_SMB_CMDS.values())
        command_map = {}
        exclude_command_ids = [ALL_SMB_CMDS[cmd] for cmd in exclude_commands]

        if mode == "trackonly":
            for cmd_dict in track_commands:
                cmd_id = ALL_SMB_CMDS[cmd_dict["command"]]
                command_map[cmd_id] = get_threshold(cmd_dict)
        elif mode == "excludeonly":
            for cmd_id in all_cmds:
                if cmd_id not in exclude_command_ids:
                    command_map[cmd_id] = default_threshold
        else:  # mode == "all"
            for cmd_id in all_cmds:
                command_map[cmd_id] = default_threshold
            for cmd_dict in track_commands:
                cmd_id = ALL_SMB_CMDS[cmd_dict["command"]]
                command_map[cmd_id] = get_threshold(cmd_dict)
            for cmd_id in exclude_command_ids:
                command_map.pop(cmd_id, None)
        return command_map

    def _get_latency_track_cmds(self, anomaly):
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
        self._validate_smb_commands(track_commands, exclude_commands)

        # Build command map
        return self._build_latency_command_map(
            latency_mode, track_commands, exclude_commands, default_threshold
        )

    def _get_error_track_cmds(self, anomaly):
        """Parse and validate latency anomaly tracking commands from the
        config."""
        track_codes = anomaly.get("track_codes", [])
        exclude_codes = anomaly.get("exclude_codes", [])
        error_mode = anomaly.get("mode", "all")

        # Normalize mode and codes
        track_codes, exclude_codes = self._normalize_track_and_exclude(
            error_mode, track_codes, exclude_codes, "error"
        )

        # Validate codes
        all_error_codes = list(
            ALL_ERROR_CODES
        )  # Replace with your actual error code list
        self._validate_cmds(all_error_codes, track_codes, exclude_codes)

        # Get track codes based on the mode
        return self._get_track_codes(
            error_mode, ALL_ERROR_CODES, track_codes, exclude_codes
        )
