#!/usr/bin/env python3
"""
Compare two CSV files with range-based analysis.
Analyzes averages for specific time ranges: 0-200s, 200-500s, 500-600s.

Usage: python3 csv_range_compare.py file1.csv file2.csv [column_name]
"""

import pandas as pd
import matplotlib.pyplot as plt
import sys
import numpy as np


def analyze_ranges(df, column, ranges):
    """Analyze data for specific time ranges."""
    results = {}

    for range_name, (start, end) in ranges.items():
        # Filter data for the time range
        mask = (df["Seconds"] >= start) & (df["Seconds"] <= end)
        range_data = df[mask]

        if len(range_data) > 0:
            results[range_name] = {
                "avg": range_data[column].mean(),
                "max": range_data[column].max(),
                "min": range_data[column].min(),
                "std": range_data[column].std(),
                "count": len(range_data),
            }
        else:
            results[range_name] = {
                "avg": 0,
                "max": 0,
                "min": 0,
                "std": 0,
                "count": 0,
            }

    return results


def create_range_comparison_plot(df1, df2, column, ranges, file1_name, file2_name):
    """Create comparison plots for each range."""
    fig, axes = plt.subplots(len(ranges), 1, figsize=(12, 4 * len(ranges)))
    if len(ranges) == 1:
        axes = [axes]

    for i, (range_name, (start, end)) in enumerate(ranges.items()):
        ax = axes[i]

        # Filter data for the time range
        mask1 = (df1["Seconds"] >= start) & (df1["Seconds"] <= end)
        mask2 = (df2["Seconds"] >= start) & (df2["Seconds"] <= end)

        range_df1 = df1[mask1]
        range_df2 = df2[mask2]

        # Plot data for this range
        if len(range_df1) > 0:
            ax.plot(
                range_df1["Seconds"],
                range_df1[column],
                label=f"{file1_name}",
                linewidth=1,
                alpha=0.8,
            )
        if len(range_df2) > 0:
            ax.plot(
                range_df2["Seconds"],
                range_df2[column],
                label=f"{file2_name}",
                linewidth=1,
                alpha=0.8,
            )

        ax.set_title(f"{column} Comparison - Range {range_name} ({start}-{end}s)")
        ax.set_ylabel(f"{column}")
        ax.set_xlabel("Time (seconds)")
        ax.set_xlim(start, end)
        ax.legend()
        ax.grid(True, alpha=0.3)

        # Add average lines
        if len(range_df1) > 0:
            avg1 = range_df1[column].mean()
            ax.axhline(
                y=avg1,
                color="blue",
                linestyle="--",
                alpha=0.6,
                label=f"{file1_name} avg: {avg1:.2f}",
            )
        if len(range_df2) > 0:
            avg2 = range_df2[column].mean()
            ax.axhline(
                y=avg2,
                color="orange",
                linestyle="--",
                alpha=0.6,
                label=f"{file2_name} avg: {avg2:.2f}",
            )

        ax.legend()

    plt.tight_layout()
    return fig


def compare_csv_ranges(file1, file2, column_name=None):
    """Compare two CSV files with range-based analysis."""

    try:
        # Read CSV files
        df1 = pd.read_csv(file1)
        df2 = pd.read_csv(file2)

        print(f"Without AOD ({file1}) columns: {list(df1.columns)}")
        print(f"With AOD ({file2}) columns: {list(df2.columns)}")

        # Auto-detect timestamp column
        timestamp_cols = ["Timestamp", "timestamp", "Time", "time"]
        timestamp_col = None
        for col in timestamp_cols:
            if col in df1.columns:
                timestamp_col = col
                break

        if timestamp_col is None:
            # Check if there's already a 'Seconds' column
            if "Seconds" not in df1.columns:
                print(
                    "No timestamp column found. Assuming first column is row index."
                )
                df1["Seconds"] = df1.index
                df2["Seconds"] = df2.index
        else:
            # Convert timestamps to relative seconds
            df1[timestamp_col] = pd.to_datetime(df1[timestamp_col])
            df2[timestamp_col] = pd.to_datetime(df2[timestamp_col])
            df1["Seconds"] = (
                df1[timestamp_col] - df1[timestamp_col].iloc[0]
            ).dt.total_seconds()
            df2["Seconds"] = (
                df2[timestamp_col] - df2[timestamp_col].iloc[0]
            ).dt.total_seconds()

        # Define ranges
        ranges = {"0-200": (0, 200), "200-500": (200, 500), "500-600": (500, 600)}

        # If no column specified, analyze both CPU and Memory by default
        if column_name is None:
            # Look for common performance columns
            performance_cols = []
            cpu_cols = [
                col
                for col in df1.columns
                if "cpu" in col.lower() or "processor" in col.lower()
            ]
            mem_cols = [
                col
                for col in df1.columns
                if "mem" in col.lower() or "memory" in col.lower()
            ]

            performance_cols.extend(cpu_cols)
            performance_cols.extend(mem_cols)

            if not performance_cols:
                # Fallback to any numeric columns
                numeric_cols = df1.select_dtypes(include=[np.number]).columns
                performance_cols = [col for col in numeric_cols if col != "Seconds"]

            if len(performance_cols) == 0:
                print("No suitable columns found for comparison!")
                return

            print(f"Analyzing columns: {performance_cols}")

            # Analyze all performance columns
            for col in performance_cols:
                if col not in df1.columns or col not in df2.columns:
                    print(
                        f"Column '{col}' not found in one or both files, skipping..."
                    )
                    continue

                analyze_single_column(df1, df2, col, ranges, file1, file2)
        else:
            # Single column analysis
            if column_name not in df1.columns or column_name not in df2.columns:
                print(f"Column '{column_name}' not found in one or both files!")
                print(f"Available columns in file1: {list(df1.columns)}")
                print(f"Available columns in file2: {list(df2.columns)}")
                return

            analyze_single_column(df1, df2, column_name, ranges, file1, file2)

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()


