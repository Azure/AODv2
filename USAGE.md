# Advanced Usage and Developer Guide

This document provides advanced guidance for developers and power users, including details on monitoring, testing, and extending the AOD system. For installation and basic configuration, please see the main [README.md](README.md).

## 📋 Requirements and Prerequisites

### System Requirements

**Operating System:**
- Linux kernel 5.15+ with eBPF support
- Note: Future eBPF scripts will require kernel 6.8+

**Permissions:**
- Root access required for eBPF program loading and system monitoring

### Python Requirements

**Python Version:**
- Python 3.9+ (based on project configuration)

### Python Dependencies

The core AOD system requires specific Python packages listed in `requirements.txt`:

```bash
# Core monitoring dependencies
pandas>=1.5.0      # Data analysis and manipulation
matplotlib>=3.5.0  # Plotting and visualization  
numpy>=1.21.0      # Numerical computing and array operations

# Fast compression library
zstandard>=0.19.0  # Log compression

# Configuration parsing
PyYAML>=6.0        # YAML configuration file parsing
```

**Development Dependencies** (in `requirements-dev.txt`):
```bash
pytest>=7.0.0      # Unit testing framework
black>=22.0.0      # Code formatting
flake8>=4.0.0      # Code linting
```

## 📊 Monitoring & Analysis Tools

The AODv2 system includes several standalone tools for performance monitoring and data analysis.

### System Resource Monitoring (`monitor.py`)

Monitor CPU and memory usage over time:

```bash
# Monitor for 5 minutes (default)
python3 monitor.py

# Monitor for 10 minutes with 2-second intervals
python3 monitor.py 2 10

# Monitor for 30 minutes with 5-second intervals  
python3 monitor.py 5 30
```

**Output:** `system_usage_YYYYMMDD_HHMMSS.csv`

### Disk Usage Monitor (`disk_monitor.py`)

Monitor AOD log directory growth:

```bash
# Monitor every 5 seconds indefinitely
python3 disk_monitor.py

# Monitor every 10 seconds for 30 minutes
python3 disk_monitor.py 10 30

# Quick 5-minute test
python3 disk_monitor.py 2 5
```

**Output:** `disk_usage_YYYYMMDD_HHMMSS.csv`

### Performance Comparison (`compare.py` and `csv_range_compare.py`)

**`compare.py` (Basic):**
Generate side-by-side performance comparisons:
```bash
# Compare two system monitoring runs
python3 compare.py system_usage_baseline.csv system_usage_with_aod.csv
```
**Output:** `comparison_graph.png` with statistics

**`csv_range_compare.py` (Advanced):**
Compare two CSV files with range-based analysis (0-200s, 200-500s, 500-800s).
Note that these ranges can be configurable, for now its hardcoded. 
The ranges you want to use will depend on the workload you are running and what kind of time ranges you want to analyze.
```bash
# Analyze both CPU and Memory
python3 csv_range_compare.py baseline.csv test.csv
# Analyze specific column
python3 csv_range_compare.py without_aod.csv with_aod.csv CPU_Percent
```
Features:
- **Automatic column detection**: Finds CPU and Memory columns automatically
- **Range-based analysis**: Separate statistics for different time periods
- **Visual comparisons**: Generates detailed plots for each range
- **Comprehensive metrics**: Average, max, min, standard deviation, and percentage changes
- **Flexible input**: Supports various timestamp formats and column naming

### Disk Usage Visualization (`disk_plot.py`)

Visualize disk usage growth over time:

```bash
# Plot single monitoring session
python3 disk_plot.py disk_usage_20241224_120000.csv

# Compare disk usage between two sessions
python3 disk_plot.py disk_baseline.csv disk_with_aod.csv
```

**Output:** `disk_usage_plot.png` or `disk_usage_comparison.png`

## 📊 Performance Testing Workflow: AOD Impact Analysis

This workflow details how to accurately measure the performance impact of AODv2 on your system while it's under a specific workload. The key is to compare system behavior with and without AOD running.

