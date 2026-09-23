# Linux Diagnostics Controller (AODv2)

Real-time monitoring and automated diagnostics collection system for Linux environments using eBPF tools to detect anomalies and collect diagnostic data.

## 🎯 Key Features

- **Real-time Anomaly Detection**: Sub-second detection of latency spikes and error patterns
- **Automated Diagnostics**: Instant collection of relevant system data when anomalies occur
- **Low Overhead Monitoring**: eBPF-based tools with minimal performance impact
- **Configurable Thresholds**: Customizable detection parameters for different environments
- **Intelligent Cleanup**: Automatic disk space management

## 🚀 How to Run

### Prerequisites
- Linux kernel 5.15+ with eBPF support (6.8+ required for future eBPF scripts)
- Python 3.10+
- Root access for eBPF program loading

### Clone and Run
```bash
# Check Python version (requires 3.10+)
python3 --version

# Clone repository
git clone https://github.com/Azure/AODv2.git
cd AODv2

# Install dependencies
pip3 install -r requirements.txt

# Run the application
sudo python3 src/Controller.py 

# With debug logging
sudo AOD_LOG_LEVEL=DEBUG python3 src/Controller.py 

# With minimal overhead
sudo python3 -O src/Controller.py 
```

If `pip3 install` is refused with `externally-managed-environment`, add
`--break-system-packages`. The daemon runs as root, so install for root:
`sudo pip3 install --break-system-packages -r requirements.txt`.

### Stop the Application
```bash
# Graceful shutdown with Ctrl+C
Ctrl+C
```

## ☁️ Auto-Upload to Azure Storage

Sends each diagnostic package to a storage account **in your own subscription**.
Disabled by default. AODv2 never creates cloud resources and never deletes cloud
data — retention is handled entirely on the Azure side.

Steps 1 to 3 are done once by an Azure admin. Step 4 runs on each host and
needs no Azure access at all — get the three values from your admin and skip
to it.

Already deployed for another host? The storage account, container, quota and
identity are shared, so steps 1 and 2 are not repeated. Do step 3 for the new
host, then step 4 on it.

### Step 1 — Sign in (admin)

Needs **Owner**, or **Contributor + User Access Administrator**, on an existing
resource group. Contributor alone cannot grant roles.

On the VM, check whether it already has a managed identity:

```bash
curl -s -H Metadata:true \
  "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/"
```

A token means yes. An error means none is attached — enable one in
*portal > your VM > Identity > System assigned > On*, and copy the
**Object (principal) ID** it shows.

