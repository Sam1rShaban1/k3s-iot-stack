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
(`manifests/nats-consumer/consumer.py`), which now publishes
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

---

## D13 — the JetStream stream was unbounded, and "backlog" was the wrong number

Found on 2026-09-29 while investigating the latency knee. Two errors: one in
the cluster, one in the harness written to measure it.

### (a) The stream can never evict anything

`IOT_DATA` was created by the consumer as

```python
await js.add_stream(name=STREAM, subjects=[SUBJECT])
```

which yields the server defaults. Read back from the live cluster:

```
retention: limits   max_msgs: -1   max_age: 0.0   max_bytes: -1
```

With `retention: limits` and no limits, **acknowledging a message does not
remove it**. The stream accumulates every message ever published. Measured:
18,236 messages occupied 3.1 MB, about 170 B each, on pi7's `/data`. At a
sustained 1,000 msg/s that is roughly **14.7 GB/day** against a 57 GB SD card
that also carries K3s, Longhorn and the JetStream store.

The failure mode is a slow disk exhaustion rather than a visible outage, which
is exactly the class worth detecting. `harness/preflight.py` now has
`check_jetstream_retention` (detector class 7), which fails the preflight when
no limit is set, and the consumer creates the stream with `max_age=3600` and
`max_bytes=512 MiB`, converging an existing stream with `update_stream` rather
than leaving a pre-fix one unbounded because it already exists.

### (b) The harness reported retained messages as "backlog"

`harness/nats.py` exposed `backlog_size()` returning
`stream_info().state.messages`, and the module docstring asserted that
JetStream "retains published messages until they are acknowledged" and that the
consumer therefore replays them. That is backwards for this configuration:
messages are retained *after* acknowledgement, and the consumer does not replay
them.

A live probe made the error obvious:

```
stream   : messages=18236 first_seq=4828993 last_seq=4847228
consumer : num_pending=0 num_ack_pending=0
delivered: consumer_seq=3567859 stream_seq=4847228
```

The consumer's delivered sequence sat exactly at the stream's last sequence
with nothing outstanding: fully consumed. `state.messages` was reporting 18,236
**retained** messages and calling them a backlog. (A second false alarm in the
same session came from counting lines in VictoriaMetrics' export payload, which
is a single JSON document, and reading "10 samples" when 18,236 were stored.)

The module now distinguishes the two numbers:

| | meaning | source |
|---|---|---|
| `unconsumed()` | published, not yet acknowledged | `num_pending + num_ack_pending` |
| `retained()` | still stored, regardless of ack | `stream.state.messages` |

`unconsumed` is a derived property rather than a field, so it cannot disagree
with the two values it is defined from; a test caught it being constructible
with contradictory values. The runner records both per scenario, plus
`nats_drained`, and only warns about a scenario when `unconsumed > 0` — a real
indication that a consumer is behind.

### What replay is actually possible

The genuine contamination path is narrower than the old docstring claimed: a
pull consumer with `deliver_policy: all` starts at the beginning of the stream,
so if the **durable is deleted or recreated** — a fresh consumer name, a config
change, a rebuilt store — it replays the whole retained history. That is why
purging per run is still correct, and why bounding retention matters
independently of benchmarking.

### Consequence for the paper

The historical corpus was collected without purging NATS (D10). That caveat
stands, but it is now correctly characterised: retained history could only have
been replayed after a durable recreation, not continuously.

---

## D14 — `kubectl apply` reported a successful rollout that changed nothing

Found on 2026-09-29 while deploying the D13 retention fix, and the reason that
fix appeared not to work at first.

The consumer script was embedded as a `data['consumer.py']` key inside
`manifests/nats-consumer/configmap.yaml`, mounted by a ConfigMap volume. Editing
a ConfigMap does not change a pod template, so:

```
$ kubectl apply -f configmap.yaml -f deployment.yaml
configmap/nats-consumer-script configured
deployment.apps/nats-consumer configured
deployment "nats-consumer" successfully rolled out
```

