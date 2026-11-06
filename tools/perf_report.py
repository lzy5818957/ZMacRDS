#!/usr/bin/env python3
"""Compare nettop cumulative byte counters; never interpret them as display FPS."""
import argparse
import csv
import json
import math
import statistics
from pathlib import Path


def bandwidth(text, interval_seconds):
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("sampling interval must be finite and positive")
    samples = []
    process = None
    for row in csv.reader(text.splitlines()):
        if not row or not row[0].strip():
            continue
        if len(row) < 3:
            raise ValueError("incomplete nettop row")
        if process is not None and row[0] != process:
            raise ValueError("compare one process per capture")
        process = row[0]
        counter = int(row[2])
        if counter < 0 or (samples and counter < samples[-1]):
            raise ValueError("byte counter reset or process restarted")
        samples.append(counter)
    if len(samples) < 2:
        raise ValueError("at least two counter samples required")
    rates = [(b - a) / interval_seconds for a, b in zip(samples, samples[1:])]
    return {
        "samples": len(samples),
        "sample_interval_seconds": interval_seconds,
        "observed_seconds": interval_seconds * len(rates),
        "average_bytes_per_second": statistics.mean(rates),
        "median_bytes_per_second": statistics.median(rates),
        "min_bytes_per_second": min(rates),
        "max_bytes_per_second": max(rates),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", help="LABEL=nettop.csv")
    parser.add_argument("--interval", type=float, required=True,
                        help="actual nettop sampling interval; use nettop -s with the same value")
    args = parser.parse_args()
    captures = {}
    for entry in args.captures:
        label, path = entry.split("=", 1)
        if not label or label in captures:
            raise ValueError("capture labels must be nonempty and unique")
        captures[label] = bandwidth(Path(path).read_text(), args.interval)
    print(json.dumps({
        "metric": "server_process_network_bytes_out",
        "captures": captures,
        "limitations": [
            "Short byte-counter samples do not establish sustained performance.",
            "These counters do not measure decoded frames or input-to-display latency.",
            "Compare only matching resolution, workload, visibility and sampling conditions.",
        ],
    }, indent=2))


if __name__ == "__main__":
    main()