That identity starts with no permissions. In
[Cloud Shell](https://shell.azure.com), have an admin grant it rights on the
resource group:

```bash
az role assignment create --assignee <vm-principal-id> --role Owner \
  --scope /subscriptions/<sub-id>/resourceGroups/<resource-group>
```

Then back on the VM, sign in as that identity — no browser, no device code:

```bash
az login --identity
```

Working in Cloud Shell instead? You are signed in already; just pick the
subscription:

```bash
az account list --output table
az account set --subscription "<subscription-name-or-id>"
```

### Step 2 — Create the Azure resources (admin, once)

```bash
# The identity the hosts will upload with. One is shared by the fleet.
az identity create -g <resource-group> -n aod-uploader

cd infra
./setup.sh deploy -g <resource-group> --uami aod-uploader --quota-gb 50
```

That creates a dedicated storage account with key access disabled, a container,
an age-based lifecycle rule, a **custom role that can write but not delete**,
and a function that keeps the container under `--quota-gb`. It shows a summary
and asks before creating anything.

`--quota-gb` and `--retention-days` apply to the container, which every host
shares. Change the budget later with
`./setup.sh set-quota -g <resource-group> --quota-gb 200`.

### Step 3 — Attach the identity to the hosts (admin)

Each host that uploads needs the identity attached. Use the portal
(*VM → Identity → User assigned*), Azure Policy, Terraform, or:

```bash
UAMI=$(az identity show -g <resource-group> -n aod-uploader --query id -o tsv)
az vm identity assign -g <resource-group> -n <vm-name> --identities "$UAMI"
```

This needs no Owner rights, and no RBAC change however many hosts you add — the
role was granted once in step 2 and covers every VM holding the identity.

Then print the values the hosts need:

```bash
./setup.sh show-config -g <resource-group>
```

The Owner grant from step 1 is only needed while setting up. Once the hosts are
attached, remove it — otherwise anyone with root on that VM has Owner on the
resource group:

```bash
az role assignment delete --assignee <vm-principal-id> --role Owner \
  --scope /subscriptions/<sub-id>/resourceGroups/<resource-group>
```

### Step 4 — Turn upload on (each host, as root)

Paste the three values from step 3. Nothing here contacts Azure's control
plane, so the host needs no CLI, no sign-in and no permissions:

```bash
sudo ./setup.sh enable-upload \
     --account <storage-account> --container <container> \
     --client-id <uploader-client-id>
```

Settings go to `config.d/10-upload.yaml`; `config.yaml` is never modified.
Restart the daemon to apply them.

Uploading starts immediately, and **packages already on disk are uploaded too**,
so turning it on after an incident still ships that incident's evidence.

### Checking it

```bash
PYTHONPATH=src python3 -m cli upload status      # uploaded, pending, errors
PYTHONPATH=src python3 -m cli upload validate    # re-check identity and permissions
PYTHONPATH=src python3 -m cli upload disable     # stop uploading; collection continues
```

`upload validate` is the one to run first when uploads are not arriving — it
names the specific cause rather than failing generically.

| Symptom | Cause and fix |
|---|---|
| no usable managed identity | The VM has no managed identity, or IMDS is unreachable |
| forbidden (403) | The role assignment is missing or still propagating. Wait a minute, then re-check |
| did not resolve | Wrong account name, or a private endpoint this host cannot reach |
| `BLOCKED_AUTH` / `BLOCKED_DESTINATION` in `status` | Upload is paused pending a config fix. Collection continues |

Design rationale for every choice here is in
**[docs/UPLOAD_DESIGN.md](docs/UPLOAD_DESIGN.md)**.

## 📚 Documentation

For comprehensive documentation, see the [docs/](docs/) directory:

- **[Architecture Guide](docs/ARCHITECTURE.md)** - System architecture and design
- **[Configuration Guide](docs/CONFIGURATION.md)** - Configuration options and examples
- **[API Reference](docs/API_REFERENCE.md)** - Complete API documentation for all classes and functions
- **[Upload Design](docs/UPLOAD_DESIGN.md)** - Auto-upload decisions and design
- **[Usage Guide](USAGE.md)** - Advanced usage and monitoring tools

## 🏗️ System Architecture

AODv2 implements a multi-threaded architecture with five core components operating in a coordinated producer-consumer model:

### Core Components

- **Controller**: Main orchestrator managing all components, handles process lifecycle, thread supervision with automatic restart capabilities, and graceful shutdown coordination
- **EventDispatcher**: Collects events from eBPF programs via shared memory ring buffer, converts C structs to NumPy arrays, and queues events for analysis
- **AnomalyWatcher**: Analyzes event batches using pluggable handlers, detects anomalies based on configurable thresholds, and triggers diagnostic collection
- **LogCollector**: Executes diagnostic collection actions using async semaphore-bounded tasks, compresses logs with zstd, and organizes output by timestamp
- **SpaceWatcher**: Monitors disk usage autonomously, performs size-based and age-based cleanup to prevent disk space exhaustion

### Communication Flow

```
eBPF Programs → Shared Memory → EventDispatcher → eventQueue → AnomalyWatcher → anomalyActionQueue → LogCollector
```

**Inter-component Communication:**
- **Event Queue**: Thread-safe queue carrying monitoring events (NumPy arrays) from EventDispatcher to AnomalyWatcher
- **Anomaly Action Queue**: Task queue carrying anomaly actions from AnomalyWatcher to LogCollector
- **Shared Memory**: Ring buffer for lock-free communication between eBPF and Python processes

### Processing Model

**Event Processing:**
1. eBPF programs capture SMB events and write to shared memory ring buffer
2. EventDispatcher polls ring buffer, batches events for efficiency 
3. AnomalyWatcher processes events in configurable intervals with specialized handlers
4. Detected anomalies trigger LogCollector to execute QuickActions asynchronously
5. SpaceWatcher maintains disk space by cleaning old logs based on size/age thresholds

**Fault Tolerance:**
- Thread supervision with automatic restart on component failures
- Graceful shutdown with proper resource cleanup
- No event loss through ring buffer design and batch processing

For detailed architecture information, see the [Architecture Guide](docs/ARCHITECTURE.md).

## 📁 Project Structure

```
linux_diagnostics/
├── src/                          # Core application source code
│   ├── Controller.py             # Main service controller and orchestrator
│   ├── AnomalyWatcher.py         # Anomaly detection engine
│   ├── EventDispatcher.py        # Event routing from eBPF to Python
│   ├── LogCollector.py           # Diagnostic data collection and compression
│   ├── SpaceWatcher.py           # Disk usage monitoring and cleanup
│   ├── ConfigManager.py          # Configuration loading and validation
│   ├── shared_data.py            # Shared constants (e.g., SMB commands, error codes)
│   ├── base/                     # Abstract base classes for core components
│   │   ├── AnomalyHandlerBase.py # Interface for anomaly handlers
│   │   └── QuickAction.py        # Interface for diagnostic actions
│   ├── handlers/                 # Concrete implementations of handlers and actions
│   │   ├── latency_anomaly_handler.py    # Logic for latency anomaly detection
│   │   ├── error_anomaly_handler.py      # Logic for error anomaly detection
│   │   └── ...                   # Implementations of all QuickActions
│   ├── utils/                    # Utility modules and helper functions
│   │   ├── anomaly_type.py       # Enum for anomaly types
│   │   └── config_schema.py      # Dataclasses for configuration schema
│   └── bin/                      # Compiled eBPF binaries
│       └── smbsloweraod          # eBPF tool for monitoring SMB latency
├── config/                       # Configuration files
│   └── config.yaml               # Main configuration file (user-editable)
├── packages/                     # Package building scripts (DEB and RPM)
├── tests/                        # Test suite for the application
│   ├── test_controller.py        # Unit tests for the Controller
│   └── ...                       # Other unit and integration tests
├── linux_diagnostics.service     # Systemd service definition file
├── Makefile                      # Build automation for packages and code quality
├── pyproject.toml                # Python project configuration (PEP 621)
├── USAGE.md                      # Detailed usage and configuration guide
└── README.md                     # This file (overview and architecture)
```





