#!/usr/bin/env python3
"""
Monitor disk usage of /var/log/aod/batches and log to CSV.
Usage: python3 disk_monitor.py [interval_seconds] [duration_minutes]
"""

import subprocess
import time
import csv
import sys
import os
from datetime import datetime
from pathlib import Path


def get_disk_usage(path):
    """Get disk usage in MB using du -sh command."""
    try:
        result = sum(
            f.stat().st_size for f in Path(path).glob("**/*") if f.is_file()
        )
        # Parse output like "1.2G	/var/log/aod/batches"
        size_mb = result / (1024 * 1024)  # Convert bytes to MB

        return size_mb
    except subprocess.CalledProcessError:
        print(f"Error: Cannot access {path}. Check permissions or path exists.")
        return None
    except Exception as e:
        print(f"Error getting disk usage: {e}")
        return None


def monitor_disk_usage(path, interval=5, duration=None):
    """Monitor disk usage and log to CSV."""

    # Create output filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = f"disk_usage_{timestamp}.csv"

    print(f"Monitoring disk usage of: {path}")
    print(f"Interval: {interval} seconds")
    if duration:
        print(f"Duration: {duration} minutes")
    print(f"Output file: {output_file}")
    print("Press Ctrl+C to stop monitoring")
    print("-" * 50)

    start_time = time.time()
    end_time = start_time + (duration * 60) if duration else None

    with open(output_file, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Timestamp", "Disk_Usage_MB"])

        try:
            while True:
                current_time = datetime.now()
                disk_usage = get_disk_usage(path)

                if disk_usage is not None:
                    writer.writerow([current_time.isoformat(), disk_usage])
                    csvfile.flush()  # Ensure data is written immediately

                    print(
                        f"{current_time.strftime('%H:%M:%S')} - Disk Usage: {disk_usage:.2f} MB"
                    )
                else:
                    print(
                        f"{current_time.strftime('%H:%M:%S')} - Failed to get disk usage"
                    )

                # Check if we've reached the duration limit
                if end_time and time.time() >= end_time:
                    print(f"\nMonitoring completed after {duration} minutes")
                    break

                time.sleep(interval)

        except KeyboardInterrupt:
            print(f"\nMonitoring stopped. Data saved to: {output_file}")
        except Exception as e:
            print(f"\nError during monitoring: {e}")

    return output_file


if __name__ == "__main__":
    # Default values
    path = "/var/log/aod/batches"
    interval = 5  # seconds
    duration = None  # minutes (None = run indefinitely)

    # Parse command line arguments
    if len(sys.argv) >= 2:
        try:
            interval = int(sys.argv[1])
        except ValueError:
            print("Error: Interval must be a number (seconds)")
            sys.exit(1)

    if len(sys.argv) >= 3:
        try:
            duration = int(sys.argv[2])
        except ValueError:
            print("Error: Duration must be a number (minutes)")
            sys.exit(1)

    # Check if path exists
    if not os.path.exists(path):
        print(f"Warning: Path {path} does not exist. Monitoring will fail.")
        print("You may need to run this script with sudo or adjust the path.")

    try:
        output_file = monitor_disk_usage(path, interval, duration)
        print(f"\nMonitoring complete. Use disk_plot.py to visualize the data:")
        print(f"python3 disk_plot.py {output_file}")
    except Exception as e:
        print(f"Error: {e}")