def analyze_single_column(df1, df2, column_name, ranges, file1, file2):
    """Analyze and compare a single column between two dataframes."""

    # Analyze ranges for both files
    results1 = analyze_ranges(df1, column_name, ranges)
    results2 = analyze_ranges(df2, column_name, ranges)

    # Print comparison results
    print(f"\n{'='*60}")
    print(f"RANGE-BASED COMPARISON: {column_name}")
    print(f"{'='*60}")

    for range_name in ranges.keys():
        print(f"\n📊 Range {range_name} seconds:")
        print(f"{'─'*40}")

        r1 = results1[range_name]
        r2 = results2[range_name]

        print(f"Without AOD ({file1}):")
        print(f"  Average: {r1['avg']:.3f}")
        print(f"  Max:     {r1['max']:.3f}")
        print(f"  Min:     {r1['min']:.3f}")
        print(f"  Std Dev: {r1['std']:.3f}")
        print(f"  Samples: {r1['count']}")

        print(f"With AOD ({file2}):")
        print(f"  Average: {r2['avg']:.3f}")
        print(f"  Max:     {r2['max']:.3f}")
        print(f"  Min:     {r2['min']:.3f}")
        print(f"  Std Dev: {r2['std']:.3f}")
        print(f"  Samples: {r2['count']}")

        if r1["count"] > 0 and r2["count"] > 0:
            diff_avg = r2["avg"] - r1["avg"]
            diff_percent = (diff_avg / r1["avg"] * 100) if r1["avg"] != 0 else 0
            print(f"Difference:")
            print(f"  Avg Diff: {diff_avg:+.3f} ({diff_percent:+.1f}%)")
            print(f"  Max Diff: {r2['max'] - r1['max']:+.3f}")
        else:
            print("Difference: Cannot calculate (missing data)")

    # Create visualization
    fig = create_range_comparison_plot(
        df1, df2, column_name, ranges, f"Without AOD", f"With AOD"
    )

    # Save plot
    output_file = f"range_comparison_{column_name}.png"
    fig.savefig(output_file, dpi=300, bbox_inches="tight")
    print(f"\n📈 Visualization saved to: {output_file}")

    # Summary table
    print(f"\n📋 SUMMARY TABLE - Average {column_name} by Range:")
    print(
        f"{'Range':<12} {'Without AOD':<12} {'With AOD':<12} {'Difference':<12} {'% Change':<10}"
    )
    print(f"{'─'*60}")

    for range_name in ranges.keys():
        r1_avg = results1[range_name]["avg"]
        r2_avg = results2[range_name]["avg"]
        diff = r2_avg - r1_avg
        pct_change = (diff / r1_avg * 100) if r1_avg != 0 else 0

        print(
            f"{range_name:<12} {r1_avg:<12.3f} {r2_avg:<12.3f} {diff:<+12.3f} {pct_change:<+10.1f}%"
        )


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "Usage: python3 csv_range_compare.py file1.csv file2.csv [column_name]"
        )
        print("\nExample:")
        print("  python3 csv_range_compare.py baseline.csv test.csv CPU_Percent")
        print("  python3 csv_range_compare.py data1.csv data2.csv")
        sys.exit(1)

    column = sys.argv[3] if len(sys.argv) > 3 else None

    try:
        compare_csv_ranges(sys.argv[1], sys.argv[2], column)
    except Exception as e:
        print(f"Error: {e}")
        print(
            "Make sure pandas and matplotlib are installed: pip3 install pandas matplotlib numpy"
        )