...while the pods kept their old ReplicaSet and their age kept climbing. The
new code — bounded stream retention — was never running. It was caught only by
comparing pod ages across the apply, after the fix appeared to have no effect
on the stream.

The deployment was also a no-op for a second, quieter reason: `kubectl apply -f
<dir>` on a directory containing a `kustomization.yaml` fails on the
kustomization file itself (`no matches for kind "Kustomization" in version
"kustomize.config.k8s.io/v1beta1"`), so the generated pieces are silently not
applied. It requires `kubectl apply -k`.

### Resolution

`manifests/nats-consumer/consumer.py` is now the single source of truth and
`kustomization.yaml` generates the ConfigMap from it with `configMapGenerator`.
The content-hash suffix is deliberately kept, because it is what makes the edit
reach the pods: kustomize rewrites the Deployment's volume reference to the
hashed name, the pod template changes, and the rollout is real. Verified by pod
age and a new ReplicaSet across an apply, with the new code visible in the logs:

```
Stream: IOT_DATA ready (max_age=3600s max_bytes=536870912)
```

Setting `disableNameSuffixHash: true` to keep a stable ConfigMap name also
removes that linkage, which is why it is not set; a test asserts it stays unset.

Also removed: the checked-in "readable copy" of the script and the
`sync_consumer.py` helper that maintained it. That copy had already drifted once
and its own documented resync command overwrote the header explaining where the
real source was. `harness/validate.py` now fails if a hand-written
`configmap.yaml` reappears, and `make render-manifests` fails if any
kustomization does not render.

---

## D15 — the paper's QoS serialization ceiling was the load generator, not the protocol

Found on 2026-09-29 while investigating why measured efficiency sat at ~93% while
the publisher demonstrably offered 98.7% of target against a local stub.

### The symptom

Running the same scenario at QoS 1 instead of QoS 0:

| QoS | throughput | efficiency |
|---|---|---|
| 0 | 833 msg/s | 83.3% |
| 1 | **86 msg/s** | **8.6%** |

A ten-fold collapse from a flag that should only add an acknowledgement.

### Root cause

Paho C defaults `maxInflightMessages` to **10**. `publisher.c` never set it, so
at QoS 1 the in-flight window filled almost immediately. Measured against the
local stub with 1 client and no sleep at all:

| target | published in 12.1 s | achieved |
|---|---|---|
| 100 msg/s | 120 | 9.95 msg/s |
| 500 msg/s | 120 | 9.95 msg/s |
| 1000 msg/s | 120 | 9.95 msg/s |

Exactly 120 messages regardless of the target — a hard 10/s per process, entirely
inside the client. The publisher also blocked inside `publish()` when the
window could not drain, so it never reached its `--duration` check and had to be
killed. The existing test suite noticed the symptom and misattributed it:

> QoS 1 and 2 cannot be exercised against the stub: it does not implement the
> PUBACK/PUBREC handshake, so paho blocks until the process is killed.

The stub was not the cause; the missing in-flight window was.

### A wrong fix, caught by measurement

The first attempt added `MQTTClient_yield()` per iteration to "process PUBACKs",
on the reasoning that nothing was reading the socket. That made it *worse* and
revealed the real story:

| build, QoS 1, 1 client, 8 s, no sleep | published | achieved |
|---|---|---|
| with `MQTTClient_yield()` | 80 | 9.95 msg/s |
| without | 66,001 | **8,249 msg/s** |

`MQTTClient_yield()` blocks for ~100 ms in this Paho build even with nothing
pending, so calling it in the hot loop *was* the 100 ms. This Paho version runs
its own sender thread, so no pump is needed at all. The yield call is now gone
and a comment warns against reintroducing it.

### The fix

`conn_opts.maxInflightMessages = 1000` when `qos > 0`, plus a `--max-inflight`
flag. QoS 0 is left at Paho's default so the historical measurement path is
untouched. Deliberately *not* used: waiting per PUBACK, which would cap the rate
at one per round trip and measure the round trip rather than the broker.

