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
- Python 3.9+
- Root access for eBPF program loading

### Clone and Run
```bash
# Check Python version (requires 3.9+)
python3 --version

# Clone repository
git clone <repository-url>
cd aodv2

# Install dependencies
pip3 install -r requirements.txt

# Run the application
sudo python3 src/Controller.py 

# With debug logging
sudo AOD_LOG_LEVEL=DEBUG python3 src/Controller.py 

# With minimal overhead
sudo python3 -O src/Controller.py 
```

### Stop the Application
```bash
# Graceful shutdown with Ctrl+C
Ctrl+C
```

## 📚 Documentation

For comprehensive documentation, see the [docs/](docs/) directory:

- **[Architecture Guide](docs/ARCHITECTURE.md)** - System architecture and design
- **[Configuration Guide](docs/CONFIGURATION.md)** - Configuration options and examples
- **[API Reference](docs/API_REFERENCE.md)** - Complete API documentation for all classes and functions
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
aodv2/
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
├── aodv2.service                 # Systemd service definition file
├── Makefile                      # Build automation for packages and code quality
├── pyproject.toml                # Python project configuration (PEP 621)
├── USAGE.md                      # Detailed usage and configuration guide
└── README.md                     # This file (overview and architecture)
```





