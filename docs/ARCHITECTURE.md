# Linux Always-On Diagnostics Controller (AODv2) - Architecture Guide

## System Architecture Overview

AODv2 is an always-on monitoring and diagnostics daemon for Linux SMB and NFS
filesystems. A single multi-threaded Python daemon supervises external eBPF
processes and consumes their events through a pinned eBPF ring buffer.

The design is a producer-consumer pipeline. eBPF programs and a small set of
userspace probes produce event and state data; dedicated threads consume, batch,
and analyze that data; on anomaly detection, system snapshot, or application
shutdown, the daemon collects diagnostics into compressed bundles.

## Core Components

### 1. Controller (Orchestrator)

**File:** `src/Controller.py` **Entry point:** `src/aod_entry.py`

Central coordinator and supervisor.

- Responsibilities:
  - Loads and validates configuration via `ConfigManager`.
  - Spawns and supervises eBPF collector processes.
  - Spawns and supervises component threads, restarting them on failure.
  - Handles signals for graceful shutdown and on-demand snapshots.
  - Coordinates cleanup and orderly shutdown.

- Threading and process model:
  - Runs in the main thread; `run()` starts everything then blocks on
    `stop_event`, which is understood by various component threads as a proxy
    for the shutdown command.
  - `_supervise_thread()` restarts a dead component thread after a 1s backoff.
    With `fatal_on_exc=True`, an unhandled exception instead escalates to a full
    service shutdown. This is used for `LogCollector`, which owns an asyncio
    loop with attached subprocess handles that cannot be safely restarted in
    place.
  - `_supervise_process()` restarts a dead eBPF process; on shutdown it sends
    `SIGINT` to the process group, waits up to 5s, then `SIGKILL`.
  - Inter-component communication uses two thread-safe `queue.Queue` instances
    (`eventQueue`, `anomalyActionQueue`) with `None` sentinels for shutdown.
  - Child processes are started with `start_new_session=True` and a
    `PR_SET_PDEATHSIG` preexec hook (`utils/pdeathsig_wrapper.py`) so orphaned
    collectors terminate with the daemon.

- eBPF command builders: `smbslower` and `nfsslower` (latency) are built by
  `_get_latency_tool_cmd`; `nfsiosnoop` (error) by `_get_error_tool_cmd`.
  Userspace tools (`ss`/sockconn) have no process-supervisor entry and are
  driven by `AnomalyWatcher`.

- Signals: `SIGTERM`/`SIGINT` trigger graceful `stop()`; `SIGUSR1` enqueues a
  full-system snapshot (collects all configured diagnostic logs).

- If configured, long captures (trace-cmd, tcpdump) run continuously until a
  signal `SIGTERM/SIGINT/SIGUSER1` is received or an anomaly is spotted by AOD.

### 2. EventDispatcher (Data Ingestion)

**File:** `src/EventDispatcher.py`

Consumes events from the kernel-pinned BPF ring buffer at `/sys/fs/bpf/aodrb`.

- Functions:
  - Loads a C shim shared library (`src/bin/libringbuf_shim.so`) via `ctypes`
    and calls `rb_open` / `rb_poll_into` / `rb_close`.
  - Bulk-drains ring buffer records directly into a preallocated NumPy scratch
    buffer (`event_dtype`), then forwards a copy of the live slice to
    `eventQueue`; the scratch buffer is reused across polls.
  - Opens the ring buffer with a bounded retry/timeout and honors `stop_event`
    for responsive shutdown.
  - Pushes a `None` sentinel to `eventQueue` on exit. The ring buffer context is
    released by `Controller` (`cleanup()`), not by `run()`, so the supervisor
    can restart `run()` without losing the context.

- Characteristics:
  - Runs in a dedicated thread; the poll timeout (1s) only affects shutdown
    responsiveness.
  - The scratch buffer is sized to the kernel ring buffer capacity
    (`RB_MAX_RECORDS`) so a single poll cannot overflow.
  - Directly reads the C type structs into a Numpy C-types variable. Care must
    be taken to ensure that both of these types are always kept memory-aligned.

