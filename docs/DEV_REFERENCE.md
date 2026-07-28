# AODv2 Developer's Guide

This is a task-oriented guide for extending AODv2: adding anomalies, handlers,
quick actions, captures, and tests. It focuses on _where to plug in_ and _what
invariants to preserve_. For the runtime model, read
[ARCHITECTURE.md](ARCHITECTURE.md) first; for the user-facing config surface,
read [CONFIGURATION.md](CONFIGURATION.md).

---

## 1. Overview

A single multi-threaded Python daemon (the `Controller`) supervises external
eBPF collector processes and a few userspace probes. Data flows through a
producer–consumer pipeline:

```
eBPF programs ─> /sys/fs/bpf/aodrb (ring buffer) ─> EventDispatcher ─> eventQueue
   ─> AnomalyWatcher ─> anomalyActionQueue ─> LogCollector ─> *.tar.zst <- SpaceWatcher
```

- **eBPF programs** ([monitoring_tools/src/bpf/](../monitoring_tools/src/bpf/))
  emit fixed-layout `struct event` records into a pinned ring buffer.
- **EventDispatcher** bulk-drains the ring buffer into a NumPy array and
  forwards batches.
- **AnomalyWatcher** runs registered handlers over each batch (eBPF handlers) or
  ticks userspace handlers, and enqueues an action on detection.
- **LogCollector** runs _quick actions_ (short diagnostic collectors) and _long
  captures_ (continuous tcpdump/trace-cmd), then bundles output.

Two independent "registries" drive most extension work:

| Registry                   | File                                                      | Maps                                         |
| -------------------------- | --------------------------------------------------------- | -------------------------------------------- |
| `PROTOCOL_SPEC`            | [src/utils/anomaly_type.py](../src/utils/anomaly_type.py) | protocol → anomaly type → tool → filter axes |
| `ANOMALY_HANDLER_REGISTRY` | [src/utils/anomaly_type.py](../src/utils/anomaly_type.py) | `AnomalyType` → handler class                |
| `action_factory`           | [src/LogCollector.py](../src/LogCollector.py)             | quick-action name → `QuickAction` factory    |
| `_CAPTURE_CLASSES`         | [src/LogCollector.py](../src/LogCollector.py)             | capture tool name → `LongCapture` class      |
| `TOOL_NAME_TO_ID`          | [src/utils/anomaly_type.py](../src/utils/anomaly_type.py) | eBPF tool name → tool id byte                |

Adding a feature almost always means: define the class, register it in the
matching table, and (if it introduces a config key) teach `ConfigManager` about
it.

---

## 2. Where things live

```
src/
  Controller.py        # supervision, signals, process/thread lifecycle
  EventDispatcher.py   # ring buffer → NumPy batches
  AnomalyWatcher.py    # batch analysis + userspace ticks → actions
  LogCollector.py      # quick actions + long captures + bundling
  SpaceWatcher.py      # disk cleanup of bundles
  ConfigManager.py     # YAML → validated Config dataclasses
  base/
    AnomalyHandlerBase.py  # AnomalyHandler / UserspaceAnomalyHandler ABCs
    QuickAction.py         # QuickAction ABC
    LongCapture.py         # LongCapture ABC
  handlers/            # concrete anomaly handlers, quick actions, captures
  utils/
    anomaly_type.py    # Protocol/AnomalyType enums + all registries
    shared_data.py     # C <-> Python struct layout, command/error id maps
    config_schema.py   # Config/AnomalyConfig/AnomalyKey dataclasses
monitoring_tools/
  src/bpf/             # *.bpf.c eBPF programs
  src/user/            # userspace loaders for each eBPF program
  src/common/          # shared C headers (aod_diag.h, *_diag.h, vmlinux.h)
  src/ringbuf_shim.c   # C shim EventDispatcher loads via ctypes
tests/                 # pytest suite + C shim test harness (tests/c/)
```

---

## 3. Recipe: add a new quick action

A quick action is a short, one-shot diagnostic collector (read a proc file, run
a command). Bundled into `aod_quick_<batch_id>.tar.zst`.

