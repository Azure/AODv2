# AODv2 Usage Guide

This guide covers day-to-day operation of AODv2: controlling the service,
requesting diagnostics, locating and interpreting the output bundles, and
configuring what the daemon monitors and collects.

For installation, see [README.md](README.md). For architecture and extension,
see [docs](docs/).

---

## Controlling the service

When installed as a package, AODv2 runs under `systemd` as the `aodv2` unit.

```bash
sudo systemctl enable --now aodv2   # start now and on boot
sudo systemctl start aodv2          # start
sudo systemctl stop aodv2           # stop (writes a final shutdown bundle)
sudo systemctl restart aodv2        # restart (e.g. after a config change)
systemctl status aodv2              # current state
sudo journalctl -u aodv2 -f         # follow logs
```

A configuration change to `/etc/aodv2/config.yaml` requires a restart to take
effect.

### Observing anomaly detections

Each detected anomaly is logged at `CRITICAL` to the system journal (syslog
facility `daemon`, identifier `aodv2`), independent of the collected bundles.
The record names the anomaly and the detection time, so anomalies remain visible
even if their bundle is later rotated away:

```bash
# All AODv2 messages from the current boot
sudo journalctl -u aodv2 -b

# Just the detection records
sudo journalctl -t aodv2 -p crit
```

---

## Requesting diagnostics on demand

AODv2 collects diagnostics automatically when it detects an anomaly. But, two
user-triggered mechanisms produce a **full-system bundle** regardless of anomaly
state:

| Trigger         | How                                 | Bundle label   |
| --------------- | ----------------------------------- | -------------- |
| Manual snapshot | `SIGUSR1` to the controller         | `aod_snapshot` |
| Shutdown dump   | service stop (`SIGTERM` / `SIGINT`) | `aod_shutdown` |

### Sending a manual snapshot

Use `--kill-whom=main` so `systemd` signals only the controller, not the whole
control group:

```bash
sudo systemctl kill --kill-whom=main -s SIGUSR1 aodv2
```

For a foreground (from-source) run, signal the process directly:

```bash
sudo kill -USR1 "$(pgrep -f aod_entry.py)"
```

---

## Where diagnostics are written

All output is written beneath `aod_output_dir` (default `/var/log/aod`)
specified in `config.yaml`:

```
<aod_output_dir>/
├── batches/     # finished, compressed diagnostic bundles (*.tar.zst)
└── captures/    # scratch space for the running capture processes
    ├── smb/
    └── nfs/
```

Completed bundles are the `*.tar.zst` files in `batches/`. The `captures/`
subtree holds live capture files and is managed by the daemon; do not rely on
its contents directly.

### Bundle types and naming

Each collection event produces up to two bundles per protocol:

| Bundle        | Name pattern                                           | Contents                                     |
| ------------- | ------------------------------------------------------ | -------------------------------------------- |
| Quick actions | `aod_quick_<ts>_<proto>_<anomaly>.tar.zst`             | One `*.log` file per configured quick action |
| Capture       | `aod_capture_<ts>_<proto>_<anomaly>[_<proto>].tar.zst` | Raw packet or trace capture                  |

`<ts>` is a nanosecond timestamp, `<proto>` is `smb` or `nfs` (or `aod` for
full-system snapshots), and `<anomaly>` is the anomaly type (`latency`, `error`,
`sockconn`, `snapshot`, or `shutdown`).

### Quick-action bundle contents

Each enabled action writes one log file into the bundle:

| Supported actions (`actions:` key) | File in bundle   | Source                     |
| ---------------------------------- | ---------------- | -------------------------- |
| `dmesg`                            | `dmesg.log`      | Kernel ring buffer         |
| `journalctl`                       | `journalctl.log` | systemd journal (windowed) |
| `syslogs`                          | `syslogs.log`    | System log tail            |
| `debugdata`                        | `debug_data.log` | `/proc/fs/cifs/DebugData`  |
| `stats`                            | `cifsstats.log`  | `/proc/fs/cifs/Stats`      |
| `mounts`                           | `mounts.log`     | Active mount inventory     |
| `smbinfo`                          | `smbinfo.log`    | `smbinfo` output           |

### Capture bundle contents

| Capture tool (`actions:` key) | File in bundle |
| ----------------------------- | -------------- |
| `tcpdump`                     | `cap.pcap`     |
| `trace-cmd`                   | `cap.dat`      |

Capture bundles appear a few seconds after the trigger, since the running
recorder is given a short grace period to flush before it is bundled and
restarted.

### Inspecting a bundle

Bundles are `zstd`-compressed tar archives:

```bash
# List contents
zstd -dc aod_quick_<ts>_smb_latency.tar.zst | tar -tv

# Extract
zstd -dc aod_quick_<ts>_smb_latency.tar.zst | tar -x
```

---

## Environment variables

| Variable           | Default                 | Purpose                                         |
| ------------------ | ----------------------- | ----------------------------------------------- |
| `AOD_CONFIG`       | `../config/config.yaml` | Path to the configuration file                  |
| `AOD_LOG_LEVEL`    | `INFO`                  | Console/journal log level                       |
| `AOD_SYSLOG_LEVEL` | `WARNING`               | Syslog log level                                |
| `AOD_LOG_STDERR`   | `0`                     | Set to `1` to also log to stderr                |
| `AOD_PYTHON`       | `/usr/bin/python3`      | Interpreter for the `systemd` unit (see README) |
| `AOD_TCPDUMP_BIN`  | autodetected            | Override the `tcpdump` binary path              |
| `AOD_TRACECMD_BIN` | autodetected            | Override the `trace-cmd` binary path            |

---

## Configuration reference