**Step 1: Establish a Baseline (Without AOD)**

First, measure your system's performance under your chosen workload *without* AOD running. This gives you a baseline to compare against.

*   **Action**: In separate terminals, start your workload and the monitoring scripts.
*   **Example**:
    ```bash
    # Terminal 1: Start your workload (e.g., a file transfer script, database query, etc.)
    ./run_my_workload.sh

    # Terminal 2: Monitor system CPU and Memory
    python3 tests/monitor.py 10 10
    # --> Let's rename the output CSV to baseline_system_usage.csv

    # Terminal 3: Monitor disk usage (optional for baseline, but good practice)
    python3 tests/disk_monitor.py 10 10
    # --> Let's rename the output CSV to baseline_disk_usage.csv
    ```
*   **Result**: You will have baseline performance data, primarily `baseline_system_usage.csv`.

**Step 2: Measure Performance with AOD**

Now, repeat the exact same test, but with the AOD service running concurrently.

*   **Action**: In separate terminals, start the AOD service, your workload, and the monitoring scripts.
*   **Example**:
    ```bash
    # Terminal 1: Start the AOD service as root
    sudo python3 src/Controller.py

    # Terminal 2: Start the same workload as before
    ./run_my_workload.sh

    # Terminal 3: Monitor system CPU and Memory
    python3 tests/monitor.py 10 10
    # --> Let's rename the output CSV to aod_system_usage.csv

    # Terminal 4: Monitor AOD's disk usage
    python3 tests/disk_monitor.py 10 10
    # --> Let's rename the output CSV to aod_disk_usage.csv
    ```
*   **Result**: You will have performance data with AOD active (`aod_system_usage.csv`) and data on its disk activity (`aod_disk_usage.csv`).

**Step 3: Compare the Results**

With both sets of CSV files, you can now use the provided analysis tools to generate a clear comparison.

*   **To Compare CPU and Memory Impact**:
    Use `compare.py` or the more advanced `csv_range_compare.py` with your system usage files.
    ```bash
    # Generate a visual comparison of system performance
    python3 tests/compare.py baseline_system_usage.csv aod_system_usage.csv
    ```
    This will create a `comparison_graph.png` showing the performance difference.

*   **To Analyze Disk Usage**:
    Use `disk_plot.py` to visualize how much disk space AOD used during the test.
    ```bash
    # Generate a plot of AOD's disk space consumption
    python3 tests/disk_plot.py aod_disk_usage.csv
    ```
    This will create a `disk_usage_plot.png`.

## 🧪 Testing the System

A robust testing strategy is essential for maintaining the quality and reliability of AOD.

### Unit Tests

The project uses `pytest` for unit testing individual components.

```bash
# Install testing dependencies
pip3 install pytest

# Run all unit tests
pytest tests/
```

### Integration Tests

**Currently, the project lacks a dedicated integration testing suite.** This is a critical area for future improvement.

Integration tests would verify that the components work together as expected in a realistic environment. A typical test case would involve:
1.  Starting the `aodv2` service.
2.  Using a workload to trigger a specific anomaly (e.g., high latency).
3.  Asserting that the `AnomalyWatcher` correctly detects the anomaly.
4.  Verifying that the `EventDispatcher` triggers the configured `QuickActions`.
5.  Checking the output directory to ensure the correct diagnostic files were created and have the expected content.

## 🔧 Extending AOD

The AOD system is designed to be extensible. You can add new anomaly detectors and diagnostic actions to tailor it to your specific needs.

### Adding a New Anomaly Type

Let's walk through the process using the `error` anomaly and `smbiosnoop` eBPF tool as a concrete example.

#### Step 1: Add the eBPF Tool

**1.1 Obtain or Create Your eBPF Tool**

First, you need an eBPF tool that will produce the data for your anomaly detection. For this example, we'll use a hypothetical `smbiosnoop` tool that traces SMB I/O error codes and prints them to standard output.

eBPF tools typically:
- Monitor kernel events (syscalls, network packets, file I/O, etc.)
- Filter relevant events based on criteria
- Output structured data (often line-by-line text or JSON)