### 3. AnomalyWatcher (Analysis Engine)

**File:** `src/AnomalyWatcher.py`

Batches and analyzes events, and ticks userspace probes.

- Handler split: at init, configured handlers are split into two groups.
  - eBPF handlers (`AnomalyHandler`) analyze NumPy event batches.
  - Userspace handlers (`UserspaceAnomalyHandler`) are polled once per tick
    (e.g. `SockconnAnomalyHandler`).

- Processing model:
  - Blocks on `eventQueue.get(timeout=watch_interval_sec)`. On timeout,
    userspace handlers still tick i.e., even if there were no kernel events
    emitted, the userspace monitoring will not be affected.
  - On receiving a batch, drains additional queued batches with `get_nowait()`
    and coalesces them, respecting the `None` sentinel.
  - For each eBPF handler, masks the batch to that tool's `tool` id byte and
    calls `handler.detect(masked_batch)`; handler exceptions are isolated
    per-batch.
  - After eBPF dispatch, ticks all userspace handlers, then sleeps on
    `stop_event` for the interval.
  - On detection, enqueues an action `{"anomaly_key", "timestamp"}` to
    `anomalyActionQueue` and logs at `CRITICAL` (surfaced to syslog).
  - Default `watch_interval_sec` fallback is 60s.

### 4. LogCollector (Diagnostic Engine)

**File:** `src/LogCollector.py`

Runs an asyncio event loop that services `anomalyActionQueue` and owns the
long-running captures.

- Execution model:
  - Blocks on the queue in a worker thread (`asyncio.to_thread`); each dequeued
    action spawns a collection task bounded by a semaphore
    (`max_concurrent_tasks = 4`).
  - One `LongCapture` supervisor task per configured capture protocol runs for
    the lifetime of the loop.

- Collection pipeline (`_create_log_collection_task`):
  - Builds a `batch_id` of `<ns_timestamp>_<protocol>_<anomaly_type>` so
    concurrent anomalies cannot collide.
  - Snapshot detection: `is_snapshot = (anomaly_key.protocol == Protocol.AOD)`.
    - Snapshot/shutdown runs all (deduplicated) quick-action handlers and
      requests a snapshot from every capture (protocol suffixed onto its
      batch_id).
    - Normal anomaly runs only that anomaly's quick actions and requests a
      snapshot only from that protocol's capture, if configured.
  - Quick actions run concurrently via `asyncio.gather`, then the staging dir is
    bundled off-thread.
  - Capture snapshots are fire-and-forget; the capture supervisor stops,
    bundles, and restarts the recorder in the background.

- Bundling: `.tar` streamed through Zstandard (level 3) to `.tar.zst`.
  - Quick-action staging dir `<aod_output_dir>/batches/aod_quick_<batch_id>/`
    becomes `aod_quick_<batch_id>.tar.zst`; the staging dir is always removed
    afterward.
  - Capture bundles are written as `aod_capture_<batch_id>[...].tar.zst` in the
    same `batches/` directory.

- Shutdown: waits (up to ~60s) for capture supervisors to finish any in-flight
  stop/bundle before cancelling, so shutdown capture context is not lost.

### 5. SpaceWatcher (Maintenance)

**File:** `src/SpaceWatcher.py`

Autonomous cleanup of the bundle output directory.

- Scans only completed `aod_*.tar.zst` files under `<aod_output_dir>/batches/`,
  avoiding races with in-progress bundles, including the live
  `<aod_output_dir>/captures/` dir.
- Size-based cleanup: when total bundle size exceeds `0.97 x` the configured
  `max_total_log_size_mb`, prunes oldest bundles down to `0.85 x` the limit.
- Age-based cleanup: every `max_log_age_days`, deletes bundles older than that
  age.
- Cadence: wakes every `cleanup_interval_sec` (default 600s) and sleeps on
  `stop_event` between runs.

## Data Flow Architecture

Processing pipeline:

1. eBPF programs (kernel space) write event records to the BPF ring buffer.
2. Common Ring buffer (`/sys/fs/bpf/aodrb`) provides an efficient kernel-to-user
   channel.
