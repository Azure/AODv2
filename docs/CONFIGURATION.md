# Configuration Guide

AODv2 is configured through a single YAML file. This document describes every
supported key, its accepted values, and the constraints enforced at load time by
`ConfigManager`.

## File Location

```
config/config.yaml    # relative to the project root
```

The path may differ depending on the installation method. It is resolved and
parsed once at service startup; an invalid file aborts startup with a
descriptive error.

## Top-Level Structure

The configuration has exactly four top-level keys:

```yaml
watch_interval_sec: 60
aod_output_dir: /var/log/aod
anomalies:
  <protocol>:
    <anomaly_type>: ...
cleanup: ...
```

| Key                  | Type    | Required | Description                                       |
| -------------------- | ------- | -------- | ------------------------------------------------- |
| `watch_interval_sec` | Integer | Yes      | Batch/evaluation interval, in seconds.            |
| `aod_output_dir`     | String  | Yes      | Root directory for diagnostic output.             |
| `anomalies`          | Mapping | Yes      | Anomaly definitions, keyed by protocol then type. |
| `cleanup`            | Mapping | Yes      | Retention and disk-usage limits.                  |

## Global Settings

### `watch_interval_sec`

**Type:** Integer

Interval in seconds over which the `AnomalyWatcher` accumulates events before
evaluating anomaly thresholds. It also defines the diffing window for socket
connection tracking and governs long-capture restart timing.

```yaml
watch_interval_sec: 60
```

Lower values increase detection responsiveness at the cost of CPU; higher values
reduce overhead, and also reduce the anomal detection resolution.

### `aod_output_dir`

**Type:** String

Root directory for all diagnostic artifacts. Quick-action bundles are written
under `<aod_output_dir>/batches`, and long captures under
`<aod_output_dir>/captures`.

```yaml
aod_output_dir: /var/log/aod
```

The directory must be writable by the service user and provisioned with
sufficient free space.

## Anomalies

The `anomalies` section is a two-level mapping: **protocol → anomaly type →
configuration**. Each leaf defines one detector.

```yaml
anomalies:
  smb:
    latency: ...
    error: ...
    sockconn: ...
  nfs:
    latency: ...
    error: ...
    sockconn: ...
  io:
    error: ...
```

### Supported Combinations

The protocol, anomaly type, and backing tool must form a combination that AOD
recognizes. The full capability matrix is:

| Protocol | Anomaly Type | Tool         | Filter Axes                      |
| -------- | ------------ | ------------ | -------------------------------- |
| `smb`    | `latency`    | `smbslower`  | `track_commands`                 |
| `smb`    | `error`      | `smbiosnoop` | `track_commands`, `track_errors` |
| `smb`    | `sockconn`   | `ss`         | none                             |
| `nfs`    | `latency`    | `nfsslower`  | `track_commands`                 |
| `nfs`    | `error`      | `nfsiosnoop` | `track_commands`, `track_errors` |
| `nfs`    | `sockconn`   | `ss`         | none                             |
| `io`     | `error`      | `iosnoop`    | `track_commands`, `track_errors` |

Any other protocol, type, or tool value is rejected.

### Common Fields