**1.2 Place the eBPF Binary**

Place your compiled eBPF tool in the `src/bin/` directory:

```bash
# Example: Copy your compiled eBPF tool
cp /path/to/your/smbiosnoop src/bin/smbiosnoop
chmod +x src/bin/smbiosnoop
```

**Naming Convention**: Use descriptive names that indicate the tool's purpose (e.g., `smbiosnoop`).

**1.3 Test the eBPF Tool Independently**

Before integrating with AOD, verify your eBPF tool works correctly:

```bash
# Test the tool directly
sudo src/bin/smbiosnoop

```

**1.4 Understand the Output Format**

Document the output format of your eBPF tool. This will be crucial for writing the anomaly detection logic later. 

**Key Requirements:** (not sure abt this)
- Tool must **emit events continuously** to the shared memory (/dev/shm) which the event dispatcher reads from
- Events must follow the same format as `Event` Class specified in shared_data.py
- Output format must be consistent and parseable
- Tool should run indefinitely until killed (like `smbsloweraod` does)

#### Step 2: Integrate the eBPF Tool in Controller

The Controller class (`src/Controller.py`) manages the execution of eBPF tools. You need to register your new tool and define how to build its command line.

**2.1 Register the Tool in `__init__` Method**

Add your tool to the `tool_cmd_builders` dictionary:

```python
# src/Controller.py (in __init__ method around line 63)

def __init__(self, config_path: str):
    # ...existing code...
    
    self.tool_cmd_builders = {
        "smbslower": self._get_smbsloweraod_cmd,
        "smbiosnoop": self._get_smbiosnoop_cmd,  # Add this line
    }
    
    # ...existing code...
```

**2.2 Create the Command Builder Function**

Add a method to build the command for executing your eBPF tool. Place this method with the other command builders (around line 136):

```python
# src/Controller.py

def _get_smbiosnoop_cmd(self) -> list[str]:
    """Build command for smbiosnoop eBPF tool."""
    # Get configuration for error anomaly if it exists
    error_anomaly = self.config.guardian.anomalies.get("error")
    
    # Build the command array
    ebpf_binary_path = os.path.join(os.path.dirname(__file__), "bin", "smbiosnoop")
    cmd = [ebpf_binary_path]
    
    # Add parameters based on configuration (optional)
    if error_anomaly:
        # Example: Add specific error codes to track
        track_codes = error_anomaly.get("track_codes", [])
        if track_codes:
            cmd.extend(["-e", ",".join(track_codes)])
    
    return cmd
```

**2.3 Command Builder Best Practices**

- Always return a list of strings (command + arguments)
- Use `os.path.join(os.path.dirname(__file__), "bin", "tool_name")` for the binary path
- Extract relevant configuration from `self.config.guardian.anomalies`
- Handle missing configuration gracefully with sensible defaults
- Follow the pattern used by `_get_smbsloweraod_cmd()` for consistency

**Key Reference**: Look at the existing `_get_smbsloweraod_cmd()` method in `src/Controller.py` (lines 136-151) to see how the current eBPF tool is integrated.

#### Step 3: Create the Anomaly Handler

Anomaly handlers analyze batches of events from eBPF tools and determine if an anomaly has occurred. Each handler implements specific detection logic for a particular type of anomaly.

**3.1 Create the Handler File**

Create a new Python file in `src/handlers/` following the naming convention `{anomaly_type}_anomaly_handler.py`:

```bash
# Create the new handler file
touch src/handlers/error_anomaly_handler.py
```

**3.2 Implement the Handler Class**

Use this template for your handler:

```python
# src/handlers/error_anomaly_handler.py

import logging
import numpy as np
from base.AnomalyHandlerBase import AnomalyHandler

logger = logging.getLogger(__name__)

class ErrorAnomalyHandler(AnomalyHandler):
    """Detects anomalies based on your specific criteria."""
    
    def __init__(self, config):
        super().__init__(config)
        # Initialize anything you need from self.config
        # e.g., thresholds, tracking parameters, etc.
    
    def detect(self, events_batch: np.ndarray) -> bool:
        """
        Analyze events_batch and return True if anomaly detected.
        
        Args:
            events_batch: NumPy structured array with parsed eBPF tool output
        
        Returns:
            bool: True if anomaly detected, False otherwise
        """
        # Add your detection logic here
        # Return True if anomaly detected, False otherwise
        return False
```

**Implementation Notes:**
- **Configuration**: Access config values via `self.config.your_field`
- **Data Structure**: `events_batch` contains parsed eBPF tool output as NumPy structured array
- **Performance**: Keep detection logic efficient as it runs frequently. **Use numpy as much as possible for performance**.

#### Step 4: Register the New Handler

To make your new anomaly handler available to the system, you need to register it in three places.

**4.1 Add the Anomaly Type to the Enum**

First, add your new anomaly type to the enum in `src/utils/anomaly_type.py`:

```python
# src/utils/anomaly_type.py

class AnomalyType(Enum):
    """Enumeration for different types of anomalies that can be detected."""
    
    LATENCY = "latency"
    ERROR = "error"  # Add this line if not already present
    # Add more types as needed


ANOMALY_TYPE_TO_TOOL_ID = {
    AnomalyType.LATENCY: 0,
    AnomalyType.ERROR: 1,  # Add this line with unique ID, find out what is the tool id for smbiosnoop in the smbiosnoop output (1 is just an example, it might be wrong)
    # Add more as needed
}
```

**4.2 Import and Register in AnomalyWatcher**

In `src/AnomalyWatcher.py`, add the import and register your handler:

```python
# src/AnomalyWatcher.py (around lines 10-14)

from utils.anomaly_type import AnomalyType, ANOMALY_TYPE_TO_TOOL_ID
from handlers.latency_anomaly_handler import LatencyAnomalyHandler
from handlers.error_anomaly_handler import ErrorAnomalyHandler  # Add this import
from base.AnomalyHandlerBase import AnomalyHandler

# ...existing code...

# Maps enum to anomaly handler classes (around lines 19-23)
ANOMALY_HANDLER_REGISTRY = {
    AnomalyType.LATENCY: LatencyAnomalyHandler,
    AnomalyType.ERROR: ErrorAnomalyHandler,  # Add this line
    # Add more types here as needed
}
```

**4.3 Verification**

After making these changes, verify the registration worked:

```bash
# Check for syntax errors
python3 -m py_compile src/utils/anomaly_type.py
python3 -m py_compile src/AnomalyWatcher.py
python3 -m py_compile src/handlers/error_anomaly_handler.py

# Test that imports work correctly
python3 -c "from src.handlers.error_anomaly_handler import ErrorAnomalyHandler; print('Handler import successful')"
```

**Registration Pattern**: The system uses this three-tier registration:
1. **Enum**: Defines the anomaly type constant
2. **Tool ID**: Maps anomaly types to unique integer IDs (used internally)
3. **Registry**: Maps enum values to handler classes for instantiation

#### Step 5: Update Configuration

The configuration defines how your new anomaly type behaves, what tools it uses, and what actions it triggers. The example `error` anomaly configuration is already present in `config/config.yaml`.

**5.1 Review Existing Configuration**

Check the current error anomaly configuration in `config/config.yaml` (lines 13-28):

```yaml
# config/config.yaml
guardian:
  anomalies:
    error:
      type: "Error"                    # Must match the enum value (ERROR = "error")(case-insensitive)
      tool: "smbiosnoop"              # Must match key in Controller.tool_cmd_builders
      mode: "trackonly"               # Options: all, trackonly, excludeonly
      acceptable_count: 10            # Number of tracked errors to trigger anomaly
      track_codes:                    # Error codes to track
        - EACCES                      # Access denied
        - EAGAIN                      # Resource temporarily unavailable  
        - EIO                         # I/O error
      actions:                        # Quick actions to trigger on anomaly
        - dmesg
        - journalctl
```

**5.2 Configuration Fields Explained**

