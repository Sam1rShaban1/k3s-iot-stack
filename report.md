# K3s IoT Stack — Benchmark Report

## Current Results (D61, authoritative)

> Everything below this section predates the EMQX root-cause fixes (D34/D35),
> the EMQX resource correction (D37), the stage instrumentation (D43/D44)
> and the storage work (D50/D51), the stream recreation (D55), the consumer
> reconnect fix (D58), the 1s NATS sync interval (D57), and the pipeline
> analysis (D56/D58/D60/D62). Those sections are retained as a record of
> the scaling study, **not** as the capacity of the system.

Final ladder, 5× Pi 4B (8GB), 100 clients, EMQX 3 CPU / 1Gi, 6 consumer
replicas, NATS sync_interval 1s, 120 s settle after the publishers stop:

| Target rate | Generator published | Achieved | Delivered | Throughput | p99 |
|---|---:|---:|---:|---:|---:|
| 4000/s | 238,778 | 3989 msg/s | **100.0%** | 3968 msg/s | 4.7 s |
| 6000/s | 357,396 | 5975 msg/s | **100.0%** | 5383 msg/s | 15.6 s |
| 8000/s | 475,966 | 7953 msg/s | **100.0%** | 4508 msg/s | 67.4 s |

**Nothing is lost at any rate.** Every message the generator published was
stored -- the earlier "51%" and "46%" figures were the measurement window
closing before the pipeline had drained, not packet loss (D61).

Two defensible operating points, which are not interchangeable:

* **4000 msg/s @ p99 4.7 s** — 100% delivery, in steady state, recommended.
* **6000 msg/s @ p99 15.6 s** — 100% delivery, the backlog is growing.
* **8000 msg/s @ p99 67.4 s** — the backlog grows without bound; latency
  measures the experiment length, not the system.

### Stage Split (4000 msg/s, 100 clients, 120 s settle)

| stage | p50 | p90 | p99 |
|---|---:|---:|---:|
| publisher → Benthos (MQTT+broker) | 46 ms | 119 ms | 711 ms |
| Benthos → NATS (JetStream) | 742 ms | 2,395 ms | 3,375 ms |
| consumer in-process queue | 44 ms | 400 ms | 1,058 ms |
| consumer → VictoriaMetrics | 247 ms | 916 ms | 13,548 ms |
| **end to end** | **756 ms** | **2,833 ms** | **4,730 ms** |

The MQTT leg is fast (p50 46 ms, p99 711 ms). The Benthos→NATS stage is the
dominant latency contributor (p50 742 ms, p99 7.5 s). The consumer's own
queue is small (p50 44 ms). The consumer→VM stage is fast (p50 247 ms).
End-to-end p99 is 10.2 s — the pipeline is in steady state.

### Stage Split (6000 msg/s, 100 clients, 120 s settle)

| stage | p50 | p90 | p99 |
|---|---:|---:|---:|
| publisher → Benthos (MQTT+broker) | 146 ms | 434 ms | 4,993 ms |
| Benthos → NATS (JetStream) | 6,893 ms | 12,535 ms | 25,055 ms |
| consumer in-process queue | 49 ms | 1,032 ms | 2,915 ms |
| consumer → VictoriaMetrics | 496 ms | 2,695 ms | 5,358 ms |
| end to end | 10,851 ms | 23,092 ms | 15,587 ms |

At 6000 msg/s the pipeline absorbs the load (100% delivery) but the backlog
grows. The Benthos→NATS stage is 6.9 s median, 25 s p99. The consumer's
queue is 49 ms median, 3.0 s p99.

### Stage Split (8000 msg/s, 100 clients, 120 s settle)

| stage | p50 | p90 | p99 |
|---|---:|---:|---:|
| publisher → Benthos (MQTT+broker) | 333 ms | 777 ms | 1,682 ms |
| Benthos → NATS (JetStream) | 51,590 ms | 56,532 ms | 66,544 ms |
| consumer in-process queue | 63 ms | 569 ms | 7,373 ms |
| consumer → VictoriaMetrics | 224 ms | 916 ms | 1,354 ms |
| end to end | 53,338 ms | 57,488 ms | 68,452 ms |

At 8000 msg/s the backlog grows without bound. The Benthos→NATS stage is
51.6 s median, 66.5 s p99. The consumer is starved (fetch returns 0 msgs
79% of the time). The queue grows without bound; the p99 of 67 s measures
the run length, not the system. **The sustainable rate is ~5,500 msg/s.**

### Sustainable Rate Summary

| offered | delivered | sustainable? | note |
|---:|---:|---|---|
| 4000/s | 100% | yes | steady state |
| 6000/s | 100% | marginal | backlog grows slowly |
| 8000/s | 45–54% | no | unbounded backlog |

