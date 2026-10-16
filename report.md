# K3s IoT Stack — Benchmark Report

## Current Results (D37, authoritative)

> Everything below this section predates the EMQX root-cause fixes (D34/D35) and
> the EMQX resource correction (D37). Those sections are retained as a record of
> the scaling study, **not** as the capacity of the system.

Final ladder, 5× Pi 4B (8GB), 100 clients, EMQX at 3 CPU / 1Gi:

| Target rate | Delivery | Throughput | p50 | p99 |
|---|---|---|---|---|
| 4000/s | **100.0%** | 3970 msg/s | 476 ms | 2335 ms |
| 6000/s | **100.0%** | 5770 msg/s | 1087 ms | 3514 ms |
| 7000/s | **100.0%** | 6709 msg/s | 2044 ms | 5408 ms |
| 8000/s | **100.0%** | 6686 msg/s | 8249 ms | 13279 ms |
| 10000/s | 67.6% | 5348 msg/s | — | — |
| 12000/s | 74.6% | 6814 msg/s | — | — |

**Throughput knee: 8000–10000 msg/s.** 8000/s confirmed at 100.0% delivery
across four independent runs.

Two defensible operating points, which are not interchangeable:

* **6000 msg/s @ p99 3.5s** — 100% delivery, latency-reasonable. Recommended.
* **8000 msg/s @ p99 13.3s** — 100% delivery, ~4× worse p99. Maximum throughput.

At the 8000/s ceiling no component is CPU-saturated: EMQX uses 98m of its 3000m
allowance, and the four busiest worker nodes sit at 15–34%. The binding
constraint at 8000 is **queueing, not compute** (D37).

What moved these numbers, in order of effect:

1. `force_shutdown.max_mailbox_size: 10000` — the real EMQX setting (D35). The
   ceiling moved 3000–4000 → 6000 msg/s.
2. EMQX `1500m`/`512Mi` → `3000m`/`1Gi` (D37). The ceiling moved 6000 → 8000.
3. Explicit `delivery_ratio_pct = stored / generator_published` as the loss
   metric. Earlier efficiency figures conflated generator inaccuracy with loss.

Two changes that were tried and reverted, both measured:

* **EMQX clustered (3 replicas)** — forms correctly but collapses delivery to
  2.2% at 3000 msg/s with 288 Benthos connection losses (D36).
* **Benthos at 10 replicas** — no measurable ingest-p99 improvement, and the
  surplus replicas will not schedule on a 5-node cluster (D37).

## Cluster Configurations

| Nodes | Configuration | Pipeline |
|-------|--------------|----------|
| 1-node | 1× Pi 4B (8GB) | MQTT → EMQX → Benthos → NATS → Go Consumer → VM |
| 2-node | 2× Pi 4B (8GB) | MQTT → EMQX → Benthos → NATS → Go Consumer → VM |
| 3-node | 3× Pi 4B (8GB) | MQTT → EMQX → Benthos → NATS → Python Consumer → VM |
| 5-node | 5× Pi 4B (8GB) | MQTT → EMQX → Benthos → NATS → Python Consumer (batch) → VM |

> **Corrections applied (see `docs/findings-p0.md` for evidence).**
>
> - Results in this report were collected while two measurement faults were
>   present and must be read with that in mind. Any efficiency figure above
>   ~100% is invalid: Benthos was duplicating every message (D21).
> - The QoS 1 cost reported here is wrong by construction. It came from Paho's
>   default 10-message in-flight window, not from a QoS serialization ceiling.
>   Corrected: **−0.66 percentage points** of efficiency, not a ~10x throughput
>   loss (D15/D16/D19).
> - Latency percentiles in this report are invalid. The timestamp was taken on
>   arrival at VictoriaMetrics rather than at publish, and the sample export was
>   not scoped to the scenario, so stale history contaminated the aggregates.
>   Throughput and message counts remain usable with caveats (D4/D5).
> - Broker ingress is now `192.168.1.241:1883` (MetalLB), not a NodePort.
> - `Go Consumer` rows predate the switch to the Python consumer and are kept
>   only for the 1- and 2-node scaling comparison.

## Test Parameters

- **Test Duration**: 60s per scenario
- **Cooldown**: 30s between tests
- **Message Format**: JSON with device_id, ts, pm1, pm25, pm10, temp, hum
- **VM**: VictoriaMetrics single-node, `-search.maxUniqueTimeseries=3000000`

---

## 1× Pi 4B (4GB) — Single-node k3s, Go consumer, old pipeline

