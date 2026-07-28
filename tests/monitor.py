#!/usr/bin/env python3
"""
Simple system monitor that outputs CPU and memory usage to CSV.
Usage: python3 monitor.py [duration_in_seconds]
"""

import psutil
import time
import csv
import sys
from datetime import datetime


def monitor_system(duration=300):
    """Monitor system for specified duration (default 5 minutes)."""
    csv_file = f"system_usage_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    print(f"Monitoring system for {duration} seconds...")
    print(f"Output: {csv_file}")
    print("Press Ctrl+C to stop early")

    with open(csv_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Timestamp", "CPU_Percent", "Memory_Percent"])

        start_time = time.time()
        try:
            while time.time() - start_time < duration:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cpu_percent = psutil.cpu_percent(interval=1)
                memory_percent = psutil.virtual_memory().percent

                writer.writerow([timestamp, cpu_percent, memory_percent])
                f.flush()

                print(
                    f"{timestamp} | CPU: {cpu_percent:6.2f}% | MEM: {memory_percent:6.2f}%"
                )

        except KeyboardInterrupt:
            print("\nStopped by user")

    print(f"Data saved to: {csv_file}")
    return csv_file


if __name__ == "__main__":
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    monitor_system(duration)