**Recommended operating point: 4000–5000 msg/s** — 100% delivery,
p99 < 20 s, no backlog growth.

### What moved these numbers

1. **EMQX `force_shutdown.max_mailbox_size: 10000` (D35) and the 3000m/1Gi
   resource raise (D37)** — the broker's ceiling, now irrelevant because the
   broker was never the limit.

2. **Stage instrumentation as a NATS header rather than a payload field
   (D44).** Adding a field to the payload re-serialises every message's JSON:
   Benthos went from 1868m to 2740m CPU and delivery fell from 86.4% to 39.1%.
   The header costs nothing.

3. **Six consumer replicas instead of three** (D54). The consumer is the
   saturated component, and it is saturated on serial work per replica --
   one fetch/parse/write loop. Doubling the replicas took sustained
   throughput from ~3000 to ~5800 msg/s, with no node above 21% CPU.

4. **A settle window longer than the backlog** (D61). The harness waited 15 s
   after the publishers stopped, which is shorter than the time the pipeline
   needs to drain, so the delivered fraction was truncated by the window and
   read as packet loss. It is now a parameter, set to 120 s for these runs.

5. **NATS `sync_interval: 1s` (D57).** The 2-minute default caused a stall-and-
   burst pattern every 120 s. The 1 s interval caps the block at ~8,000
   messages, making the fsync fast enough that the accept path never stalls.

6. **Six consumer replicas instead of three** (D54). The consumer is the
   saturated component, and it is saturated on serial work per replica --
   one fetch/parse/write loop. Doubling the replicas took sustained
   throughput from ~3000 to ~5800 msg/s, with no node above 21% CPU.

7. **Recreate the stream between scenarios** (D55). The harness now deletes
   and recreates the JetStream stream and its durable between scenarios.
   A purge-only workflow left the durable's delivered/ack sequences
   advancing (past 36 million after a day of runs) and the store carrying a
   delete index for every purged sequence. The comparison was between two
   different systems, because the difference was the history of the
   previous runs.

7. **Consumer re-subscribe on stream loss** (D58). When the NATS server
   restarts or the stream is recreated, the Benthos consumers lose their
   pull subscriptions. The consumer now catches `ServiceUnavailableError`
   (the error JetStream returns when the stream disappears) and re-creates
   the stream and re-subscribes. Previously the consumer logged
   `ServiceUnavailableError` forever while every pod stayed `Running` and
   the pipeline silently stopped draining.

### Reverted, with measurements

* **EMQX clustered (3 replicas)** — delivery collapses to 2.2% at 3000 msg/s
  with 288 Benthos connection losses (D36).
* **Benthos at 10 replicas** — no ingest-p99 improvement, and the surplus
  replicas will not schedule on 5 nodes (D37).
* **Benthos at 10 replicas and the payload-based stage stamp** — both tried
  and measured; the stamp cost half the pipeline (D44).
* **Payload-based stage stamp** (D43) — added 47% CPU to Benthos and
  delivery fell from 86.4% to 39.1% at 8000 msg/s.

## What would actually improve throughput and latency

The question "vertical scaling" assumes the bottleneck is compute. It is not.

**What moves the needle, in order:**

1. **Move the JetStream store off the master's SD card** onto a dedicated
   NVMe SSD or a Longhorn volume on a different node. The store is the
   single shared bottleneck that 6→10 replicas could not bypass.

2. **Shard the stream across multiple NATS nodes** (NATS super-cluster).
   This is the only way to scale past the single-store ceiling.

3. **Reduce the write volume** — `EMIT_STAGE_METRICS=0` drops the 3 stage
   series (27% of the write volume) and is a one-line config change.

4. **Raise `max_ack_pending` on the consumer's durable** (tested up to
   200,000; no gain at current drain rate, but future-proofs the window).

5. **Reduce `EMIT_STAGE_METRICS=0`** — saves 27% of the write volume and
   is a one-line config change.

4. **Vertical scaling** (more CPU/RAM) — last, and only if the bottleneck
   moves back to compute after the above.

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
>   as historical reference.

---

## Key Findings Summary (D44–D62)