`mqtt_stub.py` also gained the QoS 1 handshake (PUBACK, DUP accounting) and
`TCP_NODELAY` on accepted sockets. Without PUBACK the QoS 1 path could not be
tested at all, which is why the defect survived.

### Result

Publisher, local stub, 10 and 100 clients:

| clients | target | QoS 0 | QoS 1 |
|---|---|---|---|
| 10 | 500 | 99.3% | 99.3% |
| 10 | 1000 | 98.8% | 98.3% |
| 10 | 2000 | 98.0% | 96.8% |
| 100 | 2000 | 93.1% | 93.2% |

Full cluster, 10 clients x 1000 msg/s x 30 s, after the fix:

| QoS | throughput | efficiency | p99 |
|---|---|---|---|
| 0 | 973.41 msg/s | 97.34% | 1758 ms |
| 1 | 947.23 msg/s | 94.72% | 1260 ms |

QoS 1 went from 8.63% to 94.72%.

### Consequence for the paper

**Paper §V-C's "~50 msg/s per connection serialization ceiling" is an artifact of
the load generator and must not be reported as a property of QoS 2 or of the
cluster.** It is Paho C's default in-flight window of 10. The QoS 2 figure needs
re-measuring on this fixed build; the test that would do it is still skipped
because the stub does not implement the four-way QoS 2 handshake, and its skip
message now says so.

QoS 1 costs a few percent, not an order of magnitude, and the benchmark's default
QoS 0 remains the right choice for a throughput study. The loss at QoS 0 is
therefore the honest price of the cheaper path, and should be stated as such
rather than read as a pipeline defect.

---

## D16 — the ack pass re-emitted the whole payload, doubling every series

Found on 2026-09-29 while decomposing where end-to-end latency was spent.

Counting samples per series in one 10c_1000r run:

| series | samples |
|---|---|
| `iot_sensor_ts` | 55,720 |
| `iot_sensor_nats_exit_ts` | 55,720 |
| `iot_sensor_vm_write_ack_ts` | 27,860 |
| `iot_sensor_latency_ms` | 27,860 |

Exactly 2:1. The D11 two-pass write introduced this: `format_metrics_ack()`
called the same `_render(data, device)` as pass 1, so the second request
re-emitted every field in `data` rather than only the two new stamps. Per message
the consumer wrote 16 series where 9 were needed.

No error was raised, and latency and throughput were unaffected, so nothing in the
report was wrong — but the write volume, the ingestion cost and the storage were
all doubled for no additional information. It was visible only by counting
samples per series.

`format_metrics_ack` now passes `only=("vm_write_ack_ts", "latency_ms")`, and
`_render` honours that filter. Six regression tests cover it, including one that
asserts the two passes emit disjoint series. Verified on the cluster: pass-1 and
pass-2 series counts now match exactly.

This also contributed to the efficiency gap: at 1000 msg/s, efficiency moved
from ~93% to 97.3% once the consumer stopped writing twice as much as needed.

---

## D17 — the NetworkPolicy "API egress" fix never worked, and hid behind an unrelated error

Found on 2026-09-30 while fixing the pods that had been failing since April.

### Symptom

`monitoring-kube-state-metrics` and `monitoring-kube-prometheus-operator` were in
CrashLoopBackOff with ~10,000 and ~15,000 restarts, both logging the same thing:

```
Get "https://10.43.0.1:443/version": dial tcp 10.43.0.1:443: connect: connection refused
```

The error reads like a down API server. The API server was fine.

### Two wrong fixes

The repository already claimed to have fixed this. The file's own BUG 6 note
read:

> ArgoCD, Prometheus, metrics-server and node-exporter all need to reach
> 10.43.0.1:**6443** (the in-cluster kubernetes ClusterIP). FIX: explicit rule to
> kube-system:6443 in every namespace policy.

Two independent errors:

1. The `kubernetes` Service listens on **443** and forwards to 6443 on the node.
   No client ever connects to 6443.
2. The Service has no backing pods, so `namespaceSelector: kube-system` selects
   nothing regardless of port.