**Required Fields (Mandatory):**
- **`type`**: Must match your AnomalyType enum value (case-insensitive) *(covered in Step 4.1)*
- **`tool`**: Must match a key in Controller's init file `self.tool_cmd_builders` dictionary *(covered in Step 2.1)*
- **`actions`**: List of QuickActions to execute when anomaly detected *(see "Adding a New Quick Action" section)*

**Optional Fields:**
- **`mode`**: Filtering mode for the eBPF tool (`all`, `trackonly`, `excludeonly`) - defaults to "all"
- **`acceptable_count`**: Threshold for triggering anomaly detection - defaults vary by handler
- **Custom fields**: Handler-specific configuration (e.g., `track_codes` for error anomaly)

**Minimal Configuration Example:**
```yaml
guardian:
  anomalies:
    your_anomaly:
      type: "YourType"        # Required: matches AnomalyType enum
      tool: "your_ebpf_tool"  # Required: matches Controller tool key
      actions:                # Required: at least one action
        - dmesg
```

**5.3 Customize for Your Use Case**

Modify the configuration to match your specific requirements:

```yaml
# Example: Custom error tracking configuration
guardian:
  anomalies:
    error:
      type: "Error"
      tool: "smbiosnoop"              # Your eBPF tool name
      mode: "trackonly"
      acceptable_count: 5             # Lower threshold for more sensitive detection
      track_codes:                    # Add/remove error codes as needed
        - EACCES
        - EIO
        - ENOSPC                      # No space left on device
        - ETIMEDOUT                   # Connection timed out
      actions:
        - dmesg                       # Kernel messages
        - journalctl                  # System logs
        - debugdata                   # Custom diagnostic data
```

**5.4 Configuration Validation**

Test your configuration syntax and integration:

```bash
# Validate YAML syntax
python3 -c "import yaml; yaml.safe_load(open('config/config.yaml'))"

# Test that AOD can parse the configuration
python3 -c "from src.ConfigManager import ConfigManager; ConfigManager('config/config.yaml')"

# Test configuration loading for your specific anomaly
python3 -c "
from src.ConfigManager import ConfigManager
config = ConfigManager('config/config.yaml')
print('Your anomaly config:', config.guardian.anomalies.get('your_anomaly_name'))
"
```

**Key Integration Points**: The configuration values are accessed in your handler via direct attributes on `self.config`:

- `self.config.type` - Anomaly type string
- `self.config.tool` - eBPF tool name  
- `self.config.acceptable_count` - Threshold for triggering anomaly
- `self.config.track` - Dictionary of tracked items (varies by anomaly type)
- `self.config.actions` - List of QuickActions to execute
- `self.config.default_threshold_ms` - Optional default threshold (may be None)

**Example**: In `latency_anomaly_handler.py`, the config is accessed as:
```python
self.acceptable_count = self.config.acceptable_count
for smb_cmd_id, threshold in self.config.track.items():
    # Process tracked commands and thresholds
```

#### Step 6: ConfigManager Integration (For New Anomaly Types Only)

**Note**: For the error anomaly example, ConfigManager integration is already complete. **Skip this step if using the error example**.

If you're adding a completely new anomaly type (not error), you need to add ConfigManager support to parse and validate your custom configuration fields.

**6.1 Add to Configuration Dispatch**

Add your anomaly type to the dispatch dictionary in `_get_track_for_anomaly` method:

```python
# src/ConfigManager.py (around lines 79-85)

dispatch = {
    AnomalyType.LATENCY: self._get_latency_track_cmds,
    AnomalyType.ERROR: self._get_error_track_cmds,
    AnomalyType.YOUR_TYPE: self._get_your_type_track_cmds,  # Add this line
    # Add more types here as needed
}
```

**6.2 Implement Track Commands Method**

Create a method to parse your anomaly's configuration. Study `_get_latency_track_cmds` and `_get_error_track_cmds` in ConfigManager for reference:

