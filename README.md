# AODv2 — Always-On Diagnostics for Linux SMB & NFS

AODv2 is a background service that monitors Linux SMB (CIFS) and NFS traffic. On
detecting an anomaly — a latency spike, a burst of protocol errors, or a
socket-connection fault — it automatically collects the corresponding diagnostic
data. It runs continuously at low overhead using eBPF probes and writes its
findings as compressed bundles suitable for offline analysis.

---

## Capabilities

- SMB and NFS monitoring out of the box.
- Automatic diagnostic collection on anomaly detection.
- On-demand full-system snapshots via a single signal.
- Autonomous disk management through age- and size-based bundle cleanup.
- Operation as a managed `systemd` service.

---

## Requirements

| Requirement      | Details                                                                    |
| ---------------- | -------------------------------------------------------------------------- |
| Operating system | Linux kernel with eBPF and BTF support (5.15+; select probes require 6.8+) |
| Python           | 3.11 or newer                                                              |
| Privileges       | root (required for eBPF loading and privileged log sources)                |
| Runtime packages | `numpy`, `zstandard`, `PyYAML`                                             |
| External tools   | `tcpdump` and `trace-cmd`, if packet or trace captures are enabled         |
| Kernel modules   | `cifs` and `nfs` must be loaded for SMB and NFS monitoring respectively    |

### Verify prerequisites

Confirm the kernel exposes BTF type information:

```bash
# The file must exist; a non-empty size confirms BTF is available.
ls -l /sys/kernel/btf/vmlinux
```

Confirm the protocol modules are loaded for the traffic you intend to monitor:

```bash
# SMB (CIFS)
lsmod | grep -q '^cifs' && echo "cifs loaded" || sudo modprobe cifs

# NFS
lsmod | grep -q '^nfs' && echo "nfs loaded" || sudo modprobe nfs
```

---

## Obtaining AODv2

We recommend installing AODv2 from the prebuilt `.deb` or `.rpm` package hosted
on `packages.microsoft.com`. These packages bundle the compiled eBPF probes, so
no build toolchain is required.

```bash
# Download from packages.microsoft.com
# (steps to be added)
```

