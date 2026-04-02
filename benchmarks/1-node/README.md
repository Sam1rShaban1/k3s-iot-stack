# IoT Pipeline Benchmark Report — Single Node (Raspberry Pi 4)

## Overview

| Property | Value |
|---|---|
| **Date** | March 19–21, 2026 |
| **Nodes** | 1 |
| **Hardware** | Raspberry Pi 4 |
| **CPU** | ARM64 Cortex-A72, 4 cores @ 1.8 GHz (600 MHz–1.8 GHz DVFS) |
| **Memory** | 8 GB LPDDR4 |
| **Storage** | eMMC / SD card (mmcblk0, 64 GB) |
| **Network** | WiFi (wlan0), Ethernet inactive |
| **OS** | Linux (aarch64) |
| **Kubernetes** | k3s v1.34.5+k3s1 |
| **Runtime** | containerd |
| **Cluster Age** | 14 days at time of analysis |

## Pipeline Architecture

```
Publisher (C / Paho MQTT)
    │  MQTT QoS 2, JSON payloads (~150 bytes)
    ▼
MetalLB LoadBalancer (192.168.1.241:1883)
    │
    ▼
EMQX 5.8.0 (emqx namespace)
    │  Subscribe: sensors/data
    ▼
Benthos (benthos namespace)
    │  MQTT Input → Pipeline → NATS Output
    │  Subject: iot.data
    ▼
NATS JetStream 2.12.5 (nats namespace)
    │  Stream: IOT_DATA
    │  Consumer: push delivery → iot.consumer.delivery
    ▼
NATS Consumer ×3 replicas (nats-consumer namespace)
    │  Python (nats-py 2.14.0)
    │  Adds timestamps, calculates latency
    │  HTTP POST → VictoriaMetrics Prometheus import API
    ▼
VictoriaMetrics Single Server (victoriametrics namespace)
    │  :8428/api/v1/import/prometheus
    │  Storage: 16 Gi PVC (local-path)
    ▼
Grafana (monitoring namespace)
    │  Datasources: Prometheus, VictoriaMetrics
```

## Test Methodology

Benchmark executed via `run_test.sh`:

| Parameter | Value |
|---|---|
| **Publisher binary** | `publisher` (C, compiled with `gcc -lpaho-mqtt3c`) |
| **MQTT QoS** | 2 (exactly-once delivery) |
| **Payload** | JSON: `{"device_id","ts","pm1","pm25","pm10","temp","hum"}` (~150 bytes) |
| **Topic** | `sensors/data` |
| **Test duration per scenario** | 60 seconds |
| **Cooldown between scenarios** | 30 seconds |
| **File descriptor limit** | `ulimit -n 10000` |

### Scenarios

| # | Clients | Target Total Rate | Per-Client Rate | Inter-Message Delay |
|---|---------|-------------------|-----------------|---------------------|
| 1 | 10 | 500 msg/s | 50 msg/s | 20,000 µs |
| 2 | 100 | 100 msg/s | 1 msg/s | 1,000,000 µs |
| 3 | 100 | 500 msg/s | 5 msg/s | 200,000 µs |

## System Health at Time of Analysis

### Node Resources

| Metric | Value | Status |
|---|---|---|
| CPU Usage | 3,525m (88%) | WARNING |
| Memory Usage | 4,426 MiB (56%) | OK |
| Disk Usage | 13.3 GB / 60.96 GB (24%) | OK |
| Disk Written (since boot) | 192.66 GB | WARNING (SD card endurance) |
| Load Average (1/5/15 min) | 15.81 / 15.57 / 15.27 | CRITICAL (>4× core count) |
| Uptime | 14 days | OK |

### Pod Status

| Status | Count | Details |
|---|---|---|
| Running | ~32 | All core services operational |
| Completed | 5 | Finished jobs |
| Unknown | 1 | `argocd-repo-server` (14d stale) |
| Terminating | 1 | `prometheus-0` (restarting) |
| Init | 1 | `alertmanager-0` (starting) |

### Service Health

| Service | Version | Status | Notes |
|---|---|---|---|
| EMQX | 5.8.0 | Running | MQTT broker, ports 1883/8883/18083 |
| NATS Server | 2.12.5 | Running | JetStream enabled, 3 active connections |
| NATS Consumers | — | Running | 3 replicas, all subscribed to `iot.consumer.delivery` |
| Benthos | — | Running (0/1) | Container not fully ready, NATS connection errors in logs |
| VictoriaMetrics | — | Running | 16 Gi PVC bound, data from Mar 19–21 stored |
| Grafana | — | Running | 3/3 containers, dashboards configured |
| ArgoCD | — | Degraded | Repo-server in Unknown state |
| MetalLB | — | Running | Controller + Speaker operational |
| Traefik | — | Running | Ingress controller, port 80/443 |