1. **Create the class** in [src/handlers/](../src/handlers/), subclassing
   `QuickAction` and implementing `get_command()`:

   ```python
   from base.QuickAction import QuickAction

   class MyQuickAction(QuickAction):
       def __init__(self, batches_root: str):
           super().__init__(batches_root, "mything.log")  # output filename

       def get_command(self) -> tuple[list[str], str]:
           # "cat" → direct file read; "cmd" → subprocess
           return ["cat", "/proc/fs/cifs/Stats"], "cat"
   ```

   The base class handles async execution, output paths, metrics, and _graceful
   failure_ (a failing action logs a warning and never raises). Do not raise out
   of `get_command()` for expected-missing files.

2. **Register the name** in `LogCollector.action_factory`
   ([src/LogCollector.py](../src/LogCollector.py)):

   ```python
   "mything": lambda: MyQuickAction(self.aod_output_dir),
   ```

3. **Allowlist the config key** by adding `"mything"` to `KNOWN_QUICK_ACTIONS`
   in [src/utils/anomaly_type.py](../src/utils/anomaly_type.py). Without this,
   `ConfigManager` rejects the key.

4. Users can now list `mything` under any anomaly's `actions:` in
   `config/config.yaml`. It also runs automatically on full-system snapshots.

**Keep in sync:** `KNOWN_QUICK_ACTIONS` <-> `action_factory` keys. A name in one
but not the other is a silent gap (config accepts it but nothing runs, or vice
versa).

---

## 4. Recipe: add a new anomaly type + handler

There are two handler execution models; pick based on your data source:

- **eBPF handler** (`AnomalyHandler.detect`): analyzes a NumPy batch of events
  that an eBPF tool produced. Use when the signal comes from the ring buffer.
- **Userspace handler** (`UserspaceAnomalyHandler.tick`): polled once per
  `watch_interval_sec`, independent of kernel events (e.g. reading `/proc/net`).

### 4a. Add the enum

Add to `AnomalyType` in
[src/utils/anomaly_type.py](../src/utils/anomaly_type.py):

```python
class AnomalyType(Enum):
    LATENCY = "latency"
    ERROR = "error"
    SOCKCONN = "sockconn"
    THROUGHPUT = "throughput"   # new
```

### 4b. Declare it in `PROTOCOL_SPEC`

Tell the system which protocol supports it, which tool sources it, and what
filter axes that tool accepts in
[src/utils/anomaly_type.py](../src/utils/anomaly_type.py):

```python
Protocol.SMB: {
    AnomalyType.THROUGHPUT: {
        "smbslower": {"track_commands": ALL_SMB_CMDS},
    },
},
```

You can also provide a new monitoring tool here.

`get_tool_axes(protocol, anomaly_type, tool)` reads this table; an undeclared
triple raises `KeyError` at config-parse time.

### 4c. Write the handler

eBPF example ([src/handlers/](../src/handlers/)):

```python
from base.AnomalyHandlerBase import AnomalyHandler
import numpy as np

class ThroughputAnomalyHandler(AnomalyHandler):
    def __init__(self, config):
        super().__init__(config)               # config is an AnomalyConfig
        self.acceptable_count = config.acceptable_count

    def detect(self, events_batch: np.ndarray) -> bool:
        # events_batch is already masked to this tool's id byte
        return len(events_batch) >= self.acceptable_count
```

Userspace handlers subclass `UserspaceAnomalyHandler` and implement `tick()`
instead; see
[SockconnAnomalyHandler.py](../src/handlers/SockconnAnomalyHandler.py).

Handlers receive the `AnomalyConfig` dataclass for their key. Read per-axis
config via `config.track` (shape varies by type — see
[config_schema.py](../src/utils/config_schema.py)).

### 4d. Register the handler

```python
ANOMALY_HANDLER_REGISTRY = {
    AnomalyType.LATENCY: LatencyAnomalyHandler,
    AnomalyType.ERROR: ErrorAnomalyHandler,
    AnomalyType.SOCKCONN: SockconnAnomalyHandler,
    AnomalyType.THROUGHPUT: ThroughputAnomalyHandler,   # new
}
```

Import at the bottom of the module (as existing handlers are) to avoid the
circular import between `anomaly_type` and the handler modules.

**Keep in sync:** `AnomalyType` enum <-> `PROTOCOL_SPEC` entries <->
`ANOMALY_HANDLER_REGISTRY`. `AnomalyWatcher._load_anomaly_handlers` warns and
skips any configured type with no registered handler.

---

## 5. Recipe: add a new eBPF tool

An eBPF tool is an external process that writes `struct event` records into the
ring buffer. This is the highest-risk extension because it crosses the C <->
Python boundary — read section 8 carefully.