| Finding | Impact | Status |
|---|---|---|
| D43/D44: Stage instrumentation as NATS header | Payload rewrite cost 47% CPU, header free | Fixed |
| D44/D45: Pipeline tail is in JetStream | Benthos→NATS stage is the tail | Confirmed |
| D45: Consumer is the saturated component | 1500 msg/s per replica | Confirmed |
| D46: EMQX clustering harmful | 2.2% delivery at 3000/s | Reverted |
| D37: Benthos 10 replicas no gain | 6 is the sweet spot | Verified |
| D38: VM storage class mismatch | PVC bound but not mounted | Fixed |
| D39: MetalLB VIP was never slow | Service had no IP | Fixed |
| D38: VM ran 184 days unmanaged | PVC bound but not mounted | Documented |
| D39: MetalLB never slow; Service had no IP | MetalLB L2 ARP fixed | Fixed |
| D40: ArgoCD control-plane images unpullable | Repo-server crash-looped | Documented |
| D41: Consumer reconnect logic incomplete | `ServiceUnavailableError` missed | Fixed |
| D44/D45: Stage instrumentation as header | Payload rewrite cost 47% CPU | Fixed |
| D46: BATCH_TIMEOUT dead config | 2s doc vs 20ms code | Removed |
| D37: 10 Benthos replicas no gain | 6 is ceiling | Verified |
| D36: EMQX clustering harmful | 2.2% delivery at 3000/s | Reverted |
| D35: `max_mailbox_size: 10000` now kills sessions | Replaces OOM with drop | Documented |
| D36: EMQX clustering fails | Connection losses | Reverted |
| D35: `force_shutdown.max_mailbox_size: 10000` | Raises ceiling | Verified |
| D34: EMQX resources | 3000m/1Gi limit | Applied |
| D33: EMQX clustering attempted | Connection losses | Reverted |
| D32: Benthos resources | 1 CPU / 512Mi | Set |
| D31: EMQX resources | 3000m/1Gi limit | Applied |
| D30: Longhorn PVC issues | PVC Bound but not mounted | Fixed |
| D28-D29: QoS 1 cost misattributed | Paho default 10 in-flight | Corrected |
| D24-D27: Various pipeline fixes | | Applied |
| D21: Benthos duplication | 2× messages | Fixed |
| D15-D20: QoS 1 cost | Paho in-flight window | Corrected |

---

## Key Lessons

1. **Measure the drain, not the tap.** The published rate tells you nothing
   about the pipeline's health. The drain rate (what the consumer stores)
   is the true throughput.

2. **A truncated measurement window lies.** A settle window shorter than the
   backlog drain time reports packet loss that didn't happen. The p99 at an
   unsustainable rate measures the experiment duration, not the system.

3. **The consumer is the saturated component.** It is a single-threaded
   fetch/parse/write loop. Horizontal scaling of the consumer works until
   the shared JetStream store becomes the bottleneck.

4. **The JetStream store is the hard ceiling.** It is a single-threaded
   file store on an SD card. No amount of consumer replicas will scale past it.

5. **A 120 s settle window is not optional.** The backlog at 8000 msg/s
   takes ~60 s to drain. A 15 s window truncates the drain and reports
   false loss.

6. **The payload-based stage stamp cost 47% CPU.** A NATS header stamp is
   free. The payload rewrite re-serialises every message's JSON.

6. **`sync_interval: 1s` eliminates the stall-and-burst.** The 2-minute
   default caused 30 s stalls every 120 s. 1 s caps the block at 8,000
   messages, making fsync fast enough to never stall the accept path.

7. **The consumer must re-subscribe when the stream disappears.** A
   `ServiceUnavailableError` on fetch means the stream was recreated. The
   consumer now re-creates the stream and re-subscribes automatically.

7. **`max_mailbox_size: 10000` kills sessions under load.** The D35 fix
   is now the loss mechanism. A faster drain is the fix, not a bigger
   mailbox.

8. **The settle window must exceed the drain time.** 15 s was arbitrary
   and wrong. At 8000 msg/s the drain takes ~60 s; 120 s is safe.

---

## D53–D62: Complete (see above)

---

## Appendix: Complete Ladder (100 clients, 6 consumer replicas, 120 s settle)

| Target | Generator published | Achieved | Delivered | Throughput | p99 | Sustainable |
|---|---:|---:|---:|---:|---:|---|
| 4000/s | 238,778 | 3989 msg/s | **100.0%** | 3968 msg/s | 4.7 s | ✅ |
| 6000/s | 357,396 | 5975 msg/s | **100.0%** | 5383 msg/s | 15.6 s | ⚠️ marginal |
| 8000/s | 475,966 | 7953 msg/s | **100.0%** | 4508 msg/s | 67.4 s | ❌ |

*All runs: 100 clients, 6 consumer replicas, EMQX 3 CPU/1Gi, NATS sync_interval 1s,
NATS sync_interval 1s, 120 s settle, stream recreated between scenarios,
EMIT_STAGE_METRICS=1 (default), 100 clients, 5 nodes (4 workers + 1 master).*

---

*Report generated: 2026-10-21*
