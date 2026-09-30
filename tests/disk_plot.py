#!/usr/bin/env python3
"""
Plot disk usage data from CSV file.
Usage: python3 disk_plot.py file.csv [file2.csv]
"""

import pandas as pd
import matplotlib.pyplot as plt
import sys
import os


def plot_disk_usage(file1, file2=None):
    """Plot disk usage from one or two CSV files."""

    # Read the first file
    try:
        df1 = pd.read_csv(file1)
        df1["Timestamp"] = pd.to_datetime(df1["Timestamp"])
        df1["Seconds"] = (
            df1["Timestamp"] - df1["Timestamp"].iloc[0]
        ).dt.total_seconds()
    except Exception as e:
        print(f"Error reading {file1}: {e}")
        return

    # Read the second file if provided
    df2 = None
    if file2:
        try:
            df2 = pd.read_csv(file2)
            df2["Timestamp"] = pd.to_datetime(df2["Timestamp"])
            df2["Seconds"] = (
                df2["Timestamp"] - df2["Timestamp"].iloc[0]
            ).dt.total_seconds()
        except Exception as e:
            print(f"Error reading {file2}: {e}")
            return

    # Create the plot
    plt.figure(figsize=(12, 6))

    # Plot first dataset
    label1 = "Without AOD" if file2 else os.path.basename(file1)
    plt.plot(
        df1["Seconds"],
        df1["Disk_Usage_MB"],
        label=label1,
        linewidth=2,
        marker="o",
        markersize=3,
    )

    # Plot second dataset if available
    if df2 is not None:
        plt.plot(
            df2["Seconds"],
            df2["Disk_Usage_MB"],
            label="With AOD",
            linewidth=2,
            marker="s",
            markersize=3,
        )

    plt.title("AOD Batches Directory Disk Usage Over Time")
    plt.xlabel("Time (seconds)")
    plt.ylabel("Disk Usage (MB)")
    plt.grid(True, alpha=0.3)
    plt.legend()

    # Set Y-axis from 0 to 3 MB
    plt.ylim(0, 3)

    # Save the plot
    if file2:
        output_file = "disk_usage_comparison.png"
    else:
        output_file = "disk_usage_plot.png"

    plt.tight_layout()
    plt.savefig(output_file, dpi=300, bbox_inches="tight")
    print(f"Plot saved to: {output_file}")

    # Print statistics
    print("\nDisk Usage Statistics:")
    print(f"{label1}:")
    print(f"  Initial: {df1['Disk_Usage_MB'].iloc[0]:.2f} MB")
    print(f"  Final: {df1['Disk_Usage_MB'].iloc[-1]:.2f} MB")
    print(
        f"  Growth: {df1['Disk_Usage_MB'].iloc[-1] - df1['Disk_Usage_MB'].iloc[0]:+.2f} MB"
    )
    print(f"  Max: {df1['Disk_Usage_MB'].max():.2f} MB")
    print(f"  Average: {df1['Disk_Usage_MB'].mean():.2f} MB")

    if df2 is not None:
        print(f"\nWith AOD:")
        print(f"  Initial: {df2['Disk_Usage_MB'].iloc[0]:.2f} MB")
        print(f"  Final: {df2['Disk_Usage_MB'].iloc[-1]:.2f} MB")
        print(
            f"  Growth: {df2['Disk_Usage_MB'].iloc[-1] - df2['Disk_Usage_MB'].iloc[0]:+.2f} MB"
        )
        print(f"  Max: {df2['Disk_Usage_MB'].max():.2f} MB")
        print(f"  Average: {df2['Disk_Usage_MB'].mean():.2f} MB")

        # Calculate AOD impact
        growth1 = df1["Disk_Usage_MB"].iloc[-1] - df1["Disk_Usage_MB"].iloc[0]
        growth2 = df2["Disk_Usage_MB"].iloc[-1] - df2["Disk_Usage_MB"].iloc[0]
        print(f"\nAOD Impact on Disk Growth: {growth2 - growth1:+.2f} MB")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 disk_plot.py file1.csv [file2.csv]")
        print("  file1.csv: First disk usage file (e.g., without AOD)")
        print("  file2.csv: Second disk usage file (optional, e.g., with AOD)")
        sys.exit(1)

    file1 = sys.argv[1]
    file2 = sys.argv[2] if len(sys.argv) >= 3 else None

    if not os.path.exists(file1):
        print(f"Error: File {file1} does not exist")
        sys.exit(1)

    if file2 and not os.path.exists(file2):
        print(f"Error: File {file2} does not exist")
        sys.exit(1)

    try:
        plot_disk_usage(file1, file2)
    except Exception as e:
        print(f"Error: {e}")
        print(
            "Make sure pandas and matplotlib are installed: pip3 install pandas matplotlib"
        )


if __name__ == "__main__":
    main()