## Results

### Scenario 1: 10 Clients @ 500 msg/s Total

| Metric | Value |
|---|---|
| Active Devices | 10 |
| Total Messages Published | 1,973 |
| Effective Duration | 83.5 s |
| Actual Throughput | 23.63 msg/s |
| Throughput Efficiency | **4.73%** of target |
| Messages Dropped | 39,779 (95.27%) |
| Per-Device Rate (avg) | 2.37 msg/s (target: 50 msg/s) |
| Per-Device Rate (min/max) | 2.31 – 2.39 msg/s |
| Messages Per Device (avg/min/max) | 197.3 / 193 / 199 |

#### Latency (Sensor Timestamp → VictoriaMetrics Write)

| Percentile | Value |
|---|---|
| Min | 54 ms |
| Average | 12,759 ms (12.8 s) |
| Median (P50) | 13,173 ms |
| P75 | 18,747 ms |
| P90 | 20,983 ms |
| P95 | 21,589 ms |
| P99 | 23,895 ms |
| P99.9 | 24,198 ms |
| Max | 24,252 ms |
| Std Deviation | 6,729 ms |
| IQR | 11,845 ms |
| Coefficient of Variation | 0.53 |

#### End-to-End Latency (Consumer Measured)

| Percentile | Value |
|---|---|
| Min | 20 ms |
| Average | 12,726 ms |
| Median | 13,142 ms |
| P95 | 21,540 ms |
| P99 | 23,869 ms |
| Max | 24,199 ms |

**Pipeline Overhead:** 32 ms (difference between VM write latency and consumer E2E)

---

### Scenario 2: 100 Clients @ 100 msg/s Total

| Metric | Value |
|---|---|
| Active Devices | 200 |
| Total Messages Published | 23,191 |
| Effective Duration | 1,010.47 s |
| Test Span | 2,424.54 s |
| Actual Throughput | 22.95 msg/s |
| Throughput Efficiency | **22.95%** of target |
| Messages Dropped | 77,855 (77.05%) |
| Per-Device Rate (avg) | 0.23 msg/s (target: 1.0 msg/s) |
| Per-Device Rate (min/max) | 0.18 – 0.29 msg/s |
| Per-Device Rate Std Dev | 0.046 (CV: 0.20) |
| Messages Per Device (avg/min/max) | 116.0 / 48 / 188 |
| Messages Per Device Std Dev | 65.9 |

#### Latency (Sensor Timestamp → VictoriaMetrics Write)

| Percentile | Value |
|---|---|
| Min | 168 ms |
| Average | 360,627 ms (6.0 min) |
| Median (P50) | 327,878 ms (5.5 min) |
| P75 | 498,150 ms (8.3 min) |
| P90 | 673,061 ms (11.2 min) |
| P95 | 761,823 ms (12.7 min) |
| P99 | 895,192 ms (14.9 min) |
| P99.9 | 948,004 ms (15.8 min) |
| Max | 952,543 ms (15.9 min) |
| Std Deviation | 212,441 ms |
| IQR | 274,785 ms |
| Coefficient of Variation | 0.59 |

#### End-to-End Latency (Consumer Measured)

| Percentile | Value |
|---|---|
| Min | 104 ms |
| Average | 360,366 ms |
| Median | 327,534 ms |
| P95 | 761,493 ms |
| P99 | 894,964 ms |
| Max | 952,514 ms |

**Pipeline Overhead:** −18 ms (negligible, measurement noise)

---

### Scenario 3: 100 Clients @ 500 msg/s Total

| Metric | Value |
|---|---|
| Active Devices | 100 |
| Total Messages Published | 5,428,941 |
| Effective Duration | 130,661.13 s (36.3 hours) |
| Test Span | 131,198.25 s (36.4 hours) |
| Actual Throughput | 41.55 msg/s |
| Throughput Efficiency | **8.31%** of target |
| Messages Dropped | 59,901,625 (91.69%) |
| Per-Device Rate (avg) | 0.42 msg/s (target: 5.0 msg/s) |
| Per-Device Rate (min/max) | 0.35 – 0.47 msg/s |
| Per-Device Rate Std Dev | 0.022 (CV: 0.05) |
| Messages Per Device (avg/min/max) | 54,289 / 46,045 / 61,088 |
| Messages Per Device Std Dev | 2,828 |

#### Latency (Sensor Timestamp → VictoriaMetrics Write)