```python
# src/ConfigManager.py

def _get_your_type_track_cmds(self, anomaly):
    """Parse and validate your anomaly type tracking commands from config."""
    # 1. Extract configuration fields from YAML
    track_items = anomaly.get("track_items", [])
    exclude_items = anomaly.get("exclude_items", [])
    mode = anomaly.get("mode", "all")
    
    # 2. Normalize track/exclude lists based on mode
    track_items, exclude_items = self._normalize_track_and_exclude(
        mode, track_items, exclude_items, "your_anomaly_type"
    )
    
    # 3. Validate items against your predefined list
    all_valid_items = ["item1", "item2", "item3"]  # Your predefined items, can specify in shared_data.py and extract from there also
    self._validate_cmds(all_valid_items, track_items, exclude_items)
    # can use or create other validation functions like _validate_smb_thresholds if needed
    
    # 4. Build and return the tracking configuration
    # This becomes self.config.track in your handler
    return self._build_your_item_map(mode, track_items, exclude_items)
    # look at helpers to see if you can reuse any of them for your anomaly type
```

**6.3 Available Helper Functions**

The ConfigManager provides several pre-existing helper functions you can reuse:

**Validation Functions:**
```python
# Check items exist in predefined list, no duplicates, no overlap
self._validate_cmds(all_items, track_items, exclude_items)

# Check individual list for validity and duplicates  
self._check_codes(items, all_valid_items, "item type")

# Validate numerical thresholds (for latency-like patterns)
self._validate_smb_thresholds(track_commands)  # Checks threshold >= 0
```

**Mode-Based Filtering:**
```python
# Handle trackonly/excludeonly/all modes with warnings
track_items, exclude_items = self._normalize_track_and_exclude(
    mode, track_items, exclude_items, "your_anomaly_type"
)

# Build simple index mapping based on mode (like error codes)
return self._get_track_codes(mode, ALL_YOUR_ITEMS, track_items, exclude_items)
```

**6.4 Implementation Patterns**

Choose the pattern that best fits your anomaly type (or create a new one):

**Pattern A: Simple Item List** (like error codes):
```python
def _get_your_type_track_cmds(self, anomaly):
    """For simple list-based tracking (like error codes)."""
    track_codes = anomaly.get("track_codes", [])
    exclude_codes = anomaly.get("exclude_codes", [])
    mode = anomaly.get("mode", "all")
    
    # Normalize and validate
    track_codes, exclude_codes = self._normalize_track_and_exclude(
        mode, track_codes, exclude_codes, "your_type"
    )
    self._validate_cmds(ALL_YOUR_CODES, track_codes, exclude_codes)
    
    # Use existing helper for index mapping
    return self._get_track_codes(mode, ALL_YOUR_CODES, track_codes, exclude_codes)
```

**Pattern B: Complex Threshold Mapping** (like latency commands):
```python
def _get_your_type_track_cmds(self, anomaly):
    """For complex items with thresholds/parameters."""
    track_commands = anomaly.get("track_commands", [])
    exclude_commands = anomaly.get("exclude_commands", [])
    mode = anomaly.get("mode", "all")
    default_threshold = anomaly.get("default_threshold", 100)
    
    # Normalize and validate
    track_commands, exclude_commands = self._normalize_track_and_exclude(
        mode, track_commands, exclude_commands, "your_type"
    )
    
    # Custom validation for your command structure
    self._validate_your_commands(track_commands, exclude_commands)
    # eg. self._validate_smb_thresholds(track_commands, exclude_commands)
    
    # Build custom mapping
    return self._build_your_command_map(mode, track_commands, exclude_commands, default_threshold)
```

**6.5 Real Implementation Examples**

Here's how the existing patterns work:

**Error Anomaly** (Simple pattern):
```python
def _get_error_track_cmds(self, anomaly):
    track_codes = anomaly.get("track_codes", [])
    exclude_codes = anomaly.get("exclude_codes", [])
    error_mode = anomaly.get("mode", "all")
    
    # Uses ALL_ERROR_CODES = list(errno.errorcode.values())
    track_codes, exclude_codes = self._normalize_track_and_exclude(
        error_mode, track_codes, exclude_codes, "error"
    )
    
    self._validate_cmds(list(ALL_ERROR_CODES), track_codes, exclude_codes)
    
    # Returns: {error_index: None, ...}
    return self._get_track_codes(error_mode, ALL_ERROR_CODES, track_codes, exclude_codes)
```