1. **Write the eBPF program** in
   [monitoring_tools/src/bpf/](../monitoring_tools/src/bpf/) and its userspace
   loader in [monitoring_tools/src/user/](../monitoring_tools/src/user/). Emit
   records matching `struct event` from
   [aod_diag.h](../monitoring_tools/src/common/aod_diag.h), setting a unique
   `tool` byte.

2. **Assign a tool id** in `TOOL_NAME_TO_ID`
   ([src/utils/anomaly_type.py](../src/utils/anomaly_type.py)). This byte is how
   `AnomalyWatcher` masks a batch to the correct handler — it MUST match the
   value your eBPF program writes into `event.tool`.

3. **Wire supervision** in [src/Controller.py](../src/Controller.py): add a
   command builder and, if the tool must run continuously, a
   `_supervise_process` entry. Latency tools use `_get_latency_tool_cmd`; error
   tools use `_get_error_tool_cmd` — mirror the closest existing pattern.

4. **Declare it in `PROTOCOL_SPEC`** for the (protocol, anomaly type) it
   sources, including its filter axes.

5. **Build** it into `src/bin/` via `make build install-bins`. You would need to
   update the makefile and add this new tool as a target. (see section 7).

**Keep in sync:** the `tool` byte in your `.bpf.c` <-> `TOOL_NAME_TO_ID` <-> the
tool name used as a `PROTOCOL_SPEC` key and in config `source:`. If you end up
changing the event struct, make sure that it is reflected on the python side in
[src/utils/shared_data.py](../src/utils/shared_data.py).

---

## 6. Recipe: add a new long capture

Long captures are continuously-running recorders (tcpdump, trace-cmd) that
snapshot on demand. Subclass `LongCapture`
([src/base/LongCapture.py](../src/base/LongCapture.py)):

1. Implement `build_argv(output_path)` and set the `tool_name` /
   `output_extension` class attributes (see
   [TcpdumpCapture.py](../src/handlers/TcpdumpCapture.py)).
2. Register in `_CAPTURE_CLASSES`
   ([src/LogCollector.py](../src/LogCollector.py)).
3. Add the tool name to `CAPTURE_TOOLS`, and declare its reserved/required flags
   in `CAPTURE_RESERVED_FLAGS` / `CAPTURE_REQUIRED_FLAGS`
   ([src/utils/anomaly_type.py](../src/utils/anomaly_type.py)). AOD owns the
   output path and protocol filter; reserved flags are rejected if the user
   supplies them, and required flags must be present.

**Keep in sync:** `CAPTURE_TOOLS` <->`_CAPTURE_CLASSES` <-> the
reserved/required flag tables.

---

## 7. Recipe: add a new protocol

1. Add to the `Protocol` enum
   ([src/utils/anomaly_type.py](../src/utils/anomaly_type.py)). `Protocol.AOD`
   is synthetic (snapshot/shutdown) — do not reuse it.
2. Add a `PROTOCOL_SPEC` sub-tree for the protocol's anomaly types and tools.
3. Add its server port to `PROTOCOL_SERVER_PORT` if any userspace probe (e.g.
   sockconn) needs it.
4. Provide the command-id map (like `ALL_SMB_CMDS` / `ALL_NFS_CMDS`) in
   [shared_data.py](../src/utils/shared_data.py), sourced from the kernel's
   authoritative header.
5. Build the eBPF/userspace tool(s) that source its anomalies (section 5).

---

## 8. The C <-> Python contract (read before touching event layout)

The most fragile invariant in the codebase: `EventDispatcher` reads raw kernel
bytes directly into a NumPy array, so the C struct and the NumPy dtype must be
**byte-for-byte identical in size, field order, and alignment.**

Three definitions must stay in lockstep:

| Definition                          | File                                                                                |
| ----------------------------------- | ----------------------------------------------------------------------------------- |
| `struct event`                      | [monitoring_tools/src/common/aod_diag.h](../monitoring_tools/src/common/aod_diag.h) |
| `class Event(ctypes.Structure)`     | [src/utils/shared_data.py](../src/utils/shared_data.py)                             |
| `event_dtype` (NumPy, `align=True`) | [src/utils/shared_data.py](../src/utils/shared_data.py)                             |

Current layout:

```
pid              u32
command          u16
tool             char (1 byte)   ← tool id byte, matches TOOL_NAME_TO_ID
_pad             char (1 byte)   ← explicit padding to realign u64s
cmd_end_time_ns  u64
rqst_id          u64
metric           union { u64 latency_ns; int retval; }
task             char[16]
```