3. EventDispatcher bulk-polls via the C shim into NumPy and forwards batches.
4. Event queue holds NumPy event batches.
5. AnomalyWatcher runs batch analysis (eBPF handlers) plus userspace probe
   ticks.
6. Anomaly action queue holds triggered diagnostic actions.
7. LogCollector performs async quick-action collection and capture snapshots.
8. Diagnostic bundles are written as `.tar.zst` under
   `<aod_output_dir>/batches/`.

Communication channels:

- BPF ring buffer: eBPF to EventDispatcher (via C shim).
- Event queue: EventDispatcher to AnomalyWatcher.
- Anomaly action queue: AnomalyWatcher / Controller to LogCollector.

Event record (`event_dtype`) includes `command` (`uint16`), `tool` (single byte
id), and `metric_latency_ns`, among other fields (see
`src/utils/shared_data.py`).

## Threading Model

### Process and Thread Architecture

- Main process: multi-threaded Python daemon.
- External processes: eBPF collectors e.g. `smbslower`, `nfsslower`,
  `nfsiosnoop` supervised by the Controller.
- Main thread: Controller (coordination and supervision).
- Worker threads:
  - EventDispatcher thread.
  - AnomalyWatcher thread.
  - LogCollector thread (owns an asyncio event loop).
  - SpaceWatcher thread.
  - One process-supervisor thread per configured eBPF tool.

### Process Management

- Automatic restart of a dead collector process.
- Graceful stop: `SIGINT` to the process group, 5s grace, then `SIGKILL`.
- Orphan protection: `PR_SET_PDEATHSIG` plus `start_new_session=True`.

### Inter-Thread Communication and Synchronization

- Thread-safe queues (`eventQueue`, `anomalyActionQueue`).
- `None` sentinels (poison pills) for graceful shutdown propagation.
- `threading.Event` (`stop_event`) as the global stop signal.
- LogCollector escalates fatal exceptions to a full service shutdown rather than
  an in-place restart, protecting attached subprocess handles.

## Anomaly Detection

Handlers are registered by `AnomalyType` in `ANOMALY_HANDLER_REGISTRY`
(`src/utils/anomaly_type.py`): `LATENCY`, `ERROR`, `SOCKCONN`.

### Latency Detection (`LatencyAnomalyHandler`, eBPF)

Builds a dense per-command threshold lookup (ms → ns) sized to the protocol's
full command-id space, then:

```python
anomaly_count = np.sum(
    events_batch["metric_latency_ns"]
    >= self.threshold_lookup[events_batch["command"]]
)
max_latency = np.max(events_batch["metric_latency_ns"])
return anomaly_count >= self.acceptable_count or max_latency >= 1e9  # 1s
```

Applies to SMB (`smbslower`) and NFS (`nfsslower`). Kernel-side filtering is
assumed for excluded commands.

### Error Detection (`ErrorAnomalyHandler`, eBPF)

The eBPF tool (`nfsiosnoop`) kernel-filters by command/error allowlists; the
handler fires when the number of matching events in a batch meets
`acceptable_count`:

```python
return len(events_batch) >= self.acceptable_count
```

### Sockconn Detection (`SockconnAnomalyHandler`, userspace)

Ticked each `watch_interval_sec`. Reads `/proc/net/tcp{,6}`, builds the set of
ESTABLISHED client sockets to the protocol's server port (SMB `445`, NFS
`2049`), and fires when that set changes between ticks.

## QuickActions System

### QuickAction Architecture

**Base class:** `src/base/QuickAction.py`

Subclasses declare what to collect via `get_command()`; the base class handles
execution, output paths, metrics, and graceful failure.

```python
class QuickAction(ABC):
    def __init__(self, batches_root: str, log_filename: str): ...

    @abstractmethod
    def get_command(self) -> tuple[list[str], str]:
        """Return (command, command_type) where type is 'cat' or 'cmd'."""

    async def execute(self, batch_id: str) -> None:
        """Run collection asynchronously; failures are logged, not raised."""
```

