# P0 Findings — measurement defects in the paper-1 corpus

Found while building `harness/`. These are not style issues. Each one changes a
published number or a published conclusion, and they are recorded here rather
than silently fixed so the corrections are auditable.

---

## D4 — CRITICAL: the "best" 5-node run was recorded as "incomplete"

### What the paper says

Paper Table VI (Benchmark Timeline by Phase) lists the final phase as:

> **May 19 — 5-node incomplete — "VM cleared before run; 1 msg/scenario"**

and the corresponding result row reads `total_messages: 1` for all eight
scenarios. `report.md` and `CLUSTER_ANALYSIS.md` both propagate this, the latter
stating the run's data is "invalid for analysis". The May 14 12:14 run is
labelled the **"gold standard"** and is the run the paper's headline table uses.

### What the data actually contains

The raw exports for that run are 1.6 MB per metric file, and parse cleanly:

| scenario | recorded | actual | throughput | efficiency | p50 | p99 |
|---|---:|---:|---:|---:|---:|---:|
| 10c_100r | 1 | 5,977 | 100.13 | 100.13% | 20 ms | 156 ms |
| 10c_500r | 1 | 29,770 | 498.60 | 99.72% | 29 ms | 1,088 ms |
| 10c_1000r | 1 | 59,322 | 987.35 | 98.74% | 53 ms | 1,688 ms |
| 10c_2000r | 1 | 117,930 | 1,842.97 | 92.15% | 2,251 ms | 4,335 ms |
| 100c_100r | 1 | 5,986 | 101.43 | 101.43% | 32 ms | 1,079 ms |
| 100c_500r | 1 | 29,884 | 501.27 | 100.25% | 41 ms | 1,157 ms |
| 100c_1000r | 1 | 59,705 | 1,000.79 | 100.08% | 110 ms | 1,848 ms |
| 100c_2000r | 1 | 110,350 | 1,842.45 | 92.12% | 395 ms | 1,495 ms |

VictoriaMetrics was **not** cleared. The run completed all eight scenarios.

### Why it looked empty

`run_test.sh:load_data()` stored samples in a dict keyed by `msg_id`:

```python
msg_id = r.get('metric', {}).get('msg_id', 'unknown')
results[msg_id] = {...}          # overwrite on every repeated key
```

`msg_id` was removed from the deployed consumer in the paper's own §VI-C
cardinality fix. With it absent, every sample hashed to the single key
`'unknown'`, so each metric collapsed to one entry and `total_messages` was 1.

The secondary defect is that the dict could only ever retain the *last* sample
per identity. That happened to be harmless while `msg_id` was unique per
message; keying on anything bounded reproduces the collapse.

### Consequence for the paper's conclusions

This run is **not** the weakest 5-node result. It is the strongest, and it beats
the run the paper calls the gold standard on precisely the two scenarios that
were hardest to reach:

| scenario | May 14 "gold" | May 19 (recorded as invalid) |
|---|---:|---:|
| 10c_2000r | 1,966 msg/s (98.3%) | 1,842.97 msg/s (92.15%) |
| 100c_1000r | 995 msg/s (99.5%) | **1,000.79 msg/s (100.08%)** |
| 100c_2000r | 1,839 msg/s (92.0%) | **1,842.45 msg/s (92.12%)** |

So the "gold standard" label attaches to the best run *that the broken reader was
able to score*. The narrative that throughput degraded across the 5-node phase
partly reflects a scoring artefact, not only cluster state.

The degradation *is* real for May 18 (that data is intact and does show the drop
the paper attributes to MicroSD wear), so §VI-D's endurance argument survives.
What does not survive is the claim that May 19 was an incomplete run.

### Scope of the defect

Sweep of all 56 parseable `summary.json` files:

| class | runs | note |
|---|---:|---|
| unaffected | 51 | `msg_id` present (Go consumer, 1/2/3-node) |
| **20260519_130906** | 1 | **8/8 scenarios; 5-node; the correction above** |
| 20260415_154943 | 1 | 1/4 scenarios (10c_100r: 1 → 67); a failure run either way |
| 20260428_134022, 20260428_161855, 20260430_122340 | 3 | genuinely empty (1 sample); real failures, no correction possible |

### Resolution

Fixed in `harness/collect.py` (flat `Sample` list, schema-agnostic, no keying on
any per-message label) and in the consumer contract
(`manifests/nats-consumer/configmap.yaml`), which now publishes
`iot_sensor_latency_ms` directly so no join is required at all.

**Action required outside this repository:** paper 1's Table VI, the 5-node
results narrative, and any "gold standard" framing need revision. This is a
correction to a submitted or draft manuscript and is the author's call, but it
should be made before paper 2 is written on top of the same corpus.

---

## D5 — the workload generator is not what the paper describes

Paper §IV-E describes a publisher that *"spawns one POSIX thread per simulated
device"*, each running `while (elapsed < duration)`.

`publisher.c` is single-threaded: one OS process per device, unbounded `while (1)`,
killed externally by `pkill -f publisher`. There is no thread creation and no
duration handling in the C source. The aggregate load shape is equivalent, so
this is not a correctness problem, but the released artifact and the paper must
agree. P0.5 adds `--duration`; the paper text should describe process-per-device.

---

## D6 — BATCH_TIMEOUT was dead config, and not the documented value

Paper §III-F states the Python consumer *"accumulates up to 5 000 messages or
waits a maximum of 2 seconds"*. The code declared `BATCH_TIMEOUT` with a default
of `0.02` (20 ms) and then **never read it** — `batch_sender` drained on every
incoming message. The documented 2-second window did not exist.

This matters beyond bookkeeping: the paper attributes the 5-node P99 of 2,075 ms
to *"the 2-second batch accumulation window"* (§V-E2 and Table XIV). If no such
window exists, that attribution is unsupported, and the observed P99 has some
other cause. Resolved in P0.4 by removing the dead variable and making the drain
a single explicit path; the P99 explanation still needs re-derivation against the
clean corpus.

---

## D7 — `nats_exit_ts` was stamped per batch, not per message

`send_vm_batch` computed one `vm_entry_ts` for the whole batch and
`format_metrics` applied it to every message. The NATS→consumer stage was
therefore quantised to the batch boundary and could not be measured per message,
which is the stage the paper's per-stage table depends on.

Fixed in P0.4: the stamp is taken per message, and `vm_write_ack_ts` plus a
pre-joined `latency_ms` were added so end-to-end latency is measurable at all
(paper §VIII-A lists this as future work).

---

## D8 — messages were acked before they were written

`await msg.ack()` executed on enqueue, before the VictoriaMetrics POST. A failed
write therefore lost the message permanently, and `max_deliver: -1` could never
redeliver it.

This makes paper §VII's claim false as written: *"With max_deliver: -1, messages
will be redelivered until acknowledged, so a clean pod restart loses nothing."*
A pod restart is survivable, but a failed write is not.

Fixed in P0.4: ack after a successful write, nak with bounded backoff on failure.
The durability claim is now true and worth re-verifying in P1 with fault
injection.
