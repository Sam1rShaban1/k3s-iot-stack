#!/bin/bash

# =============================================================================
# K3s IoT Stack Benchmark Test Runner
# Tests MQTT → EMQX → Benthos → NATS → VictoriaMetrics pipeline
# =============================================================================

set -e

# --- CONFIGURATION ---
BROKER_IP="192.168.1.50"
BROKER_PORT="32399"
TOPIC="sensors/data"
PUBLISHER_BIN="./publisher"
TEST_DURATION=60
COOLDOWN=30

# Test scenarios: (clients, total_rate_msg_s)
CLIENT_COUNTS=(10 100)
TOTAL_RATES=(100 500)

# Output directories
BENCHMARK_DIR="./benchmarks/$(date +%Y%m%d_%H%M%S)"
RESULTS_DIR="${BENCHMARK_DIR}/results"
LOGS_DIR="${BENCHMARK_DIR}/logs"
DATA_DIR="${BENCHMARK_DIR}/raw_data"

# Metadata
RUN_ID="run_$(date +%Y%m%d_%H%M%S)"
RUN_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
NODE_COUNT=2
NODES="raspberrypi,pi7"

# ulimit for high concurrency
ulimit -n 10000

# --- FUNCTIONS ---

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

cleanup() {
    log "Cleaning up publisher processes..."
    pkill -f "$PUBLISHER_BIN" 2>/dev/null || true
    sleep 2
}

clear_victoriametrics() {
    log "Clearing VictoriaMetrics data..."
    # Restart VictoriaMetrics to ensure clean state (delete_series API is unreliable)
    kubectl rollout restart statefulset victoriametrics-victoria-metrics-single-server -n victoriametrics 2>/dev/null || true
    kubectl rollout status statefulset victoriametrics-victoria-metrics-single-server -n victoriametrics --timeout=60s 2>/dev/null || true
    sleep 5
    log "VictoriaMetrics cleared"
}

verify_pipeline() {
    log "Verifying pipeline connectivity..."
    
    # Check EMQX
    if ! kubectl get pods -n emqx 2>/dev/null | grep -q "Running"; then
        log "ERROR: EMQX not running"
        exit 1
    fi
    
    # Check Benthos (allow CrashLoopBackOff for testing)
    benthos_pods=$(kubectl get pods -n benthos 2>/dev/null | grep -v "NAME" | wc -l)
    if [ "$benthos_pods" -eq 0 ]; then
        log "ERROR: Benthos not running"
        exit 1
    fi
    
    # Check NATS
    if ! kubectl get pods -n nats 2>/dev/null | grep -q "Running"; then
        log "ERROR: NATS not running"
        exit 1
    fi
    
    # Check VictoriaMetrics
    if ! kubectl get pods -n victoriametrics 2>/dev/null | grep -q "Running"; then
        log "ERROR: VictoriaMetrics not running"
        exit 1
    fi
    
    # Check NATS Consumer
    if ! kubectl get pods -n nats-consumer 2>/dev/null | grep -q "Running"; then
        log "ERROR: NATS Consumer not running"
        exit 1
    fi
    
    log "Pipeline verified - all components running"
}

