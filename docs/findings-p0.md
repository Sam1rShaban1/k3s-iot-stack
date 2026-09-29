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

---

## D9 — the harness never cleared VictoriaMetrics, and never scoped its queries

Found on 2026-09-29 while validating the P0 harness against the live cluster. A
20 s, 10-client, 500 msg/s scenario reported **22,335 stored messages across 20
devices at 30.04 msg/s (6.01% efficiency)**. The pipeline had actually delivered
9,856 messages across 10 devices at ~503 msg/s.

### Two independent defects, one visible symptom

**(a) `delete_series` silently failed with HTTP 400.** The request sent

```
match[]=__name__=~"iot_.*"
```

VictoriaMetrics parses `match[]` as a *full selector* and rejects the unbraced
form with `identExpr: unexpected token "=~"`. The caller discarded the return
value, so a 400 looked identical to a success from the harness's point of view
and the database was never cleared.

**(b) `export()` queried by metric name only.** No time window, no device
filter, so every export returned the metric's entire retained history.

The combination inflated `total_messages` and `unique_devices` with another
run's series. It also corrupted throughput, which is not a count but a ratio:
the report derives the denominator from the observed `(max - min)` timestamp
span, so stale samples 27 days old stretched that span across the whole
retention period. 22,335 samples over a 743 s span is 30.04 msg/s.

### A third, separate trap

The device filter needs a trailing `.*`. VictoriaMetrics anchors a
`label=~"..."` regex to the **entire** label value, so

```
device_id=~".*_10c_500r_.*_run_20260929_104711"      -> 0 bytes
device_id=~".*_10c_500r_.*_run_20260929_104711._.*" -> 175,603 bytes
```

The unanchored form silently matched nothing, which would have made every
scenario report zero messages. The harness now uses
`.*_<scenario>_[0-9]+_<run_id>_.*`, verified against the live cluster to
exclude other runs, other scenarios of the same run, and stale series.

### Scope: does this invalidate the paper-1 corpus?

**No.** `harness/collect.py` was *added* in commit `96c216d` (P0). The
pre-harness `run_test.sh` sent the correctly braced form:

```
curl -X POST .../api/v1/admin/tsdb/delete_series -d 'match[]={__name__=~"iot_.*"}'
```

so the database **was** cleared before each historical scenario. The old script
did use a 5-minute rolling lookback window, but because the delete succeeded,
that window only ever contained the current scenario. The retained numbers are
consistent with this: `10c_500r` stored 29,770 messages at 498.6 msg/s, an
effective span of 59.7 s against a nominal 60 s, so no adjacent scenario's data
was included.

This defect is confined to the new harness and is fixed before it produced any
published result.

### Resolution

- `delete_series` sends a braced selector and the runner checks the result.
- `export()` requires a time window and a device pattern; the docstring
  explains why omitting them invalidates the numbers.
- `wait_for_delete()` polls the `/api/v1/series` API until the store is
  genuinely empty, and distinguishes "checked and empty" (0) from "could not
  check" (`None`). An instant `count()` query was rejected for this purpose
  because it only sees the lookbehind window, so data older than that would be
  invisible and the check would wrongly report success.
- 23 regression tests in `harness/tests/test_collect_scoping.py`, including one
  that reproduces the 22,335 / 20-device / 30 msg/s corruption in-sample and
  asserts the scoped result is 9,875 / 10 devices / >400 msg/s.

Verified live before and after, same scenario:

| | stored | devices | throughput | efficiency |
|---|---|---|---|---|
| before | 22,335 | 20 | 30.04 msg/s | 6.01% |
| after | 9,856 | 10 | 503.09 msg/s | 100.62% |

---

## D10 — the historical corpus never purged the JetStream backlog

`run_test.sh` cleared VictoriaMetrics but had no equivalent for JetStream. A
message is retained until acknowledged, so any backlog accumulated by an
interrupted run, or by a period when the consumer was not keeping up, remained
in the stream and was replayed during the following scenario.

The consumer acked on enqueue rather than after the write (D8), so in the
deployed configuration the backlog drained faster than the intended
at-least-once semantics, which limited the effect. It was not eliminated
though, and the size of any backlog at the start of each historical scenario is
**not recoverable from the retained data**.

The symptom is well characterised, though, because it was observed directly
during P0.6: a 500 msg/s scenario read 36% efficiency while the consumer
drained a backlog from earlier runs, and the same scenario read 99.68% after a
purge.

**Consequence for the paper.** Historical per-scenario latency figures may be
inflated by replay, concentrated in the earliest scenario of each run and
wherever the consumer fell behind. This cannot be quantified from the retained
corpus and must be stated as a limitation rather than corrected. The
re-instrumented runs scheduled in P1/P2 purge the stream and record
`nats_messages_before` for every scenario, so the effect becomes auditable
going forward.

---

## D11 — `latency_ms` did not measure end-to-end latency, and the fetch cadence dominated it

Found on 2026-09-29 while investigating a bimodal latency distribution. Two
independent defects, both in code added during P0, both of which changed the
headline latency numbers by roughly an order of magnitude.

### (a) The acknowledgement stamp was taken before the write it acknowledged

`format_metrics()` was documented as:

> Added `vm_write_ack_ts`, stamped after the VictoriaMetrics POST returns.

It was not. The call site was

```python
body = "\n".join(
    line for _msg, data, exit_ts in stamped
    for line in format_metrics(data, exit_ts, now_ms())   # <-- here
)
# ... only then:
async with session.post(VM_URL, data=body) as resp:
```