Rules when changing the event:

- Change all three definitions together, in the same field order.
- Keep the explicit `_pad` byte (or add equivalent) so 8-byte fields stay
  8-aligned. `event_dtype` uses `align=True`; the C struct relies on natural
  alignment — mismatched padding silently corrupts every field after the drift.
- `metric` is a union: `metric_latency_ns` in NumPy reads the same 8 bytes as
  either `latency_ns` (latency tools) or `retval` (error tools). The handler
  decides interpretation.
- If you resize the record, recheck `RB_MAX_RECORDS` in
  [shared_data.py](../src/utils/shared_data.py) — it is derived from the kernel
  ring size (`MAX_ENTRIES` in `aod_diag.h`) divided by per-record cost.

Other cross-language constants that must match:

- `TASK_COMM_LEN` (16) — `aod_diag.h` and `shared_data.py`.
- `RINGBUF_PINNED` (`/sys/fs/bpf/aodrb`) — `aod_diag.h` and `shared_data.py`.
- Command/error id maps (`ALL_SMB_CMDS`, `ALL_NFS_CMDS`, `ALL_NFS_ERRS`) must
  match the kernel enum values the eBPF program filters on.

The C shim test (section 9) exists specifically to catch layout/logic drift
without a live kernel and should be run after any event-layout change.

---

## 9. Building and testing

### Build the eBPF binaries

```bash
make build          # compiles monitoring_tools/ eBPF + userspace tools
make install-bins   # copies built binaries into src/bin/
```

Requires `clang`, `bpftool`, `libbpf`, and BTF (`/sys/kernel/btf/vmlinux`).

### Run the Python test suite

```bash
pip install -e '.[dev]'        # pytest, pytest-asyncio, black, etc.
pytest                         # full suite
pytest -m 'not slow'           # skip soak/integration (fast inner loop)
pytest tests/test_log_collector.py -q          # one file
pytest tests/test_space_watcher.py -k cleanup  # one pattern
```

`pyproject.toml` sets `pythonpath = ["src"]`, so imports use the same shape as
runtime (`from utils...`, `from handlers...`). The `slow` marker gates
long-running soak/integration tests.

### Run the C shim test

The C shim is compiled twice: production links real `libbpf`
([monitoring_tools/](../monitoring_tools/)); the test build
([tests/c/](../tests/c/)) compiles the _same_ `ringbuf_shim.c` against stubbed
`bpf/libbpf.h` headers so the C logic runs without a kernel.

```bash
make -C tests/c        # builds libringbuf_shim_test.so
```

The Python integration tests load that `.so` via `ctypes` to exercise the
ring-buffer drain path end to end.

### Adding tests

- Put unit tests in [tests/](../tests/) as `test_<component>.py`.
- Reuse shared fixtures from [tests/conftest.py](../tests/conftest.py):
  `make_fake_controller` (a `SimpleNamespace` Controller stand-in exposing
  `.config.<section>` and `.stop_event`), `make_batch` (sparse `aod_*.tar.zst`
  fixtures with backdatable mtimes), and `install_fake_tcpdump` /
  `install_fake_tracecmd` (bash stubs wired via `AOD_TCPDUMP_BIN` /
  `AOD_TRACECMD_BIN`).
- Mark anything that sleeps or spawns real processes for seconds with
  `@pytest.mark.slow`.
- Format with `black` (line length 85, configured in `pyproject.toml`).

Most components take only a `controller`-like object, so you can test them in
isolation without a running daemon, kernel, or root. Snapshots in particular do
not require the ring buffer — you can drive
`LogCollector._create_log_collection_task` directly (see the repo snapshot notes
for a minimal config).

---

## 10. Running AOD directly (verbose / without systemd)

In production AOD runs as the `aodv2.service` systemd unit. For development you
usually want to run the entry point by hand so you can see logs live and iterate
without installing. The daemon is configured entirely through environment
variables read in `main()` ([src/Controller.py](../src/Controller.py)); there
are no CLI flags yet.

The service normally logs only to syslog (`WARNING`+). Two things make a manual
run verbose: routing logs to your terminal (`AOD_LOG_STDERR=1`), and lowering
the log level (`AOD_LOG_LEVEL=DEBUG`).

### Environment variables

