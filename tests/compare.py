#!/usr/bin/env python3
"""
Compare two CSV files and create graphs.
Usage: python3 compare.py file1.csv file2.csv
"""

import pandas as pd
import matplotlib.pyplot as plt
import sys


def compare_csv_files(file1, file2):
    """Compare two CSV files and create comparison graphs."""

    # Read CSV files
    df1 = pd.read_csv(file1)
    df2 = pd.read_csv(file2)

    # Convert timestamps to relative seconds
    df1["Timestamp"] = pd.to_datetime(df1["Timestamp"])
    df2["Timestamp"] = pd.to_datetime(df2["Timestamp"])
    df1["Seconds"] = (df1["Timestamp"] - df1["Timestamp"].iloc[0]).dt.total_seconds()
    df2["Seconds"] = (df2["Timestamp"] - df2["Timestamp"].iloc[0]).dt.total_seconds()

    # Create plots
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8))

    # Calculate dynamic Y-axis limits for CPU
    max_cpu = max(df1["CPU_Percent"].max(), df2["CPU_Percent"].max())
    cpu_limit = min(100, max_cpu + 15)  # Add 15% buffer, cap at 100
    cpu_ticks = [i for i in range(0, int(cpu_limit) + 1, 25) if i <= cpu_limit]
    if cpu_limit not in cpu_ticks:
        cpu_ticks.append(int(cpu_limit))

    # CPU comparison
    ax1.plot(df1["Seconds"], df1["CPU_Percent"], label="Without AOD", linewidth=1)
    ax1.plot(df2["Seconds"], df2["CPU_Percent"], label="With AOD", linewidth=1)
    ax1.set_title("CPU Usage Comparison")
    ax1.set_ylabel("CPU (%)")
    ax1.set_ylim(0, cpu_limit)
    ax1.set_yticks(cpu_ticks)
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    # Memory comparison
    ax2.plot(df1["Seconds"], df1["Memory_Percent"], label="Without AOD", linewidth=1)
    ax2.plot(df2["Seconds"], df2["Memory_Percent"], label="With AOD", linewidth=1)
    ax2.set_title("Memory Usage Comparison")
    ax2.set_ylabel("Memory (%)")
    ax2.set_ylim(0, 100)
    ax2.set_yticks([0, 25, 50, 75, 100])
    ax2.set_xlabel("Time (seconds)")
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    output_file = "comparison_graph.png"
    plt.savefig(output_file, dpi=300, bbox_inches="tight")
    print(f"Graph saved to: {output_file}")

    # Print statistics
    print("\nStatistics:")
    print(f"Without AOD ({file1}):")
    print(
        f"  CPU: avg={df1['CPU_Percent'].mean():.2f}%, max={df1['CPU_Percent'].max():.2f}%"
    )
    print(
        f"  Memory: avg={df1['Memory_Percent'].mean():.2f}%, max={df1['Memory_Percent'].max():.2f}%"
    )

    print(f"With AOD ({file2}):")
    print(
        f"  CPU: avg={df2['CPU_Percent'].mean():.2f}%, max={df2['CPU_Percent'].max():.2f}%"
    )
    print(
        f"  Memory: avg={df2['Memory_Percent'].mean():.2f}%, max={df2['Memory_Percent'].max():.2f}%"
    )

    print("AOD Impact (With AOD - Without AOD):")
    print(f"  CPU: {df2['CPU_Percent'].mean() - df1['CPU_Percent'].mean():+.2f}%")
    print(
        f"  Memory: {df2['Memory_Percent'].mean() - df1['Memory_Percent'].mean():+.2f}%"
    )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 compare.py file1.csv file2.csv")
        sys.exit(1)

    try:
        compare_csv_files(sys.argv[1], sys.argv[2])
    except Exception as e:
        print(f"Error: {e}")
        print(
            "Make sure pandas and matplotlib are installed: pip3 install pandas matplotlib"
        )