| Field              | Type    | Applies To        | Description                                                        |
| ------------------ | ------- | ----------------- | ------------------------------------------------------------------ |
| `tool`             | String  | All               | Backing tool. Must match the capability matrix above.              |
| `acceptable_count` | Integer | Latency, flat error config (default `1`) | Count that triggers within `watch_interval_sec`. |
| `actions`          | Mapping | All               | Diagnostics to run on trigger. See [Actions](#actions).            |

### Latency Anomalies

Latency detectors flag operations whose duration exceeds a threshold.

```yaml
latency:
  tool: "smbslower"
  mode: "all" # all | trackonly | excludeonly
  acceptable_count: 10
  default_threshold_ms: 20
  track_commands:
    - command: SMB2_WRITE
      threshold: 50
  actions:
    dmesg:
    journalctl:
    tcpdump: ["-s", "65536", "-B", "10240", "-C", "2", "-W", "100"]
```

| Field                  | Type            | Description                                                                              |
| ---------------------- | --------------- | ---------------------------------------------------------------------------------------- |
| `mode`                 | String          | Tracking mode: `all`, `trackonly`, or `excludeonly`. Default `all`.                      |
| `default_threshold_ms` | Number ≥ 0      | Threshold applied to commands without an explicit override. Default `10`.                |
| `track_commands`       | List of objects | `{command: NAME, threshold: N}` entries. `threshold` defaults to `default_threshold_ms`. |
| `exclude_commands`     | List of strings | Command names to exclude from tracking.                                                  |

**Mode semantics:**

- `trackonly` — only commands listed in `track_commands` are monitored;
  `exclude_commands` is ignored (with a warning).
- `excludeonly` — every command is monitored at `default_threshold_ms` except
  those in `exclude_commands`; `track_commands` is ignored (with a warning).
- `all` — every command is monitored at `default_threshold_ms`, overridden by
  any per-command threshold in `track_commands`, then commands in
  `exclude_commands` are dropped.

A latency anomaly must resolve to at least one tracked command. A command may
not appear in both `track_commands` and `exclude_commands`. Thresholds must be
numbers ≥ 0. Valid command names are the SMB2 / NFSv4 operation names for the
respective protocol.

### Error Anomalies

Error detectors count matching SMB NTSTATUS, NFSv4 status, or Linux errno
events. Use `rules` when different command/error combinations need different
thresholds. AOD sends the union of all rule filters to one eBPF process, then
evaluates each rule's exact command/error intersection independently within
`watch_interval_sec`.

```yaml
error:
  tool: "smbiosnoop"
  rules:
    - name: severe
      acceptable_count: 3
      track_errors:
        - STATUS_IO_TIMEOUT
        - STATUS_DISK_FULL
    - name: missing-path-burst
      acceptable_count: 100
      track_commands:
        - SMB2_CREATE
      track_errors:
        - STATUS_OBJECT_NAME_NOT_FOUND
  actions:
    dmesg:
    journalctl:
```

| Field              | Type            | Description                                                        |
| ------------------ | --------------- | ------------------------------------------------------------------ |
| `rules`            | List of mappings | Named command/error filters with independent count thresholds.     |
| `name`             | String          | Unique non-empty rule name.                                        |
| `acceptable_count` | Integer >= 1    | Matching events that trigger this rule in one watcher interval.    |
| `track_commands`   | List of strings | Optional command allowlist; omitted means every command.           |
| `track_errors`     | List of strings | Optional NTSTATUS/NFSv4/errno allowlist; omitted means every error. |

Each rule must specify at least one of `track_commands` or `track_errors`.
Names are validated against the selected tool's command and error tables.
Top-level `track_commands`, `track_errors`, and `acceptable_count` remain
supported as the legacy single-rule form, but cannot be mixed with `rules`.

The shipped defaults assign low thresholds to high-signal failures and higher
thresholds to expected outcomes such as SMB missing-name responses and NFS
`NFS4ERR_NOENT`, `NFS4ERR_DELAY`, and `NFS4ERR_OLD_STATEID`. This suppresses
individual expected failures while still detecting concentrated bursts.

For `io/error`, `iosnoop` emits only failed VFS operations. `track_commands`
uses names such as `OPEN`, `WRITE`, and `LOCK_FCNTL`, while
`track_errors` uses positive Linux errno names such as `EIO`, `ENOSPC`, and
`ENOENT`. This supports syscall-specific policies without counting successful
operations. For example:

```yaml
io:
  error:
    tool: "iosnoop"
    rules:
      - name: failed-writes
        acceptable_count: 3
        track_commands:
          - WRITE
          - WRITEV
        track_errors:
          - EIO
          - ENOSPC
      - name: open-miss-burst
        acceptable_count: 100
        track_commands:
          - OPEN
        track_errors:
          - ENOENT
    actions:
      dmesg:
      journalctl:
```

Only operations emitted by the current VFS probes are accepted as command
names. Network-only detectors and captures, including `sockconn` and
`tcpdump`, do not apply to the `io` protocol.

### Socket Connection Anomalies

Socket-connection detectors track changes in the number of client sockets
connected to the protocol's server port between successive intervals. They use
the userspace `ss` tool and accept no per-item filter fields.

```yaml
sockconn:
  tool: "ss"
  actions:
    dmesg:
```

## Actions

The `actions` mapping declares which diagnostics run when an anomaly triggers.
Keys are action names; the value type depends on the action class.

```yaml
actions:
  dmesg: # quick action -> no value
  journalctl:
  tcpdump: ["-s", "65536", "-C", "2", "-W", "100"] # capture -> CLI args
```

### Quick Actions

Quick actions capture a point-in-time snapshot and take no parameters (the value
must be empty/null).

| Name         | Source                           |
| ------------ | -------------------------------- |
| `dmesg`      | Kernel ring buffer.              |
| `journalctl` | Systemd journal (recent window). |
| `debugdata`  | `/proc/fs/cifs/DebugData`.       |
| `stats`      | `/proc/fs/cifs/Stats`.           |
| `mounts`     | `/proc/mounts`.                  |
| `smbinfo`    | `smbinfo` output.                |
| `syslogs`    | Recent system log lines.         |

Supplying a value to a quick action is an error.

### Captures

Captures are long-running tools whose value is a list of CLI arguments. AOD owns
the output file and the protocol filter, so certain flags are reserved, and some
flags are mandatory because they determine the capture footprint.

| Tool        | Reserved flags (rejected) | Required flags |
| ----------- | ------------------------- | -------------- |
| `tcpdump`   | `-w`, `--write-file`      | `-C`, `-W`     |
| `trace-cmd` | `-o`                      | `-e`           |

**Capture exclusivity:** each capture tool may be bound to only one protocol.
Multiple anomalies of the same protocol may share a capture tool, but they must
supply identical arguments, because a single capture process serves all
anomalies of that protocol.

## Cleanup

The `cleanup` section bounds retention and total disk usage of the output
directory.

```yaml
cleanup:
  cleanup_interval_sec: 300
  max_log_age_days: 2
  max_total_log_size_mb: 1024
```

| Field                   | Type    | Description                                    |
| ----------------------- | ------- | ---------------------------------------------- |
| `cleanup_interval_sec`  | Integer | Interval between cleanup passes, in seconds.   |
| `max_log_age_days`      | Integer | Maximum age of retained artifacts, in days.    |
| `max_total_log_size_mb` | Number  | Maximum combined size of all artifacts, in MB. |

## Complete Example

```yaml
watch_interval_sec: 60
aod_output_dir: /var/log/aod

anomalies:
  smb:
    latency:
      tool: "smbslower"
      mode: "all"
      acceptable_count: 10
      default_threshold_ms: 20
      track_commands:
        - command: SMB2_WRITE
          threshold: 50
      actions:
        dmesg:
        journalctl:
        debugdata:
        stats:
        mounts:
        smbinfo:
        syslogs:
        tcpdump: ["-s", "65536", "-B", "10240", "-C", "2", "-W", "100"]
    sockconn:
      tool: "ss"
      actions:
        dmesg:
        journalctl:
        stats:
        tcpdump: ["-s", "65536", "-B", "10240", "-C", "2", "-W", "100"]

  nfs:
    latency:
      tool: "nfsslower"
      mode: "all"
      acceptable_count: 10
      default_threshold_ms: 50
      actions:
        dmesg:
        journalctl:
        syslogs:
        trace-cmd: ["-e", "nfs", "-e", "nfs4", "-e", "sunrpc", "-b", "1024"]
    sockconn:
      tool: "ss"
      actions:
        dmesg:
    error:
      tool: "nfsiosnoop"
      acceptable_count: 5
      track_commands:
      track_errors:
        - NFS4ERR_BAD_STATEID
        - NFS4ERR_OLD_STATEID
      actions:
        dmesg:
        journalctl:
        syslogs:
        trace-cmd: ["-e", "nfs", "-e", "nfs4", "-e", "sunrpc", "-b", "1024"]

cleanup:
  cleanup_interval_sec: 300
  max_log_age_days: 2
  max_total_log_size_mb: 1024
```
