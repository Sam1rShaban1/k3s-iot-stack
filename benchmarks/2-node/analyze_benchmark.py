#!/usr/bin/env python3
"""
2-Node K3s IoT Stack Benchmark Analysis
Analyzes data from VictoriaMetrics and generates benchmark reports
"""

import urllib.request
import urllib.parse
import json
import os
import sys
from datetime import datetime

# VictoriaMetrics URL (use service DNS from within cluster or IP from outside)
VM_URL = os.environ.get(
    "VM_URL",
    "http://victoriametrics-victoria-metrics-single-server.victoriametrics.svc.cluster.local:8428",
)


def query_vm(query):
    """Query VictoriaMetrics API"""
    url = f"{VM_URL}/api/v1/query?query={urllib.parse.quote(query)}"
    try:
        req = urllib.request.Request(url)
        response = urllib.request.urlopen(req, timeout=10)
        if response.status == 200:
            data = json.loads(response.read().decode())
            return data.get("data", {}).get("result", [])
    except Exception as e:
        print(f"Error querying {query}: {e}", file=sys.stderr)
    return []


def query_range(query, start, end, step=60):
    """Query VictoriaMetrics range API"""
    url = f"{VM_URL}/api/v1/query_range?query={urllib.parse.quote(query)}&start={start}&end={end}&step={step}"
    try:
        req = urllib.request.Request(url)
        response = urllib.request.urlopen(req, timeout=30)
        if response.status == 200:
            data = json.loads(response.read().decode())
            return data.get("data", {}).get("result", [])
    except Exception as e:
        print(f"Error querying range {query}: {e}", file=sys.stderr)
    return []


def percentile(sorted_data, p):
    """Calculate percentile from sorted data"""
    if not sorted_data:
        return 0
    idx = int(len(sorted_data) * p / 100)
    idx = min(idx, len(sorted_data) - 1)
    return sorted_data[idx]


def analyze_benchmark():
    print("=" * 60)
    print("2-Node K3s IoT Stack Benchmark Analysis")
    print("=" * 60)

    # Get total message count
    total_msgs = query_vm("count(iot_sensor_ts)")
    if not total_msgs:
        print("No data found in VictoriaMetrics")
        return

    total = int(total_msgs[0]["value"][1])
    print(f"Total messages: {total}")

    # Get unique device count
    unique_devices = query_vm("count(count(iot_sensor_ts) by (device_id))")
    if unique_devices:
        device_count = int(unique_devices[0]["value"][1])
        print(f"Unique devices: {device_count}")

    # Get time range
    ts_results = query_vm("min(iot_sensor_ts)")
    nats_results = query_vm("max(iot_sensor_nats_exit_ts)")

    if ts_results and nats_results:
        min_ts = float(ts_results[0]["value"][1])
        max_ts = float(nats_results[0]["value"][1])
        duration_s = (max_ts - min_ts) / 1000
        print(f"Time range: {min_ts:.0f} to {max_ts:.0f}")
        print(f"Duration: {duration_s:.1f} seconds ({duration_s / 60:.1f} minutes)")

        # Calculate throughput
        throughput = total / duration_s if duration_s > 0 else 0
        print(f"Throughput: {throughput:.2f} msg/s")

    # Analyze latency
    print("\nAnalyzing latency distribution...")
    latency_results = query_vm("iot_sensor_nats_exit_ts - iot_sensor_ts")
    if latency_results:
        latencies = [float(r["value"][1]) for r in latency_results]
        latencies.sort()

        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            min_latency = min(latencies)
            max_latency = max(latencies)
            median_latency = percentile(latencies, 50)
            p75_latency = percentile(latencies, 75)
            p90_latency = percentile(latencies, 90)
            p95_latency = percentile(latencies, 95)
            p99_latency = percentile(latencies, 99)
            p999_latency = percentile(latencies, 99.9)

            # Calculate std dev
            variance = sum((x - avg_latency) ** 2 for x in latencies) / len(latencies)
            std_dev = variance**0.5

            # IQR
            iqr = percentile(latencies, 75) - percentile(latencies, 25)

            print(f"\nLatency Statistics ({len(latencies)} samples):")
            print(f"  Average: {avg_latency:.2f} ms")
            print(f"  Min: {min_latency:.2f} ms")
            print(f"  Max: {max_latency:.2f} ms")
            print(f"  Median (P50): {median_latency:.2f} ms")
            print(f"  P75: {p75_latency:.2f} ms")
            print(f"  P90: {p90_latency:.2f} ms")
            print(f"  P95: {p95_latency:.2f} ms")
            print(f"  P99: {p99_latency:.2f} ms")
            print(f"  P99.9: {p999_latency:.2f} ms")
            print(f"  Std Dev: {std_dev:.2f} ms")
            print(f"  IQR: {iqr:.2f} ms")

            # Latency distribution buckets
            print("\nLatency Distribution:")
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

            for name, min_val, max_val in buckets:
                count = sum(1 for l in latencies if min_val <= l < max_val)
                pct = (count / len(latencies)) * 100 if latencies else 0
                print(f"  {name:12}: {count:8} ({pct:6.2f}%)")

    # Analyze per-device metrics
    print("\nAnalyzing per-device metrics...")
    device_results = query_vm("count(iot_sensor_ts) by (device_id)")
    if device_results:
        devices = []
        for r in device_results:
            device_id = r["metric"].get("device_id", "unknown")
            msg_count = int(r["value"][1])
            devices.append({"device_id": device_id, "messages": msg_count})

        devices.sort(key=lambda x: x["messages"], reverse=True)

        print(f"\nTop 10 devices by message count:")
        for i, d in enumerate(devices[:10]):
            print(f"  {i + 1:2}. {d['device_id']}: {d['messages']:,} messages")

        # Calculate device statistics
        if devices:
            msg_counts = [d["messages"] for d in devices]
            avg_msgs = sum(msg_counts) / len(msg_counts)
            min_msgs = min(msg_counts)
            max_msgs = max(msg_counts)

            print(f"\nDevice Statistics:")
            print(f"  Total devices: {len(devices)}")
            print(f"  Avg messages/device: {avg_msgs:,.1f}")
            print(f"  Min messages/device: {min_msgs:,}")
            print(f"  Max messages/device: {max_msgs:,}")

    # Analyze sensor data
    print("\nAnalyzing sensor data...")
    temp_results = query_vm("avg(iot_sensor_temp)")
    if temp_results:
        avg_temp = float(temp_results[0]["value"][1])
        print(f"Average temperature: {avg_temp:.2f}°C")

    pm25_results = query_vm("avg(iot_sensor_pm25)")
    if pm25_results:
        avg_pm25 = float(pm25_results[0]["value"][1])
        print(f"Average PM2.5: {avg_pm25:.2f} μg/m³")

    pm10_results = query_vm("avg(iot_sensor_pm10)")
    if pm10_results:
        avg_pm10 = float(pm10_results[0]["value"][1])
        print(f"Average PM10: {avg_pm10:.2f} μg/m³")

    hum_results = query_vm("avg(iot_sensor_hum)")
    if hum_results:
        avg_hum = float(hum_results[0]["value"][1])
        print(f"Average humidity: {avg_hum:.2f}%")

    print("\n" + "=" * 60)
    print("Benchmark analysis complete!")
    print("=" * 60)


if __name__ == "__main__":
    analyze_benchmark()
