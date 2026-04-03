#!/usr/bin/env python3
"""
Generate benchmark CSV reports from collected VictoriaMetrics data
"""

import json
import csv
import os
import sys
import glob
import statistics
from datetime import datetime


def load_data(filename):
    if not os.path.exists(filename):
        return {}
    with open(filename) as f:
        data = json.load(f)
    results = {}
    for r in data.get("data", {}).get("result", []):
        dev = r["metric"].get("device_id", "unknown")
        val = float(r["value"][1])
        ts = float(r["value"][0])
        results[dev] = {"value": val, "timestamp": ts}
    return results


def percentile(data, p):
    if not data:
        return 0
    k = (len(data) - 1) * (p / 100)
    f = int(k)
    c = f + 1
    if c >= len(data):
        return data[f]
    return data[f] + (k - f) * (data[c] - data[f])


def process_scenario(
    data_dir,
    scenario_name,
    client_count,
    total_rate,
    test_duration,
    output_dir,
    run_id,
    run_date,
    nodes,
    node_count,
):
    """Process a single scenario and generate CSV files"""

    # Load data
    nats_exit = load_data(os.path.join(data_dir, f"{scenario_name}_nats_exit.json"))
    sensor_ts = load_data(os.path.join(data_dir, f"{scenario_name}_sensor_ts.json"))
    sensor_temp = load_data(os.path.join(data_dir, f"{scenario_name}_sensor_temp.json"))
    sensor_pm25 = load_data(os.path.join(data_dir, f"{scenario_name}_sensor_pm25.json"))
    sensor_pm10 = load_data(os.path.join(data_dir, f"{scenario_name}_sensor_pm10.json"))
    sensor_hum = load_data(os.path.join(data_dir, f"{scenario_name}_sensor_hum.json"))

    # Calculate latencies
    latencies = {}
    devices = {}
    for dev in nats_exit:
        if dev in sensor_ts:
            lat = nats_exit[dev]["value"] - sensor_ts[dev]["value"]
            latencies[dev] = lat
            devices[dev] = {
                "sensor_ts": sensor_ts[dev]["value"],
                "nats_exit_ts": nats_exit[dev]["value"],
                "latency_ms": lat,
                "temp_c": sensor_temp.get(dev, {}).get("value", 0),
                "pm25": sensor_pm25.get(dev, {}).get("value", 0),
                "pm10": sensor_pm10.get(dev, {}).get("value", 0),
                "hum_pct": sensor_hum.get(dev, {}).get("value", 0),
            }

    all_latencies = sorted(latencies.values())
    n = len(all_latencies)

    if n == 0:
        print(f"No data found for scenario: {scenario_name}")
        return None

    # Calculate statistics
    avg_lat = statistics.mean(all_latencies)
    min_lat = min(all_latencies)
    max_lat = max(all_latencies)
    median_lat = statistics.median(all_latencies)
    std_lat = statistics.stdev(all_latencies) if n > 1 else 0
    p50 = percentile(all_latencies, 50)
    p75 = percentile(all_latencies, 75)
    p90 = percentile(all_latencies, 90)
    p95 = percentile(all_latencies, 95)
    p99 = percentile(all_latencies, 99)
    p999 = percentile(all_latencies, 99.9)
    iqr = p75 - percentile(all_latencies, 25)
    cv = (std_lat / avg_lat * 100) if avg_lat > 0 else 0

    # Time range
    min_ts = min(d["sensor_ts"] for d in devices.values())
    max_ts = max(d["nats_exit_ts"] for d in devices.values())
    duration_ms = max_ts - min_ts
    duration_s = duration_ms / 1000 if duration_ms > 0 else 0
    throughput = n / duration_s if duration_s > 0 else 0

    # Latency distribution buckets
    buckets = [
        ("0-10ms", 0, 10),
        ("10-50ms", 10, 50),
        ("50-100ms", 50, 100),
        ("100-200ms", 100, 200),
        ("200-500ms", 200, 500),
        ("500ms-1s", 500, 1000),
        ("1-2s", 1000, 2000),
        ("2-5s", 2000, 5000),
        ("5-10s", 5000, 10000),
        ("10-30s", 10000, 30000),
        ("30-60s", 30000, 60000),
        ("1-2min", 60000, 120000),
        ("2-5min", 120000, 300000),
        ("5-10min", 300000, 600000),
        ("10min+", 600000, float("inf")),
    ]

    # Generate CSV files
    scenario_label = f"{client_count} clients @ {total_rate} msg/s"

    # 1. Scenario Summary CSV
    summary_file = os.path.join(output_dir, "benchmark_scenario_summary.csv")
    file_exists = os.path.exists(summary_file)
    with open(summary_file, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(
                [
                    "scenario",
                    "num_clients",
                    "target_total_rate_msg_s",
                    "target_per_device_msg_s",
                    "active_devices",
                    "total_messages",
                    "test_span_s",
                    "effective_duration_s",
                    "actual_throughput_msg_s",
                    "throughput_efficiency_pct",
                    "messages_dropped",
                    "drop_rate_pct",
                    "device_rate_avg",
                    "device_rate_min",
                    "device_rate_max",
                    "device_rate_stddev",
                    "device_rate_cv",
                    "msgs_per_device_avg",
                    "msgs_per_device_min",
                    "msgs_per_device_max",
                    "msgs_per_device_stddev",
                    "latency_samples",
                    "latency_avg_ms",
                    "latency_min_ms",
                    "latency_max_ms",
                    "latency_median_ms",
                    "latency_p50_ms",
                    "latency_p75_ms",
                    "latency_p90_ms",
                    "latency_p95_ms",
                    "latency_p99_ms",
                    "latency_p999_ms",
                    "latency_stddev_ms",
                    "latency_iqr_ms",
                    "latency_cv",
                    "e2e_samples",
                    "e2e_avg_ms",
                    "e2e_min_ms",
                    "e2e_max_ms",
                    "e2e_median_ms",
                    "e2e_p95_ms",
                    "e2e_p99_ms",
                ]
            )

        target_rate = total_rate
        target_per_device = total_rate / client_count if client_count > 0 else 0
        efficiency = (throughput / target_rate) * 100 if target_rate > 0 else 0
        expected_msgs = target_rate * test_duration
        dropped = max(0, int(expected_msgs) - n)
        drop_rate = (dropped / expected_msgs) * 100 if expected_msgs > 0 else 0

        # Device rates (each device sent 1 message in this test)
        device_rates = [1] * len(devices)
        avg_rate = statistics.mean(device_rates) if device_rates else 0
        min_rate = min(device_rates) if device_rates else 0
        max_rate = max(device_rates) if device_rates else 0
        std_rate = statistics.stdev(device_rates) if len(device_rates) > 1 else 0
        cv_rate = (std_rate / avg_rate * 100) if avg_rate > 0 else 0

        writer.writerow(
            [
                scenario_label,
                client_count,
                target_rate,
                round(target_per_device, 2),
                len(devices),
                n,
                test_duration,
                round(duration_s, 2),
                round(throughput, 2),
                round(efficiency, 2),
                dropped,
                round(drop_rate, 2),
                round(avg_rate, 4),
                round(min_rate, 4),
                round(max_rate, 4),
                round(std_rate, 4),
                round(cv_rate, 4),
                1,
                1,
                1,
                0,
                n,
                round(avg_lat, 3),
                round(min_lat, 3),
                round(max_lat, 3),
                round(median_lat, 3),
                round(p50, 3),
                round(p75, 3),
                round(p90, 3),
                round(p95, 3),
                round(p99, 3),
                round(p999, 3),
                round(std_lat, 3),
                round(iqr, 3),
                round(cv, 4),
                n,
                round(avg_lat, 3),
                round(min_lat, 3),
                round(max_lat, 3),
                round(median_lat, 3),
                round(p95, 3),
                round(p99, 3),
            ]
        )

    # 2. Per-Device CSV
    per_device_file = os.path.join(output_dir, "benchmark_per_device.csv")
    file_exists = os.path.exists(per_device_file)
    with open(per_device_file, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(
                [
                    "device_id",
                    "scenario",
                    "num_clients",
                    "target_total_rate",
                    "device_index",
                    "pid",
                    "messages_published",
                    "messages_written_to_vm",
                    "test_duration_s",
                    "device_msg_per_s",
                    "first_sensor_ts",
                    "last_sensor_ts",
                    "first_vm_write_ts",
                    "last_vm_write_ts",
                    "latency_samples",
                    "latency_min_ms",
                    "latency_max_ms",
                    "latency_avg_ms",
                    "latency_median_ms",
                    "latency_p50_ms",
                    "latency_p75_ms",
                    "latency_p90_ms",
                    "latency_p95_ms",
                    "latency_p99_ms",
                    "latency_p999_ms",
                    "latency_stddev_ms",
                    "latency_iqr_ms",
                    "latency_cv",
                    "e2e_samples",
                    "e2e_avg_ms",
                    "e2e_min_ms",
                    "e2e_max_ms",
                    "e2e_median_ms",
                    "e2e_p95_ms",
                    "e2e_p99_ms",
                    "temp_c_samples",
                    "temp_c_avg",
                    "temp_c_min",
                    "temp_c_max",
                    "temp_c_stddev",
                    "pm25_samples",
                    "pm25_avg",
                    "pm25_min",
                    "pm25_max",
                    "pm25_stddev",
                    "pm10_samples",
                    "pm10_avg",
                    "pm10_min",
                    "pm10_max",
                    "pm10_stddev",
                    "hum_pct_samples",
                    "hum_pct_avg",
                    "hum_pct_min",
                    "hum_pct_max",
                    "hum_pct_stddev",
                ]
            )

        for dev, data in devices.items():
            # Extract device index and PID from device_id
            parts = dev.split("_")
            device_idx = parts[2] if len(parts) > 2 else "0"
            pid = parts[3] if len(parts) > 3 else "0"

            lat = data["latency_ms"]
            temp = data["temp_c"]
            pm25 = data["pm25"]
            pm10 = data["pm10"]
            hum = data["hum_pct"]

            writer.writerow(
                [
                    dev,
                    f"{client_count}c_{total_rate}r",
                    client_count,
                    total_rate,
                    device_idx,
                    pid,
                    1,
                    1,
                    test_duration,
                    1 / test_duration if duration_s > 0 else 0,
                    data["sensor_ts"],
                    data["sensor_ts"],
                    data["nats_exit_ts"],
                    data["nats_exit_ts"],
                    1,
                    lat,
                    lat,
                    lat,
                    lat,
                    lat,
                    lat,
                    lat,
                    lat,
                    lat,
                    lat,
                    0,
                    0,
                    0,
                    1,
                    lat,
                    lat,
                    lat,
                    lat,
                    lat,
                    lat,
                    1,
                    temp,
                    temp,
                    temp,
                    0,
                    1,
                    pm25,
                    pm25,
                    pm25,
                    0,
                    1,
                    pm10,
                    pm10,
                    pm10,
                    0,
                    1,
                    hum,
                    hum,
                    hum,
                    0,
                ]
            )

    # 3. Latency Distribution CSV
    latency_file = os.path.join(output_dir, "benchmark_latency_distribution.csv")
    file_exists = os.path.exists(latency_file)
    with open(latency_file, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(
                [
                    "scenario",
                    "latency_bucket",
                    "bucket_min_ms",
                    "bucket_max_ms",
                    "count",
                    "percentage",
                    "cumulative_count",
                    "cumulative_pct",
                ]
            )

        cumulative = 0
        for name, min_val, max_val in buckets:
            count = sum(1 for l in all_latencies if min_val <= l < max_val)
            pct = (count / n) * 100 if n else 0
            cumulative += count
            cum_pct = (cumulative / n) * 100 if n else 0
            writer.writerow(
                [
                    scenario_label,
                    name,
                    min_val,
                    max_val,
                    count,
                    round(pct, 4),
                    cumulative,
                    round(cum_pct, 4),
                ]
            )

    # 4. Timeseries Sampled CSV
    timeseries_file = os.path.join(output_dir, "benchmark_timeseries_sampled.csv")
    file_exists = os.path.exists(timeseries_file)
    with open(timeseries_file, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(
                [
                    "timestamp_ms",
                    "device_id",
                    "sensor_ts",
                    "nats_exit_ts",
                    "latency_ms",
                    "temp_c",
                    "pm25",
                    "pm10",
                    "hum_pct",
                ]
            )

        for dev in sorted(devices.keys()):
            data = devices[dev]
            writer.writerow(
                [
                    data["sensor_ts"],
                    dev,
                    data["sensor_ts"],
                    data["nats_exit_ts"],
                    data["latency_ms"],
                    data["temp_c"],
                    data["pm25"],
                    data["pm10"],
                    data["hum_pct"],
                ]
            )

    # Print summary
    print(f"\n{'=' * 60}")
    print(f"Scenario: {scenario_label}")
    print(f"{'=' * 60}")
    print(f"Total messages: {n}")
    print(f"Unique devices: {len(devices)}")
    print(f"Duration: {duration_s:.1f}s")
    print(f"Throughput: {throughput:.2f} msg/s")
    print(f"\nLatency Statistics:")
    print(f"  Average: {avg_lat:.2f} ms")
    print(f"  Min: {min_lat:.2f} ms")
    print(f"  Max: {max_lat:.2f} ms")
    print(f"  Median (P50): {p50:.2f} ms")
    print(f"  P95: {p95:.2f} ms")
    print(f"  P99: {p99:.2f} ms")

    return {
        "scenario": scenario_label,
        "total_messages": n,
        "unique_devices": len(devices),
        "throughput": round(throughput, 2),
        "avg_latency": round(avg_lat, 3),
        "p95_latency": round(p95, 3),
        "p99_latency": round(p99, 3),
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 generate_benchmark_csv.py <data_dir> [output_dir]")
        print("  data_dir: Directory containing raw VictoriaMetrics JSON files")
        print("  output_dir: Directory for CSV output (default: data_dir/../csv)")
        sys.exit(1)

    data_dir = sys.argv[1]
    output_dir = (
        sys.argv[2] if len(sys.argv) > 2 else os.path.join(data_dir, "..", "csv")
    )
    os.makedirs(output_dir, exist_ok=True)

    # Find all scenario data files
    scenarios = []
    for f in glob.glob(os.path.join(data_dir, "*_sensor_ts.json")):
        base = os.path.basename(f).replace("_sensor_ts.json", "")
        scenarios.append(base)

    if not scenarios:
        print("No scenario data found in:", data_dir)
        sys.exit(1)

    print(f"Found {len(scenarios)} scenarios")

    # Process each scenario
    results = []
    for scenario in sorted(scenarios):
        # Parse scenario name (e.g., "10c_100r" -> 10 clients, 100 rate)
        parts = scenario.replace("c_", "c").replace("r", "").split("c")
        if len(parts) == 2:
            try:
                client_count = int(parts[0])
                total_rate = int(parts[1])
            except ValueError:
                client_count = 100
                total_rate = 500
        else:
            client_count = 100
            total_rate = 500

        test_duration = 60  # Default
        run_id = "benchmark"
        run_date = datetime.utcnow().isoformat() + "Z"
        nodes = "raspberrypi,pi7"
        node_count = 2

        result = process_scenario(
            data_dir,
            scenario,
            client_count,
            total_rate,
            test_duration,
            output_dir,
            run_id,
            run_date,
            nodes,
            node_count,
        )
        if result:
            results.append(result)

    # Print summary
    if results:
        print(f"\n{'=' * 60}")
        print(f"BENCHMARK SUMMARY")
        print(f"{'=' * 60}")
        for r in results:
            print(
                f"  {r['scenario']}: {r['total_messages']} msgs, "
                f"{r['throughput']} msg/s, avg={r['avg_latency']}ms, "
                f"p95={r['p95_latency']}ms, p99={r['p99_latency']}ms"
            )
        print(f"\nCSV files generated in: {output_dir}")


if __name__ == "__main__":
    main()