The first correction — an `ipBlock` on the service CIDR `10.43.0.0/16` on 443 —
also failed, and this is the non-obvious part. **Calico applies NetworkPolicy in
the FORWARD chain, which runs after kube-proxy's DNAT in PREROUTING.** By the
time policy is evaluated the destination is no longer the ClusterIP
`10.43.0.1:443` but the endpoint `10.0.0.1:6443`.

Isolated with two otherwise identical busybox pods pinned to the same node:

| pod | namespace | result |
|---|---|---|
| `apitest-def` | `default` (no policy) | `10.43.0.1:443` **OPEN** |
| `apitest-mon` | `monitoring` (policy, service CIDR only) | **REFUSED** |
| `apitest-mon2` | `monitoring`, after adding `10.0.0.0/16` | **OPEN** |

### The fix

Each of the six policies now allows `10.0.0.0/16` and `192.168.1.0/24` on 443 and
6443, naming the networks the API server is actually reached on. The tradeoff is
that this permits egress to those two ports anywhere in those ranges; acceptable
on an air-gapped lab, and tighter rules would need a CNI-specific rewrite.

Verified: kube-state-metrics and the Prometheus Operator are past the network
error and now fail only on the image problem below. The pipeline is unaffected
(96.18% at 1000 msg/s, preflight 11/11 apart from the image check).

### A crash loop was hiding a second fault

kube-state-metrics had been crash-looping so long that its original cause was
invisible. Deleting the pod to read the real error rescheduled it from
`raspberrypi` to `pi3`, and it went to `ImagePullBackOff` — because `pi3` does
not have the image. Two unrelated faults, one masking the other, on the same pod.

### New detectors

- `apiserver-egress` (class 9) fails if any policy lacks a node-network ipBlock
  on 443/6443. It derives the service CIDR from the live Service and explicitly
  rejects it, so the "obvious" wrong rule cannot pass. Verified by patching the
  live policy back to the broken form and watching the check fail.
- `airgap-image-pull` (class 10) lists every image in ImagePullBackOff and the
  nodes missing it, so the side-load list is derivable rather than guessed. It
  deliberately does not fire on CrashLoopBackOff, which is a different fault and
  is exactly what caused the confusion here.

### Remaining, not fixed

The air-gapped image problem itself is untouched and is a project in its own
right: every image must be side-loaded into every node's containerd, nothing
enforces it, and scheduling does not consider it. Current state:

```
longhornio/longhorn-instance-manager:v1.7.2   pi2, pi3, pi4
quay.io/argoproj/argocd:v3.3.6                pi3
registry.k8s.io/kube-state-metrics:v2.10.1    pi3
```

The local host is x86_64, so `docker save` of the arm64 Longhorn image produces an
amd64 archive; an arm64-capable pull path is needed. This is a prerequisite for
P1 observability (Prometheus PVC, Loki, Tempo), so it should be scheduled as its
own piece of work rather than absorbed into it.

---

## D18 — static node addressing, and three consequences that were not obvious

The wired segment now carries fixed addresses, so cluster addressing no longer
depends on DHCP. Final state, all five nodes advertising from 10.0.0.0/24:

| node | eth0 | wlan0 | k3s_node_ip |
|---|---|---|---|
| raspberrypi | 10.0.0.1 | 192.168.1.50 | 10.0.0.1 |
| pi7 | 10.0.0.2 | 192.168.1.162 | 10.0.0.2 |
| pi2 | 10.0.0.3 | — | 10.0.0.3 |
| pi3 | 10.0.0.4 | — | 10.0.0.4 |
| pi4 | 10.0.0.5 | — | 10.0.0.5 |

`ansible/inventory.ini` had two stale entries as a result. pi2 was recorded as
`192.168.1.189`, a DHCP address captured in P0.3 that no longer applied, and
pi7 as `192.168.1.162`. Both now record the wired-segment address, and the file
explains that the 10.0.0.0/24 segment is not routable from a workstation on
192.168.1.0/24, so provisioning has to run from a host on the wired segment.