| Scenario | Target (msg/s) | Throughput (msg/s) | Efficiency | Msgs | Devices | Avg Latency | P50 | P75 | P90 | P95 | P99 | P99.9 | StdDev | Drop Rate |
|----------|---------------:|-------------------:|-----------:|-----:|--------:|------------:|----:|----:|----:|----:|----:|------:|-------:|----------:|
| 10c_100r | 100 | — | — | — | — | — | — | — | — | — | — | — | — | — |
| 10c_500r | 500 | 24 | 4.7% | 1,973 | 10 | 12,758.8 ms | 13,173 | 18,747 | 20,983 | 21,589 | 23,895 | 24,198 | 6,729.4 | 95.3% |
| 10c_1000r | 1,000 | — | — | — | — | — | — | — | — | — | — | — | — | — |
| 10c_2000r | 2,000 | — | — | — | — | — | — | — | — | — | — | — | — | — |
| 100c_100r | 100 | 23 | 22.9% | 23,191 | 200 | 360,627.1 ms | 327,878 | 498,150 | 673,061 | 761,823 | 895,192 | 948,004 | 212,440.5 | 77.1% |
| 100c_500r | 500 | 42 | 8.3% | 5,428,941 | 100 | 66,989,874.2 ms | 67,693,745 | 99,715,719 | 118,500,581 | 124,847,602 | 129,839,474 | 130,958,279 | 37,615,186.2 | 91.7% |
| 100c_1000r | 1,000 | — | — | — | — | — | — | — | — | — | — | — | — | — |
| 100c_2000r | 2,000 | — | — | — | — | — | — | — | — | — | — | — | — | — |

## 2× Pi 4B (8GB) — raspberrypi + pi7, Go consumer, old pipeline

| Scenario | Target (msg/s) | Throughput (msg/s) | Efficiency | Msgs | Devices | Avg Latency | P50 | P75 | P90 | P95 | P99 | P99.9 | StdDev | Drop Rate |
|----------|---------------:|-------------------:|-----------:|-----:|--------:|------------:|----:|----:|----:|----:|----:|------:|-------:|----------:|
| 10c_100r | 100 | 48 | 48.5% | — | 10 | 157.1 ms | — | — | — | 226.0 | 329.8 | — | — | — |
| 10c_500r | 500 | 39 | 7.9% | — | 10 | 156.8 ms | — | — | — | 232.0 | 332.7 | — | — | — |
| 10c_1000r | 1,000 | — | — | — | — | — | — | — | — | — | — | — | — | — |
| 10c_2000r | 2,000 | — | — | — | — | — | — | — | — | — | — | — | — | — |
| 100c_100r | 100 | 44 | 43.7% | — | 100 | 179.0 ms | — | — | — | 315.0 | 624.0 | — | — | — |
| 100c_500r | 500 | 67 | 13.4% | — | 100 | 598.1 ms | — | — | — | 2,864.3 | 3,062.2 | — | — | — |
| 100c_1000r | 1,000 | — | — | — | — | — | — | — | — | — | — | — | — | — |
| 100c_2000r | 2,000 | — | — | — | — | — | — | — | — | — | — | — | — | — |

## 3× Pi 4B (8GB) — raspberrypi + pi7 + pi2, Python consumer, msg_id labels

| Scenario | Target (msg/s) | Throughput (msg/s) | Efficiency | Msgs | Devices | Avg Latency | P50 | P75 | P90 | P95 | P99 | P99.9 | StdDev | Drop Rate |
|----------|---------------:|-------------------:|-----------:|-----:|--------:|------------:|----:|----:|----:|----:|----:|------:|-------:|----------:|
| 10c_100r | 100 | 100 | 100.3% | 6,022 | 10 | 134.0 ms | 6 | 29 | 43 | 2,301 | 2,365 | 2,365 | — | — |
| 10c_500r | 500 | 458 | 91.6% | 29,766 | 10 | 172.0 ms | -3 | 5 | 8 | 9 | 9,804 | 9,805 | — | — |
| 10c_1000r | 1,000 | — | — | — | — | — | — | — | — | — | — | — | — | — |
| 10c_2000r | 2,000 | — | — | — | — | — | — | — | — | — | — | — | — | — |
| 100c_100r | 100 | 96 | 96.2% | 6,300 | 100 | -26.0 ms | 317 | 416 | 471 | 486 | 4,442 | 4,986 | — | — |
| 100c_500r | 500 | 447 | 89.4% | 31,324 | 100 | 562.0 ms | 9 | 65 | 95 | 7,583 | 7,698 | 7,730 | — | — |
| 100c_1000r | 1,000 | — | — | — | — | — | — | — | — | — | — | — | — | — |
| 100c_2000r | 2,000 | — | — | — | — | — | — | — | — | — | — | — | — | — |

## 5× Pi 4B (8GB) — raspberrypi + pi7 + pi2 + pi3 + pi4, Python consumer, no msg_id labels