Command types:

- `cat`: direct file read (e.g. `/proc/fs/cifs/DebugData`).
- `cmd`: subprocess execution (e.g. `journalctl`, `dmesg`).

Output: each action writes `<log_filename>` into
`<aod_output_dir>/batches/aod_quick_<batch_id>/`.

### Available QuickActions (`src/handlers/`)

- `DmesgQuickAction`: kernel ringbuffer over a 5 min lookback window.
- `JournalctlQuickAction`: systemd journal over a 5 min lookback window.
- `SysLogsQuickAction`: system log tail (default 100 lines).
- `DebugDataQuickAction`: `cat /proc/fs/cifs/DebugData`.
- `CifsstatsQuickAction`: `cat` CIFS stats.
- `MountsQuickAction`: mount information.
- `SmbinfoQuickAction`: SMB connection info.

## LongCapture System (Continuous Packet/Event Capture)

**Base class:** `src/base/LongCapture.py` **Implementations:** `TcpdumpCapture`,
`TraceCmdCapture`

Each `LongCapture` owns one continuously-running recorder bound to a single
protocol. On a snapshot request it stops the recorder, bundles its rotated
output files into a sibling `aod_capture_*.tar.zst`, and restarts it.

- Fire-and-forget snapshots: `capture.snapshot(batch_id)` enqueues a request
  handled by the supervisor coroutine.
- Coalescing and cooldown: requests arriving while a snapshot is in progress, or
  during the post-restart warmup, are coalesced into the same bundle or dropped
  with a log line pointing at the shared bundle.
- Spawn back-off: after 3 consecutive spawn failures the capture disables itself
  for that protocol.
- Stop grace: `SIGINT`, wait `stop_grace_sec` (tcpdump 5s, trace-cmd longer),
  then `SIGKILL`.
- Output layout: live files in `<aod_output_dir>/captures/<protocol>/`; bundles
  written into `<aod_output_dir>/batches/`.

AOD owns the recorder's output flag (`-w` for tcpdump, `-o` for trace-cmd) and
the protocol port filter; user config supplies the remaining args.

## Configuration System

### Configuration Architecture

**Files:** `src/ConfigManager.py`, `src/utils/config_schema.py`,
`src/utils/anomaly_type.py`

- YAML config parsed into frozen dataclasses (`Config`, `AnomalyConfig`).
- Anomalies are keyed by an `AnomalyKey(protocol, anomaly_type)` NamedTuple.
- `PROTOCOL_SPEC` in `anomaly_type.py` is the single source of truth for which
  protocols exist, which anomaly types each supports, which tools source each
  type, and which filter axes/lookup tables each tool accepts.
- `ConfigManager` validates tool bindings, capture reserved/required flags, and
  per-axis tracking before the daemon starts.

### Config Structure (per anomaly)

Each anomaly entry declares a `tool`, detection knobs (e.g. `acceptable_count`,
`default_threshold_ms`, `track_commands`, `track_errors`), and an `actions:`
block mixing quick actions (null-valued keys such as `dmesg`, `journalctl`,
`debugdata`, `stats`, `mounts`, `smbinfo`, `syslogs`) and capture tools
(`tcpdump`, `trace-cmd`) whose value is a CLI-args list.

### Top-level Sections

- `watch_interval_sec`, `aod_output_dir`.
- `anomalies:` (per-protocol, per-anomaly-type config).
- `cleanup:` (`cleanup_interval_sec`, `max_log_age_days`,
  `max_total_log_size_mb`).

## Snapshot and Shutdown Flow

- Snapshot (`SIGUSR1`): `Controller.trigger_snapshot()` enqueues
  `AnomalyKey(Protocol.AOD, AnomalyType.SNAPSHOT)` onto `anomalyActionQueue`.
- Shutdown (`SIGTERM`/`SIGINT` or service stop): `Controller.stop()` enqueues a
  `SHUTDOWN` snapshot before the sentinels, sets `stop_event`, and pushes `None`
  sentinels to both queues. So, log bundles are always collected on shutdown.