pi7 was re-joined onto 10.0.0.2 by editing `--node-ip` in
`/etc/systemd/system/k3s-agent.service` and restarting `k3s-agent`. Backup at
`k3s-agent.service.bak-pre-nodeip`. The kubelet came back immediately serving a
certificate for 10.0.0.2 and the Node object followed within ~45 s; no Node
deletion or data-dir clean was needed. Note the value sits on its own line in
the unit file, so a single-line regex will not match it.

### (a) The harness stopped finding the broker, and reported it as a pipeline result

Once pi7 advertised 10.0.0.2, every node address the harness could see was on
the wired segment and unreachable from the workstation. Discovery probes each
candidate and correctly rejected all of them -- and then returned the first
candidate anyway:

```python
# Nothing answered. Return the first candidate anyway so the failure is a
# clear connection error naming the address, rather than a silent default.
return ordered[0] if ordered else None
```

The first candidate is the ClusterIP, which lives in the service CIDR and is
never routable from outside the cluster. The run that followed published nothing
and reported `stored=0 ... eff=0.0%`, which reads as a measurement rather than as
a harness that could not find the broker.

Root cause underneath: Kubernetes reports only each node's InternalIP, and a
NodePort is programmed on *every* address a node holds. A node reachable only on
a secondary interface is therefore invisible to discovery. There is no fix in
the API for that, so discovery now falls back to addresses it can legitimately
know: the `ansible_host` values in the inventory, and the kubeconfig server
address, which is routable from whatever machine is running the harness. With
that it resolves to `192.168.1.50:31883` and the scenario runs at 96.89%.

It also never returns an address that failed its own probe again.

### (b) MetalLB advertises the wrong interface for its address pool

`files/metallb-config.yaml` allocates `192.168.1.240-192.168.1.250` and
advertises it on:

```yaml
  interfaces:
  - eth0
```

On these nodes `eth0` is the **wired cluster segment (10.0.0.0/24)**;
192.168.1.0/24 is the home LAN, which on pi7 is `wlan0`. An L2 advertisement
for a 192.168.1.x address on an interface whose subnet is 10.0.0.0/24 cannot
work. This is consistent with EMQX having no reachable LoadBalancer address:
P0.3 recorded the intent to expose EMQX as a `LoadBalancer` with
`loadBalancerIP: 192.168.1.241`, but the live service is still `NodePort` and
nothing is listening on 192.168.1.241.

**Not fixed here.** The fix needs the per-node interface layout, which means
inspecting every node's interfaces rather than pi7's alone, and the
advertisement interface may differ per node. Guessing it would produce a broker
address that appears healthy and silently drops traffic. Flagged as the
remaining prerequisite for a stable, node-independent broker endpoint.

### (c) The D17 netpol fix is unaffected

Worth stating explicitly, because the endpoint the policy has to allow moves
when the advertised address does. The rule allows `10.0.0.0/16` and
`192.168.1.0/24` on 443/6443, so it covers the API server whether it is reached
at 10.0.0.1:6443 (the endpoint other nodes see) or 192.168.1.50:6443 (the LAN
address). `apiserver-egress` still passes and the pipeline is unaffected.

---

## D19 — full benchmark on the corrected pipeline, at both QoS levels

Run 2026-09-30, after every fix in D4–D18. This is the first result set in the
project produced by instrumentation that has been checked against the failure
modes found by auditing it.

**Conditions, all recorded in the summaries and all verified true:**

- `nats_purged_all_scenarios: true` — the JetStream stream was emptied before
  every scenario.
- `nats_drained_all_scenarios: true` — the consumer had acknowledged everything
  before each scenario was measured, so no scenario inherited another's backlog.
- 5 nodes, `v1.35.4+k3s1`, single version.
- 30 s per scenario, 15 s settle, 15 s cooldown.
- Latency metric `iot_sensor_latency_ms`, which after D11 is genuinely
  sensor-timestamp to VictoriaMetrics acknowledging the write.

### QoS 0 — `benchmarks/20260930_115549`