| Variable           | Default                     | Effect                                                         |
| ------------------ | --------------------------- | -------------------------------------------------------------- |
| `AOD_CONFIG`       | `src/../config/config.yaml` | Path to the YAML config file.                                  |
| `AOD_LOG_LEVEL`    | `INFO`                      | Root log level (`DEBUG`/`INFO`/`WARNING`/…).                   |
| `AOD_LOG_STDERR`   | `0`                         | `1` adds a stderr handler so logs appear in your terminal.     |
| `AOD_SYSLOG_LEVEL` | `WARNING`                   | Minimum level forwarded to syslog (`/dev/log`).                |
| `AOD_TCPDUMP_BIN`  | `tcpdump` on `PATH`         | Override the tcpdump binary (used by capture tests/dev stubs). |
| `AOD_TRACECMD_BIN` | `trace-cmd` on `PATH`       | Override the trace-cmd binary.                                 |

### Run it in the foreground

Root is required (eBPF loading + privileged log sources), and `src/` must be on
`PYTHONPATH` so the `utils`/`handlers` imports resolve the same way they do
under systemd:

```bash
sudo AOD_LOG_LEVEL=DEBUG \
     AOD_LOG_STDERR=1 \
     AOD_CONFIG="$PWD/config/config.yaml" \
     PYTHONPATH="$PWD/src" \
     python3 src/aod_entry.py
```

Stop it with `Ctrl+C` (`SIGINT`) — the signal handler triggers a graceful
shutdown. Send `SIGUSR1` to force a full-system snapshot bundle.

### `-O` and `__debug__`: the verbosity switch

The systemd unit launches the interpreter with `python3 -O`. `-O` sets
`__debug__` to `False`, which strips out every `if __debug__:` block — and most
of AOD's `logger.debug`/`logger.info` calls and per-handler metrics live inside
those blocks. So:

- **Verbose dev run:** use plain `python3` (no `-O`). `__debug__` is `True`, the
  debug logging and metrics compile in, and `AOD_LOG_LEVEL=DEBUG` surfaces them.
- **Production-like run:** add `-O` to match the service. Debug blocks are gone
  regardless of `AOD_LOG_LEVEL`, so don't rely on them for troubleshooting a
  packaged install.

### Minimal / non-root runs

You cannot run the full daemon without root (the `os.geteuid() != 0` check in
`main()` raises immediately, and the ring buffer must be pinned). To exercise
logic without a kernel or root, drive components directly from tests instead —
see section 9 and the snapshot notes.

### When installed as a service

Override any of the variables above in `/etc/aodv2/aodv2.env` (an
`EnvironmentFile` for the unit), then:

```bash
sudo systemctl restart aodv2
journalctl -u aodv2 -f          # follow the daemon's syslog output
sudo systemctl kill -s SIGUSR1 aodv2   # trigger an on-demand snapshot
```

Because the unit runs with `-O`, raising `AOD_LOG_LEVEL` to `DEBUG` there only
adds the non-`__debug__` messages; for full verbosity reproduce the issue with a
foreground run as above.

---

## 11. Invariant checklist (things that break silently)

Before you open a PR, confirm the paired definitions below still agree:

- [ ] **Event layout:** `struct event` == `Event` ctypes == `event_dtype` (size,
      order, alignment).
- [ ] **Tool id byte:** value written in `*.bpf.c` == `TOOL_NAME_TO_ID` entry.
- [ ] **Quick action:** name in `KNOWN_QUICK_ACTIONS` == key in
      `action_factory`.
- [ ] **Anomaly type:** enum member has both a `PROTOCOL_SPEC` entry and an
      `ANOMALY_HANDLER_REGISTRY` handler.
- [ ] **Capture tool:** name in `CAPTURE_TOOLS` == key in `_CAPTURE_CLASSES`,
      with reserved/required flag tables populated.
- [ ] **Shared constants:** `TASK_COMM_LEN`, `RINGBUF_PINNED`, `RB_MAX_RECORDS`,
      command/error id maps match their C counterparts.
- [ ] **New config keys** are validated in `ConfigManager` and documented in
      [CONFIGURATION.md](CONFIGURATION.md).
- [ ] Handler imports for the registry sit at the _bottom_ of `anomaly_type.py`
      (circular-import guard).
- [ ] Ran `pytest -m 'not slow'` and, for C changes, `make -C tests/c`.
- [ ] **Formatting** new docs (markdown) is formatted with `Prettier` (line
      length 80) and python code is formatted with `black` (line length 85).