collect_data() {
    local scenario_name="$1"
    local data_file="${DATA_DIR}/${scenario_name}"
    
    log "Collecting data from VictoriaMetrics..."
    
    # Use SSH with password to run query from master node
    sshpass -p 'mvdsi304' ssh -o StrictHostKeyChecking=no master@192.168.1.50 "curl -s 'http://10.43.222.150:8428/api/v1/query?query=iot_sensor_ts'" > "${DATA_DIR}/${scenario_name}_sensor_ts.json" 2>/dev/null || echo '{}' > "${DATA_DIR}/${scenario_name}_sensor_ts.json"
    
    sshpass -p 'mvdsi304' ssh -o StrictHostKeyChecking=no master@192.168.1.50 "curl -s 'http://10.43.222.150:8428/api/v1/query?query=iot_sensor_nats_exit_ts'" > "${DATA_DIR}/${scenario_name}_nats_exit.json" 2>/dev/null || echo '{}' > "${DATA_DIR}/${scenario_name}_nats_exit.json"
    
    sshpass -p 'mvdsi304' ssh -o StrictHostKeyChecking=no master@192.168.1.50 "curl -s 'http://10.43.222.150:8428/api/v1/query?query=iot_sensor_temp'" > "${DATA_DIR}/${scenario_name}_sensor_temp.json" 2>/dev/null || echo '{}' > "${DATA_DIR}/${scenario_name}_sensor_temp.json"
    
    sshpass -p 'mvdsi304' ssh -o StrictHostKeyChecking=no master@192.168.1.50 "curl -s 'http://10.43.222.150:8428/api/v1/query?query=iot_sensor_pm25'" > "${DATA_DIR}/${scenario_name}_sensor_pm25.json" 2>/dev/null || echo '{}' > "${DATA_DIR}/${scenario_name}_sensor_pm25.json"
    
    sshpass -p 'mvdsi304' ssh -o StrictHostKeyChecking=no master@192.168.1.50 "curl -s 'http://10.43.222.150:8428/api/v1/query?query=iot_sensor_pm10'" > "${DATA_DIR}/${scenario_name}_sensor_pm10.json" 2>/dev/null || echo '{}' > "${DATA_DIR}/${scenario_name}_sensor_pm10.json"
    
    sshpass -p 'mvdsi304' ssh -o StrictHostKeyChecking=no master@192.168.1.50 "curl -s 'http://10.43.222.150:8428/api/v1/query?query=iot_sensor_hum'" > "${DATA_DIR}/${scenario_name}_sensor_hum.json" 2>/dev/null || echo '{}' > "${DATA_DIR}/${scenario_name}_sensor_hum.json"
    
    log "Data collected to ${DATA_DIR}/${scenario_name}_*.json"
}

generate_report() {
    local scenario_name="$1"
    local client_count="$2"
    local total_rate="$3"
    local test_duration="$4"
    local data_file="${DATA_DIR}/${scenario_name}"
    local report_file="${RESULTS_DIR}/${scenario_name}_report.json"
    
    log "Generating report for: $scenario_name"
    
    # Process data locally with Python
    python3 << PYEOF
import json
import os
import sys
import statistics
from datetime import datetime

DATA_FILE = "${data_file}"
REPORT_FILE = "${report_file}"
SCENARIO_NAME = "${scenario_name}"
CLIENT_COUNT = ${client_count}
TOTAL_RATE = ${total_rate}
TEST_DURATION = ${test_duration}
RUN_ID = "${RUN_ID}"
RUN_DATE = "${RUN_DATE}"
NODES = "${NODES}"
NODE_COUNT = ${NODE_COUNT}

def load_data(filename):
    if not os.path.exists(filename):
        return {}
    with open(filename) as f:
        data = json.load(f)
    results = {}
    for r in data.get('data', {}).get('result', []):
        msg_id = r['metric'].get('msg_id', 'unknown')
        dev = r['metric'].get('device_id', 'unknown')
        val = float(r['value'][1])
        ts = float(r['value'][0])
        results[msg_id] = {'value': val, 'timestamp': ts, 'device_id': dev}
    return results

# Load all data
nats_exit = load_data(f"{DATA_FILE}_nats_exit.json")
sensor_ts = load_data(f"{DATA_FILE}_sensor_ts.json")
sensor_temp = load_data(f"{DATA_FILE}_sensor_temp.json")
sensor_pm25 = load_data(f"{DATA_FILE}_sensor_pm25.json")
sensor_pm10 = load_data(f"{DATA_FILE}_sensor_pm10.json")
sensor_hum = load_data(f"{DATA_FILE}_sensor_hum.json")

# Calculate latencies
latencies = {}
devices = {}
for msg_id in nats_exit:
    if msg_id in sensor_ts:
        lat = nats_exit[msg_id]['value'] - sensor_ts[msg_id]['value']
        latencies[msg_id] = lat
        devices[msg_id] = {
            'device_id': sensor_ts[msg_id]['device_id'],
            'msg_id': msg_id,
            'sensor_ts': sensor_ts[msg_id]['value'],
            'nats_exit_ts': nats_exit[msg_id]['value'],
            'latency_ms': lat,
            'temp_c': sensor_temp.get(msg_id, {}).get('value', 0),
            'pm25': sensor_pm25.get(msg_id, {}).get('value', 0),
            'pm10': sensor_pm10.get(msg_id, {}).get('value', 0),
            'hum_pct': sensor_hum.get(msg_id, {}).get('value', 0)
        }

all_latencies = sorted(latencies.values())
n = len(all_latencies)

if n == 0:
    print("No data found in VictoriaMetrics")
    report = {
        'run_id': RUN_ID,
        'scenario': SCENARIO_NAME,
        'timestamp': RUN_DATE,
        'error': 'No data found'
    }
    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    with open(REPORT_FILE, 'w') as f:
        json.dump(report, f, indent=2)
    sys.exit(0)

# Calculate statistics
avg_lat = statistics.mean(all_latencies)
min_lat = min(all_latencies)
max_lat = max(all_latencies)
median_lat = statistics.median(all_latencies)
std_lat = statistics.stdev(all_latencies) if n > 1 else 0

def percentile(data, p):
    k = (len(data) - 1) * (p / 100)
    f = int(k)
    c = f + 1
    if c >= len(data):
        return data[f]
    return data[f] + (k - f) * (data[c] - data[f])

p50 = percentile(all_latencies, 50)
p75 = percentile(all_latencies, 75)
p90 = percentile(all_latencies, 90)
p95 = percentile(all_latencies, 95)
p99 = percentile(all_latencies, 99)
p999 = percentile(all_latencies, 99.9)

# Time range
min_ts = min(d['sensor_ts'] for d in devices.values())
max_ts = max(d['nats_exit_ts'] for d in devices.values())
duration_ms = max_ts - min_ts
duration_s = duration_ms / 1000 if duration_ms > 0 else 0
throughput = n / duration_s if duration_s > 0 else 0

# Latency distribution
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
    ("10min+", 600000, float('inf'))
]