`now_ms()` is evaluated while the request body is still being assembled, so
`vm_write_ack_ts` was a *write-start* timestamp. Since
`latency_ms = vm_write_ack_ts - sensor ts`, the reported metric excluded the
entire VictoriaMetrics write, and the paper's central claim — that §VIII-A's
future work of "unified end-to-end latency measurement" is now solved — was
measuring something else. This is the same class of error as D7, where a
timestamp was stamped at the wrong point in the pipeline.

**Fix.** Split the write into two passes, because the value cannot exist until
the request carrying the payload has returned:

1. `format_metrics(data, nats_exit_ts)` — payload, no ack stamp.
2. POST, and on success take `ack_ts = now_ms()`.
3. `format_metrics_ack(data, ack_ts)` — emits `vm_write_ack_ts` and
   `latency_ms`.
4. POST again, then `ack()`.

The payload is already durable when pass 2 runs, so a failure there costs the
latency series and not the measurement; the consumer reports that rather than
hiding it. Four regression tests pin the ordering, including one that asserts
`ack_ts = now_ms()` appears *after* the first `session.post` in the AST.

### (b) `FETCH_TIMEOUT_S=1.0` set a floor under measured latency

The pull consumer requests `sub.fetch(batch=5000, timeout=1.0)`. Below 5,000
msg/s the batch never fills, so every fetch returned a partial batch only after
the full one-second timeout. A message's wait was therefore dominated by where
it fell in the fetch cycle, not by the pipeline.

The signature was unmistakable once the reporting bugs were fixed: a flat ramp
from 10 ms to 1 s with 2,944 samples in the 500 ms–1 s bucket and `max_ms` of
1,011. That is a periodic timer, not a pipeline.

**Fix.** `FETCH_TIMEOUT_S=0.05`, so a partial batch returns promptly while
batching still amortises the write.

### Measured effect, same scenario (10 clients, 500 msg/s, 20 s)

| | avg | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| `FETCH_TIMEOUT=1.0`, stamp before write | 322 ms | 210 ms | 925 ms | 993 ms | 1011 ms |
| `FETCH_TIMEOUT=0.05`, stamp before write | 108 ms | 34 ms | 652 ms | 936 ms | 1109 ms |
| `FETCH_TIMEOUT=0.05`, stamp after write | **25–28 ms** | **17–18 ms** | **62–78 ms** | **105–189 ms** | **268–342 ms** |

The last row is the first one that measures what it claims. Throughput is
unaffected at 99.4–99.6% of target across all three, so this is a latency
result and not a throughput trade.

### Consequence for the paper

All historical latency figures carry up to ~1 s of consumer-side fetch delay
that is an artefact of `FETCH_TIMEOUT_S=1.0`, and none of them can be
reconstructed as sensor-to-write-acknowledged because `vm_write_ack_ts` did not
exist in the deployed consumer at the time. The historical corpus remains valid
for **throughput and message-count** claims. Latency must be quoted from the
re-instrumented runs only, with the fetch timeout stated.

The corrected distribution (p50 17 ms, p99 ~105–189 ms) is a far more
plausible result for a five-node Raspberry Pi cluster, and it is the first
end-to-end latency measurement this pipeline has actually produced.

---

## D12 — GitOps drift that no component reported: Benthos at 1/5 replicas

Found on 2026-09-29 while measuring the latency knee (below). Not a measurement
defect, but a live instance of the failure mode the second paper targets, so it
is recorded here with the evidence.

### The state

| | value |
|---|---|
| `manifests/benthos/deployment.yaml` | `replicas: 5` |
| live Deployment | `spec.replicas: 1`, `readyReplicas: 1` |
| ArgoCD `benthos` app | `SYNC STATUS: Unknown`, `HEALTH STATUS: Healthy` |
| running pod age | 132 d, `2 (21h ago)` restarts |

So Git says five replicas, the cluster runs one, and **nothing surfaces the
disagreement**:

- ArgoCD reports `Unknown` for sync status across *every* application, not just
  this one, because the application-controller cannot pull its own image
  (the air-gapped image problem from P0.6). An app whose controller is down
  cannot report drift, so `Unknown` is being read as "nothing to see".
- `HEALTH STATUS: Healthy` is actively misleading here. ArgoCD's health check
  asks whether the *deployed* workload is available, and one healthy Benthos pod
  is available. Replica count is not part of that check.
- Nothing else in the stack compares the declared spec to the live spec.

### Why it matters for the measurements

Benthos is the MQTT → NATS stage, and a single replica makes it a serial
bottleneck and a single point of failure. The latency knee sits right where
that would be predicted:

| target | throughput | efficiency | p99 |
|---|---|---|---|
| 500 msg/s | 494.81 | 98.96% | 322 ms |
| 1000 msg/s | 948.27 | 94.83% | 2,475 ms |
| 2000 msg/s | 1965.72 | 98.29% | 2,679 ms |

This is **not** resource exhaustion: the cluster sat at 3–14% CPU and 12–62%
memory throughout, and the consumer shows no CPU throttling and no restarts.
The jump from 322 ms to ~2.5 s p99 between 500 and 1000 msg/s is consistent
with queueing behind one Benthos instance, but that is a hypothesis consistent
with the evidence, not a measured cause. Settling it needs a controlled
comparison, which is deferred to the P2 fault-injection lab rather than
asserted here.

### Open decision

Scaling Benthos to 5 changes the system under test, so it is not done
unilaterally. It needs to be settled before the paper-2 corpus is collected,
because "scaled the streaming bridge to match Git" is precisely the kind of
remediation the agent is meant to perform — and it is only a fair evaluation
if the initial fault state is a real one rather than one invented to be easy.

Recorded as the first concrete candidate for a detector-plus-remediation
scenario: *declared replicas ≠ live replicas, controller unhealthy, no
component reports it.*