| Scenario | Target (msg/s) | Throughput (msg/s) | Efficiency | Msgs | Devices | Avg Latency | P50 | P75 | P90 | P95 | P99 | P99.9 | StdDev | Drop Rate |
|----------|---------------:|-------------------:|-----------:|-----:|--------:|------------:|----:|----:|----:|----:|----:|------:|-------:|----------:|
| 10c_100r | 100 | 100 | 99.7% | 5,979 | 10 | 11.0 ms | 5.0 | 9.0 | 56.0 | 57.0 | 59.0 | 59.0 | — | 0.0% |
| 10c_500r | 500 | 496 | 99.2% | 29,770 | 10 | 2.9 ms | 2.0 | 4.0 | 6.0 | 9.0 | 9.0 | 10.0 | — | 0.0% |
| 10c_1000r | 1,000 | 989 | 98.9% | 59,322 | 10 | 1.9 ms | 1.0 | 2.0 | 4.0 | 5.0 | 6.0 | 6.0 | — | 0.0% |
| 10c_2000r | 2,000 | 1,966 | 98.3% | 117,930 | 10 | 1.3 ms | 1.0 | 1.0 | 2.0 | 2.0 | 3.0 | 3.0 | — | 0.0% |
| 100c_100r | 100 | 100 | 100.0% | 6,000 | 100 | 14.8 ms | 2.0 | 3.0 | 6.0 | 10.0 | 843.0 | 844.0 | — | 0.0% |
| 100c_500r | 500 | 498 | 99.7% | 29,900 | 100 | 3.2 ms | 1.0 | 2.0 | 4.0 | 7.0 | 72.0 | 74.0 | — | 0.0% |
| 100c_1000r | 1,000 | 995 | 99.5% | 59,723 | 100 | 1.7 ms | 1.0 | 2.0 | 3.0 | 3.0 | 5.0 | 7.0 | — | 0.0% |
| 100c_2000r | 2,000 | 1,839 | 92.0% | 110,352 | 100 | 1.3 ms | 1.0 | 1.0 | 2.0 | 2.0 | 3.0 | 4.0 | — | 0.0% |

> **Note**: 5-node latency is inter-arrival time (ms between consecutive messages). 1/2/3-node latency is end-to-end (sensor_ts → nats_exit_ts). Not directly comparable.

---

## Throughput Comparison Across Node Counts

| Scenario | Target | 1-Node | 2-Node | 3-Node | 5-Node |
|----------|-------:|-------:|-------:|-------:|-------:|
| 10c_100r | 100 | — | 48 | 100 | 100 |
| 10c_500r | 500 | 24 | 39 | 458 | 496 |
| 10c_1000r | 1,000 | — | — | — | 989 |
| 10c_2000r | 2,000 | — | — | — | 1,966 |
| 100c_100r | 100 | 23 | 44 | 96 | 100 |
| 100c_500r | 500 | 42 | 67 | 447 | 498 |
| 100c_1000r | 1,000 | — | — | — | 995 |
| 100c_2000r | 2,000 | — | — | — | 1,839 |

## Efficiency Comparison (% of Target)

| Scenario | 1-Node | 2-Node | 3-Node | 5-Node |
|----------|-------:|-------:|-------:|-------:|
| 10c_100r | — | 48.5% | 100.3% | 99.7% |
| 10c_500r | 4.7% | 7.9% | 91.6% | 99.2% |
| 10c_1000r | — | — | — | 98.9% |
| 10c_2000r | — | — | — | 98.3% |
| 100c_100r | 22.9% | 43.7% | 96.2% | 100.0% |
| 100c_500r | 8.3% | 13.4% | 89.4% | 99.7% |
| 100c_1000r | — | — | — | 99.5% |
| 100c_2000r | — | — | — | 92.0% |

## P99 Latency Comparison (ms)

| Scenario | 1-Node | 2-Node | 3-Node | 5-Node |
|----------|-------:|-------:|-------:|-------:|
| 10c_100r | — | 330 | 2,365 | 59 |
| 10c_500r | 23,895 | 333 | 9,804 | 9 |
| 10c_1000r | — | — | — | 6 |
| 10c_2000r | — | — | — | 3 |
| 100c_100r | 895,192 | 624 | 4,442 | 843 |
| 100c_500r | 129,839,474 | 3,062 | 7,698 | 72 |
| 100c_1000r | — | — | — | 5 |
| 100c_2000r | — | — | — | 3 |

---

## Key Findings

> **[SUPERSEDED]** These findings describe the system *before* D34/D35/D37 and
> undercount capacity by more than an order of magnitude. Finding 2 in
> particular ("throughput scales linearly", topping out at ~1,966 msg/s) is
> contradicted by the 8000 msg/s lossless result above. Retained for the scaling
> study only.


1. **5-node achieves 92–100% target accuracy** across all 8 scenarios, vs 4–48% for 1/2-node setups
2. **Throughput scales linearly**: 10c_2000r = 1,966 msg/s (98.3%), 100c_2000r = 1,839 msg/s (92.0%)
3. **Latency improved 1000×+**: 5-node P99 is 3–843ms vs 1-node P99 of 23,895–129,839,474ms
4. **Cardinality bounded**: 5-node uses 7 series/device (no msg_id labels) vs unbounded growth in 3-node
5. **Zero message drops** on 5-node (vs 77–95% drop rate on 1-node)
6. **Compensated publisher timing** delivers exact target rate (49.6 msg/s vs 50 target per device)

### Bottlenecks Fixed

- **Cardinality explosion**: Removed `msg_id` from metric labels → 7 series/device instead of unbounded
- **Publisher timing drift**: Compensated `usleep` with `clock_gettime` → exact target rate delivery
- **Network policy DNS block**: Fixed egress rule to allow DNS to any namespace → consumer connects to VM
- **k3s version mismatch**: Upgraded all 5 nodes to v1.35.4+k3s1 → ClusterIP/DNS working
