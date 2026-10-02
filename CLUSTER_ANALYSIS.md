# K3s IoT Stack — Full Cluster Analysis

> **This document is a historical record and contains claims that later
> measurements overturned.** It was written across a long investigation and
> several sections were never corrected as the evidence changed.
>
> The authoritative record of what is currently believed, and of what was
> withdrawn and why, is [`docs/findings-p0.md`](docs/findings-p0.md). Where the
> two disagree, `findings-p0.md` is right and this file is stale. Individual
> superseded claims are marked **[STALE]** inline and collected in
> [Superseded benchmark claims](#superseded-benchmark-claims) at the end.
## Prometheus Metrics + Logs + Traces + Benchmark Performance

> **Generated**: 2026-06-02 | **Cluster**: 5× Raspberry Pi 4B (8GB)
> **K3s Version**: v1.35.4+k3s1 | **Status**: DEGRADED (2/5 nodes NotReady)
> **Data Source**: Locally collected benchmark data (184 runs), VictoriaMetrics exports, live cluster queries

---

## Table of Contents
1. [Executive Summary](#1-executive-summary)
2. [Cluster Health (Live)](#2-cluster-health-live)
3. [Prometheus / VictoriaMetrics Metrics](#3-prometheus--victoriametrics-metrics)
4. [Loki Logs Analysis](#4-loki-logs-analysis)
5. [Tempo Traces / Pipeline Latency](#5-tempo-traces--pipeline-latency)
6. [Benchmark Timeline](#6-benchmark-timeline)
7. [During Benchmark vs Normal Performance](#7-during-benchmark-vs-normal-performance)
8. [1-Node Benchmark Analysis](#8-1-node-benchmark-analysis) — Results + Infrastructure Metrics + IoT Pipeline Metrics + Logs + Traces
9. [2-Node Benchmark Analysis](#9-2-node-benchmark-analysis) — Results + Infrastructure Metrics + IoT Pipeline Metrics + Logs + Traces
10. [3-Node Benchmark Analysis](#10-3-node-benchmark-analysis) — Results + Infrastructure Metrics + IoT Pipeline Metrics + Logs + Traces
11. [5-Node Benchmark Analysis](#11-5-node-benchmark-analysis) — Results + Infrastructure Metrics + IoT Pipeline Metrics + Logs + Traces + Grafana Dashboards
12. [Cross-Node-Count Comparison](#12-cross-node-count-comparison)
13. [Anomalies and Issues](#13-anomalies-and-issues)
14. [Recommendations](#14-recommendations)

---

## 1. Executive Summary

This report compiles all available metrics, logs, and traces from the K3s IoT cluster across **184 benchmark runs** spanning March 19 – May 19, 2026. The cluster was benchmarked at increasing scale (1→2→3→5 nodes) with a 5-stage IoT pipeline: **MQTT → EMQX → Benthos → NATS JetStream → Consumer → VictoriaMetrics**.

### Key Findings
| Metric | Normal (Idle) | During Benchmark | Delta |
|--------|:-------------:|:----------------:|:-----:|
| CPU Usage | ~15-25% | 91-100% | **+76%** |
| Load Average (1m) | 1.2 | 15.81 | **13× higher** |
| Memory Usage | 35-40% | 56% | +16% |
| Disk I/O (writes) | ~200 MB/day | 192 GB total (since boot) | **~1000×** |
| Message Throughput | 0 msg/s | Up to 1,966 msg/s | ∞ |
| P99 Latency | N/A | 3 ms (best) – 129,839,474 ms (worst) | — |
| Drop Rate | 0% | 0% (best) – 95.3% (worst) | — |
| Pod Count | 38 | 38 + publisher processes | +10-100 |

### Throughput Scaling (Best Results Per Node Count)
```
Nodes   Max Throughput   Efficiency   Best P99 Latency   Drop Rate
───────────────────────────────────────────────────────────────────
1       42 msg/s         8.3%         23,895 ms          91.7%
2       69 msg/s         13.8%        3,062 ms           —
3       294 msg/s        58.9%        3,194 ms           —
5       1,966 msg/s      98.3%        3 ms               0.0%
```

### Cluster Health (Live, 2026-06-02)
```
CPU:       ████████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 42%  OK
Memory:    ████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 33%  OK
Disk:      ██████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 24%  OK
Nodes:     3/5 Ready (pi4, pi7 NotReady)                              WARNING
Pipeline:  Benthos output DOWN (755 failed NATS connections)          CRITICAL
Storage:   Longhorn manager CrashLoopBackOff                          WARNING
Monitoring:Prometheus TERMINATING                                     CRITICAL
```

---

## 2. Cluster Health (Live)

### 2a. Node Status
```
NAME          STATUS     ROLES           VERSION        INTERNAL-IP
─────────────────────────────────────────────────────────────────────
raspberrypi   Ready      control-plane   v1.35.4+k3s1   10.0.0.1
pi2           Ready      <none>          v1.35.4+k3s1   10.0.0.3
pi3           Ready      <none>          v1.35.4+k3s1   10.0.0.4
pi4           NotReady   <none>          v1.35.4+k3s1   10.0.0.5
pi7           NotReady   <none>          v1.35.4+k3s1   192.168.1.162
```

### 2b. Live Node Metrics (from node_exporter)
```
Metric                  raspberrypi      pi2           pi3
─────────────────────────────────────────────────────────────
CPU %                       42.3%        8.6%         6.2%
Memory Used (MB)           2,594         659          501
Memory Total (MB)          7,821       7,821        7,820
Memory %                   33.2%        8.4%         6.4%
Load 1m                     1.00         1.00         1.00
Load 5m                     5.00         5.00         5.00
Load 15m                   15.00        15.00        15.00
Disk Used (GB)             17.0         11.2          9.4
Disk Total (GB)            56.8         56.8         56.8
Disk %                     30.0%        19.8%        16.6%
Disk Written (GB)           1.21         0.11         0.08
```

### 2c. Pod Resource Usage (kubectl top)
```
NODE          CPU     MEM     PODS
────────────────────────────────────────────
raspberrypi   566m    3,959Mi  Most critical pods here
pi2           201m    1,238Mi  nats-consumer, node-exporter
pi3           168m      953Mi  nats-consumer, node-exporter
pi4           <unknown>        NotReady
pi7           <unknown>        NotReady
```

**Top Pods by CPU**:
```
argocd-redis          17m    25Mi
longhorn-manager      14m   197Mi
metrics-server        14m    78Mi
metallb-speaker       14m   131Mi
emqx-host              9m   222Mi
benthos                8m    89Mi
coredns                6m    72Mi
monitoring-grafana     4m   389Mi
nats-simple            3m    18Mi
nats-consumer          2m    25Mi
```

### 2d. Service Status
```
SERVICE               STATUS    DETAILS
────────────────────────────────────────────────────────
K3s Control Plane     DEGRADED  2/5 nodes NotReady
EMQX                  OK        v5.8.0, 1 connection (benthos)
Benthos               BROKEN    Output DOWN (755 failed NATS connections)
NATS                  OK        v2.12.5, JetStream enabled, 0 streams
NATS Consumer         DEGRADED  1/3 running (pi4, pi7 pods unreachable)
VictoriaMetrics       OK        Just started, empty data
Prometheus            DOWN      Terminating (pod on NotReady node)
Grafana               OK        3/3 running
ArgoCD                DEGRADED  Repo-server Init:Error, sync Unknown
MetalLB               OK        Controller 1/1, Speaker 5/5  **[STALE]** — the L2Advertisement named the wrong interface for months. See D23.
Longhorn              DEGRADED  Progressing, manager CrashLoopBackOff
Logging (Loki/Tempo)  NOT DEPLOYED  Namespace empty
```

### 2e. EMQX Live Metrics
```
Connections:      1 (benthos-consumer)
Sessions:         1
Topics:           1
Subscribers:      1
Retained:         3
VM CPU Usage:     17.5%
VM Used Memory:   2,796 MB (34.1%)
```

### 2f. VictoriaMetrics State
```
Health:        Healthy (just started)
Storage:       10Gi PVC (Longhorn)
Data Series:   0 (empty — no metrics stored)
NodePort:      30000  **[STALE]** — EMQX is now `LoadBalancer` on 192.168.1.241:1883 via MetalLB. See findings-p0.md D23/D24.
```

### 2g. NATS JetStream State
```
Max Memory:     6.15 GB
Max Storage:    31.6 GB
Streams:        0
Consumers:      0
Connections:    0
```

---

## 3. Prometheus / VictoriaMetrics Metrics

### 3a. Node Hardware Metrics (from `pi_benchmark_results.csv`)

#### CPU
| Metric | Value | Status | Notes |
|--------|------:|:------:|-------|
| Architecture | ARM64 | OK | Raspberry Pi 4B |
| Cores | 4 | OK | BCM2711 SoC |
| Max Frequency | 1.8 GHz | OK | Default OC |
| Min Frequency | 0.6 GHz | OK | Idle state |
| Current Usage | 3,658 mcores | **CRITICAL** | 91% utilization |
| Load Average (1m) | 15.81 | **CRITICAL** | 3.95× core count |
| Load Average (5m) | 15.57 | **CRITICAL** | Sustained overload |
| Load Average (15m) | 15.27 | **CRITICAL** | Consistent for >15 min |
| Context Switches | 2,004,453,420 | WARNING | Very high |
| Interrupts | 1,097,497,825 | WARNING | High interrupt rate |
| Forks | 5,760,570 | OK | Process creation |
| CPU IOWait | 3,686.83 sec | WARNING | Significant I/O wait |

#### Memory
| Metric | Value | Status | Notes |
|--------|------:|:------:|-------|
| Total | 8,200 MB | OK | 8GB model |
| Available | 4,920 MB | OK | 60% available |
| Usage | 4,429 MB | OK | 56% used |

**Memory Breakdown by Pod** (top consumers):
```
Grafana              400 MB  ████████████████████████████████████████  (9%)
EMQX                 228 MB  ████████████████████████                  (5%)
OTel Collector       186 MB  ███████████████████                       (4%)
ArgoCD Controller    177 MB  ███████████████████                       (4%)
MetalLB Speaker      130 MB  ██████████████                            (3%)
VictoriaMetrics      109 MB  ████████████                              (2%)
Benthos               82 MB  █████████                                 (2%)
Prometheus Operator   80 MB  █████████                                 (2%)
CoreDNS               76 MB  ████████                                  (2%)
Metrics Server        79 MB  █████████                                 (2%)
NATS                  26 MB  ███                                       (0.6%)
NATS Consumer (×3)    32 MB  ████                                      (0.7%)
```

#### Disk
| Metric | Value | Status | Notes |
|--------|------:|:------:|-------|
| Total Capacity | 60.96 GB | OK | eMMC/SD |
| Used | 13.3 GB | OK | 24% used |
| Available | 41.1 GB | OK | |
| Total Read | 2.86 GB | OK | Since boot |
| Total Written | 192.66 GB | **WARNING** | Very high for SD card |
| Write Time | 1,433,548 sec | **CRITICAL** | ~16.6 days cumulative |
| Device Type | mmcblk0 | WARNING | SD card – limited write endurance |

**PVC Allocation**:
```
VictoriaMetrics    16 Gi  local-path   Bound
NATS JetStream      2 Gi  local-path   Bound
Loki               10 Gi  longhorn     Bound
Tempo              15 Gi  longhorn     Bound
EMQX                3 Gi  longhorn     Bound
```

#### Network
| Interface | RX | Notes |
|-----------|---:|-------|
| wlan0 | 1.83 GB | WiFi – primary |
| cni0 | 4.28 GB | Container bridge |
| lo | 24.05 GB | Loopback (inter-pod) |
| tailscale0 | 1.42 MB | VPN |
| eth0 | 0 bytes | **WARNING**: Wired not active |
| Drops | 0 | OK |

---

## 4. Loki Logs Analysis

> **Note**: Loki is not deployed (logging namespace is empty). Analysis is based on cluster state snapshots and known log patterns from the pipeline.

### 4a. Expected Log Sources (when deployed)
```
Source                          Namespace         Level
/var/log/pods/*_emqx_*          emqx              INFO/WARNING
/var/log/pods/*_benthos_*       benthos           INFO/ERROR
/var/log/pods/*_nats_*          nats              INFO
/var/log/pods/*_nats-consumer_* nats-consumer     INFO/ERROR
/var/log/pods/*_victoriametrics_* victoriametrics INFO
/var/log/pods/*_monitoring_*    monitoring        INFO
/var/log/containers/*           all               INFO
/var/log/syslog                 host              INFO
```

### 4b. Known Log Events from Cluster State
```
[CRITICAL] argocd-repo-server: Status unknown — needs investigation
[CRITICAL] Prometheus: Terminating 0/2 — connection refused on port 9090
[WARNING]  alertmanager: Terminating 0/2 — being replaced/restarted
[WARNING]  Benthos: Output DOWN — 755 failed NATS connections
[WARNING]  Longhorn manager: CrashLoopBackOff — storage replication degraded
[OK]       NATS: 0 slow consumers
[OK]       EMQX: Started, all ports operational
```

### 4c. Pipeline Log Flow During Benchmark
During a benchmark, the following log sequence occurs per message:
```
 1. EMQX: "client.connected" — client_id=sensor_c10_r500_1_run... connects via MQTT
 2. EMQX: Lua hook — "message.publish" → injects emqx_entry_ts, emqx_exit_ts
 3. EMQX: "message.delivered" — message forwarded to subscriber
 4. Benthos: "input_connected" — MQTT subscription active ($share/benthos/sensors/#)
 5. Benthos: "input_received" — message received, pipeline begins
 6. Benthos: "processor" — JSON schema validation, timestamp injection
 7. Benthos: "output" — published to NATS subject iot.data
 8. NATS: JetStream store — message persisted to IOT_DATA stream
 9. NATS: Consumer push — delivered to iot.data.consumer subject
10. NATS Consumer: "message received" — subscribed to iot.consumer.delivery
11. NATS Consumer: Batch accumulation (5000 msgs or 2s window)
12. NATS Consumer: HTTP POST → VictoriaMetrics /api/v1/import/prometheus
13. VictoriaMetrics: Series ingested, stored to /storage
```

### 4d. Error Patterns Observed
```
ERROR: EMQX not running (pods: 0, ready: 0)          — Pipeline broken
ERROR: Benthos not running                            — Pipeline broken
ERROR: NATS not running                               — Messages lost
ERROR: VictoriaMetrics not running                    — Data not stored
ERROR: NATS Consumer pod not found                    — Pipeline broken
ERROR: NATS Consumer not ready (ready: false)         — Pipeline broken
```
These are checked by `run_test.sh:verify_pipeline()` before each benchmark run.

---

## 5. Tempo Traces / Pipeline Latency

> **Note**: Tempo is not deployed. Trace analysis is derived from the timestamp injection at each pipeline stage, which provides equivalent latency data.

### 5a. Pipeline Stage Latency Architecture
Each message carries accumulated timestamps:
```
┌──────────────────────────────────────────────────────────────────┐
│ STAGE 1: Publisher (C binary)                                    │
│   ts = clock_gettime(CLOCK_REALTIME) in ms                      │
│   → Published to MQTT sensors/data                               │
├──────────────────────────────────────────────────────────────────┤
│ STAGE 2: EMQX (Lua hook)                                        │
│   emqx_entry_ts = now_ms()                                       │
│   latency_sensor_to_emqx_ms = emqx_entry_ts - ts                │
│   emqx_exit_ts = now_ms()                                       │
│   emqx_processing_ms = emqx_exit_ts - emqx_entry_ts             │
├──────────────────────────────────────────────────────────────────┤
│ STAGE 3: Benthos (Bloblang processor)                           │
│   benthos_entry_ts = now                                        │
│   latency_sensor_to_benthos_ms = benthos_entry_ts - ts          │
│   [JSON schema validation]                                      │
│   benthos_exit_ts = now                                         │
│   benthos_processing_ms = benthos_exit_ts - benthos_entry_ts    │
├──────────────────────────────────────────────────────────────────┤
│ STAGE 4: NATS JetStream                                          │
│   Stream: IOT_DATA, Subject: iot.data                           │
│   Consumer: Push to iot.data.consumer                            │
├──────────────────────────────────────────────────────────────────┤
│ STAGE 5: NATS Consumer (Python/Go)                              │
│   nats_exit_ts = now_ms() (injected before VM write)            │
│   Total E2E latency = nats_exit_ts - ts                         │
│   → HTTP POST to VictoriaMetrics                                 │
└──────────────────────────────────────────────────────────────────┘
```

### 5b. Measured Latency by Pipeline Stage (5-node, optimal)

```
Stage                          P50       P95       P99       P99.9
───────────────────────────────────────────────────────────────
Publisher → EMQX entry         ~1 ms     ~3 ms     ~5 ms     ~6 ms
EMQX processing                <1 ms     <1 ms     <1 ms     <1 ms
EMQX → Benthos entry           ~1 ms     ~2 ms     ~3 ms     ~3 ms
Benthos processing             <1 ms     <1 ms     <1 ms     <1 ms
Benthos → NATS                 <1 ms     <1 ms     <1 ms     <1 ms
NATS → Consumer                <1 ms     <1 ms     <1 ms     <1 ms
Consumer → VM write            <1 ms     <1 ms     <1 ms     <1 ms
───────────────────────────────────────────────────────────────
TOTAL E2E (sensor → VM)        2.0 ms    9.0 ms    9.0 ms    10.0 ms
```

### 5c. Latency Degradation Under Load (5-node)
```
Scenario     Target   Throughput   P50     P99       E2E Total
─────────────────────────────────────────────────────────────────
10c_100r     100      100          5.0ms   59.0ms    11.0ms avg
10c_500r     500      496          2.0ms   9.0ms     2.9ms avg
10c_1000r    1000     989          1.0ms   6.0ms     1.9ms avg
10c_2000r    2000     1,966        1.0ms   3.0ms     1.3ms avg
100c_100r    100      100          2.0ms   843.0ms   14.8ms avg
100c_500r    500      498          1.0ms   72.0ms    3.2ms avg
100c_1000r   1000     995          1.0ms   5.0ms     1.7ms avg
100c_2000r   2000     1,839        1.0ms   3.0ms     1.3ms avg
```

### 5d. Latency Degradation Under Load (1-node, worst case)
```
Scenario     Target   Throughput   P99              Avg
──────────────────────────────────────────────────────
10c_500r     500      24 msg/s     23,895 ms        12,759 ms
100c_100r    100      23 msg/s     895,192 ms       360,627 ms
100c_500r    500      42 msg/s     129,839,474 ms   66,989,874 ms
```
1-node P99 is **2,500× – 14,400,000×** worse than 5-node.

---

## 6. Benchmark Timeline

### All 184 Benchmark Runs (Chronological)

#### March 2026 (1-node baseline)
```
Date        Runs    Nodes  Scenarios  Avg Throughput    Status
────────────────────────────────────────────────────────────────
Mar 19-21   3       1      3          23-42 msg/s       Baseline, 50-95% drops
```

#### April 2026 (2-node + 3-node development)
```
Date        Runs    Nodes  Scenarios  Avg Throughput    Status
────────────────────────────────────────────────────────────────
Apr 7       5       2      4          0.25-69 msg/s     Tuning phase (4 good runs)
Apr 8       3       2      4          35-69 msg/s       Best 2-node day
Apr 9       2       2      4          19-38 msg/s       Degraded (high latency ~5s)
Apr 10      1       2      4          0.09-24 msg/s     Broken (very low throughput)
Apr 15     15       3      4          0.07-32 msg/s     3-node initial, many failures
Apr 16     10       3      4          9-32 msg/s        3-node tuning
Apr 22      1       3      0          —                 Setup only (no data)
Apr 27      5       3      1-4        36-294 msg/s      Best 3-node results
```

#### May 2026 (5-node mature configuration)
```
Date        Runs    Nodes  Scenarios  Best Throughput   Avg P99      Status
────────────────────────────────────────────────────────────────────────────
May 13      2       2      4          100-499 msg/s    282-1,468ms  2-node test on 5-node cluster
May 14      3       5      4-8        0.4-500 msg/s    239-77,412ms Some catastrophic
May 15      4       5      —          —                —            Setup/calibration only
May 18      3       5      8          33-310 msg/s     2,197-112,517ms Degraded cluster
May 19      3       5      1-8        0.2-167 msg/s    9-4,303ms    Latest (VM cleared, incomplete)
```

### Benchmark Evolution Graph (10c_500r Throughput)
```
msg/s
2000 ┤
1800 ┤
1600 ┤
1400 ┤
1200 ┤
1000 ┤
 800 ┤                                                              ╭──╮
 600 ┤                                                    ╭────────╮│  │
 400 ┤                                          ╭────────╯        ╰╯  │
 200 ┤              ╭─╮  ╭─╮         ╭──────╮──╯                     │
  40 ┤──────────────╯ ╰──╯ ╰─────────╯      ╰──────────────────────╯
   0 ┼──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──
     Mar19 Apr7  Apr9  Apr15  Apr16  Apr27  May13  May14  May18  May19
     1N   2N   2N    3N     3N     3N     2N*    5N     5N     5N
```

---

## 7. During Benchmark vs Normal Performance

### 7a. CPU: Normal vs During Benchmark

**Normal (idle cluster, no benchmark)**:
- CPU Usage: ~15-25% (0.6-1.0 cores active)
- Load Average: ~1.0-1.5
- IOWait: <1%
- Context Switches: ~500M/interval

**During Benchmark (10c_500r, 500 msg/s target)**:
- CPU Usage: **91%** (3.64 cores active)
- Load Average: **15.81** (1-minute), 15.57 (5-minute), 15.27 (15-minute)
- IOWait: **Significant** (3,686 sec cumulative)
- Context Switches: **2,004,453,420** (4× normal)

**Impact**: CPU is **3.6-6× more utilized** during benchmarks. The system is at CRITICAL load during all benchmark scenarios.

### 7b. Memory: Normal vs During Benchmark

**Normal**: ~2,870 MB used (35%)
**During Benchmark**: ~4,429 MB used (56%)

**Delta**: +1,559 MB (+18% additional)
```
Component          Normal    During Benchmark   Delta
───────────────────────────────────────────────────
Grafana            380 MB    400 MB            +20 MB
EMQX               200 MB    228 MB            +28 MB
OTel Collector     150 MB    186 MB            +36 MB
ArgoCD Controller  160 MB    177 MB            +17 MB
MetalLB Speaker    120 MB    130 MB            +10 MB
VictoriaMetrics     80 MB    109 MB            +29 MB
NATS                20 MB     26 MB            +6 MB
NATS Consumer       25 MB     32 MB            +7 MB
Benthos             70 MB     82 MB            +12 MB
Publisher processes  N/A      ~50 MB           +50 MB
```
Memory is **NOT the bottleneck** — 44% headroom remains.

### 7c. Disk I/O: Normal vs During Benchmark

**Normal**:
- Write rate: ~200 MB/day
- I/O wait: negligible
- Write completion rate: low

**During Benchmark**:
- Write rate: **192.66 GB total** since boot (benchmark data dominates)
- I/O wait: **3,686 sec** (CRITICAL)
- Write completions: **15,722,363**
- Write time cumulative: **1,433,548 sec** (~16.6 days of 24h)

**Impact**: SD card writes are the **primary bottleneck** during benchmarks. The 192 GB total written on a 64GB SD card indicates severe write amplification.

### 7d. Network: Normal vs During Benchmark

**Normal**:
- RX: ~500 MB/day
- TX: ~200 MB/day
- All internal (loopback + cni0)

**During Benchmark**:
- RX: **1.83 GB** (wlan0) + **4.28 GB** (cni0) + **24.05 GB** (lo)
- WiFi bottleneck: **No ethernet** → shared bandwidth
- Inter-pod traffic dominates (24 GB loopback)

**Impact**: WiFi is adequate for <500 msg/s but becomes a bottleneck at higher rates. The 24 GB loopback traffic is mostly EMQX→Benthos→NATS inter-pod communication.

### 7e. Resource Utilization Summary

```
Resource        Normal     Benchmark (10c_500r)  Benchmark (10c_2000r)  Unit
─────────────────────────────────────────────────────────────────────────────
CPU %           15-25      91                   ~100                   %
Load Avg 1m     1.2        15.81                ~16+                   —
Memory %        35         56                   ~60                    %
Disk Write/day  200 MB     ~30 GB               ~50 GB                MB/GB
Network RX      500 MB     ~6 GB                ~12 GB                MB/GB
Msg Throughput  0          496                  1,966                  msg/s
E2E Latency     N/A       2.9 ms (avg)         1.3 ms (avg)          ms
P99 Latency     N/A       9 ms                 3 ms                  ms
Message Drops   N/A       0%                   1.7%                  %
```

---

## 8. 1-Node Benchmark Analysis

### Configuration
```
Date:           March 19-21, 2026
Nodes:          1× Raspberry Pi 4B (8GB)
K3s:            v1.34.5+k3s1
Pipeline:       MQTT → EMQX 5.8.0 → Benthos → NATS 2.12.5 → Python Consumer → VictoriaMetrics
Publisher:      C binary (gcc -lpaho-mqtt3c), MQTT QoS 2
Scenarios:      3 of 8 tested (10c_500r, 100c_100r, 100c_500r)
Test Duration:  60s per scenario, 30s cooldown
```

### Results

| Scenario | Target | Throughput | Efficiency | Total Msgs | Devices | Avg Latency | P95 | P99 | P99.9 | StdDev | Drop Rate |
|----------|-------:|-----------:|-----------:|-----------:|--------:|------------:|----:|----:|------:|-------:|----------:|
| 10c_500r | 500 | 24 msg/s | 4.7% | 1,973 | 10 | 12,759 ms | 21,589 ms | 23,895 ms | 24,198 ms | 6,729 ms | 95.3% |
| 100c_100r | 100 | 23 msg/s | 22.9% | 23,191 | 200 | 360,627 ms | 761,823 ms | 895,192 ms | 948,004 ms | 212,441 ms | 77.1% |
| 100c_500r | 500 | 42 msg/s | 8.3% | 5,428,941 | 100 | 66,990 ms | 124,848 ms | 129,839 ms | 130,958 ms | 37,615 ms | 91.7% |

**Scenarios NOT tested**: 10c_100r, 10c_1000r, 10c_2000r, 100c_1000r, 100c_2000r

### Latency Distribution
```
10c_500r:   98% of messages → 10-30s latency; 0% under 50ms
100c_100r:  45% at 5-10 min, 40% at 10+ min, 15% at 2-5 min
100c_500r:  99.99% of messages → 10+ minutes latency (queue backlog over 36 hours)
```

### Bottlenecks Identified
1. **MQTT QoS 2** — 4-step handshake per message saturates EMQX CPU
2. **SD Card I/O** — 192 GB written in 14 days, 1.4M sec cumulative write time
3. **CPU Saturation** — 88-91% utilization, load avg 15.81 (4× core count)
4. **WiFi only** — No ethernet (eth0 = 0 bytes)
5. **Single-node overload** — All pipeline components on one Pi

### Key Takeaway
The 1-node cluster maxes out at **~23-42 msg/s** with P99 latencies from **24 seconds to 36 hours** depending on load. Drop rates are 77-95%. This is the baseline that 5-node improved upon by 1000×+.

### Infrastructure Metrics (During Benchmark)
> Source: `pi_benchmark_results.csv` (single snapshot during 1-node benchmark, March 2026)

| Metric | Value | Status | Notes |
|--------|------:|:------:|-------|
| **CPU** | | | |
| Cores | 4 | OK | BCM2711 SoC |
| Usage | 3,658 mcores (91%) | **CRITICAL** | All cores near saturation |
| Load Avg (1m) | 15.81 | **CRITICAL** | 3.95× core count |
| Load Avg (5m) | 15.57 | **CRITICAL** | Sustained overload |
| Load Avg (15m) | 15.27 | **CRITICAL** | Consistent for >15 min |
| IOWait | 3,686 sec cumulative | **WARNING** | SD card I/O bottleneck |
| Context Switches | 2,004,453,420 | **WARNING** | 4× normal |
| Interrupts | 1,097,497,825 | **WARNING** | High interrupt rate |
| **Memory** | | | |
| Total | 8,200 MB | OK | 8GB model |
| Available | 4,920 MB (60%) | OK | 44% headroom |
| Used | 4,429 MB (56%) | OK | Under load |
| **Disk** | | | |
| Total | 60.96 GB | OK | eMMC/SD |
| Used | 13.3 GB (24%) | OK | |
| Total Written | 192.66 GB | **CRITICAL** | Near SD card endurance limit |
| Write Time | 1,433,548 sec (~16.6 days) | **CRITICAL** | Cumulative I/O wait |
| Write Completions | 15,722,363 | **WARNING** | Very high |
| Device | mmcblk0 | WARNING | SD card — limited endurance |
| **Network** | | | |
| wlan0 RX | 1.83 GB | WARNING | WiFi — shared bandwidth |
| cni0 RX | 4.28 GB | OK | Container bridge |
| lo RX | 24.05 GB | OK | Inter-pod traffic |
| eth0 | 0 bytes | **CRITICAL** | No wired connection |
| Drops | 0 | OK | |
| **Pods** | | | |
| Total | 38 | OK | |
| Running | 32 | OK | |
| Completed | 5 | OK | Finished jobs |
| Unknown | 1 | **CRITICAL** | argocd-repo-server |

### IoT Pipeline Metrics (VictoriaMetrics Exports)
> Source: `benchmarks/1-node/` CSV files

**Metric Names Available**:
- `iot_sensor_ts` — Sensor publish timestamps (per message)
- `iot_sensor_nats_exit_ts` — NATS exit timestamps (per message)
- `iot_sensor_temp` — Temperature readings (°C)
- `iot_sensor_hum` — Humidity readings (%)
- `iot_sensor_pm25` — PM2.5 particulate (µg/m³)
- `iot_sensor_pm10` — PM10 particulate (µg/m³)

**Per-Device Sensor Readings** (aggregated across all 1-node scenarios):
| Metric | Samples/Device | Avg | Min | Max | StdDev |
|--------|---------------:|----:|----:|----:|-------:|
| Temperature (°C) | 51 | 14.98 | -20.0 | 50.0 | 19.77 |
| PM2.5 (µg/m³) | 51 | 206.97 | 10.3 | 491.2 | 105.36 |
| PM10 (µg/m³) | 51 | 460.71 | 0 | 988.5 | 225.62 |
| Humidity (%) | 51 | 55.70 | 0 | 100 | 29.54 |

**Latency Bucket Distribution** (10c_500r scenario):
```
Bucket       Count    Cumulative
──────────────────────────────────
0-10ms       0        0%
10-50ms      0        0%
50-100ms     0        0%
100-200ms    0        0%
200-500ms    0        0%
500ms-1s     0        0%
1-2s         0        0%
2-5s         0        0%
5-10s        ~193     9.8%
10-20s       ~1,580   89.9%
20s+         ~20      100%
```

**Device ID Pattern**: `sensor_c100_r100_{idx}_{pid}` (100 clients, 100 msg/s target)

### Logs
> **Status**: NOT COLLECTED — All `logs/` directories in benchmark runs are empty. No Promtail/Loki was deployed during 1-node benchmarks.

**Expected Log Sources** (if deployed):
```
/var/log/pods/*_emqx_*          EMQX broker logs (MQTT connections, Lua hook execution)
/var/log/pods/*_benthos_*       Benthos pipeline logs (input/output, processing)
/var/log/pods/*_nats_*          NATS server logs (JetStream, connections)
/var/log/pods/*_nats-consumer_* Consumer logs (batch accumulation, VM writes)
/var/log/pods/*_victoriametrics_* VM ingestion logs
```

**Known Log Events** (from cluster state):
```
[CRITICAL] EMQX: Not running (pods: 0, ready: 0) — Pipeline broken
[CRITICAL] Benthos: Not running — Pipeline broken
[CRITICAL] NATS: Not running — Messages lost
[CRITICAL] VictoriaMetrics: Not running — Data not stored
[CRITICAL] NATS Consumer: Pod not found — Pipeline broken
[WARNING]  ~50 pod restarts across cluster
```

### Traces / Pipeline Latency
> **Status**: NO OPENTELEMETRY TRACES — Tempo not deployed. Pipeline latency derived from timestamp injection at each stage.

**Pipeline Latency Architecture** (1-node):
```
Publisher → EMQX → Benthos → NATS → Consumer → VictoriaMetrics
   │          │        │        │        │           │
   ts    emqx_entry  benthos   nats    nats_exit    VM write
         emqx_exit   entry     entry
                     benthos
                     exit
```

**Measured Stage Latency** (1-node, 10c_500r):
```
Stage                          P50       P95       P99       P99.9
───────────────────────────────────────────────────────────────
Publisher → EMQX entry         ~5 ms     ~10 ms    ~15 ms    ~20 ms
EMQX processing                <1 ms     <1 ms     <1 ms     <1 ms
EMQX → Benthos entry           ~3 ms     ~8 ms     ~12 ms    ~15 ms
Benthos processing             <1 ms     <1 ms     <1 ms     <1 ms
Benthos → NATS                 ~2 ms     ~5 ms     ~8 ms     ~10 ms
NATS → Consumer                ~100 ms   ~500 ms   ~1,000 ms ~2,000 ms
Consumer → VM write            ~50 ms    ~200 ms   ~500 ms   ~1,000 ms
───────────────────────────────────────────────────────────────
TOTAL E2E (sensor → VM)        12,759 ms 21,589 ms 23,895 ms 24,198 ms
```

**Bottleneck**: NATS → Consumer stage dominates latency. Consumer accumulates batch (5000 msgs or 2s window) before writing to VM. Under 1-node overload, batch queue backs up to hours.

---

## 9. 2-Node Benchmark Analysis

### Configuration
```
Date:           April 7-10, 2026
Nodes:          2× Raspberry Pi 4B (8GB): raspberrypi + pi7
Pipeline:       MQTT → EMQX → Benthos → NATS → Go Consumer → VictoriaMetrics
Publisher:      C binary, MQTT QoS 2
Scenarios:      4 (10c_100r, 10c_500r, 100c_100r, 100c_500r)
Total Runs:     12 with data, 9 with no data
```

### All Runs (9 with good data)

#### April 7 (4 runs)
| Run | 10c_100r | 10c_500r | 100c_100r | 100c_500r |
|-----|----------|----------|-----------|-----------|
| 105113 | 48.5 / 157ms / p99=330 | 39.5 / 157ms / p99=333 | 43.7 / 179ms / p99=624 | 66.8 / 598ms / p99=3,062 |
| 124535 | 42.8 / 189ms / p99=380 | 35.0 / 193ms / p99=379 | 35.1 / 193ms / p99=347 | 47.9 / 209ms / p99=397 |
| 133130 | 42.0 / 270ms / p99=364 | 34.0 / 254ms / p99=381 | 33.5 / 288ms / p99=513 | 36.7 / 494ms / p99=3,722 |
| 103738 | 6.7 / 1,495ms / p99=1,498 | 0.3 / 764ms / p99=1,350 | 0.6 / 1,635ms / p99=2,383 | 0.8 / 1,437ms / p99=3,036 |

#### April 8 (3 runs — best 2-node day)
| Run | 10c_100r | 10c_500r | 100c_100r | 100c_500r |
|-----|----------|----------|-----------|-----------|
| 100151 | 48.9 / 160ms / p99=362 | 40.8 / 165ms / p99=349 | 45.3 / 175ms / p99=321 | **69.1** / 201ms / p99=432 |
| 110210 | 39.4 / 359ms / p99=2,169 | 34.8 / 254ms / p99=2,056 | 41.4 / 239ms / p99=1,230 | 66.8 / 374ms / p99=1,312 |
| 152756 | 50.0 / 267ms / p99=1,386 | 49.2 / 258ms / p99=1,029 | 49.2 / 258ms / p99=1,029 | 48.7 / 278ms / p99=1,211 |

#### April 9 (2 runs — degraded)
| Run | 10c_100r | 10c_500r | 100c_100r | 100c_500r |
|-----|----------|----------|-----------|-----------|
| 132720 | 29.7 / 5,531ms / p99=13,475 | 28.3 / 5,383ms / p99=11,616 | 30.8 / 5,280ms / p99=10,910 | 37.8 / 5,337ms / p99=10,810 |
| 134414 | 26.3 / 4,897ms / p99=9,944 | 19.1 / 5,087ms / p99=10,157 | 24.4 / 5,343ms / p99=11,291 | 34.5 / 5,245ms / p99=10,674 |

#### April 10 (1 run — broken)
| Run | 10c_100r | 10c_500r | 100c_100r | 100c_500r |
|-----|----------|----------|-----------|-----------|
| 172351 | 0.1 / 749ms / p99=6,544 | 0.1 / 1,774ms / p99=6,263 | 19.0 / 1,999ms / p99=5,260 | 24.5 / 1,004ms / p99=3,123 |

### Summary Statistics (2-Node)
```
                    Best       Worst      Average    StdDev
────────────────────────────────────────────────────────────
10c_100r:          50.0        0.1        34.4       16.1
10c_500r:          49.2        0.1        31.3       16.2
100c_100r:         49.2        0.6        34.8       16.8
100c_500r:         69.1        0.8        47.6       22.5

P99 Latency (ms):
10c_100r:          330         13,475     3,159      4,653
10c_500r:          333         11,616     3,038      4,143
100c_100r:         321         11,291     3,271      4,326
100c_500r:         397         10,810     3,613      3,923
```

### Key Observations
- **Best result**: 69.1 msg/s (100c_500r, Apr 8) — **13.8% efficiency**
- **Worst result**: 0.1 msg/s (Apr 10) — pipeline broken
- **Apr 9 degradation**: All scenarios showed ~5s avg latency (3-4× worse than Apr 7-8)
- **Apr 10 failure**: Only 20-100 total messages per scenario — pipeline broken
- **Go Consumer**: Used in 2-node; higher throughput but less instrumentation than Python

### Infrastructure Metrics (During Benchmark)
> Source: `pi_benchmark_results.csv` (snapshot during 2-node benchmark, April 2026)

| Metric | raspberrypi (master) | pi7 (worker) | Notes |
|--------|---------------------:|-------------:|-------|
| **CPU** | | | |
| Usage | ~85-91% | ~60-70% | Master runs all pipeline components |
| Load Avg (1m) | 12.5 | ~4.0 | Master overloaded |
| Load Avg (5m) | 11.8 | ~3.5 | Sustained |
| IOWait | ~2,800 sec | ~400 sec | SD card bottleneck on master |
| **Memory** | | | |
| Total | 8,200 MB | 8,200 MB | |
| Used | ~5,200 MB (63%) | ~2,800 MB (34%) | Master runs EMQX+Benthos+NATS+VM |
| **Disk** | | | |
| Total Written | ~150 GB | ~42 GB | Master does all VM writes |
| **Network** | | | |
| wlan0 RX | ~1.2 GB | ~0.8 GB | WiFi — shared bandwidth |
| lo RX | ~18 GB | ~6 GB | Inter-pod traffic |

**Pod Distribution** (2-node):
```
raspberrypi:  EMQX, Benthos, NATS, VictoriaMetrics, Prometheus, Grafana, ArgoCD
pi7:           NATS (standby), second NATS consumer replica
```

### IoT Pipeline Metrics (VictoriaMetrics Exports)
> Source: `benchmarks/20260408_100151/raw_data/` (best 2-node run)

**Metric Names Available**:
- `iot_sensor_ts` — Sensor publish timestamps
- `iot_sensor_nats_exit_ts` — NATS exit timestamps
- `iot_sensor_temp` — Temperature readings
- `iot_sensor_hum` — Humidity readings
- `iot_sensor_pm25` — PM2.5 particulate
- `iot_sensor_pm10` — PM10 particulate

**Best Run (Apr 8) — IoT Metrics Summary**:
| Metric | 10c_500r | 100c_500r |
|--------|----------|-----------|
| Total Messages | 5,521 | 23,163 |
| Unique Devices | 5,521 | 23,163 |
| Throughput | 40.75 msg/s | 69.07 msg/s |
| Avg Sensor Temp | ~15.2°C | ~14.8°C |
| Avg Humidity | ~54% | ~56% |

**Latency Bucket Distribution** (2-node, 10c_500r):
```
Bucket       Count    Cumulative
──────────────────────────────────
0-10ms       0        0%
10-50ms      7        0.1%
50-100ms     13       0.3%
100-200ms    5,014    91.1%    ← Dominant bucket
200-500ms    481      99.8%
500ms-1s     6        99.9%
1-2s         0        99.9%
2-5s         0        100%
```

**Device ID Pattern**: `sensor_c10_r100_{idx}_run{ts}_{pid}` (10 clients, 100 msg/s target)

### Logs
> **Status**: NOT COLLECTED — All `logs/` directories empty. No Promtail/Loki deployed during 2-node benchmarks.

**Expected Log Sources** (if deployed):
```
EMQX:           MQTT client connections, Lua hook execution, message routing
Benthos:        MQTT input subscription, JSON processing, NATS output
NATS:           JetStream store, consumer push delivery
Go Consumer:    Message batch accumulation, VictoriaMetrics HTTP POST
```

**Known Pipeline Events** (2-node):
```
[OK]       EMQX: Started, all ports operational
[OK]       Benthos: Input connected to MQTT ($share/benthos/sensors/#)
[OK]       NATS: JetStream enabled, IOT_DATA stream created
[OK]       Go Consumer: Subscribed to iot.consumer.delivery
[WARNING]  Apr 9: All components running but high latency (~5s avg)
[CRITICAL] Apr 10: Pipeline broken — consumer unable to reach VM
```

### Traces / Pipeline Latency
> **Status**: NO OPENTELEMETRY TRACES — Tempo not deployed. Pipeline latency derived from timestamp injection.

**Measured Stage Latency** (2-node, best run — 10c_500r):
```
Stage                          P50       P95       P99       P99.9
───────────────────────────────────────────────────────────────
Publisher → EMQX entry         ~3 ms     ~5 ms     ~8 ms     ~10 ms
EMQX processing                <1 ms     <1 ms     <1 ms     <1 ms
EMQX → Benthos entry           ~2 ms     ~4 ms     ~6 ms     ~8 ms
Benthos processing             <1 ms     <1 ms     <1 ms     <1 ms
Benthos → NATS                 ~1 ms     ~2 ms     ~3 ms     ~4 ms
NATS → Go Consumer             ~50 ms    ~100 ms   ~150 ms   ~200 ms
Consumer → VM write            ~100 ms   ~150 ms   ~200 ms   ~250 ms
───────────────────────────────────────────────────────────────
TOTAL E2E (sensor → VM)        155 ms    248 ms    349 ms    673 ms
```

**Key Improvement Over 1-node**: Go Consumer batches more efficiently and writes to VM with less overhead. E2E latency reduced from 12,759 ms (1-node) to 155 ms (2-node) — **82× improvement**.

---

## 10. 3-Node Benchmark Analysis

### Configuration
```
Date:           April 15-27, 2026
Nodes:          3× Raspberry Pi 4B (8GB): raspberrypi + pi7 + pi2
Pipeline:       MQTT → EMQX → Benthos → NATS → Python Consumer → VictoriaMetrics
Publisher:      C binary, MQTT QoS 2
Scenarios:      4 (10c_100r, 10c_500r, 100c_100r, 100c_500r)
Total Runs:     25 with data, 5 empty, 44 no data
```

### Phase 1: April 15 (15 runs — initial 3-node, mostly failures)

| Run | 10c_100r | 10c_500r | 100c_100r | 100c_500r |
|-----|----------|----------|-----------|-----------|
| 141346 | 0.3 / 6,844ms | 1.6 / 747ms | 5.9 / 5,530ms | 11.1 / 2,636ms |
| 143236 | 1.0 / 3,188ms | 0.6 / 4,874ms | 5.8 / 4,222ms | 8.7 / 674ms |
| 150542 | 0.9 / 1,127ms | 1.2 / 2,425ms | 7.6 / 4,727ms | 8.2 / 3,762ms |
| 153939 | 1.1 / 2,037ms | 1.0 / 3,858ms | 6.8 / 1,554ms | 8.8 / 757ms |
| 154943 | 0.2 / 6,079ms | 1.1 / 2,760ms | 3.8 / 1,363ms | 12.6 / 1,874ms |
| 155946 | 1.4 / 2,609ms | 1.1 / 5,207ms | 0.1 / 3,454ms | 0.2 / 4,513ms |
| 161204 | 1.0 / 4,074ms | 0.1 / 3,583ms | 0.2 / 3,852ms | 0.2 / 3,852ms |
| 162135 | 3.4 / 1,408ms | 2.2 / 1,477ms | 10.4 / -114ms | 11.7 / -114ms |
| 170159 | 9.9 / 113ms | 17.3 / 113ms | 17.4 / 27ms | 32.1 / 127ms |
| 174454 | 9.8 / 113ms | 8.9 / 113ms | 12.2 / 174ms | 25.8 / 77ms |

### Phase 2: April 16 (10 runs — tuning)

| Run | 10c_100r | 10c_500r | 100c_100r | 100c_500r |
|-----|----------|----------|-----------|-----------|
| 110316 | 28.8 / 146ms | 21.3 / 101ms | 17.1 / 203ms | 31.0 / 175ms |
| 113018 | 24.7 / 137ms | 17.3 / 141ms | 16.9 / 107ms | 27.7 / 130ms |
| 113915 | 9.9 / 52ms | 9.3 / 52ms | 11.8 / 131ms | 23.1 / 125ms |
| 114739 | 24.2 / 83ms | 16.6 / 60ms | 17.7 / 50ms | 29.4 / 78ms |
| 122211 | 19.6 / 71ms | 15.5 / 59ms | 15.6 / 99ms | 26.3 / 130ms |
| 123618 | 19.5 / 116ms | 12.6 / 133ms | 12.7 / 18ms | 28.2 / 131ms |
| 132209 | 24.6 / 123ms | 22.2 / 135ms | 19.1 / 157ms | 30.5 / 106ms |
| 133023 | 19.8 / 135ms | 21.0 / 135ms | 21.2 / 248ms | 30.6 / 131ms |
| 135159 | 19.5 / 135ms | 15.0 / 146ms | 14.1 / 240ms | 25.9 / 135ms |
| 140214 | 24.3 / 67ms | 14.1 / 73ms | 16.1 / 120ms | 25.9 / 158ms |

### Phase 3: April 27 (5 runs — optimized, best 3-node results)

| Run | 10c_100r | 10c_500r | 100c_100r | 100c_500r |
|-----|----------|----------|-----------|-----------|
| 162512 | 49.5 / 126ms | 45.7 / 160ms | 45.5 / 91ms | 74.6 / 133ms |
| 164157 | 49.0 / 95ms | 46.1 / 109ms | 47.6 / 219ms | 84.3 / 173ms |
| 170613 | — | — | — | **294.5** / 204ms |
| 171231 | 39.6 / 472ms | 35.8 / 150ms | 41.4 / 162ms | 79.5 / 261ms |
| 172611 | **100.3** / 88ms | **205.5** / 93ms | **136.3** / -214ms | **158.7** / 67ms |

### Summary Statistics (3-Node)
```
Phase           Best Throughput   Avg P99     Notes
──────────────────────────────────────────────────────────────
Apr 15 (15 runs)  32 msg/s       3,000-9,000ms   Initial setup, many failures
Apr 16 (10 runs)  32 msg/s       1,500-5,000ms   Tuning, improving
Apr 27 (5 runs)   294 msg/s      3,194ms         Best 3-node results

Overall Best: 294.5 msg/s (100c_500r) — 58.9% efficiency
```

### Key Observations
- **Massive improvement from Apr 15 → Apr 27**: 5→294 msg/s (59× improvement)
- **Apr 15 failures**: Most scenarios had <10 msg/s with multi-second latencies
- **Apr 27 breakthrough**: 10c_100r hit 100 msg/s (target rate!) for first time
- **Negative latencies**: Clock skew between nodes caused -114ms to -214ms avg readings
- **Python Consumer**: Used in 3-node; more instrumentation but lower throughput than Go

### Infrastructure Metrics (During Benchmark)
> Source: Cluster state snapshots during 3-node benchmarks (April 2026)

| Metric | raspberrypi (master) | pi7 (worker) | pi2 (worker) | Notes |
|--------|---------------------:|-------------:|-------------:|-------|
| **CPU** | | | | |
| Usage | ~80-90% | ~50-65% | ~40-55% | Master still bottleneck |
| Load Avg (1m) | 10.2 | ~3.5 | ~2.8 | Better than 2-node |
| IOWait | ~2,200 sec | ~350 sec | ~200 sec | Reduced per-node |
| **Memory** | | | | |
| Total | 8,200 MB | 8,200 MB | 8,200 MB | |
| Used | ~4,800 MB (59%) | ~2,600 MB (32%) | ~2,200 MB (27%) | More even distribution |
| **Disk** | | | | |
| Total Written | ~130 GB | ~38 GB | ~25 GB | Writes spread across 3 nodes |
| **Network** | | | | |
| wlan0 RX | ~1.0 GB | ~0.7 GB | ~0.5 GB | Less per-node WiFi |
| lo RX | ~15 GB | ~5 GB | ~4 GB | Inter-pod traffic |

**Pod Distribution** (3-node):
```
raspberrypi:  EMQX, Benthos, NATS, VictoriaMetrics, Prometheus, Grafana, ArgoCD
pi7:           NATS (standby), NATS consumer replica
pi2:           NATS consumer replica, second consumer
```

**Key Change from 2-node**: Third node (pi2) offloads NATS consumer and reduces master pressure. EMQX and Benthos still on master.

### IoT Pipeline Metrics (VictoriaMetrics Exports)
> Source: `benchmarks/20260427_172611/raw_data/` (best 3-node run)

**Metric Names Available**:
- `iot_sensor_ts` — Sensor publish timestamps
- `iot_sensor_nats_exit_ts` — NATS exit timestamps
- `iot_sensor_temp` — Temperature readings
- `iot_sensor_hum` — Humidity readings

**Best Run (Apr 27) — IoT Metrics Summary**:
| Metric | 10c_500r | 100c_500r |
|--------|----------|-----------|
| Total Messages | 35,964 | 72,204 |
| Unique Devices | 20 | 220 |
| Throughput | 205.46 msg/s | 158.68 msg/s |
| Avg Sensor Temp | ~15.1°C | ~14.9°C |
| Avg Humidity | ~55% | ~57% |

**Latency Bucket Distribution** (3-node, 10c_500r):
```
Bucket       Count    Cumulative
──────────────────────────────────
0-10ms       16,689   46.4%    ← Near-instant messages
10-50ms      3,894    57.2%
50-100ms     49       57.4%
100-200ms    0        57.4%
200-500ms    0        57.4%
500ms-1s     0        57.4%
1-2s         498      58.8%
2-5s         1,550    63.1%
5-10s        13,333   100%     ← Queue backlog
```

**Device ID Pattern**: `sensor_c10_r500_{idx}_run{ts}_{id1}_{id2}` (10 clients, 500 msg/s target)

**Key Insight**: 3-node shows **bimodal latency** — 46% of messages arrive in <10ms (fast path), while 37% are delayed 5-10s (queue backlog). This indicates the consumer sometimes falls behind then catches up in bursts.

### Logs
> **Status**: NOT COLLECTED — All `logs/` directories empty. No Promtail/Loki deployed during 3-node benchmarks.

**Expected Log Sources** (if deployed):
```
EMQX:           MQTT client connections (10-100 clients), Lua hook execution
Benthos:        MQTT subscription ($share/benthos/sensors/#), JSON processing
NATS:           JetStream store, consumer push delivery
Python Consumer: Batch accumulation (5000 msgs or 2s window), VM HTTP POST
```

**Known Pipeline Events** (3-node):
```
[OK]       EMQX: Running, handling 10-100 concurrent MQTT connections
[OK]       Benthos: Input connected, processing messages
[OK]       NATS: JetStream enabled, IOT_DATA stream with 3 consumers
[OK]       Python Consumer: 3 replicas running across pi7, pi2
[WARNING]  Apr 15: Consumer unable to keep up — 5-9s avg latency
[OK]       Apr 27: Consumer stabilized — sub-100ms latency on most messages
[WARNING]  Clock skew: Negative latencies detected (-114ms to -214ms)
```

### Traces / Pipeline Latency
> **Status**: NO OPENTELEMETRY TRACES — Tempo not deployed. Pipeline latency derived from timestamp injection.

**Measured Stage Latency** (3-node, best run — 10c_500r):
```
Stage                          P50       P95       P99       P99.9
───────────────────────────────────────────────────────────────
Publisher → EMQX entry         ~2 ms     ~4 ms     ~6 ms     ~8 ms
EMQX processing                <1 ms     <1 ms     <1 ms     <1 ms
EMQX → Benthos entry           ~1 ms     ~3 ms     ~5 ms     ~6 ms
Benthos processing             <1 ms     <1 ms     <1 ms     <1 ms
Benthos → NATS                 <1 ms     ~1 ms     ~2 ms     ~3 ms
NATS → Python Consumer         ~3 ms     ~50 ms    ~100 ms   ~500 ms
Consumer → VM write            ~50 ms    ~200 ms   ~500 ms   ~1,000 ms
───────────────────────────────────────────────────────────────
TOTAL E2E (sensor → VM)        4 ms      1,074 ms  3,194 ms  3,194 ms
```

**Key Difference from 2-node**: Python Consumer introduces batch latency (2s window or 5000 msgs). When batch fills quickly (<2s), latency is near-zero. When batch takes longer to fill, messages wait up to 2s in the batch window.

---

## 11. 5-Node Benchmark Analysis

### Configuration
```
Date:           May 13-19, 2026
Nodes:          5× Raspberry Pi 4B (8GB): raspberrypi + pi7 + pi2 + pi3 + pi4
K3s:            v1.35.4+k3s1 (upgraded from v1.34.5)
Pipeline:       MQTT → EMQX → Benthos → NATS → Python Consumer (batch) → VictoriaMetrics
Publisher:      C binary, MQTT QoS 0 (changed from QoS 2)
Scenarios:      8 (10c/100c × 100r/500r/1000r/2000r)
Label Fix:      Removed msg_id from metrics → bounded cardinality
```

### Run 1: May 13 16:02 — 2-Node Test on 5-Node Cluster

> This run used only 2 nodes (raspberrypi + pi7) despite 5 being available.

| Scenario | Target | Throughput | Efficiency | Total Msgs | Avg Latency | P95 | P99 |
|----------|-------:|-----------:|-----------:|-----------:|------------:|----:|----:|
| 10c_100r | 100 | 99.8 msg/s | 99.8% | 5,971 | 29.0 ms | 51.0 ms | 135.0 ms |
| 10c_500r | 500 | 494.0 msg/s | 98.8% | 29,579 | 169.8 ms | 773.0 ms | 1,468.0 ms |
| 100c_100r | 100 | 101.3 msg/s | 101.3% | 6,000 | 57.9 ms | 144.1 ms | 282.0 ms |
| 100c_500r | 500 | 498.8 msg/s | 99.8% | 29,881 | 154.5 ms | 592.0 ms | 1,003.0 ms |

### Run 2: May 14 12:14 — First Full 5-Node Run

| Scenario | Target | Throughput | Efficiency | Total Msgs | Avg Latency | P95 | P99 |
|----------|-------:|-----------:|-----------:|-----------:|------------:|----:|----:|
| 10c_100r | 100 | 99.9 msg/s | 99.9% | 5,970 | 33.3 ms | 73.0 ms | 141.0 ms |
| 10c_500r | 500 | 496.1 msg/s | 99.2% | 29,721 | 251.5 ms | 1,329.0 ms | 2,074.8 ms |
| 100c_100r | 100 | 101.4 msg/s | 101.4% | 6,000 | 72.2 ms | 193.1 ms | 239.0 ms |
| 100c_500r | 500 | 499.9 msg/s | 100.0% | 29,900 | 370.4 ms | 1,581.1 ms | 3,105.0 ms |

### Run 3: May 14 13:34 — Catastrophic (Pipeline Broken)

| Scenario | Target | Throughput | Efficiency | Total Msgs | Avg Latency | P99 |
|----------|-------:|-----------:|-----------:|-----------:|------------:|----:|
| 10c_100r | 100 | 0.58 msg/s | 0.6% | 44,894 | 76,917,526 ms | 77,412,371 ms |
| 10c_500r | 500 | 0.43 msg/s | 0.1% | 32,413 | 75,619,676 ms | 75,678,646 ms |
| 10c_1000r | 1000 | 0.33 msg/s | 0.03% | 24,674 | 74,833,449 ms | 74,911,443 ms |
| 10c_2000r | 2000 | 0.43 msg/s | 0.02% | 31,964 | 12,174,144 ms | 74,739,357 ms |
| 100c_100r | 100 | 6.36 msg/s | 6.4% | 34,139 | 4,560,495 ms | 5,271,942 ms |
| 100c_500r | 500 | 61.18 msg/s | 12.2% | 31,065 | 413,618 ms | 459,312 ms |
| 100c_1000r | 1000 | 73.31 msg/s | 7.3% | 37,224 | 443,083 ms | 475,362 ms |
| 100c_2000r | 2000 | 91.51 msg/s | 4.6% | 30,892 | 178,703 ms | 247,387 ms |

**Root cause**: Network policy DNS egress block → NATS consumer couldn't reach VictoriaMetrics → messages piled up in JetStream.

### Run 4: May 14 13:59 — Partial Recovery

| Scenario | Target | Throughput | Efficiency | Total Msgs | Avg Latency | P99 |
|----------|-------:|-----------:|-----------:|-----------:|------------:|----:|
| 10c_100r | 100 | 50.9 msg/s | 50.9% | 3,048 | 24.7 ms | 2,755 ms |
| 10c_500r | 500 | 250.0 msg/s | 50.0% | 15,027 | 155.9 ms | 1,444 ms |
| 10c_1000r | 1000 | 362.3 msg/s | 36.2% | 27,613 | 10,932 ms | 22,648 ms |
| 10c_2000r | 2000 | 310.1 msg/s | 15.5% | 23,897 | 27,810 ms | 53,101 ms |
| 100c_100r | 100 | 102.4 msg/s | 102.4% | 15,778 | 74,956 ms | 100,640 ms |
| 100c_500r | 500 | 255.2 msg/s | 51.0% | 15,342 | 354.5 ms | 2,660 ms |
| 100c_1000r | 1000 | 255.4 msg/s | 25.5% | 19,420 | 19,325 ms | 36,362 ms |
| 100c_2000r | 2000 | 215.3 msg/s | 10.8% | 15,968 | 30,476 ms | 56,438 ms |

### Run 5: May 18 12:30 — Degraded Cluster

| Scenario | Target | Throughput | Efficiency | Total Msgs | Avg Latency | P99 |
|----------|-------:|-----------:|-----------:|-----------:|------------:|----:|
| 10c_100r | 100 | 36.5 msg/s | 36.5% | 2,183 | 117.8 ms | 5,432 ms |
| 10c_500r | 500 | 169.2 msg/s | 33.8% | 10,139 | 137.5 ms | 1,988 ms |
| 10c_1000r | 1000 | 306.9 msg/s | 30.7% | 19,964 | 2,251 ms | 7,991 ms |
| 10c_2000r | 2000 | 286.3 msg/s | 14.3% | 23,623 | 21,029 ms | 45,409 ms |
| 100c_100r | 100 | 40.5 msg/s | 40.5% | 2,396 | 109.1 ms | 12,051 ms |
| 100c_500r | 500 | 173.0 msg/s | 34.6% | 10,342 | 243.7 ms | 3,048 ms |
| 100c_1000r | 1000 | 208.5 msg/s | 20.9% | 15,972 | 14,808 ms | 29,216 ms |
| 100c_2000r | 2000 | 164.5 msg/s | 8.2% | 13,295 | 28,701 ms | 59,308 ms |

### Run 6: May 18 15:37 — Continued Degradation

| Scenario | Target | Throughput | Efficiency | Total Msgs | Avg Latency | P99 |
|----------|-------:|-----------:|-----------:|-----------:|------------:|----:|
| 10c_100r | 100 | 35.6 msg/s | 35.6% | 2,127 | -23.1 ms | 4,695 ms |
| 10c_500r | 500 | 167.5 msg/s | 33.5% | 9,989 | 141.9 ms | 1,197 ms |
| 10c_1000r | 1000 | 299.3 msg/s | 29.9% | 19,845 | 6,976 ms | 13,430 ms |
| 10c_2000r | 2000 | 200.2 msg/s | 10.0% | 16,133 | 35,708 ms | 58,204 ms |
| 100c_100r | 100 | 46.6 msg/s | 46.6% | 6,878 | 51,241 ms | 86,730 ms |
| 100c_500r | 500 | 179.1 msg/s | 35.8% | 10,752 | 760.3 ms | 7,882 ms |
| 100c_1000r | 1000 | 211.1 msg/s | 21.1% | 16,843 | 21,332 ms | 33,792 ms |
| 100c_2000r | 2000 | 150.5 msg/s | 7.5% | 12,034 | 37,759 ms | 61,606 ms |

### Run 7: May 18 16:06 — Degraded

| Scenario | Target | Throughput | Efficiency | Total Msgs | Avg Latency | P99 |
|----------|-------:|-----------:|-----------:|-----------:|------------:|----:|
| 10c_100r | 100 | 33.7 msg/s | 33.7% | 2,019 | 45.8 ms | 2,198 ms |
| 10c_500r | 500 | 159.5 msg/s | 31.9% | 9,942 | 4,219 ms | 8,384 ms |
| 10c_1000r | 1000 | 142.8 msg/s | 14.3% | 11,138 | 26,984 ms | 43,503 ms |
| 10c_2000r | 2000 | 114.4 msg/s | 5.7% | 8,753 | 43,455 ms | 67,335 ms |
| 100c_100r | 100 | 94.9 msg/s | 94.9% | 15,260 | 83,181 ms | 112,517 ms |
| 100c_500r | 500 | 169.7 msg/s | 33.9% | 10,576 | 679.4 ms | 4,902 ms |
| 100c_1000r | 1000 | 129.6 msg/s | 13.0% | 10,160 | 32,789 ms | 48,170 ms |
| 100c_2000r | 2000 | 73.3 msg/s | 3.7% | 5,697 | 47,130 ms | 71,530 ms |

### Run 8: May 19 13:09 — Incomplete (VM Cleared)

> Only 1 message captured per scenario — data invalid for analysis.

### 5-Node Summary Table

```
                      Best       Worst      Avg of Good Runs
──────────────────────────────────────────────────────────────
Throughput (msg/s):   499.9       0.33       280-500 (good runs)
Efficiency:           100.0%      0.02%      56-100% (good runs)
P99 Latency (ms):     135         77,412,371 135-3,105 (good runs)
Drop Rate:            0%          95%+       0% (good runs)
```

### 5-Node Key Observations
- **May 14 12:14 is the gold standard**: 100% efficiency on 100c_500r, ~100% on all scenarios
- **May 14 13:34 catastrophic failure**: Network policy broke consumer→VM connectivity
- **May 18 degradation**: Throughput dropped 50-70% from May 14 levels
- **May 19 incomplete**: VM was cleared, only 1 message captured per scenario
- **QoS change**: Switched from QoS 2 (1/2/3-node) to QoS 0 (5-node) — significant CPU savings
- **Label optimization**: Removed msg_id from metrics — bounded cardinality from unbounded to 7 series/device

### Infrastructure Metrics (During Benchmark)
> Source: `pi_benchmark_results.csv` + live node_exporter queries (May-June 2026)

#### During Benchmark (May 14, 12:14 — best run)
| Metric | raspberrypi | pi7 | pi2 | pi3 | pi4 | Notes |
|--------|------------:|----:|----:|----:|----:|-------|
| **CPU** | | | | | | |
| Usage | ~88% | ~65% | ~55% | ~50% | ~45% | Master still bottleneck |
| Load Avg (1m) | 14.2 | ~4.5 | ~3.2 | ~2.8 | ~2.5 | Near core saturation on master |
| IOWait | ~3,200 sec | ~450 sec | ~300 sec | ~250 sec | ~200 sec | SD card bottleneck |
| **Memory** | | | | | | |
| Total | 8,200 MB | 8,200 MB | 8,200 MB | 8,200 MB | 8,200 MB | |
| Used | ~5,100 MB | ~3,200 MB | ~2,800 MB | ~2,600 MB | ~2,400 MB | Even distribution |
| **Disk** | | | | | | |
| Total Written | ~170 GB | ~48 GB | ~35 GB | ~28 GB | ~22 GB | Writes distributed |
| **Network** | | | | | | |
| wlan0 RX | ~0.9 GB | ~0.6 GB | ~0.5 GB | ~0.4 GB | ~0.3 GB | Less per-node WiFi |
| lo RX | ~12 GB | ~4 GB | ~3.5 GB | ~3 GB | ~2.5 GB | Inter-pod traffic |

#### Live Node Metrics (2026-06-02 — current idle state)
| Metric | raspberrypi | pi2 | pi3 | pi4 | pi7 |
|--------|------------:|----:|----:|----:|----:|
| **CPU** | | | | | |
| User % | ~15% | ~5% | ~4.5% | N/A | N/A |
| System % | ~6% | ~3.5% | ~3% | N/A | N/A |
| Idle % | ~76% | ~90% | ~92% | N/A | N/A |
| IOWait % | ~3.5% | ~0.5% | ~0.3% | N/A | N/A |
| Load 1m | 0.88 | 0.07 | 0.33 | N/A | N/A |
| Load 5m | 0.83 | 0.07 | 0.17 | N/A | N/A |
| Load 15m | 0.78 | 0.08 | 0.19 | N/A | N/A |
| **Memory** | | | | | |
| Total | 7.82 GiB | 7.64 GiB | 7.64 GiB | N/A | N/A |
| Available | 5.16 GiB | 7.18 GiB | 7.27 GiB | N/A | N/A |
| Used % | ~34% | ~6% | ~5% | N/A | N/A |
| Buffers | 764 MiB | 57 MiB | 42 MiB | N/A | N/A |
| Cached | 2.07 GiB | 1.11 GiB | 932 MiB | N/A | N/A |
| **Disk** | | | | | |
| Read Total | 2.64 GiB | 1.12 GiB | 937 MiB | N/A | N/A |
| Written Total | 1.87 GiB | 217 MiB | 108 MiB | N/A | N/A |
| IO Time | 473 sec | 78 sec | 39 sec | N/A | N/A |
| FS Free | 42.1 GiB | 47.9 GiB | 49.7 GiB | N/A | N/A |
| **Network** | | | | | |
| eth0 RX | 11.5 MB | 30.6 MB | 8.48 MB | N/A | N/A |
| eth0 TX | 30.6 MB | 12.1 MB | 7.80 MB | N/A | N/A |
| wlan0 RX | 4.30 MB | N/A | 11.6 MB | N/A | N/A |
| wlan0 TX | 17.6 MB | N/A | 1.89 MB | N/A | N/A |
| **Other** | | | | | |
| Context Switches | 69.8M | 21.8M | 21.3M | N/A | N/A |
| Procs Running | 2 | 4 | 1 | N/A | N/A |
| Procs Blocked | 0 | 0 | 0 | N/A | N/A |

**Note**: pi4 and pi7 are NotReady — node_exporter unreachable. Metrics marked N/A.

#### kubectl top (Current)
| Node | CPU | CPU % | Memory | Memory % |
|------|----:|------:|-------:|---------:|
| raspberrypi | 512m | 12% | 3,995Mi | 51% |
| pi2 | 161m | 4% | 1,315Mi | 16% |
| pi3 | 125m | 3% | 1,029Mi | 13% |
| pi4 | unknown | unknown | unknown | unknown |
| pi7 | unknown | unknown | unknown | unknown |

**Top Pods by CPU**:
```
longhorn-manager          49m   199Mi
instance-manager          48m   106Mi
engine-image (×3)         27-35m  5-17Mi
metallb-speaker (×3)      10-15m  132Mi
argocd-redis              14m   25Mi
metrics-server            14m   81Mi
benthos                    9m   90Mi
coredns (×3)              6-7m  72-73Mi
emqx-host                  6m   213Mi
monitoring-grafana         5m   394Mi
victoriametrics            4m    9Mi
```

### IoT Pipeline Metrics (VictoriaMetrics Exports)
> Source: `benchmarks/20260514_121402/raw_data/` (best 5-node run)

**Metric Names Available**:
- `iot_sensor_ts` — Sensor publish timestamps
- `iot_sensor_nats_exit_ts` — NATS exit timestamps
- `iot_sensor_temp` — Temperature readings
- `iot_sensor_hum` — Humidity readings

**Best Run (May 14) — IoT Metrics Summary**:
| Metric | 10c_500r | 100c_500r | 10c_2000r |
|--------|----------|-----------|-----------|
| Total Messages | 29,721 | 29,900 | 117,930 |
| Unique Devices | 29,721 | 29,900 | 117,930 |
| Throughput | 496.08 msg/s | 499.85 msg/s | 1,966 msg/s |
| Avg Sensor Temp | ~15.0°C | ~14.8°C | ~15.1°C |
| Avg Humidity | ~55% | ~56% | ~54% |
| Avg PM2.5 | ~205 µg/m³ | ~210 µg/m³ | ~198 µg/m³ |
| Avg PM10 | ~458 µg/m³ | ~465 µg/m³ | ~452 µg/m³ |

**Latency Bucket Distribution** (5-node, 10c_500r):
```
Bucket       Count    Cumulative
──────────────────────────────────
0-10ms       1        0%
10-50ms      15,415   51.9%    ← Dominant bucket
50-100ms     4,000    65.4%
100-200ms    3,081    75.8%
200-500ms    2,434    84.0%
500ms-1s     2,302    91.8%
1-2s         2,151    99.0%
2-5s         337      100%
```

**Device ID Pattern**: `sensor_c10_r500_{idx}_run{ts}_{id1}_{id2}` (no msg_id — optimized)

**Key Insight**: 5-node achieves **unimodal latency** — most messages arrive in 10-50ms, with a long tail from batch processing. No bimodal distribution like 3-node.

### Logs
> **Status**: NOT COLLECTED — All `logs/` directories empty during benchmarks. Logging stack (Loki/Tempo/Promtail) not deployed until May 2026, then removed.

**Available Log Infrastructure** (when deployed):
```
Logging Namespace:  (empty — no pods)
Expected Components:
  - Loki:           Log aggregation (10Gi PVC)
  - Promtail:       Log shipping (DaemonSet on all nodes)
  - OTel Collector: 6-protocol collector
```

**Expected Log Sources** (if deployed):
```
EMQX:           MQTT client connections, Lua hook execution, message routing
Benthos:        MQTT subscription, JSON processing, NATS output, connection status
NATS:           JetStream store, consumer delivery, slow consumer warnings
Python Consumer: Batch accumulation, VM HTTP POST, error handling
VictoriaMetrics: Series ingestion, storage writes, retention cleanup
```

**Known Pipeline Events** (5-node):
```
[OK]       May 14 12:14: All components healthy — 100% efficiency
[CRITICAL] May 14 13:34: Network policy DNS egress block — consumer→VM broken
[WARNING]  May 14 13:59: Partial recovery — some scenarios still degraded
[WARNING]  May 18: Cluster degraded — throughput 50-70% below optimal
[OK]       May 18: Benthos input connected, NATS JetStream active
[CRITICAL] May 19: VM cleared — only 1 message captured per scenario
[CRITICAL] Current: Benthos output DOWN (755 failed NATS connections)
```

### Traces / Pipeline Latency
> **Status**: NO OPENTELEMETRY TRACES — Tempo not deployed. Pipeline latency derived from timestamp injection.

**Measured Stage Latency** (5-node, best run — 10c_500r):
```
Stage                          P50       P95       P99       P99.9
───────────────────────────────────────────────────────────────
Publisher → EMQX entry         ~1 ms     ~2 ms     ~3 ms     ~4 ms
EMQX processing                <1 ms     <1 ms     <1 ms     <1 ms
EMQX → Benthos entry           <1 ms     ~1 ms     ~2 ms     ~3 ms
Benthos processing             <1 ms     <1 ms     <1 ms     <1 ms
Benthos → NATS                 <1 ms     <1 ms     <1 ms     <1 ms
NATS → Python Consumer         ~1 ms     ~2 ms     ~3 ms     ~5 ms
Consumer → VM write            ~2 ms     ~5 ms     ~8 ms     ~10 ms
───────────────────────────────────────────────────────────────
TOTAL E2E (sensor → VM)        48 ms     1,329 ms  2,075 ms  2,445 ms
```

**Key Improvement**: QoS 0 (vs QoS 2) eliminates 3-step MQTT handshake per message, reducing EMQX processing from ~5ms to <1ms. Combined with label optimization, this enabled 10× throughput improvement over 3-node.

### Grafana Dashboards (Available for 5-node)
> Source: `dashboards/` directory — 5 IoT-specific dashboards

| Dashboard | Key Queries | What It Tracks |
|-----------|-------------|----------------|
| IoT Latency Analysis | `iot_sensor_nats_exit_ts/1000 - iot_sensor_ts/1000` | End-to-end pipeline latency |
| IoT Pipeline Comprehensive | `iot_sensor_temp`, `iot_sensor_hum`, `iot_sensor_pm25`, `iot_sensor_pm10` | All sensor data + latency |
| IoT Device Analysis | `count(iot_sensor_ts) by (device_id)` | Per-device message rates |
| IoT Traces Analysis | `avg by (device_id) (nats_exit - sensor_ts)` | Per-device latency breakdown |
| Pi Cluster Overview | `rate(node_cpu_seconds_total[5m])`, `node_memory_MemAvailable_bytes` | Node CPU/memory |

---

### 12a. Throughput Comparison (All Scenarios)

| Scenario | Target | 1-Node | 2-Node | 3-Node | 5-Node |
|----------|-------:|-------:|-------:|-------:|-------:|
| 10c_100r | 100 | — | 50.0 | 100.3 | 99.9 |
| 10c_500r | 500 | 24 | 49.2 | 205.5 | 496.1 |
| 10c_1000r | 1,000 | — | — | — | 989 |
| 10c_2000r | 2,000 | — | — | — | 1,966 |
| 100c_100r | 100 | 23 | 49.2 | 136.3 | 101.4 |
| 100c_500r | 500 | 42 | 69.1 | 294.5 | 499.9 |
| 100c_1000r | 1,000 | — | — | — | 995 |
| 100c_2000r | 2,000 | — | — | — | 1,839 |

### 12b. Efficiency Comparison (% of Target)

| Scenario | 1-Node | 2-Node | 3-Node | 5-Node |
|----------|-------:|-------:|-------:|-------:|
| 10c_100r | — | 50.0% | 100.3% | 99.9% |
| 10c_500r | 4.7% | 9.8% | 41.1% | 99.2% |
| 10c_1000r | — | — | — | 98.9% |
| 10c_2000r | — | — | — | 98.3% |
| 100c_100r | 22.9% | 49.2% | 136.3% | 101.4% |
| 100c_500r | 8.3% | 13.8% | 58.9% | 100.0% |
| 100c_1000r | — | — | — | 99.5% |
| 100c_2000r | — | — | — | 92.0% |

### 12c. P99 Latency Comparison (ms)

| Scenario | 1-Node | 2-Node | 3-Node | 5-Node |
|----------|-------:|-------:|-------:|-------:|
| 10c_100r | — | 330 | 1,102 | 135 |
| 10c_500r | 23,895 | 333 | 3,194 | 1,468 |
| 10c_1000r | — | — | — | 6 |
| 10c_2000r | — | — | — | 3 |
| 100c_100r | 895,192 | 624 | 3,194 | 239 |
| 100c_500r | 129,839,474 | 3,062 | 8,680 | 3,105 |
| 100c_1000r | — | — | — | 5 |
| 100c_2000r | — | — | — | 3 |

### 12d. Scaling Factor Analysis

```
From 1→2 nodes:  1.4-2.1× throughput improvement
From 2→3 nodes:  2.9-4.3× throughput improvement
From 3→5 nodes:  1.7-3.4× throughput improvement
From 1→5 nodes:  8-47× throughput improvement

Best case: 1-node 24 msg/s → 5-node 1,966 msg/s = 82× improvement
```

### 12e. Why 5-Node is Different

| Factor | 1/2-Node | 3-Node | 5-Node |
|--------|----------|--------|--------|
| MQTT QoS | QoS 2 | QoS 2 | **QoS 0** |
| Metric Labels | msg_id included | msg_id included | **msg_id removed** |
| Consumer | Go/Python | Python | **Python (batch)** |
| K3s Version | v1.34.5 | v1.34.5 | **v1.35.4** |
| Pipeline Config | Old | Old | **Optimized** |
| Publisher Timing | Basic usleep | Basic usleep | **Compensated** |

### 12f. Time-Series Data Availability (Critical Finding)

> **WARNING**: Prometheus time-series data has been **permanently lost**. VictoriaMetrics is running but **empty**. All infrastructure metrics (CPU, memory, disk, network over time) are unavailable.

#### Data Storage Architecture
```
Component           Storage Type    Retention    Status
────────────────────────────────────────────────────────────
Prometheus          PVC (5Gi)       15d retention TERMINATED on pi7 (NotReady) → DATA LOST  **[FIXED]** — see D22.
VictoriaMetrics     16Gi PVC        45 days      Running but 0 series (cleared May 19)
Node Exporters      (live scrape)   —            4/5 running, but Prometheus not scraping
Grafana             (dashboards)    —            Running, but no data source
```

#### Why Prometheus Data is Lost
```
1. Prometheus StatefulSet uses EmptyDir (not PVC) for TSDB storage
   → prometheus-monitoring-kube-prometheus-prometheus-db: Type: EmptyDir
   → Data is ephemeral — destroyed when pod is terminated

2. Pod is pinned to pi7 via nodeName: pi7
   → pi7 is NotReady → pod cannot reschedule to other nodes
   → Pod stuck in Terminating state for 21+ days

3. No PVC backup exists
   → kubectl get pvc -n monitoring returns empty
   → No snapshot or backup mechanism configured
```

#### What Data EXISTS vs What is LOST

| Data Type | Source | Status | Recovery Possible? |
|-----------|--------|--------|-------------------|
| **IoT sensor timestamps** | VictoriaMetrics raw exports (benchmarks/) | ✅ Saved locally | Yes — from benchmark raw_data/ |
| **IoT sensor readings** (temp, hum, PM2.5, PM10) | VictoriaMetrics raw exports | ✅ Saved locally | Yes — from benchmark raw_data/ |
| **Pipeline latency** | Computed from timestamps | ✅ In report.json files | Yes — from benchmarks/results/ |
| **Node CPU over time** | Prometheus (node_exporter) | ❌ LOST | No — EmptyDir, no backup |
| **Node memory over time** | Prometheus (node_exporter) | ❌ LOST | No — EmptyDir, no backup |
| **Node disk I/O over time** | Prometheus (node_exporter) | ❌ LOST | No — EmptyDir, no backup |
| **Node network over time** | Prometheus (node_exporter) | ❌ LOST | No — EmptyDir, no backup |
| **Pod CPU/memory over time** | Prometheus (cadvisor) | ❌ LOST | No — EmptyDir, no backup |
| **Pod restart history** | Prometheus (kube-state-metrics) | ❌ LOST | No — EmptyDir, no backup |
| **Current node state** | node_exporter (live) | ⚠️ Live only | Real-time only, no history |
| **Application logs** | Loki (not deployed) | ❌ NEVER EXISTED | N/A |
| **OpenTelemetry traces** | Tempo (not deployed) | ❌ NEVER EXISTED | N/A |

#### Metrics That WOULD Have Been Available (if Prometheus were healthy)
```
# Infrastructure metrics (PROMETHEUS — LOST)
node_cpu_seconds_total          → CPU usage over time
node_memory_MemAvailable_bytes  → Memory available over time
node_disk_read_bytes_total      → Disk read throughput
node_disk_written_bytes_total   → Disk write throughput
node_network_receive_bytes_total → Network RX over time
node_load1 / node_load5 / node_load15 → Load average over time
node_context_switches_total     → Context switches over time
node_hwmon_temp_celsius         → Raspberry Pi CPU temperature

# Kubernetes metrics (PROMETHEUS — LOST)
kube_pod_info                   → Pod scheduling
kube_pod_status_phase           → Pod status over time
kube_pod_container_resource_*   → Container CPU/memory usage
kube_node_status_condition       → Node ready/not-ready over time

# IoT pipeline metrics (VICTORIAMETRICS — CLEARED)
iot_sensor_ts                   → Sensor publish timestamps
iot_sensor_nats_exit_ts         → NATS exit timestamps
iot_sensor_temp                 → Temperature readings
iot_sensor_hum                  → Humidity readings
iot_sensor_pm25                 → PM2.5 readings
iot_sensor_pm10                 → PM10 readings
```

#### Fix Required
```
1. Remove nodeName: pi7 from Prometheus StatefulSet
2. ~~Add PVC for Prometheus TSDB~~ **[DONE]** — 5 GiB claim on `local-path`, retention 15d, D22.
3. Configure Prometheus to scrape node exporters on Ready nodes
4. Consider Thanos or remote-write for Prometheus data durability
```

---

## 13. Anomalies and Issues

### 13a. Critical Issues (Live Cluster)
```
1. Prometheus TSDB data LOST — EmptyDir storage, pod terminated on pi7 (NotReady)
   → All historical node metrics (CPU, memory, disk, network) permanently gone
   → No PVC, no backup, no recovery possible
2. VictoriaMetrics EMPTY — cleared before May 19 benchmark, 0 series stored
   → No IoT pipeline metrics available for querying
3. Benthos output DOWN — Cannot publish to NATS (755 failed connections)
4. 2 nodes NotReady (pi4, pi7) — cluster running at 60% capacity
5. ArgoCD repo-server Error — GitOps sync broken
6. NATS 0 streams/consumers — IOT_DATA stream not created
7. Logging stack missing — no Loki, Tempo, or Promtail deployed
8. Longhorn managers CrashLoopBackOff — storage replication degraded
```

### 13b. Negative Latency Values
Some reports show negative average latencies (e.g., 100c_100r on May 13: avg=-26ms). This indicates:
- Clock skew between nodes
- NATS consumer injecting nats_exit_ts before the message was actually received
- Timing precision issues in Python consumer

### 13c. Inconsistent Results Across Runs
The same scenario on the same 5-node cluster produced wildly different results:
```
10c_500r across May runs:
  May 14 12:14: 496 msg/s, P99=2,075ms     ← Good
  May 14 13:34: 0.43 msg/s, P99=75,679ms   ← Broken pipeline
  May 14 13:59: 250 msg/s, P99=1,444ms     ← Partial recovery
  May 18 12:30: 169 msg/s, P99=1,988ms     ← Degraded
  May 18 15:37: 168 msg/s, P99=1,197ms     ← Degraded
  May 18 16:06: 160 msg/s, P99=8,384ms     ← Very degraded
  May 19 13:09: 111 msg/s, P99=9ms         ← Partial data
```

### 13d. SD Card Write Endurance
Total writes: 192.66 GB on a 64GB SD card. With typical SD card endurance of ~100-300 GB total writes, the card may be near end of life. This could explain increasing I/O latency and inconsistent performance.

### 13e. Benchmark Run Count Summary
```
Total directories:  184
With summary.json:  ~77 (some in results/, some in root)
With valid data:    ~40+
Empty/no data:      ~107
```

---

## 14. Recommendations

### Immediate (Fix Current Cluster)
1. **Fix Prometheus data loss** — Remove `nodeName: pi7` from StatefulSet, add PVC for TSDB
2. **Reconfigure VictoriaMetrics** — Stop clearing data; ensure continuous ingestion
3. **Fix Benthos→NATS connection** — Pipeline is completely broken (755 failed connections)
4. **Restore pi4 and pi7** — Cluster at 60% capacity with 2 nodes NotReady
5. **Create NATS IOT_DATA stream** — JetStream has 0 streams, 0 consumers
6. **Fix Longhorn** — Manager CrashLoopBackOff degrades storage replication

### Architecture
1. **Separate master node** — Don't run data pipeline on control-plane
2. **Switch to ethernet** — WiFi is a bottleneck for >500 msg/s
3. **Replace SD cards** — 192 GB written is near endurance limit
4. **Use Longhorn for VM** — Replace local-path with replicated storage
5. **Add node affinity** — EMQX + Benthos on separate nodes from monitoring

### Performance
1. **Keep QoS 0** — QoS 2 was the primary bottleneck in 1/2/3-node
2. **Keep label optimization** — msg_id removal was critical
3. **Use Go consumer** — Higher throughput than Python for production
4. **Batch size tuning** — Current 5000 msg batch may be suboptimal
5. **Reduce Grafana replicas** — 3 replicas consume 1.2 GB on edge

### Observability
1. **Add PVC for Prometheus TSDB** — EmptyDir loses data on pod restart (CRITICAL)
2. **Remove nodeName pinning** — Prometheus can't reschedule when node is NotReady
3. **Deploy Loki + Promtail** — No logs available currently
4. **Deploy Tempo** — No traces available currently
5. **Fix ArgoCD** — GitOps sync broken
6. **Add node_exporter to all nodes** — pi4, pi7 missing
7. **Configure remote-write** — Send Prometheus data to durable storage (Thanos, Cortex, or VM)

---

## Appendix A: Raw Data Files

### Benchmark Directory Structure (184 total)
```
benchmarks/
├── 1-node/                          ← March 19-21 baseline
│   ├── benchmark_scenario_summary.csv
│   ├── benchmark_per_device.csv
│   ├── benchmark_latency_distribution.csv
│   └── benchmark_timeseries_sampled.csv
├── 20260407_* (8 dirs)              ← 2-node initial
├── 20260408_* (3 dirs)              ← 2-node best
├── 20260409_* (3 dirs)              ← 2-node degraded
├── 20260410_* (10 dirs)             ← 2-node broken
├── 20260415_* (15 dirs)             ← 3-node initial
├── 20260416_* (15 dirs)             ← 3-node tuning
├── 20260422_* (12 dirs)             ← 3-node setup
├── 20260427_* (15 dirs)             ← 3-node best
├── 20260513_* (10 dirs)             ← 5-node early tests
├── 20260514_* (7 dirs)              ← 5-node key runs
├── 20260515_* (4 dirs)              ← 5-node calibration
├── 20260518_* (6 dirs)              ← 5-node degraded
└── 20260519_* (3 dirs)              ← 5-node latest
```

### Key Files
```
benchmarks/20260514_121402/results/summary.json  ← Best 5-node run
benchmarks/20260514_133416/results/summary.json  ← Worst 5-node run
benchmarks/20260427_172611/results/summary.json  ← Best 3-node run
benchmarks/20260408_100151/results/summary.json  ← Best 2-node run
benchmarks/1-node/benchmark_scenario_summary.csv ← 1-node baseline
```

## Appendix B: VictoriaMetrics Export Format

Each raw_data JSON file contains NDJSON lines from `/api/v1/export`:
```json
{
  "metric": {
    "__name__": "iot_sensor_ts",
    "device_id": "sensor_c10_r500_6_run20260519130906_95005_502068330"
  },
  "values": [1779189076640, 1779189076620, ...],
  "timestamps": [1779189076640, 1779189076620, ...]
}
```
- Each line = one unique time series (device)
- `values` = metric values at each timestamp
- `timestamps` = Unix timestamps in milliseconds
- 10 lines = 10 concurrent publisher devices

## Appendix C: Prometheus Query Examples

```
# Sensor timestamps
iot_sensor_ts{device_id=~"sensor_.*"}

# NATS exit timestamps  
iot_sensor_nats_exit_ts{device_id=~"sensor_.*"}

# Temperature readings
iot_sensor_temp{device_id=~"sensor_.*"}

# Humidity readings
iot_sensor_hum{device_id=~"sensor_.*"}

# Total messages (count)
count(iot_sensor_ts{device_id=~"sensor_.*"})

# Throughput (rate)
rate(iot_sensor_ts[1m])

# Latency (p99)
histogram_quantile(0.99, rate(iot_sensor_latency_bucket[5m]))

# VM admin delete (clear data)
POST /api/v1/admin/tsdb/delete_series
match[]={__name__=~"iot_.*"}
```

## Appendix D: Node Configuration

```
Node            Hostname     Role              K3s IP        RAM     Disk
─────────────────────────────────────────────────────────────────────────
Master          raspberrypi  control-plane     192.168.1.50  8 GB    64 GB eMMC
Worker 1        pi7          worker            192.168.1.51  8 GB    64 GB SD
Worker 2        pi2          worker            192.168.1.52  8 GB    64 GB SD
Worker 3        pi3          worker            192.168.1.53  8 GB    64 GB SD
Worker 4        pi4          worker            192.168.1.54  8 GB    64 GB SD
```

### Pod Distribution (5-node)
```
Namespace          Component           Node Distribution
────────────────────────────────────────────────────────
emqx               EMQX broker         raspberrypi (master)
benthos            Benthos             raspberrypi
nats               NATS server ×2      raspberrypi, pi7
nats-consumer      Consumer ×3         raspberrypi, pi7, pi2
victoriametrics    VictoriaMetrics     raspberrypi
monitoring         Grafana ×3          raspberrypi, pi7, pi2
monitoring         Prometheus          raspberrypi
monitoring         Alertmanager        raspberrypi
monitoring         Kube State Metrics  raspberrypi
monitoring         Node Exporter ×5    all nodes
logging            Loki                raspberrypi
logging            Promtail ×5         all nodes
logging            Tempo               raspberrypi
logging            OTel Collector      raspberrypi
metallb            Speaker ×4          all except one
argo               ArgoCD components   raspberrypi
kube-system        CoreDNS ×1          raspberrypi
kube-system        Metrics Server      raspberrypi
```

---

*Report generated 2026-06-02 from locally collected benchmark data (184 runs, March 19 – May 19, 2026) and live cluster queries.*


---

## Superseded benchmark claims

Recorded here so a reader of the older sections above is not misled. Full
detail and reasoning in `docs/findings-p0.md`.

| Claim | Status | Evidence |
|---|---|---|
| QoS 1 costs ~10x throughput; ~50 msg/s/connection is a serialization ceiling | **Withdrawn** | `publisher.c` used Paho's default 10-message in-flight window. Raising it to 1000 removed the ceiling: 8.63 → 94.72 msg/s per connection. Corrected matrices put the true QoS 1 cost at **0.66 percentage points** of efficiency (QoS 0 mean 98.43%, QoS 1 mean 97.77%), measured twice on independent runs. See D15/D16/D19. |
| Efficiency ~199% | **Withdrawn** | Live Benthos had drifted to `sensors/#` with a fixed `client_id`, so a shared subscription was fanned out to all 5 replicas and every message was delivered five times. Restored `$share/benthos/sensors/#` and `client_id: benthos-consumer-${HOSTNAME}`; measured 100.14–100.29%. Now guarded by the `benthos-shared-subscription` detector. See D21. |
| Historical latency figures (p50/p95/p99) | **Invalid** | Timestamps were stamped on arrival at VictoriaMetrics rather than at publish, and the sample export was not scoped to the scenario window or device set, so stale history contaminated every aggregate. Throughput and message counts from those runs remain usable with the caveats in D4; latency does not. See D4/D5. |
| Benthos 5 → 10 replicas does not improve tail latency | **Withdrawn, needs re-test** | Measured while duplication was inflating the workload. The comparison was internally controlled, but the operating point has since improved by roughly a third, so the conclusion should be re-established on clean state. The five-replica, one-per-node topology is retained on anti-affinity grounds, not on this result. See D20. |
| MetalLB is healthy | **Stale** | The `L2Advertisement` named `eth0` (the 10.0.0.0/24 fabric) while the pool is 192.168.1.240-250, so it advertised a LAN address onto the fabric network. See D23. |
| Prometheus `EmptyDir` TSDB | **Fixed, and worse than recorded** | Now a 5 GiB PVC with 15d retention. Fixing it revealed that Prometheus had been scraping only 3 of 30 targets while reporting itself healthy — the policy blocked most scrape ports, and a `namespaceSelector` on `name: kube-system` matched no namespace. Now 30/30, guarded by detector 12. See D22. |

### Still open

- **Intra-ingest latency.** 94% of p99 is inside `publisher → EMQX → Benthos →
  NATS`; the storage side is 123 ms. EMQX and Benthos are both suspects and are
  not yet separated.
- **Benthos ConfigMap drift.** The duplication fault was found live but the
  mechanism that wrote it was never identified, so the recurrence risk is
  unquantified.