**Latency Anomaly** (Complex pattern):
```python
def _get_latency_track_cmds(self, anomaly):
    track_commands = anomaly.get("track_commands", [])
    exclude_commands = anomaly.get("exclude_commands", [])
    latency_mode = anomaly.get("mode", "all")
    default_threshold = anomaly.get("default_threshold_ms", 10)
    
    # Custom validation for SMB commands
    self._validate_smb_commands(track_commands, exclude_commands)
    
    # Returns: {cmd_id: threshold_ms, ...}
    return self._build_latency_command_map(
        latency_mode, track_commands, exclude_commands, default_threshold
    )
```

**6.6 Helper Function Details**

**`_normalize_track_and_exclude(mode, track_items, exclude_items, type_name)`:**
- Handles `trackonly`, `excludeonly`, `all` modes
- Issues warnings and clears irrelevant lists
- Returns normalized `(track_items, exclude_items)` tuple

**`_validate_cmds(all_items, track_items, exclude_items)`:**
- Validates items exist in `all_items` list
- Checks for duplicates within each list
- Ensures no overlap between track and exclude lists

**`_check_codes(codes, all_codes, code_type)`:**
- Validates individual list against predefined values
- Warns about duplicates within the list
- Raises ValueError for invalid codes

**`_get_track_codes(mode, all_codes, track_codes, exclude_codes)`:**
- Builds simple `{index: None}` mapping based on mode
- Uses `ALL_ERROR_CODES.index(code)` for indexing
- Handles trackonly/excludeonly/all logic automatically

**`_validate_smb_thresholds(track_commands)`:**
- Validates threshold values are numbers >= 0
- Used for latency-style configurations with thresholds

The returned value becomes `self.config.track` in your anomaly handler.



### Adding a New Quick Action

Quick Actions are simple, standalone diagnostic commands that are triggered when any anomaly is detected.

#### Step 1: Create the Quick Action Class
Create a new file in `src/handlers/`, for example, `LsofQuickAction.py`:

```python
# src/handlers/LsofQuickAction.py

import subprocess
from pathlib import Path
from base.QuickAction import QuickAction

class LsofQuickAction(QuickAction):
    """Captures network connections using lsof command."""
    
    def __init__(self, batches_root: str, params=None):
        """Initialize the QuickAction.
        
        Args:
            batches_root (str): Root directory for log batches.
            params: Optional parameters for the action.
        """
        super().__init__(batches_root, "lsof.log")
        # Add any self.params if needed
    
    def get_command(self) -> tuple:
        """Return command to execute.
        
        Returns:
            tuple: (command_array, execution_type)
                   execution_type: "cmd" for subprocess, "cat" for simple file operation
        """
        return ["lsof", "-i"], "cmd"
```

#### Step 2: Register in LogCollector
Import and register your QuickAction in `src/LogCollector.py`:

```python
# src/LogCollector.py

# ...existing imports...
from handlers.LsofQuickAction import LsofQuickAction

class LogCollector:
    def __init__(self, controller):
        # ...existing code...
        
        self.action_factory = {
            "journalctl": lambda: JournalctlQuickAction(self.aod_output_dir, self.anomaly_interval),
            # ...existing actions...
            "syslogs": lambda: SysLogsQuickAction(self.aod_output_dir, num_lines=100),
            "lsof": lambda: LsofQuickAction(self.aod_output_dir, params),  # Add this line
        }
        
        # ...existing code...
```

#### Step 3: Update Configuration
Add your new action to the `watcher: actions:` list in `config/config.yaml`:

```yaml
# config/config.yaml
watcher:
  actions:
    - dmesg
    - journalctl
    - lsof  # Add your new action here
```