AODv2 reads a single YAML file (`/etc/aodv2/config.yaml` for packaged installs).
The top-level structure is:

```yaml
watch_interval_sec: 60 # analysis cadence (seconds)
aod_output_dir: /var/log/aod # base directory for all output

anomalies: { ... } # what to monitor and collect (see below)
cleanup: { ... } # retention policy
```

### Global settings

| Key                  | Meaning                                                                   |
| -------------------- | ------------------------------------------------------------------------- |
| `watch_interval_sec` | How often event batches are analysed.                                     |
| `aod_output_dir`     | Base output directory. `batches/` and `captures/` are created beneath it. |

The `journalctl` / `dmesg` lookback window is fixed at 5 minutes and is not
configurable.

### `anomalies`

Anomalies are keyed by **protocol** (`smb`, `nfs`) and then by **anomaly type**
(`latency`, `error`, `sockconn`). Each entry names the source tool, its
detection thresholds, and the diagnostic actions to run when it fires.

```yaml
anomalies:
  smb:
    latency:
      tool: "smbslower" # source eBPF probe
      mode: "all" # all | trackonly | excludeonly
      acceptable_count: 10 # breaches tolerated per watch_interval_sec
      default_threshold_ms: 20 # default latency threshold
      track_commands: # per-command overrides
        - command: SMB2_WRITE
          threshold: 50
      actions: # diagnostics to collect on trigger
        dmesg:
        journalctl:
        stats:
        tcpdump: ["-s", "65536", "-B", "10240", "-C", "2", "-W", "100"]
```

**Common fields:**

| Field                  | Applies to     | Meaning                                                                                            |
| ---------------------- | -------------- | -------------------------------------------------------------------------------------------------- |
| `tool`                 | all            | Source tool for the anomaly (must be valid for the protocol/type)                                  |
| `acceptable_count`     | all            | Number of matching events tolerated within `watch_interval_sec` before triggering                  |
| `mode`                 | latency        | `all` (defaults + `track_commands` overrides, then `exclude_commands` dropped), `trackonly` (only `track_commands`), `excludeonly` (all except `exclude_commands`) |
| `default_threshold_ms` | latency        | Latency threshold applied to commands without an explicit override                                 |
| `track_commands`       | latency, error | Commands to track; latency uses `{command, threshold}` pairs, error uses a name list               |
| `track_errors`         | error          | Error codes to track (e.g. `NFS4ERR_BAD_STATEID`)                                                  |
| `exclude_commands`     | latency        | Commands to exclude; honored in `all` and `excludeonly` modes, ignored (with a warning) in `trackonly` |
| `actions`              | all            | Diagnostics to collect on trigger (see below)                                                      |

For `error`, at least one of `track_commands` / `track_errors` must be
non-empty. `sockconn` has no per-item thresholds.

### `actions`

The `actions:` block lists the diagnostics to collect when the anomaly fires.
Keys fall into two groups:

- **Quick actions** — `dmesg`, `journalctl`, `syslogs`, `debugdata`, `stats`,
  `mounts`, `smbinfo`. Take no arguments; leave the value empty.
- **Captures** — `tcpdump`, `trace-cmd`. Take a list of CLI arguments. AODv2
  supplies the output file and protocol filter itself; the following are
  required and reserved:

  | Tool        | You must supply                     | AODv2 reserves |
  | ----------- | ----------------------------------- | -------------- |
  | `tcpdump`   | `-C` and `-W` (rotation size/count) | `-w`           |
  | `trace-cmd` | `-e` (at least one event)           | `-o`           |

### Caveats

Captures are long-running processes with lifecycle constraints that quick
actions do not have. Keep the following in mind when configuring them:

- **Dropped captures.** If a capture supervisor is busy and drops a queued
  snapshot, a `WARNING` naming the dropped `batch_id` is logged. If a `SIGUSR1`
  is received during shutdown, it is ignored to avoid racing the shutdown dump.
- **One capture process per protocol.** A single capture process serves every
  anomaly of a given protocol. Consequently:
  - A capture tool may be bound to **only one protocol** across the whole
    config. Configuring, say, `tcpdump` under both `smb` and `nfs` is rejected
    at startup.
  - Multiple anomalies of the same protocol may list the same capture tool, but
    they **must supply identical CLI args** — the config is rejected if they
    conflict.
- **Snapshots are coalesced.** If several snapshot requests for one protocol
  arrive close together, they are drained into a single bundle reflecting one
  recorder state; you get one capture bundle, not one per request.
- **Cooldown drops overlapping requests.** While a capture is being stopped,
  bundled, and restarted (including the post-restart warmup), further snapshot
  requests for that protocol are dropped. A `WARNING` names the dropped
  `batch_id` and points at the nearest `aod_capture_*` bundle that shares the
  recorder state.
- **Repeated spawn failures disable the capture.** If a capture process fails to
  spawn 3 times in a row, that protocol's capture is disabled for the remainder
  of the run and subsequent snapshot requests are dropped with a `WARNING`.
  Quick-action bundles are still produced. Check the logs for spawn errors
  (missing `tcpdump` / `trace-cmd` binary, insufficient privileges, invalid
  args) and restart the service once resolved.
- **Bundles lag the trigger.** On each snapshot the running recorder is given a
  short grace period to flush before it is stopped and bundled, so a capture
  bundle appears a few seconds after the corresponding quick-action bundle.

### `cleanup`

Governs autonomous retention in `aod_output_dir`.

```yaml
cleanup:
  cleanup_interval_sec: 300 # how often cleanup runs
  max_log_age_days: 2 # delete bundles older than this
  max_total_log_size_mb: 1024 # cap total bundle size; oldest removed first
```