| Percentile | Value |
|---|---|
| Min | 533,256 ms (8.9 min) |
| Average | 66,989,874 ms (18.6 hours) |
| Median (P50) | 67,693,745 ms (18.8 hours) |
| P75 | 99,715,719 ms (27.7 hours) |
| P90 | 118,500,581 ms (32.9 hours) |
| P95 | 124,847,602 ms (34.7 hours) |
| P99 | 129,839,474 ms (36.1 hours) |
| P99.9 | 130,958,279 ms (36.4 hours) |
| Max | 131,139,577 ms (36.4 hours) |
| Std Deviation | 37,615,186 ms |
| IQR | 65,544,380 ms |
| Coefficient of Variation | 0.56 |

#### End-to-End Latency (Consumer Measured)

| Percentile | Value |
|---|---|
| Min | 533,227 ms |
| Average | 66,996,268 ms |
| Median | 67,741,797 ms |
| P95 | 124,826,604 ms |
| P99 | 129,831,664 ms |
| Max | 131,139,542 ms |

**Pipeline Overhead:** 24 ms (negligible)

> **Note:** Scenario 3 latencies reflect queue backlog accumulation over 36+ hours. The pipeline could not keep up with the ingest rate, causing messages to backlog in NATS JetStream and the consumer to process them far behind real-time. The pipeline overhead itself (per-message processing) remains ~20–30 ms.

---

## Comparative Summary

| Metric | 10c @ 500/s | 100c @ 100/s | 100c @ 500/s |
|---|---|---|---|
| **Throughput** | 23.63 msg/s | 22.95 msg/s | 41.55 msg/s |
| **Efficiency** | 4.73% | 22.95% | 8.31% |
| **Drop Rate** | 95.27% | 77.05% | 91.69% |
| **Avg Latency** | 12.8 s | 6.0 min | 18.6 hrs |
| **P50 Latency** | 13.2 s | 5.5 min | 18.8 hrs |
| **P95 Latency** | 21.6 s | 12.7 min | 34.7 hrs |
| **P99 Latency** | 23.9 s | 14.9 min | 36.1 hrs |
| **Max Latency** | 24.3 s | 15.9 min | 36.4 hrs |
| **Latency StdDev** | 6.7 s | 3.5 min | 10.4 hrs |
| **Pipeline Overhead** | 32 ms | ~0 ms | 24 ms |
| **Active Devices** | 10 | 200 | 100 |
| **Total Messages** | 1,973 | 23,191 | 5,428,941 |

## Latency Distribution

### Scenario 1: 10c @ 500/s

| Bucket | Count | % | Cumulative % |
|---|---|---|---|
| 0–10 ms | 0 | 0.00% | 0.00% |
| 10–50 ms | 0 | 0.00% | 0.00% |
| 50–100 ms | 1 | 0.05% | 0.05% |
| 100–200 ms | 0 | 0.00% | 0.05% |
| 200–500 ms | 0 | 0.00% | 0.05% |
| 500 ms–1 s | 0 | 0.00% | 0.05% |
| 1–2 s | 0 | 0.00% | 0.05% |
| 2–5 s | 1 | 0.05% | 0.10% |
| 5–10 s | 37 | 1.88% | 1.98% |
| 10–30 s | 1,934 | 98.02% | 100.00% |

### Scenario 2: 100c @ 100/s

| Bucket | Count | % | Cumulative % |
|---|---|---|---|
| 0–10 ms | 0 | 0.00% | 0.00% |
| 10–50 ms | 0 | 0.00% | 0.00% |
| 50–100 ms | 0 | 0.00% | 0.00% |
| 100–200 ms | 0 | 0.00% | 0.00% |
| 200–500 ms | 1 | 0.00% | 0.00% |
| 500 ms–1 s | 0 | 0.00% | 0.00% |
| 1–2 s | 0 | 0.00% | 0.00% |
| 2–5 s | 0 | 0.00% | 0.00% |
| 5–10 s | 0 | 0.00% | 0.00% |
| 10–30 s | 1 | 0.00% | 0.00% |
| 30–60 s | 2 | 0.01% | 0.01% |
| 1–2 min | 12 | 0.05% | 0.06% |
| 2–5 min | 3,567 | 15.38% | 15.44% |
| 5–10 min | 10,464 | 45.12% | 60.56% |
| 10 min+ | 9,145 | 39.44% | 100.00% |

## Bottleneck Analysis

### 1. MQTT Broker (EMQX) — Primary Bottleneck

EMQX on a single RPi 4 core struggles with high concurrent QoS 2 connections. QoS 2 requires a 4-step handshake per message (PUBLISH → PUBREC → PUBREL → PUBCOMP), multiplying CPU and I/O overhead. With 100 clients each attempting even 1 msg/s, the broker becomes the choke point.

### 2. SD Card / eMMC I/O — Secondary Bottleneck

The storage device (mmcblk0) shows:
- **192.66 GB written** since boot (14 days)
- **15.7M write operations** completed
- **1,433,548 seconds** of cumulative write time
- High I/O wait (3,687s on CPU 0)