- LogCollector treats `Protocol.AOD` events as full-system dumps: it runs every
  configured quick action once and requests a snapshot from every capture.

## Performance Characteristics

### Scalability

- Event ingestion: single-poll bulk drain into NumPy; minimal per-event Python
  overhead.
- Memory: preallocated scratch buffer plus NumPy batches.
- CPU: async quick-action collection; batch analysis.
- Disk: compressed log bundles plus SpaceWatcher pruning.

### Reliability

- Automatic thread/process restart, with escalation-to-shutdown for
  LogCollector.
- No event loss designed into the ring buffer path.
- Individual quick-action, handler, and capture failures are isolated.
- Comprehensive exception handling and logging.

## Security Considerations

- Root required: enforced at startup via `os.geteuid() != 0`, needed for eBPF
  loading and diagnostic access.
- Process isolation: eBPF collectors run as separate, supervised processes with
  `PDEATHSIG` orphan protection.
- Output location: bundles land in a configurable `aod_output_dir` (default
  `/var/log/aod`).

## Monitoring and Observability

- Debug metrics for developers: components track counts/timing/success rates
  when `__debug__` is enabled (that is, when not run with `python -O`).
- Component health: supervised thread/process restarts.
- Syslog integration (`src/utils/syslogger.py`): anomaly detections are logged
  at `CRITICAL`; component restarts at `WARNING`.

Example messages:

```
AOD detected anomaly: <anomaly> with <n> events, at UTC time <ts>
AOD component <name> restarted due to unexpected exit
```

## Deployment Architecture

### Packaging and Service

- Ships as Debian (`packages/debian/`) and RPM (`packages/rpm/`) packages that
  pull in `python3-numpy`, `python3-pyyaml` (`python3-yaml` on Debian), and
  `python3-zstandard`.
- Runs under systemd (`aodv2.service`, `Type=simple`, `User=root`,
  `KillSignal=SIGTERM`, `TimeoutStopSec=90s`).
- Daemon is launched as `"$AOD_PYTHON" -O /opt/aodv2/src/aod_entry.py`.
- Environment (via `aodv2.env` / `EnvironmentFile=/etc/aodv2/aodv2.env`):
  - `AOD_PYTHON`: interpreter (fallback `/usr/bin/python3`; override for a venv
    or a newer interpreter).
  - `AOD_CONFIG`: config path (default `/etc/aodv2/config.yaml`).
  - `PYTHONPATH=/opt/aodv2/src`.

### Entry Point and Preflight

`src/aod_entry.py` runs `utils.preflight.verify_runtime_deps()` before importing
any application module, checking the interpreter meets the minimum Python and
that `numpy` / `PyYAML` / `zstandard` are installed at satisfying versions, then
calls `Controller.main`.

### Prerequisites

- Python 3.11+ (`requires-python = ">=3.11"`).
- Root access for eBPF loading and diagnostics.
- Linux with eBPF ring buffer support (kernel 5.15+; newer kernels required for
  some eBPF collectors).
- Runtime deps: `numpy`, `zstandard`, `PyYAML`.

### Runtime Environment Variables

```bash
AOD_CONFIG=<path>                 # Config file (default /etc/aodv2/config.yaml)
AOD_PYTHON=<path>                 # Interpreter used by the systemd unit
AOD_LOG_LEVEL=INFO|DEBUG|WARNING  # Application log level
AOD_SYSLOG_LEVEL=WARNING          # Syslog level
AOD_LOG_STDERR=0|1                # Also log to stderr (default 0)
```

### Startup Sequence

1. Preflight: dependency/interpreter verification (`aod_entry.py`).
2. Logging setup and root check (`os.geteuid()`).
3. Config load (`AOD_CONFIG`).
4. Signal handlers installed (`SIGTERM`/`SIGINT` stop, `SIGUSR1` snapshot).
5. Supervision start: eBPF tool processes plus EventDispatcher, AnomalyWatcher,
   LogCollector, and SpaceWatcher threads.
6. Run until `stop_event`, then graceful shutdown.
