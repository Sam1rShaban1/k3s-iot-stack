#!/bin/bash
# 2-Node K3s IoT Stack Benchmark Analysis
# Run this on the master node to analyze VictoriaMetrics data

set -e

VM_URL="http://victoriametrics-victoria-metrics-single-server.victoriametrics.svc.cluster.local:8428"
OUTPUT_DIR="/home/master/k3s-iot-stack/benchmarks/2-node"

mkdir -p "$OUTPUT_DIR"

echo "============================================"
echo "2-Node K3s IoT Stack Benchmark Analysis"
echo "============================================"

# Get total messages
TOTAL_MSGS=$(kubectl exec -n monitoring deployment/monitoring-grafana -c grafana -- wget -qO- "${VM_URL}/api/v1/query?query=count(iot_sensor_ts)" 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
if data.get('status') == 'success' and data['data']['result']:
    print(int(data['data']['result'][0]['value'][1]))
else:
    print(0)
")

echo "Total messages: $TOTAL_MSGS"

# Get unique devices
DEVICE_COUNT=$(kubectl exec -n monitoring deployment/monitoring-grafana -c grafana -- wget -qO- "${VM_URL}/api/v1/series?match[]={__name__=~\"iot_sensor_ts\"}" 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
if 'data' in data:
    devices = set(d.get('device_id', '') for d in data['data'])
    print(len([d for d in devices if d]))
else:
    print(0)
")

echo "Unique devices: $DEVICE_COUNT"

# Get time range
MIN_TS=$(kubectl exec -n monitoring deployment/monitoring-grafana -c grafana -- wget -qO- "${VM_URL}/api/v1/query?query=min(iot_sensor_ts)" 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
if data.get('status') == 'success' and data['data']['result']:
    print(int(data['data']['result'][0]['value'][1]))
else:
    print(0)
")

MAX_TS=$(kubectl exec -n monitoring deployment/monitoring-grafana -c grafana -- wget -qO- "${VM_URL}/api/v1/query?query=max(iot_sensor_nats_exit_ts)" 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
if data.get('status') == 'success' and data['data']['result']:
    print(int(data['data']['result'][0]['value'][1]))
else:
    print(0)
")

DURATION_MS=$((MAX_TS - MIN_TS))
DURATION_S=$((DURATION_MS / 1000))

echo "Time range: $MIN_TS to $MAX_TS"
echo "Duration: ${DURATION_S}s (${DURATION_MS}ms)"

if [ "$DURATION_S" -gt 0 ]; then
    THROUGHPUT=$((TOTAL_MSGS / DURATION_S))
    echo "Throughput: ${THROUGHPUT} msg/s"
fi

# Analyze latency
echo ""
echo "Analyzing latency distribution..."
kubectl exec -n monitoring deployment/monitoring-grafana -c grafana -- wget -qO- "${VM_URL}/api/v1/query?query=iot_sensor_nats_exit_ts - iot_sensor_ts" 2>/dev/null | python3 << 'PYEOF'
import json
import sys
import os

data = json.load(sys.stdin)
if data.get('status') == 'success' and data['data']['result']:
    latencies = [float(r['value'][1]) for r in data['data']['result']]
    latencies.sort()
    
    if latencies:
        n = len(latencies)
        avg = sum(latencies) / n
        mn = min(latencies)
        mx = max(latencies)
        p50 = latencies[int(n * 0.50)]
        p75 = latencies[int(n * 0.75)]
        p90 = latencies[int(n * 0.90)]
        p95 = latencies[int(n * 0.95)]
        p99 = latencies[int(n * 0.99)]
        p999 = latencies[min(int(n * 0.999), n-1)]
        
        variance = sum((x - avg) ** 2 for x in latencies) / n
        std_dev = variance ** 0.5
        iqr = p75 - latencies[int(n * 0.25)]
        cv = (std_dev / avg) * 100 if avg > 0 else 0
        
        print(f"Latency samples: {n}")
        print(f"Average: {avg:.2f} ms")
        print(f"Min: {mn:.2f} ms")
        print(f"Max: {mx:.2f} ms")
        print(f"Median (P50): {p50:.2f} ms")
        print(f"P75: {p75:.2f} ms")
        print(f"P90: {p90:.2f} ms")
        print(f"P95: {p95:.2f} ms")
        print(f"P99: {p99:.2f} ms")
        print(f"P99.9: {p999:.2f} ms")
        print(f"Std Dev: {std_dev:.2f} ms")
        print(f"IQR: {iqr:.2f} ms")
        print(f"CV: {cv:.2f}%")
        
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
            ("10min+", 600000, float('inf'))
        ]
        
        for name, min_val, max_val in buckets:
            count = sum(1 for l in latencies if min_val <= l < max_val)
            pct = (count / n) * 100 if n else 0
            print(f"  {name:12}: {count:8} ({pct:6.2f}%)")
else:
    print("No latency data found")
PYEOF

echo ""
echo "Analyzing per-device metrics..."
kubectl exec -n monitoring deployment/monitoring-grafana -c grafana -- wget -qO- "${VM_URL}/api/v1/query?query=count(iot_sensor_ts) by (device_id)" 2>/dev/null | python3 << 'PYEOF'
import json
import sys

data = json.load(sys.stdin)
if data.get('status') == 'success' and data['data']['result']:
    devices = []
    for r in data['data']['result']:
        device_id = r['metric'].get('device_id', 'unknown')
        count = int(r['value'][1])
        devices.append({'device_id': device_id, 'messages': count})
    
    devices.sort(key=lambda x: x['messages'], reverse=True)
    
    print(f"\nTotal devices: {len(devices)}")
    
    # Calculate device statistics
    if devices:
        msg_counts = [d['messages'] for d in devices]
        avg_msgs = sum(msg_counts) / len(msg_counts)
        min_msgs = min(msg_counts)
        max_msgs = max(msg_counts)
        
        print(f"  Avg messages/device: {avg_msgs:.1f}")
        print(f"  Min messages/device: {min_msgs}")
        print(f"  Max messages/device: {max_msgs}")
        
        print(f"\nTop 10 devices by message count:")
        for i, d in enumerate(devices[:10]):
            print(f"  {i+1:2}. {d['device_id']}: {d['messages']:,} messages")
else:
    print("No device data found")
PYEOF

echo ""
echo "Analyzing sensor data..."
kubectl exec -n monitoring deployment/monitoring-grafana -c grafana -- wget -qO- "${VM_URL}/api/v1/query?query=avg(iot_sensor_temp)" 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
if data.get('status') == 'success' and data['data']['result']:
    print(f'Average temperature: {float(data[\"data\"][\"result\"][0][\"value\"][1]):.2f}°C')
" 2>/dev/null

kubectl exec -n monitoring deployment/monitoring-grafana -c grafana -- wget -qO- "${VM_URL}/api/v1/query?query=avg(iot_sensor_pm25)" 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
if data.get('status') == 'success' and data['data']['result']:
    print(f'Average PM2.5: {float(data[\"data\"][\"result\"][0][\"value\"][1]):.2f} μg/m³')
" 2>/dev/null

kubectl exec -n monitoring deployment/monitoring-grafana -c grafana -- wget -qO- "${VM_URL}/api/v1/query?query=avg(iot_sensor_pm10)" 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
if data.get('status') == 'success' and data['data']['result']:
    print(f'Average PM10: {float(data[\"data\"][\"result\"][0][\"value\"][1]):.2f} μg/m³')
" 2>/dev/null

kubectl exec -n monitoring deployment/monitoring-grafana -c grafana -- wget -qO- "${VM_URL}/api/v1/query?query=avg(iot_sensor_hum)" 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
if data.get('status') == 'success' and data['data']['result']:
    print(f'Average humidity: {float(data[\"data\"][\"result\"][0][\"value\"][1]):.2f}%')
" 2>/dev/null

echo ""
echo "============================================"
echo "Benchmark analysis complete!"
echo "============================================"