| scenario | stored | devices | msg/s | efficiency | p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|
| 10c_500r | 14,824 | 10 | 493.9 | 98.79% | 39 ms | 277 ms | 725 ms |
| 10c_1000r | 29,487 | 10 | 988.5 | 98.85% | 167 ms | 962 ms | 1,743 ms |
| 10c_2000r | 58,382 | 10 | 1,929.9 | 96.49% | 219 ms | 1,269 ms | 1,940 ms |
| 100c_500r | 14,958 | 100 | 490.5 | 98.09% | 55 ms | 450 ms | 914 ms |
| 100c_1000r | 29,849 | 100 | 960.3 | 96.03% | 202 ms | 1,707 ms | 2,304 ms |
| 100c_2000r | 59,334 | 100 | 1,936.2 | 96.81% | 565 ms | 8,931 ms | 9,531 ms |

Efficiency 96.03–98.85%, mean 97.51%. For comparison, the same harness reported
**6.01%** before the D9 export-scoping fix, and the pre-harness `run_test.sh`
corpus is not comparable at all (D4).

### QoS 1 — `benchmarks/20260930_120326`

| scenario | stored | devices | msg/s | efficiency | p99 |
|---|---|---|---|---|---|
| 10c_500r | 14,785 | 10 | 494.1 | 98.83% | 548 ms |
| 10c_1000r | 29,301 | 10 | 953.0 | 95.30% | 1,610 ms |
| 10c_2000r | 57,410 | 10 | 1,898.4 | 94.92% | 2,209 ms |
| 100c_500r | 14,974 | 100 | 501.3 | 100.26% | 920 ms |
| 100c_1000r | 29,848 | 100 | 960.2 | 96.02% | 3,896 ms |
| 100c_2000r | 57,701 | 100 | 1,924.5 | 96.22% | 4,132 ms |

### QoS 1 costs 0.59 percentage points of efficiency, on average

| scenario | QoS 0 | QoS 1 | delta |
|---|---|---|---|
| 10c_500r | 98.79% | 98.83% | +0.04 |
| 10c_1000r | 98.85% | 95.30% | −3.55 |
| 10c_2000r | 96.49% | 94.92% | −1.57 |
| 100c_500r | 98.09% | 100.26% | +2.17 |
| 100c_1000r | 96.03% | 96.02% | −0.01 |
| 100c_2000r | 96.81% | 96.22% | −0.59 |
| **mean** | **97.51%** | **96.92%** | **−0.59** |

This is the replacement evidence for D15. With a correct load generator, an
acknowledged publish costs well under one percent of throughput at every rate
tested, and the spread (−3.55 to +2.17) is the same order as run-to-run
variation. **Paper §V-C's "~50 msg/s per connection serialization ceiling" is
contradicted by measurement** and should be withdrawn rather than re-qualified:
the constraint was Paho C's default in-flight window of 10 in the load
generator, and it disappears once the window is widened.

The reason QoS 1 is nearly free is that the acknowledgement round trip is not
the bottleneck — EMQX and Benthos are. At 2,000 msg/s across 100 connections
there is enough queueing elsewhere to absorb one extra round trip.

### One anomaly worth reporting rather than smoothing

`100c_2000r` at QoS 0 has p95 8,931 ms and p99 9,531 ms, roughly four times the
QoS 1 figure for the same scenario (p99 4,132 ms) and three to five times every
other cell. Efficiency and throughput are unremarkable (96.81%, 1,936 msg/s), so
this is a latency-tail effect, not a throughput one.

It is most likely the Benthos MQTT shared-subscription group rebalancing across 5
replicas under the highest fan-in in the matrix, but that is a hypothesis, not a
measured cause. It is recorded as-is rather than re-run until it looks
reasonable, and it is the obvious first thing for the P2 fault-injection lab to
explain.

### Provenance

Both `results/` directories are committed (14 files, 152 kB). The ~600
historical summaries are **not**, and `.gitignore` explains why: they were
produced by the instrumentation that D4, D9, D11 and D15 show to be wrong, and
committing them unannotated would imply they are citable. The raw
VictoriaMetrics exports for all runs remain local (~3.3 GB) and are
regenerable.
