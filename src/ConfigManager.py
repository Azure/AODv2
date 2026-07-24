"""Parses the config YAML into python dataclass."""

import logging
import warnings
import yaml
from utils.anomaly_type import (
    AnomalyType,
    Protocol,
    PROTOCOL_SPEC,
    KNOWN_QUICK_ACTIONS,
    CAPTURE_TOOLS,
    CAPTURE_RESERVED_FLAGS,
    CAPTURE_REQUIRED_FLAGS,
)
from utils.config_schema import Config, AnomalyConfig, AnomalyKey

logger = logging.getLogger(__name__)

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
        tool_binding: dict[str, tuple[Protocol, list[str]]] = {}
        for key, cfg in anomalies.items():
            for tool, args in cfg.captures.items():
                bound = tool_binding.get(tool)
                if bound is None:
                    tool_binding[tool] = (cfg.key.protocol, args)
                    continue
                bound_proto, bound_args = bound
                if bound_proto != cfg.key.protocol:
                    raise ValueError(
                        f"Capture tool '{tool}' is configured for both "
                        f"protocol '{bound_proto.value}' and "
                        f"'{cfg.key.protocol.value}'. Each capture tool may "
                        f"be bound to only one protocol at a time."
                    )
                if bound_args != args:
                    raise ValueError(
                        f"Capture tool '{tool}' for protocol "
                        f"'{cfg.key.protocol.value}' has conflicting args "
                        f"across anomalies: {bound_args!r} vs {args!r}. A "
                        f"single capture process serves all anomalies of one "
                        f"protocol, so args must match."
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
                try:
                    anomaly_type = AnomalyType(anomaly_name.strip().lower())
                except ValueError as exc:
                    raise ValueError(
                        f"Unknown anomaly type '{anomaly_name}' for protocol "
                        f"'{protocol.value}'. Must be one of: "
                        f"{[t.value for t in AnomalyType]}"
                    ) from exc
                key = AnomalyKey(protocol, anomaly_type)
                track = self._get_track_for_anomaly(anomaly_type, anomaly, key)
                quick_actions, captures = self._parse_actions(
                    anomaly.get("actions"), key
                )
                anomalies[key] = AnomalyConfig(
                    key=key,
                    tool=anomaly["tool"],
                    acceptable_count=anomaly.get("acceptable_count", 1),
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

    def _parse_latency_overrides(
        self,
        items,
        cmd_lookup: dict,
        default_ms,
        key: AnomalyKey,
    ) -> dict[int, int]:
        """Resolve a list of ``{command: NAME, threshold: N}`` entries into a
        ``{cmd_id: threshold_ms}`` dict. Validates names against ``cmd_lookup``,
        warns on duplicates, and rejects non-numeric / negative thresholds. A
        missing ``threshold`` falls back to ``default_ms``.
        """
        if not isinstance(items, list):
            raise ValueError(
                f"'track_commands' for {key} must be a list, got "
                f"{type(items).__name__}"
            )
        out: dict[int, int] = {}
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or "command" not in item:
                raise ValueError(
                    f"Each track_commands entry for {key} must be a mapping "
                    f"with a 'command' key, got {item!r}"
                )
            name = item["command"]
            if name not in cmd_lookup:
                raise ValueError(
                    f"Unknown command '{name}' in track_commands for {key}. "
                    f"Allowed: {sorted(cmd_lookup)}"
                )
            if name in seen:
                warnings.warn(
                    f"Command '{name}' is duplicated in track_commands for {key}.",
                    UserWarning,
                )
            seen.add(name)
            threshold = item.get("threshold", default_ms)
            if (
                not isinstance(threshold, (int, float))
                or isinstance(threshold, bool)
                or threshold < 0
            ):
                raise ValueError(
                    f"Invalid threshold {threshold!r} for command '{name}' "
                    f"in {key}; must be a number >= 0."
                )
            out[cmd_lookup[name]] = threshold
        return out

    def _get_latency_track_cmds(
        self, anomaly: dict, axes: dict, key: AnomalyKey
    ) -> dict[str, dict[int, int]]:
        """Parse a latency anomaly's per-command threshold map.

        Returns ``{"track_commands": {cmd_id: threshold_ms}}``

        Mode dictates which cmds are tracked and at what thresholds:
          - ``trackonly``    -> only listed commands are tracked
          - ``excludeonly``  -> all commands tracked at default_threshold_ms
                                except those listed in exclude_commands
          - ``all``          -> all commands tracked at default_threshold_ms,
                                then track_commands override thresholds, then
                                exclude_commands are dropped
        """
        cmd_lookup = axes["track_commands"]
        mode = anomaly.get("mode", "all")
        if mode not in VALID_TRACKING_MODES:
            raise ValueError(
                f"Invalid mode '{mode}' for {key}. Must be one of: "
                f"{sorted(VALID_TRACKING_MODES)}"
            )
        default_ms = anomaly.get("default_threshold_ms", 10)
        if (
            not isinstance(default_ms, (int, float))
            or isinstance(default_ms, bool)
            or default_ms < 0
        ):
            raise ValueError(
                f"'default_threshold_ms' for {key} must be a number >= 0, "
                f"got {default_ms!r}."
            )

        track_items = anomaly.get("track_commands") or []
        exclude_items = anomaly.get("exclude_commands") or []
        if mode == "trackonly" and exclude_items:
            warnings.warn(
                f"exclude_commands ignored in trackonly mode for {key}.",
                UserWarning,
            )
            exclude_items = []
        elif mode == "excludeonly" and track_items:
            warnings.warn(
                f"track_commands ignored in excludeonly mode for {key}.",
                UserWarning,
            )
            track_items = []

        overrides = self._parse_latency_overrides(
            track_items, cmd_lookup, default_ms, key
        )
        excluded_ids = self._parse_axis_for_anomaly(
            exclude_items, cmd_lookup, "exclude_commands", key
        )
        overlap = excluded_ids & overrides.keys()
        if overlap:
            names = sorted(
                name for name, cid in cmd_lookup.items() if cid in overlap
            )
            raise ValueError(
                f"Command(s) {names} appear in both track_commands and "
                f"exclude_commands for {key}; cannot tell whether to track "
                f"or exclude."
            )

        if mode == "trackonly":
            return {"track_commands": overrides}
        out: dict[int, int] = {cid: default_ms for cid in cmd_lookup.values()}
        out.update(overrides)
        for cid in excluded_ids:
            out.pop(cid, None)
        return {"track_commands": out}

    def _parse_axis_for_anomaly(
        self, names, lookup, axis_name: str, key: AnomalyKey
    ) -> frozenset[int]:
        """Resolve a list of human-readable command/error names to their numeric IDs using
        `lookup`."""
        if not names:
            return frozenset()
        if not isinstance(names, list):
            raise ValueError(
                f"'{axis_name}' for {key} must be a list of names, got "
                f"{type(names).__name__}"
            )
        ids: set[int] = set()
        seen: set[str] = set()
        for name in names:
            if name in seen:
                warnings.warn(
                    f"Name '{name}' is duplicated in '{axis_name}' for {key}.",
                    UserWarning,
                )
            seen.add(name)
            if name not in lookup:
                raise ValueError(
                    f"Unknown name '{name}' in '{axis_name}' for {key}. "
                    f"Allowed: {sorted(lookup)}"
                )
            ids.add(lookup[name])
        return frozenset(ids)

    def _get_error_track_cmds(
        self, anomaly, axes: dict, key: AnomalyKey
    ) -> dict[str, frozenset[int]]:
        """Parse error anomaly commands and errors (axes) from the config. Returns a per-axis frozenset of IDs.

        Axes accepted are those declared in PROTOCOL_SPEC for this tool
        (nfsiosnoop -> track_commands + track_errors). An empty axis
        means "no allowlist filter" in the BPF program. At least one axis
        must be non-empty.
        """
        track: dict[str, frozenset[int]] = {}
        for axis_name, lookup in axes.items():
            track[axis_name] = self._parse_axis_for_anomaly(
                anomaly.get(axis_name), lookup, axis_name, key
            )
        if not any(track.values()):
            raise ValueError(
                f"Error anomaly '{key}' must specify at least one entry in "
                f"one of: {sorted(axes)}."
            )
        return track

    def _resolve_tool_axes(
        self,
        anomaly_type: AnomalyType,
        tool: str,
        key: AnomalyKey,
    ) -> dict:
        """Validate (protocol, anomaly_type, tool) against the capability
        matrix and return the axes dict for the tool.
        """
        type_map = PROTOCOL_SPEC.get(key.protocol)
        if type_map is None:
            raise ValueError(
                f"Protocol '{key.protocol.value}' is not supported (anomaly '{key}')."
            )
        tool_map = type_map.get(anomaly_type)
        if tool_map is None:
            allowed = sorted(t.value for t in type_map)
            raise ValueError(
                f"Anomaly type '{anomaly_type.value}' is not supported for "
                f"protocol '{key.protocol.value}' (anomaly '{key}'). "
                f"Allowed types: {allowed}."
            )
        axes = tool_map.get(tool)
        if axes is None:
            raise ValueError(
                f"Tool '{tool}' cannot source {key.protocol.value}/"
                f"{anomaly_type.value} anomalies (anomaly '{key}'). "
                f"Allowed tools: {sorted(tool_map)}."
            )
        return axes

    def _get_track_for_anomaly(
        self, anomaly_type: AnomalyType, anomaly: dict, key: AnomalyKey
    ):
        """Dispatch to the correct track extraction function based on anomaly type."""
        tool = anomaly["tool"]
        axes = self._resolve_tool_axes(anomaly_type, tool, key)

        if anomaly_type == AnomalyType.LATENCY:
            track = self._get_latency_track_cmds(anomaly, axes, key)
            if not track["track_commands"]:
                raise ValueError(
                    f"Latency anomaly '{key}' must specify at least one "
                    f"command in track_commands or exclude_commands."
                )
            return track
        elif anomaly_type == AnomalyType.SOCKCONN:
            # Userspace probe; AnomalyWatcher polls ss/proc each tick and
            # the handler diffs the state. No per-command tracking knobs.
            return {}
        elif anomaly_type == AnomalyType.ERROR:
            return self._get_error_track_cmds(anomaly, axes, key)
        else:
            raise ValueError(f"No handler for anomaly type: {anomaly_type.value}")