latency_distribution = []
for name, min_val, max_val in buckets:
    count = sum(1 for l in all_latencies if min_val <= l < max_val)
    pct = (count / n) * 100 if n else 0
    latency_distribution.append({
        'bucket': name,
        'min_ms': min_val,
        'max_ms': max_val,
        'count': count,
        'percentage': round(pct, 4)
    })

# Per-device stats
device_list = []
for dev, data in devices.items():
    device_list.append({
        'device_id': dev,
        'sensor_ts': data['sensor_ts'],
        'nats_exit_ts': data['nats_exit_ts'],
        'latency_ms': round(data['latency_ms'], 3),
        'temp_c': round(data['temp_c'], 2),
        'pm25': round(data['pm25'], 2),
        'pm10': round(data['pm10'], 2),
        'hum_pct': round(data['hum_pct'], 2)
    })

device_list.sort(key=lambda x: x['latency_ms'])

# Generate report
report = {
    'run_id': RUN_ID,
    'scenario': SCENARIO_NAME,
    'timestamp': RUN_DATE,
    'configuration': {
        'num_clients': CLIENT_COUNT,
        'total_rate_msg_s': TOTAL_RATE,
        'per_device_rate_msg_s': round(TOTAL_RATE / CLIENT_COUNT, 2) if CLIENT_COUNT > 0 else 0,
        'test_duration_s': TEST_DURATION,
        'topic': "sensors/data",
        'nodes': NODES,
        'node_count': NODE_COUNT
    },
    'results': {
        'total_messages': n,
        'unique_devices': len(devices),
        'duration_s': round(duration_s, 2),
        'throughput_msg_s': round(throughput, 2),
        'latency': {
            'samples': n,
            'avg_ms': round(avg_lat, 3),
            'min_ms': round(min_lat, 3),
            'max_ms': round(max_lat, 3),
            'median_ms': round(median_lat, 3),
            'p50_ms': round(p50, 3),
            'p75_ms': round(p75, 3),
            'p90_ms': round(p90, 3),
            'p95_ms': round(p95, 3),
            'p99_ms': round(p99, 3),
            'p999_ms': round(p999, 3),
            'stddev_ms': round(std_lat, 3)
        },
        'latency_distribution': latency_distribution,
        'devices': device_list
    }
}

os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
with open(REPORT_FILE, 'w') as f:
    json.dump(report, f, indent=2)

# Print summary
print(f"\n{'='*60}")
print(f"Scenario: {SCENARIO_NAME}")
print(f"{'='*60}")
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
print(f"\nReport saved to: {REPORT_FILE}")
PYEOF
}