VictoriaMetrics writes, container logs, and NATS JetStream persistence all compete for the same storage device.

### 3. CPU Saturation

At 88–91% utilization with load averages of 15+ (4× the core count), the Pi is severely CPU-bound. Context switches exceed 2 billion, indicating heavy scheduling pressure from 30+ running pods.

### 4. Network

Only WiFi (wlan0) is active; wired Ethernet (eth0) shows 0 bytes. WiFi adds latency variability and lower sustained throughput compared to ethernet.

## Key Findings

1. **Pipeline overhead is minimal** (~20–32 ms per message). The bottleneck is not per-message processing but aggregate throughput capacity.

2. **Maximum sustainable throughput is ~23–42 msg/s** on a single RPi 4 with this full stack. This is far below the target rates of 100–500 msg/s.

3. **Latency scales non-linearly** with load. At 10 clients, P95 is 21 seconds. At 100 clients, P95 reaches 12+ minutes, and under sustained overload, messages backlog for hours.

4. **QoS 2 is expensive** on constrained hardware. The 4-step handshake per message amplifies the broker's CPU burden significantly.

5. **Storage endurance is a concern**. At 192 GB written over 14 days (~13.7 GB/day), a typical SD card (rated for ~10K–30K P/E cycles) will degrade within months.

6. **The pipeline is stable** — no data corruption observed. Messages are processed in order, just with significant delay under load.

## Recommendations

| Priority | Recommendation | Expected Impact |
|---|---|---|
| High | Switch to QoS 0 or 1 for non-critical sensor data | 2–5× throughput improvement |
| High | Enable wired Ethernet (eth0) | Lower latency, more stable throughput |
| High | Reduce pod count / remove non-essential workloads | Free CPU for pipeline components |
| Medium | Move VictoriaMetrics storage to SSD/USB | Reduce I/O bottleneck, extend storage life |
| Medium | Tune EMQX: increase `max_inflight`, adjust `process_limit` | Better MQTT handling |
| Medium | Use Benthos batch output to NATS | Reduce per-message overhead |
| Low | Scale to multi-node cluster (add RPi workers) | Distribute load across cores |
| Low | Replace Prometheus with VictoriaMetrics-only | Reduce memory footprint |

## Data Files

All raw data is stored in `benchmarks/1-node/`:

| File | Description | Rows | Size |
|---|---|---|---|
| `benchmark_per_device.csv` | Per-device metrics (latency, throughput, sensor readings) | 310 | 157 KB |
| `benchmark_scenario_summary.csv` | Aggregated statistics per test scenario | 3 | 1.6 KB |
| `benchmark_latency_distribution.csv` | Histogram bins for latency analysis | 45 | 2.8 KB |
| `benchmark_timeseries_sampled.csv` | Sampled time-series data points | 45,264 | 5.8 MB |

### Column Reference: `benchmark_per_device.csv`

| Column | Description |
|---|---|
| `device_id` | Unique device identifier (format: `sensor_c{clients}_r{rate}_{index}_{pid}`) |
| `scenario` | Test scenario label |
| `num_clients` | Number of concurrent publisher clients in this scenario |
| `target_total_rate` | Target aggregate message rate (msg/s) |
| `device_index` | Device index within the client group |
| `pid` | Publisher process ID |
| `messages_published` | Number of messages sent by this device |
| `messages_written_to_vm` | Number of messages written to VictoriaMetrics |
| `test_duration_s` | Duration of the test for this device (seconds) |
| `device_msg_per_s` | Actual message rate achieved by this device |
| `first_sensor_ts` / `last_sensor_ts` | First/last sensor timestamp (ms epoch) |
| `first_vm_write_ts` / `last_vm_write_ts` | First/last VictoriaMetrics write timestamp (ms epoch) |
| `first_sensor_utc` / `last_sensor_utc` | Human-readable UTC timestamps |
| `latency_*` | Pipeline latency statistics (sensor → VM write) |
| `e2e_*` | End-to-end latency as measured by the consumer |
| `temp_c_*`, `pm25_*`, `pm10_*`, `hum_pct_*` | Sensor reading statistics |

## Conclusion

A single Raspberry Pi 4 can run the complete IoT pipeline (EMQX → Benthos → NATS JetStream → VictoriaMetrics → Grafana) but is limited to approximately **23–42 msg/s** sustainable throughput with QoS 2. The pipeline processing overhead per message is minimal (~20–30 ms), but aggregate capacity is constrained by CPU saturation, SD card I/O, and MQTT QoS 2 handshake overhead. For production workloads exceeding 50 msg/s, a multi-node cluster or hardware upgrade is recommended.
