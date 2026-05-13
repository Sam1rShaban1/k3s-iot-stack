#!/usr/bin/env python3
"""
Simplified benchmark script - handles data collection properly.
"""
import subprocess
import time
import json
import os
import datetime
import sys

def run_publisher(broker_ip, broker_port, device_id, delay, topic, duration):
    """Run publisher for specified duration."""
    cmd = f"./publisher {broker_ip} {broker_port} {device_id} {delay} {topic}"
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(duration)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except:
        proc.kill()

def query_vm(metric, start_time, end_time):
    """Query VictoriaMetrics and return data."""
    query = f"{metric}[{end_time-start_time}s]"
    url = f"http://192.168.1.50:30000/api/v1/query?query={query}"
    
    try:
        result = subprocess.run(
            ["curl", "-s", url],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(result.stdout)
        if data.get("status") == "success":
            return data.get("data", {}).get("result", [])
    except Exception as e:
        print(f"Query failed for {metric}: {e}", file=sys.stderr)
    return []

def generate_report(scenario_name, client_count, total_rate, test_duration, data_dir):
    """Generate report from collected data."""
    # Load data
    def load_data(filename):
        if not os.path.exists(filename):
            return {}
        with open(filename) as f:
            content = f.read().strip()
            if not content:
                return {}
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                return {}
            
            if isinstance(data, dict) and data.get("status") == "success":
                results = {}
                for r in data.get("data", {}).get("result", []):
                    msg_id = r.get("metric", {}).get("msg_id", "unknown")
                    if "value" in r and len(r["value"]) == 2:
                        val, ts = r["value"]
                        results[msg_id] = {
                            "value": float(val),
                            "timestamp": float(ts),
                            "device_id": r.get("metric", {}).get("device_id", "unknown")
                        }
                return results
        return {}
    
    nats_exit = load_data(f"{data_dir}/{scenario_name}_nats_exit.json")
    sensor_ts = load_data(f"{data_dir}/{scenario_name}_sensor_ts.json")
    
    if not nats_exit or not sensor_ts:
        print("No data found in VictoriaMetrics")
        return {"error": "No data found"}
    
    # Calculate latencies
    latencies = []
    devices = {}
    for msg_id, exit_data in nats_exit.items():
        if msg_id in sensor_ts:
            lat = exit_data["value"] - sensor_ts[msg_id]["value"]
            latencies.append(lat)
            devices[msg_id] = {
                "device_id": sensor_ts[msg_id]["device_id"],
                "latency_ms": lat
            }
    
    if not latencies:
        print("No matching data found")
        return {"error": "No matching data"}
    
    n = len(latencies)
    avg_lat = sum(latencies) / n
    min_lat = min(latencies)
    max_lat = max(latencies)
    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[int(n*0.5)]
    p95 = latencies_sorted[int(n*0.95)]
    p99 = latencies_sorted[int(n*0.99)]
    
    # Time range
    min_ts = min(d["sensor_ts"] for d in devices.values())
    max_ts = max(d["nats_exit_ts"] for d in devices.values())
    duration_ms = max_ts - min_ts
    throughput = n / (duration_ms / 1000) if duration_ms > 0 else 0
    
    report = {
        "scenario": scenario_name,
        "total_messages": n,
        "unique_devices": len(devices),
        "duration_s": round(duration_ms / 1000, 2),
        "throughput_msg_s": round(throughput, 2),
        "latency": {
            "avg_ms": round(avg_lat, 2),
            "min_ms": round(min_lat, 2),
            "max_ms": round(max_lat, 2),
            "p50_ms": round(p50, 2),
            "p95_ms": round(p95, 2),
            "p99_ms": round(p99, 2)
        }
    }
    
    print(f"\n{'='*60}")
    print(f"Scenario: {scenario_name}")
    print(f"{'='*60}")
    print(f"Total messages: {n}")
    print(f"Unique devices: {len(devices)}")
    print(f"Duration: {duration_ms/1000:.1f}s")
    print(f"Throughput: {throughput:.2f} msg/s")
    print(f"\nLatency Statistics:")
    print(f"  Average: {avg_lat:.2f} ms")
    print(f"  Min: {min_lat:.2f} ms")
    print(f"  Max: {max_lat:.2f} ms")
    print(f"  Median (P50): {p50:.2f} ms")
    print(f"  P95: {p95:.2f} ms")
    print(f"  P99: {p99:.2f} ms")
    
    return report

def main():
    broker_ip = "192.168.1.50"
    broker_port = "1883"
    topic = "sensors/data"
    test_duration = 60
    
    # Test scenarios
    scenarios = [
        (10, 100),   # 10 clients, 100 msg/s
        (10, 500),   # 10 clients, 500 msg/s
        (100, 100),  # 100 clients, 100 msg/s
        (100, 500),  # 100 clients, 500 msg/s
    ]
    
    # Create output directory
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base_dir = f"./benchmarks/{timestamp}"
    data_dir = f"{base_dir}/raw_data"
    results_dir = f"{base_dir}/results"
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(results_dir, exist_ok=True)
    
    print(f"Output directory: {base_dir}")
    print(f"Nodes: raspberrypi,pi7 (2 nodes)")
    
    # Build publisher if needed
    if not os.path.exists("./publisher"):
        subprocess.run(["gcc", "publisher.c", "-o", "publisher", "-lpaho-mqtt3c"], check=True)
        print("Publisher built")
    
    # Run scenarios
    reports = []
    for clients, rate in scenarios:
        scenario_name = f"{clients}c_{rate}r"
        delay = int((1000000 * clients) / rate)
        
        print(f"\n{'='*60}")
        print(f"TEST: {scenario_name}")
        print(f"  Clients: {clients}")
        print(f"  Total Rate: {rate} msg/s")
        print(f"  Per-Device Rate: {rate//clients} msg/s")
        print(f"  Delay: {delay} us")
        print(f"  Duration: {test_duration}s")
        print(f"{'='*60}")
        
        # Start publishers
        run_suffix = timestamp.replace("_", "")
        procs = []
        for i in range(1, clients + 1):
            device_id = f"sensor_c{clients}_r{rate}_{i}_{run_suffix}"
            cmd = f"./publisher {broker_ip} {broker_port} {device_id} {delay} {topic}"
            proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(proc)
        
        # Wait for test duration
        print(f"Running test for {test_duration}s...")
        time.sleep(test_duration)
        
        # Stop publishers
        print("Cleaning up publisher processes...")
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except:
                proc.kill()
        
        # Collect data
        print("Collecting data from VictoriaMetrics...")
        time.sleep(15)  # Wait for consumer to drain
        
        end_time = int(time.time())
        start_time = end_time - 300  # Last 5 minutes
        
        # Query VM for each metric
        for key, metric in [("sensor_ts", "iot_sensor_ts"), ("nats_exit", "iot_sensor_nats_exit_ts"), ("temp", "iot_sensor_temp"), ("hum", "iot_sensor_hum")]:
            output_file = f"{data_dir}/{scenario_name}_{key}.json"
            query = f"{metric}[{end_time-start_time}s]"
            url = f"http://192.168.1.50:30000/api/v1/query?query={query}"
            
            try:
                result = subprocess.run(
                    ["curl", "-s", "-o", output_file, url],
                    capture_output=True, timeout=30
                )
                if os.path.getsize(output_file) == 0:
                    with open(output_file, "w") as f:
                        f.write('{"status":"success","data":{"resultType":"vector","result":[]}}')
            except Exception as e:
                print(f"Collection failed for {metric}: {e}", file=sys.stderr)
                with open(output_file, "w") as f:
                    f.write('{"status":"success","data":{"resultType":"vector","result":[]}}')
        
        # Generate report
        print(f"Generating report for: {scenario_name}")
        report = generate_report(scenario_name, clients, rate, test_duration, data_dir)
        reports.append(report)
        
        # Save report
        report_file = f"{results_dir}/{scenario_name}_report.json"
        with open(report_file, "w") as f:
            json.dump(report, f, indent=2)
        
        # Cooldown
        print("Cooldown: 30s...")
        time.sleep(30)
    
    # Generate summary
    print(f"\n{'='*60}")
    print("SUMMARY REPORT")
    print(f"{'='*60}")
    print(f"Run ID: run_{timestamp}")
    print(f"Date: {datetime.datetime.utcnow().isoformat()}Z")
    print(f"Nodes: raspberrypi,pi7 (2 nodes)")
    print(f"Total scenarios: {len(scenarios)}")
    print("\nScenario Results:")
    for r in reports:
        if "error" not in r:
            print(f"  {r['scenario']}: {r['total_messages']} msgs, {r['throughput_msg_s']} msg/s, "
                  f"avg={r['latency']['avg_ms']}ms, p95={r['latency']['p95_ms']}ms, p99={r['latency']['p99_ms']}ms")
    
    # Save summary
    summary = {
        "run_id": f"run_{timestamp}",
        "run_date": datetime.datetime.utcnow().isoformat() + "Z",
        "nodes": "raspberrypi,pi7",
        "node_count": 2,
        "total_scenarios": len(scenarios),
        "scenarios": reports
    }
    summary_file = f"{results_dir}/summary.json"
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)
    
    print(f"\nSummary saved to: {summary_file}")
    print(f"\n{'='*60}")
    print("Tests Complete!")
    print(f"Results: {base_dir}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