generate_summary() {
    local summary_file="${RESULTS_DIR}/summary.json"
    
    log "Generating summary report..."
    
    python3 << PYEOF
import json
import os
import glob

RESULTS_DIR = "${RESULTS_DIR}"
SUMMARY_FILE = "${summary_file}"
RUN_ID = "${RUN_ID}"
RUN_DATE = "${RUN_DATE}"
NODES = "${NODES}"
NODE_COUNT = ${NODE_COUNT}

# Load all scenario reports
reports = []
for f in sorted(glob.glob(f"{RESULTS_DIR}/*_report.json")):
    with open(f) as fp:
        reports.append(json.load(fp))

# Generate summary
summary = {
    'run_id': RUN_ID,
    'run_date': RUN_DATE,
    'nodes': NODES,
    'node_count': NODE_COUNT,
    'total_scenarios': len(reports),
    'scenarios': []
}

for report in reports:
    if 'error' not in report and 'results' in report:
        summary['scenarios'].append({
            'name': report['scenario'],
            'total_messages': report['results']['total_messages'],
            'unique_devices': report['results']['unique_devices'],
            'throughput_msg_s': report['results']['throughput_msg_s'],
            'avg_latency_ms': report['results']['latency']['avg_ms'],
            'p95_latency_ms': report['results']['latency']['p95_ms'],
            'p99_latency_ms': report['results']['latency']['p99_ms']
        })

os.makedirs(os.path.dirname(SUMMARY_FILE), exist_ok=True)
with open(SUMMARY_FILE, 'w') as f:
    json.dump(summary, f, indent=2)

print(f"\n{'='*60}")
print(f"SUMMARY REPORT")
print(f"{'='*60}")
print(f"Run ID: {RUN_ID}")
print(f"Date: {RUN_DATE}")
print(f"Nodes: {NODES} ({NODE_COUNT} nodes)")
print(f"Total scenarios: {len(reports)}")
print(f"\nScenario Results:")
for s in summary['scenarios']:
    print(f"  {s['name']}: {s['total_messages']} msgs, {s['throughput_msg_s']} msg/s, "
          f"avg={s['avg_latency_ms']}ms, p95={s['p95_latency_ms']}ms, p99={s['p99_latency_ms']}ms")
print(f"\nSummary saved to: {SUMMARY_FILE}")
PYEOF
}

# --- MAIN ---

log "============================================"
log "K3s IoT Stack Benchmark Test"
log "Run ID: $RUN_ID"
log "Date: $RUN_DATE"
log "Nodes: $NODES ($NODE_COUNT nodes)"
log "============================================"

# Create output directories
mkdir -p "$RESULTS_DIR" "$LOGS_DIR" "$DATA_DIR"
log "Output directory: $BENCHMARK_DIR"

# Build publisher if needed
if [ ! -f "$PUBLISHER_BIN" ]; then
    log "Building publisher..."
    gcc publisher.c -o publisher -lpaho-mqtt3c || {
        log "ERROR: Failed to build publisher"
        exit 1
    }
fi

# Verify pipeline
verify_pipeline

# Clear old data before starting
clear_victoriametrics

# Run tests
for CLIENTS in "${CLIENT_COUNTS[@]}"; do
    for RATE in "${TOTAL_RATES[@]}"; do
        SCENARIO_NAME="${CLIENTS}c_${RATE}r"
        DELAY=$(( (1000000 * CLIENTS) / RATE ))
        
        log "============================================"
        log "TEST: $SCENARIO_NAME"
        log "  Clients: $CLIENTS"
        log "  Total Rate: $RATE msg/s"
        log "  Per-Device Rate: $(( RATE / CLIENTS )) msg/s"
        log "  Delay: $DELAY us"
        log "  Duration: ${TEST_DURATION}s"
        log "============================================"
        
        # Clear data before each test
        clear_victoriametrics
        
        # Start publishers with unique device IDs per run to avoid stale data conflicts
        log "Starting $CLIENTS publishers..."
        RUN_SUFFIX="${RUN_ID//_/}"
        for i in $(seq 1 $CLIENTS); do
            $PUBLISHER_BIN $BROKER_IP $BROKER_PORT "sensor_c${CLIENTS}_r${RATE}_${i}_${RUN_SUFFIX}" $DELAY $TOPIC > /dev/null 2>&1 &
        done
        
        # Wait for test duration
        log "Running test for ${TEST_DURATION}s..."
        sleep $TEST_DURATION
        
        # Stop publishers
        cleanup
        
        # Collect data
        collect_data "$SCENARIO_NAME"
        
        # Generate report
        generate_report "$SCENARIO_NAME" "$CLIENTS" "$RATE" "$TEST_DURATION"
        
        # Cooldown
        log "Cooldown: ${COOLDOWN}s..."
        sleep $COOLDOWN
    done
done

# Generate summary
generate_summary

log "============================================"
log "Tests Complete!"
log "Results: $BENCHMARK_DIR"
log "============================================"