If you want to build AODv2 from source, refer to
[Building from Source](#building-from-source).

---

## Installation

For a prebuilt package, or one produced via `make deb` / `make rpm`:

```bash
# Debian / Ubuntu
sudo dpkg -i aodv2_<version>_amd64.deb

# RHEL / Fedora / SLES
sudo rpm -i aodv2-<version>.rpm
```

The package always installs system-wide, regardless of interpreter choice:

- Application files: `/opt/aodv2`
- Configuration: `/etc/aodv2/config.yaml`
- Interpreter overrides: `/etc/aodv2/aodv2.env`
- `systemd` unit: `aodv2`

The unit sets `PYTHONPATH=/opt/aodv2/src` internally, so no manual path
configuration is required.

### Selecting the Python interpreter

The daemon runs under the interpreter named by `AOD_PYTHON`, configured in
`/etc/aodv2/aodv2.env`. Choose one of the following.

- **System interpreter (default).** Leave `AOD_PYTHON` unset. The unit falls
  back to `/usr/bin/python3`, which should be ensured to be 3.11+ on the host
  system. Runtime dependencies are satisfied by the `python3-numpy`,
  `python3-yaml` (`python3-pyyaml` on Fedora), and `python3-zstandard` packages
  pulled in automatically.

- **Alternate system interpreter.** If the default `python3` is older than 3.11,
  install a newer one and point `AOD_PYTHON` at it:

  ```bash
  # /etc/aodv2/aodv2.env
  AOD_PYTHON=/usr/bin/python3.11
  ```

- **Dedicated virtual environment.** To isolate the runtime dependencies:

  ```bash
  sudo python3.11 -m venv /opt/aodv2-venv
  sudo /opt/aodv2-venv/bin/pip install numpy PyYAML zstandard
  ```

  Then set:

  ```bash
  # /etc/aodv2/aodv2.env
  AOD_PYTHON=/opt/aodv2-venv/bin/python
  ```

After editing `/etc/aodv2/aodv2.env`, restart the service:

```bash
sudo systemctl restart aodv2
```

### Verify the installation

```bash
# The unit is active (running)
systemctl status aodv2

# The daemon started cleanly, with no import or preflight errors
sudo journalctl -u aodv2 -b --no-pager | tail -n 20

# A diagnostic bundle is produced on demand (see On-demand snapshots)
sudo systemctl kill --kill-whom=main -s SIGUSR1 aodv2
ls -1 /var/log/aod/batches/
```

A healthy install shows `active (running)`, logs no tracebacks, and writes a new
`aod_quick_*.tar.zst` bundle after the snapshot signal.

---

## Building From Source

For development, or on hosts where an external package install is undesirable.
In addition to the [prerequisites](#requirements), building requires the
following tools:

- `clang`
- `llvm`
- `libbpf-dev`
- `bpftool`
- `make`

```bash
# 1. Obtain the source
git clone https://github.com/Azure/AODv2.git
cd AODv2

# 2. Create and activate a virtual environment (Python 3.11+)
python3.11 -m venv .venv
source .venv/bin/activate

# 3. Install AODv2 and its runtime dependencies
pip install .

# 4. Build the eBPF probes and stage them into src/bin/
make build install-bins
```

> `pip install .` installs the Python package and its runtime dependencies only;
> it does not compile or stage the eBPF probes. Step 4 is what builds them into
> `src/bin/`, from where a from-source run loads them directly.

> The service must run as root, whereas the virtual environment resides in a
> user account. When launching from a virtual environment as root, invoke its
> interpreter explicitly: `sudo .venv/bin/python src/aod_entry.py`.

You can run AODv2 now as a Python program, or build your own RPM/DEB package to
install and run it as a `systemd` service.

Building packages requires the eBPF build tools listed above plus the relevant
packaging toolchain:

- RPM: `rpm-build` (provides `rpmbuild`) and `systemd-rpm-macros`
- DEB: `dpkg-dev` (provides `dpkg-buildpackage`) and `debhelper` (compat 13)

```bash
make rpm   # output: rpms/aodv2-<version>.rpm
make deb   # output: debs/aodv2_<version>_amd64.deb
```

For installing the DEB/RPM packages and post-install config, refer to
[Installation](#installation).

---

## Configuration

AODv2 reads a single YAML file defining what to monitor and what to collect.

- Packaged install: `/etc/aodv2/config.yaml`
- From source: `config/config.yaml`, or any path set via the `AOD_CONFIG`
  environment variable.

The primary setting is the output directory for diagnostic bundles:

```yaml
aod_output_dir: /var/log/aod # default bundle output location
```

Anomaly thresholds, diagnostic selection, and cleanup policy are documented in
[USAGE.md](USAGE.md).

---

## Operation

### As a `systemd` service

```bash
# Enable and start
sudo systemctl enable --now aodv2

# Query status
systemctl status aodv2

# Follow logs
sudo journalctl -u aodv2 -f
```

### From source (foreground)

```bash
sudo AOD_CONFIG=./config/config.yaml \
     PYTHONPATH=./src \
     python3 src/aod_entry.py
```

Terminate with Ctrl+C. AODv2 shuts down gracefully and writes a final diagnostic
bundle before exiting.

---

## On-demand snapshots

A full diagnostic bundle may be requested at any time, independent of anomaly
detection:

```bash
# Packaged service
sudo systemctl kill --kill-whom=main -s SIGUSR1 aodv2
```

For a foreground run, locate the controller PID first:

```bash
# Foreground run: match the entry-point process
pgrep -f aod_entry.py

# Signal the process directly
sudo kill -USR1 <pid>
```

Bundles are written to `<aod_output_dir>/batches/`. Bundle contents and naming
are described in [USAGE.md](USAGE.md).

---

## Uninstallation

```bash
# Packaged install
sudo systemctl disable --now aodv2
sudo dpkg -r aodv2        # Debian / Ubuntu
sudo rpm -e aodv2         # RHEL / Fedora / SLES

# From source: stop the process and remove the checkout and virtual environment.
```

---

## Documentation

For comprehensive documentation, refer to the below:

- **[Architecture Guide](docs/ARCHITECTURE.md)** - System architecture and
  design
- **[Configuration Guide](docs/CONFIGURATION.md)** - Configuration options and
  examples
- **[API Reference](docs/API_REFERENCE.md)** - Complete API documentation for
  all classes and functions
- **[Usage Guide](USAGE.md)** - Advanced usage and monitoring tools
