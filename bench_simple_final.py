#!/usr/bin/env python3
import subprocess, time, json, os, datetime, sys

def query_vm(metric):
    url = "http://192.168.1.50:30000/api/v1/query?query=" + metric
    try:
        result = subprocess.run(
            ["curl", "-s", url],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(result.stdout)
        if data.get("status") == "success":
            return data.get("data", {}).get("result", [])
    except Exception as e:
        print("Query failed: " + str(e), file=sys.stderr)
    return []

def generate_report(scenario_name, nats_exit, sensor_ts):
    if not nats_exit or not sensor_ts:
        print("No data found")
        return {"error": "No data"}
    
    latencies = {}
    for msg_id in nats_exit:
        if msg_id in sensor_ts:
            lat = nats_exit[msg_id] - sensor_ts[msg_id]
            latencies[msg_id] = lat
    
    if not latencies:
        print("No matching data")
        return {"error": "No matching data"}
    
    all_lats = sorted(latencies.values())
    n = len(all_lats)
    avg_lat = sum(all_lats) / n
    
    print("")
    print("=" * 60)
    print("Scenario: " + scenario_name)
    print("=" * 60)
    print("Total messages: " + str(n))
    print("Avg latency: " + str(round(avg_lat, 2)) + " ms")
    print("P95 latency: " + str(round(all_lats[int(n*0.95)], 2)) + " ms")
    print("P99 latency: " + str(round(all_lats[int(n*0.99)], 2)) + " ms")
    
    return {
        "scenario": scenario_name,
        "total_messages": n,
        "avg_latency_ms": round(avg_lat, 2),
        "p95_latency_ms": round(all_lats[int(n*0.95)], 2),
        "p99_latency_ms": round(all_lats[int(n*0.99)], 2)
    }

# Main
broker_ip = "192.168.1.50"
broker_port = "1883"
topic = "sensors/data"
test_duration = 60

scenarios = [(10, 100), (10, 500), (100, 100), (100, 500)]

timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
base_dir = "/home/samir/code/k3s-iot-stack/benchmarks/" + timestamp
os.makedirs(base_dir, exist_ok=True)

print("Output directory: " + base_dir)
print("="*60)

for clients, rate in scenarios:
    scenario_name = str(clients) + "c_" + str(rate) + "r"
    delay = int((1000000 * clients) / rate)
    
    print("")
    print("="*60)
    print("TEST: " + scenario_name)
    print("  Clients: " + str(clients))
    print("  Rate: " + str(rate) + " msg/s")
    print("="*60)
    
    # Start publishers
    procs = []
    for i in range(1, clients + 1):
        device_id = "sensor_c" + str(clients) + "_r" + str(rate) + "_" + str(i) + "_" + timestamp.replace("_", "")
        cmd = "./publisher " + broker_ip + " " + broker_port + " " + device_id + " " + str(delay) + " " + topic
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(proc)
    
    # Wait
    print("Running test for " + str(test_duration) + "s...")
    time.sleep(test_duration)
    
    # Stop publishers
    print("Cleaning up publishers...")
    for proc in procs:
        proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=5)
        except:
            proc.kill()
    
    # Wait for consumer
    print("Collecting data...")
    time.sleep(15)
    
    # Query VM
    nats_exit = {}
    for r in query_vm("iot_sensor_nats_exit_ts"):
        if "value" in r and len(r["value"]) == 2:
            msg_id = r.get("metric", {}).get("msg_id", "unknown")
            val = r["value"][0]
            nats_exit[msg_id] = val
    
    sensor_ts = {}
    for r in query_vm("iot_sensor_ts"):
        if "value" in r and len(r["value"]) == 2:
            msg_id = r.get("metric", {}).get("msg_id", "unknown")
            val = r["value"][0]
            sensor_ts[msg_id] = val
    
    # Generate report
    report = generate_report(scenario_name, nats_exit, sensor_ts)
    
    # Save report
    report_file = base_dir + "/" + scenario_name + "_report.json"
    with open(report_file, "w") as f:
        json.dump(report, f, indent=2)
    
    # Cooldown
    print("Cooldown: 30s...")
    time.sleep(30)

print("")
print("="*60)
print("Tests Complete!")
print("Results: " + base_dir)
print("="*60)
