#!/bin/bash
# Super simple 2-node benchmark

BROKER_IP="192.168.1.50"
BROKER_PORT="1883"
TOPIC="sensors/data"
TEST_DURATION=60
DELAY=1000000  # 10 seconds between messages for 1 msg/s per device

echo "============================================"
echo "2-Node Benchmark (raspberrypi, pi7)"
echo "============================================"

# Create output directory
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BASE_DIR="./benchmarks/$TIMESTAMP"
DATA_DIR="$BASE_DIR/raw_data"
RESULTS_DIR="$BASE_DIR/results"
mkdir -p "$DATA_DIR" "$RESULTS_DIR"

# Build publisher if needed
if [ ! -f "./publisher" ]; then
    echo "Building publisher..."
    gcc publisher.c -o publisher -lpaho-mqtt3c
fi

# Run ONE scenario: 10 clients, 100 msg/s
SCENARIO="10c_100r"
CLIENTS=10
RATE=100
DELAY=$((1000000 * CLIENTS / RATE))

echo ""
echo "============================================"
echo "TEST: $SCENARIO"
echo "  Clients: $CLIENTS"
echo "  Rate: $RATE msg/s"
echo "  Delay: $DELAY us"
echo "============================================"

# Start publishers
echo "Starting $CLIENTS publishers..."
RUN_SUFFIX="${TIMESTAMP//_/}"
for i in $(seq 1 $CLIENTS); do
    ./publisher $BROKER_IP $BROKER_PORT "sensor_c${CLIENTS}_r${RATE}_${i}_${RUN_SUFFIX}" $DELAY $TOPIC > /dev/null 2>&1 &
done

# Wait for test duration
echo "Running test for $TEST_DURATION seconds..."
sleep $TEST_DURATION

# Stop publishers
echo "Cleaning up publishers..."
pkill -f publisher
sleep 2

# Wait for consumer to drain
echo "Waiting for consumer to drain (15s)..."
sleep 15

# Collect data using curl and Python
echo "Collecting data from VictoriaMetrics..."
END_TIME=$(date +%s)
START_TIME=$((END_TIME - 300))

# Query VM for each metric
for metric_pair in "sensor_ts:iot_sensor_ts" "nats_exit:iot_sensor_nats_exit_ts" "temp:iot_sensor_temp" "hum:iot_sensor_hum"; do
    key="${metric_pair%%:*}"
    metric="${metric_pair##*:}"
    output="$DATA_DIR/${SCENARIO}_${key}.json"
    
    # Use curl with query API
    curl -s "http://192.168.1.50:30000/api/v1/query?query=${metric}" -o "$output"
done

# Generate report using Python
echo "Generating report..."
python3 << PYEOF
import json
import sys`

# Load data
def load_data(filename):
    with open(filename) as f:
        data = json.load(f)
    results = {}
    if data.get("status") == "success":
        for r in data.get("data", {}).get("result", []):
            metric = r.get("metric", {})
            msg_id = metric.get("msg_id", "unknown")
            if "value" in r and len(r["value"]) == 2:
                val, ts = r["value"]
                results[msg_id] = float(val)
    return results

nats_exit = load_data("$DATA_DIR/${SCENARIO}_nats_exit.json")
sensor_ts = load_data("$DATA_DIR/${SCENARIO}_sensor_ts.json")

if not nats_exit or not sensor_ts:
    print("No data found")
    sys.exit(1)

# Calculate latencies
latencies = []
for msg_id in nats_exit:
    if msg_id in sensor_ts:
        lat = nats_exit[msg_id] - sensor_ts[msg_id]
        latencies.append(lat)

n = len(latencies)
avg_lat = sum(latencies) / n
print(f"Total messages: {n}")
print(f"Avg latency: {avg_lat:.2f} ms")
print(f"P95 latency: {sorted(latencies)[int(n*0.95)]:.2f} ms")
print(f"P99 latency: {sorted(latencies)[int(n*0.99)]:.2f} ms")
PYEOF

echo ""
echo "============================================"
echo "Benchmark complete!"
echo "Results: $BASE_DIR"
echo "============================================"
