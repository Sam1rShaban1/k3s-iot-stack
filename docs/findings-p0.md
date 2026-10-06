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

---

## D20 — what actually drives the tail latency: the Benthos bridge, not IO

Asked directly after D19 left the 100c_2000r anomaly unexplained. This is the
measurement, and the answer is not disk.

### The tail is not IO, and not the write path

Split end-to-end latency using the timestamps the consumer already publishes.
`ts` is set by the publisher, `nats_exit_ts` when the consumer pulls the
message, `vm_write_ack_ts` after VictoriaMetrics accepts the write. Messages
are paired by the VictoriaMetrics sample timestamp, which is shared by every
series of one message.

100c_2000r, the worst cell (QoS 0):

| stage | p50 | p90 | p99 | max |
|---|---|---|---|---|
| publisher -> EMQX -> Benthos -> NATS | 495 ms | 6,798 ms | **9,400 ms** | 9,794 ms |
| consumer -> VictoriaMetrics | 17 ms | 49 ms | **456 ms** | 496 ms |
| end to end | 806 ms | 4,971 ms | 7,121 ms | 8,503 ms |

The storage path is 5% of the tail. For comparison, 10c_500r: ingest p99 358 ms,
write p99 6 ms.

**A methodology note, because it nearly produced the opposite answer.** The
first version of this analysis paired series by list position. The series are
returned in independent orders, so a positional zip pairs unrelated messages and
reports confident nonsense -- it gave a p99 of 0-1 ms for this very run, which
would have said "the pipeline is fast and the tail is elsewhere". Pairing on the
shared sample timestamp is the only correct alignment. `harness/stage_latency.py`
now does that, and its module docstring records the trap.

### Ruled out, each by measurement

| hypothesis | test | result |
|---|---|---|
| CPU saturation | `kubectl top` during a 100c_2000r run | 6-14% per node; nothing near a limit |
| disk / SD-card stall | write-stage p99 is 456 ms, max 496 ms | rejected |
| VictoriaMetrics ingest | write stage is p50 17 ms | rejected |
| the load generator | 100 publishers self-report 20.0 msg/s each, 0 errors, 0 reconnects | rejected |
| the WiFi hop (laptop to master) | ping under full load | p50 8 ms, p99 15 ms, max 16 ms, no loss |
| the consumer's fetch loop | `FETCH_TIMEOUT_S` reduced 1.0 -> 0.05 in D11 | already removed as a cause |

### The bridge is the constraint

Holding rate and client count fixed and varying only the number of Benthos
replicas consuming the MQTT shared subscription:

| Benthos replicas | ingest p99 | end-to-end p99 | throughput | efficiency |
|---|---|---|---|---|
| 1 | 8,761 ms | 8,814 ms | 1,919 msg/s | 95.97% |
| 5 | 9,400 ms | 9,262 ms | 1,930 msg/s | 96.26% |
| 10 | **1,923 ms** | **1,984 ms** | 1,968 msg/s | 98.38% |

A 4.5x reduction in tail latency from scaling the bridge alone. EMQX's
replica count is constant across all three rows, so a slow broker cannot explain
it; a slow bridge can, and does.

The mechanism is the shared subscription. Benthos subscribes as
`$share/benthos/sensors/#`, so EMQX hands each message to exactly one group
member, and at QoS 1 that member acknowledges back to EMQX per message. Each
replica therefore has a bounded message rate, and at 2,000 msg/s with few
replicas the per-replica queue is where the seconds accumulate. The stage label
"publisher -> EMQX -> Benthos -> NATS" is a compound of three hops; the
experiment above localises the cost to the Benthos end of it, not to the
publisher's WiFi or to EMQX's accept path.

### Consequences

- **For the cluster:** `replicas: 5` in the manifest is measurably
  under-provisioned for 100-client 2,000 msg/s workloads. 10 is 4.5x better on
  p99 and 2.4 points better on efficiency. The manifest is left at 5 to match
  Git and because changing it changes the system the paper describes, so this
  is a decision for the author, not a silent tuning change. Recorded as a
  concrete remediation candidate: "scale the streaming bridge to match the
  configured rate".
- **For the paper:** the 100c_2000r tail in D19 is not an anomaly to be
  smoothed. It is the bridge queueing, it is reproducible, and it is a
  configuration-dependent property rather than a fixed cost of the hardware.
  Reporting it with the replica count attached is more useful than reporting the
  better-behaved 10c_2000r cell alone.
- **For tooling:** `harness/stage_latency.py` makes this a one-command check
  for any past or future run, which is how a future regression in any stage
  would be attributed rather than guessed at.

### Not fixed here: the intra-ingest split

Adding a `benthos_entry_ts` processor would separate the MQTT hop from
Benthos-onwards, and the consumer has computed `benthos_to_nats_latency_ms` from
a field of that name since D11 -- but nothing ever set it.

Wiring it up **broke the pipeline**, and is worth recording because it failed
silently. Benthos's MQTT input root is the raw payload bytes, not parsed JSON,
so `root.benthos_entry_ts = ...` coerced each message into an object containing
only that field. The consumer received payloads with no `device_id` and no `ts`,
emitted a single series, and the scenario reported `stored=0, eff=0.0%` while
every pod stayed Running, preflight stayed green, and Benthos logged no error.
It needs `root = this.parse_json().assign(...)` and verification on a bench
before it goes near the live pipeline.

Also note that the ConfigMap edit did not trigger a Benthos rollout -- the pods
ran the previous day's config -- which is the D14 failure mode recurring in a
second component. Worth adding the same content-hash mechanism there.

---

## D20a — CORRECTION to D20: the Benthos scaling result was an artifact

**D20's conclusion was wrong and is withdrawn.** The claim that scaling the
bridge from 5 to 10 replicas cut tail latency 4.5x was not a measurement. It was
a broken experiment, and this entry records what happened so the error is not
re-derived later.

### What went wrong

`manifests/benthos/deployment.yaml` carried
`requiredDuringSchedulingIgnoredDuringExecution` anti-affinity — one Benthos per
node, hard. On a five-node cluster that caps the Deployment at 5 replicas.
Scaling to 10 therefore left 5 replicas **Pending**:

```
desired=10  ready=5  updated=5
Running: 5   Pending: 5
```

`kubectl rollout status` against that does not fail — it **times out**. The
experiment loop sent its output to `/dev/null` and never checked the exit
status, so the timeout was invisible, and the benchmark that followed ran with
5 replicas while being labelled "10".

So D20's table compared two 5-replica runs against each other:

| D20 claimed | actually measured |
|---|---|
| 1 replica  -> ingest p99 8,761 ms | 1 replica (correct) |
| 5 replicas -> ingest p99 9,400 ms | 5 replicas (correct) |
| 10 replicas -> ingest p99 **1,923 ms** | **5 replicas, rollout timed out** |

The "4.5x improvement" was run-to-run variance, and it was read as a causal
effect. D19 had already shown this cell swinging between roughly 2 s and 9.5 s at
a constant replica count; that spread is the same size as the "effect".

### The corrected experiment

Anti-affinity relaxed to `preferred`, replicas set explicitly, and the ready
count **read back and recorded** before every measurement. Three runs each:

| Benthos | ready (verified) | ingest p99 per run | median | efficiency per run | median |
|---|---|---|---|---|---|
| 5 | 5 | 2236, 3561, 3356 ms | 3356 ms | 100.0, 99.45, 98.81% | 99.45% |
| 10 | 10 | 3490, 2868, 2184 ms | 2868 ms | 99.15, 95.15, 98.66% | 98.66% |

**No significant difference.** The ranges overlap almost entirely on both
metrics. Ten bridge replicas is not better than five at 100 clients and
2,000 msg/s.

Relaxing the anti-affinity also turned out to be actively undesirable:
`preferred` alone placed all five replicas on pi2, the node with the most free
capacity. Required anti-affinity with 5 replicas is restored, giving one bridge
per node, which is the topology the cluster is built around.

### What D20 got right, and what is still open

Still correct, and independently established:

- The tail is **not** the storage path. Write stage p99 456 ms against ingest
  p99 9,400 ms on the same run.
- It is **not** CPU (6-14% per node during a run), **not** disk, **not**
  VictoriaMetrics (write p50 17 ms), **not** the load generator (100 publishers
  self-report 20.0 msg/s each, 0 errors), and **not** the network from the
  workstation to the broker (ping p99 15 ms under full load).
- It **is** in the stage `publisher -> EMQX -> Benthos -> NATS`, and that stage
  is where essentially all of the tail lives.

Still open: *which* hop inside that stage. Scaling Benthos does not move it, so
the remaining candidate is EMQX itself — a single pod, and therefore both a
serialisation point and a burst-queueing candidate, consistent with idle CPU and
high variance. That is a hypothesis, not a result. Settling it needs either the
`benthos_entry_ts` stamp (attempted in D20 and reverted: it silently destroyed
the pipeline because Benthos's MQTT root is raw payload bytes) or a second
instrumented hop at EMQX.

**Method note, which is the transferable part.** Two separate experiments this
session were invalidated by infrastructure state that looked like success:
`kubectl apply` reporting a successful rollout that changed no pods (D14), and
`kubectl rollout status` timing out on an unschedulable replica count while its
output was discarded (here). Both produced plausible numbers. The habit that
catches them is to read the *consequence* back — pod age, ready count,
ReplicaSet — and record it next to the result, rather than trusting the command's
exit narrative.

### Memory, for the record

The 8 GB per node constrained nothing here. With 10 Benthos replicas:

| node | memory used | % |
|---|---|---|
| pi2 | 1531 Mi | 19% |
| pi3 | 1989 Mi | 25% |
| pi4 | 1410 Mi | 18% |
| pi7 | 1961 Mi | 25% |
| raspberrypi | 5211 Mi | 66% |

Benthos runs at ~65 MiB resident against a 256 MiB request, so 10 replicas add
roughly 512 MiB of requests per node. `raspberrypi` is the memory-heaviest node
at 66%, carrying EMQX, ArgoCD, the local-path provisioner and Longhorn, and is
the one to watch if the bridge is ever scaled much further.

---

## D21 — every measurement was silently doubled by a lost shared subscription

Found on 2026-09-30, immediately after the cluster was recovered and the
air-gapped images finally delivered. It is the worst class of fault in this
project: nothing failed, and every derived statistic was wrong.

### Symptom

A 25 s, 500 msg/s, 100-client scenario reported **187% efficiency** — 24,930
messages stored where 12,500 were published:

| run | stored | devices | reported efficiency |
|---|---|---|---|
| 1 | 24,930 | 100 | 187.32% |
| 2 | 24,956 | 100 | 188.38% |
| 3 | 24,936 | 100 | 197.27% |

Perfectly reproducible, and close to exactly 2x. An impossible number was the
only symptom: every pod stayed Running, preflight was green, and the report
looked entirely plausible.

### Cause

The live `benthos-config` ConfigMap had drifted from Git:

| | live | Git |
|---|---|---|
| topics | `sensors/#` | `$share/benthos/sensors/#` |
| client_id | `benthos-consumer` | `benthos-consumer-${HOSTNAME}` |

Without the `$share/<group>/` prefix, MQTT delivers each message to **every**
matching subscription rather than splitting the group, so each message reached
the consumer more than once. The fixed `client_id` is worse on its own: the
broker disconnects the previous session each time a new replica connects.

The duplication is invisible in the obvious places, which is why it survived:

- `unique_devices` stayed correct at 100, because duplicates carry the same
  `device_id`;
- all four timestamp series held identical sample counts, so the D16
  duplication check passed — the duplication was upstream of the consumer, not
  in its rendering;
- throughput and latency were self-consistent, just computed over twice the
  real traffic.

### Why the drift happened

The ConfigMap's `last-applied-configuration` annotation still recorded the
correct `$share/` version while `data` held the wrong one, so the object had
been written by something that did not go through `kubectl apply` — or applied
from a different source. **Not established.** What is established is that the
effective configuration was not the version in Git, which is the same failure as
D12 (declared replicas != live replicas) and the reason the new detector reads
the live object rather than the manifest.

### Resolution

Applied the Git version, restarted Benthos (it does not hot-reload), and
confirmed:

| run | stored | devices | efficiency |
|---|---|---|---|
| 1 | 12,422 | 100 | 100.14% |
| 2 | 12,455 | 100 | 100.29% |

against 12,500 expected.

### New detector

`benthos-shared-subscription` (class 11) reads the **live** ConfigMap and fails
when there is more than one replica and either the topic filter has no
`$share/<group>/` prefix, or `client_id` is not unique per replica. A single
replica legitimately needs neither, so the check is replica-aware.

Seven tests cover the healthy case, each fault separately, both together, the
single-replica tolerance, and a missing ConfigMap.

The detector was verified against the live cluster by reverting the ConfigMap
to the broken form and watching it fail, then restoring from Git. One honest
detail: that live test only exercised the `client_id` half, because the topic
substitution in the throwaway copy silently did not apply. The `$share` half is
covered by unit test instead, which is the more reliable place for it.

### Corrected full matrix — `benchmarks/20261001_094417`

Re-run after the shared-subscription fix, superseding D19's numbers, which were
taken while messages were being duplicated. Same conditions as D19: 5 nodes,
single k3s version, 30 s per scenario, `purged_all` and `drained_all` true.

| scenario | stored | devices | msg/s | efficiency | p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|
| 10c_500r | 14,862 | 10 | 496.6 | 99.32% | 46 ms | 253 ms | 428 ms |
| 10c_1000r | 29,623 | 10 | 982.7 | 98.27% | 190 ms | 1,055 ms | 1,522 ms |
| 10c_2000r | 58,841 | 10 | 1,914.0 | 95.70% | 316 ms | 1,158 ms | 1,500 ms |
| 100c_500r | 14,904 | 100 | 493.2 | 98.63% | 47 ms | 229 ms | 630 ms |
| 100c_1000r | 29,860 | 100 | 992.4 | 99.24% | 178 ms | 831 ms | 1,153 ms |
| 100c_2000r | 59,594 | 100 | 1,988.7 | 99.44% | 311 ms | 5,051 ms | 6,163 ms |

Efficiency 95.70–99.44%, and every cell is now inside a plausible band.

**The D19 numbers for latency were also inflated, and not only by duplication.**
Comparing like for like:

| scenario | D19 p99 | corrected p99 |
|---|---|---|
| 10c_500r | 725 ms | 428 ms |
| 10c_1000r | 1,743 ms | 1,522 ms |
| 10c_2000r | 1,940 ms | 1,500 ms |
| 100c_500r | 914 ms | 630 ms |
| 100c_1000r | 2,304 ms | 1,153 ms |
| 100c_2000r | 9,531 ms | 6,163 ms |

Every tail improved, and 100c_2000r — the cell D20 was investigating — came
down by a third. Duplicated traffic loads the consumer twice, so the corrected
figures are the ones to quote, and D19's table should be treated as withdrawn
rather than as a second data point.

`100c_2000r` remains the one cell with a pronounced tail (p95 5,051 ms). That is
consistent with the D20 finding that the tail lives in the ingest path and is
not moved by scaling Benthos; it has not been re-investigated since the
duplication fix, and the intra-ingest split remains open.

### Corrected QoS 1 matrix — `benchmarks/20261001_113106`

Taken after the duplication fix, same conditions as the corrected QoS 0 matrix.

| scenario | QoS 0 msg/s | QoS 0 eff% | QoS 0 p99 | QoS 1 msg/s | QoS 1 eff% | QoS 1 p99 | delta eff |
|---|---|---|---|---|---|---|---|
| 10c_500r | 496.6 | 99.32 | 428 ms | 497.8 | 99.56 | 421 ms | +0.24 |
| 10c_1000r | 982.7 | 98.27 | 1,522 ms | 977.8 | 97.78 | 1,978 ms | −0.49 |
| 10c_2000r | 1,914.0 | 95.70 | 1,500 ms | 1,932.0 | 96.60 | 1,739 ms | +0.90 |
| 100c_500r | 493.2 | 98.63 | 630 ms | 499.5 | 99.89 | 954 ms | +1.26 |
| 100c_1000r | 992.4 | 99.24 | 1,153 ms | 972.0 | 97.20 | 2,211 ms | −2.04 |
| 100c_2000r | 1,988.7 | 99.44 | 6,163 ms | 1,911.7 | 95.58 | 3,623 ms | −3.86 |

QoS 0 mean 98.43%, QoS 1 mean 97.77%, difference **−0.66 points**, range −3.86
to +1.26.

This reproduces the pre-duplication D19 result (−0.59 points) on an independent
set of runs, which is the useful part: the conclusion that QoS 1 costs well
under a point of throughput at these rates does not depend on either the
duplication fault or a single lucky run. **Paper §V-C's "~50 msg/s per
connection serialization ceiling" remains contradicted**, and now by two
independent measurements.

One asymmetry worth recording rather than smoothing: at 100c_2000r, QoS 1 has a
*better* tail than QoS 0 (p99 3,623 ms against 6,163 ms) while being 3.86 points
worse on efficiency. Reliable per-message acknowledgement appears to change how
the ingest path queues rather than simply adding work — consistent with the
D20 finding that the tail lives upstream of NATS, where ack semantics would
matter most. Not explained, only observed.

---

## D22 — Prometheus was durable-less *and* blind

The `EmptyDir` TSDB was the known half of this. Fixing it exposed a second
half nobody had noticed, because the Prometheus pod had been reporting itself
healthy throughout.

### Durable storage

The `Prometheus` CR is rendered from the upstream `kube-prometheus-stack`
chart, which was given no storage parameters, so the TSDB landed on an
`emptyDir` and every pod restart destroyed all infrastructure metrics.

The field is **`spec.storage`**, not `spec.storageSpec`. A patch naming
`storageSpec` is accepted with `Warning: unknown field` and silently discarded,
which cost one round trip; the CRD for this operator version accepts only
`spec.storage`, with `volumeClaimTemplate` beneath it.

Now `retention: 15d` and a 5 GiB `ReadWriteOnce` claim on `local-path`.
`local-path` deliberately, not Longhorn: Longhorn's instance-manager image was
missing on three of five nodes for months, so depending on it for the metrics
store would repeat the mistake.

### The blind half

With the PVC in place and Prometheus actually scraping, only **3 of 30 targets
were up**. The `monitoring-network-policy` permitted a handful of ports:

| target | port | reachable? |
|---|---|---|
| apiserver | 6443 | yes |
| coredns metrics | 9153 | no — port absent |
| kube-state-metrics | 8080 | no — port absent |
| prometheus-operator | 8080 | no — port absent |
| kubelet | 10250 | no — port absent |
| node-exporter | 9100 | no — see below |

node-exporter was the instructive one. The policy *did* allow 9100, but with
`namespaceSelector: {}`, and node-exporter runs with `hostNetwork: true`, so it
listens on a **node** address. A namespaceSelector only ever matches pod IPs, so
no namespace rule can permit scraping a host-network target — it needs an
`ipBlock` covering the node networks.

So Prometheus had been running, reporting itself healthy, and collecting almost
nothing. This is the same failure as losing paper 1's metrics, one layer down:
the loss was noticed, the blindness that preceded it was not.

After adding `ipBlock` egress for `10.0.0.0/16` and `192.168.1.0/24` on 9100 and
10250, plus 8080 to `monitoring` and 9153 to `kube-system`:

| job | before | after |
|---|---|---|
| apiserver | 1/1 | 1/1 |
| kubelet | 0/15 | **15/15** |
| node-exporter | 0/5 | **5/5** |
| kube-state-metrics | 0/1 | **1/1** |
| prometheus-operator | 0/1 | **1/1** |
| prometheus (self) | 2/2 | 4/4 |
| coredns | 0/3 | 0/3 |
| **total** | **3/28** | **27/30** |

coredns metrics remain down. The 9153 rule targets `name: kube-system`, and the
likely cause is that the namespace carries only
`kubernetes.io/metadata.name=kube-system`, not a `name` label — which would
mean every `matchLabels: {name: ...}` selector in these policies matches
nothing, including the DNS rule that preflight credits as working. **Not
confirmed and not fixed here**; it is the next thing to check, because it would
affect several rules at once.

### Images the recreated StatefulSet needed

Switching to a PVC recreates the pod, which pulled two images the nodes had
never needed: `prometheus-config-reloader:v0.70.0` and
`prometheus:v2.48.1`. Both were delivered through the mirror from D21's
tooling. Worth expecting on any storage or version change: a StatefulSet
recreation is an image-pull event.

### The `name: kube-system` selector was inert

Fixing coredns turned out to be one label. The 9153 rule selected

```yaml
namespaceSelector:
  matchLabels:
    name: kube-system
```

and `kube-system` carries **only** `kubernetes.io/metadata.name`:

```
kube-system   {"kubernetes.io/metadata.name":"kube-system"}
monitoring    {"kubernetes.io/metadata.name":"monitoring","name":"monitoring",...}
```

`name` is a convention this project applied to its *own* namespaces; namespaces
created by Kubernetes get the `kubernetes.io/metadata.name` label instead. So
every selector of the form `matchLabels: {name: kube-system}` matched nothing
and the rule was inert — which is why coredns stayed at 0/3 while the preflight
DNS check reported the policy as fine, since that check looks for a bare
`podSelector`, not at whether the namespace selector resolves.

Final state, after correcting the selector:

| job | before | after |
|---|---|---|
| apiserver | 1/1 | 1/1 |
| coredns | 0/3 | **3/3** |
| kube-state-metrics | 0/1 | 1/1 |
| kubelet | 0/15 | 15/15 |
| prometheus-operator | 0/1 | 1/1 |
| prometheus (self) | 2/2 | 4/4 |
| node-exporter | 0/5 | 5/5 |
| **total** | **3/28** | **30/30** |

**The lesson generalises past this one label.** A NetworkPolicy rule whose
namespace selector matches nothing is indistinguishable, from the API, from a
rule that is doing its job — it parses, it applies, and it permits nothing.
Every policy in this cluster had been checked for *shape* (bare podSelector,
API-server egress) and never for whether its selectors actually resolve to
anything. A cheap detector would evaluate each egress rule's namespaceSelector
against the live namespace labels and fail when it selects zero namespaces; that
is the natural next detector after the eleven already in place, and it would
have caught this before it cost a Prometheus redeploy.

### Detector 12 — `policy-selectors-resolve`

Added, with six tests. It evaluates each NetworkPolicy egress rule's
`namespaceSelector` against live namespace labels and fails when a rule selects
zero namespaces, reporting the policy, namespace and selector so the offender is
named rather than counted.

Covered: inert `matchLabels`, resolvable selectors, empty selectors (which mean
"all namespaces" by definition and must not be flagged), `matchExpressions` for
all four operators (`In`, `NotIn`, `Exists`, `DoesNotExist`), `ipBlock` peers
(which have no namespace and cannot be inert), and namespace-listing failure
reported rather than raised.

`matchExpressions` is the subtle part. `NotIn` and `DoesNotExist` must *not*
raise when the key is absent — absence is exactly what they test for — so a
naive "key missing means no match" shortcut would invert their meaning and
manufacture false failures.

Preflight is now 15 checks; 146 tests pass.

---

## D23 — MetalLB was announcing on the wrong interface

The `L2Advertisement` named `eth0`, while the pool is `192.168.1.240-250`. But
`eth0` is the `10.0.0.0/24` segment; the nodes reach the home LAN on `wlan0`.
The speaker was therefore emitting ARP replies for an address belonging to
192.168.1.0/24 onto 10.0.0.0/24 — announcements no client on the home LAN would
ever accept.

The `interfaces` restriction is now removed entirely rather than corrected to a
different name. Two reasons:

1. **The name is not uniform.** The speaker is a DaemonSet, so it runs on all
   five nodes, and which interface fronts the LAN differs per node. Pinning
   `wlan0` would be correct only on some of them.
2. **The restriction is what made it break.** Letting MetalLB announce on every
   interface makes the advertisement follow the address instead of assuming
   where it lives.

Verified rather than assumed: a throwaway `LoadBalancer` service was assigned
`192.168.1.240`, and the workstation — on the home LAN, not in-cluster — fetched
it with `HTTP 200` in 60 ms. The probe was then removed.

Note the verification trap hit along the way. The first probe used
`registry.k8s.io/echoserver`, which this air-gapped cluster cannot pull, so the
VIP was assigned and correctly announced while nothing was listening behind it —
a connection refused that reads like a MetalLB failure. The address assignment
and the advertisement were both fine. Reusing an image already present
(`busybox:1.36`) separated the two questions. **EMQX stays on its NodePort**
(`192.168.1.50:31883`) for now: it is the path the benchmark harness uses, and
moving ingest ingress to a VIP is a change worth making on purpose rather than
as a side effect of fixing the announcer.

The 3446 restarts on `metallb-speaker-r5rdc` are historical, not current:
the logs are dated 2026-06-30 and are all `dial tcp 10.43.0.1:443: network is
unreachable`, i.e. a node that had lost contact with the API service IP. All
five speakers are Running now.

---

## D24 — Stage attribution: bench-testing the Benthos processor

The `benthos_entry_ts` processor that would split the ingest tail had been
attempted once and silently destroyed the pipeline. Two forms were wrong, and
the second was wrong in a way that would have shipped:

| form | result |
|---|---|
| `root.benthos_entry_ts = ...` | MQTT root is **raw bytes**, not an object. Coerced the message to a single field; consumer saw no `device_id`/`ts`; scenario reported `stored=0` at 0% efficiency with every pod Running and preflight green. |
| `root = this.parse_json().assign("k", v)` | `assign` in Benthos **4.1.0 takes one argument**, not a path and a value. `benthos lint` rejects it: *"wrong number of arguments, expected 1, got 2"*. |

The second form is what the code comment proposed as "the correct form". It was
never correct for this Benthos version — it would have been caught by `lint`
before it reached the cluster, had anyone run it. The working form parses first
and then sets the field on the parsed object:

```bloblang
root = this.parse_json()
root.benthos_entry_ts = timestamp_unix_nano() / 1000000
```

### The unit test is not a faithful substitute

`benthos test` exists and accepts a config with `tests:`, and it passes on the
working form. It also **passes on the broken form**, because in the unit-test
context the root is an object rather than bytes. So a green unit test would not
have distinguished the two — the failure is specific to the MQTT input's root
type.

Consequence: the live check is the real one. The symptom to watch is exactly
the one that caught the original — `stored=0`, `devices=0`, everything else
green.

### Verified

`benthos lint` clean on the extracted live config. Applied to the cluster, all
five Benthos pods restarted, and the pipeline stayed healthy:
98.84% efficiency, 20 devices, 24710 messages stored.

Note that applying the ConfigMap did **not** restart the pods — Benthos does not
watch the config unless started with `-w`, and Benthos here is a Deployment, not
the DaemonSet the first `kubectl rollout status` assumed (it failed with
`NotFound`). The pods were still running the old config while the pipeline
looked perfectly healthy. The field only began flowing after an explicit pod
delete. This is the same class of bug as the ConfigMap drift in D21: the live
workload and the declared config silently disagreeing, with every health signal
green.

### Latency at 100 clients / 2000 msg/s

```
  stage                                     p50      p90       p99       max
  publisher -> EMQX -> Benthos -> NATS      341     1427      3861      4502
  consumer -> VictoriaMetrics               26       54        55        55
  end to end                               766     1471      4393      4473
```

Still **97% of p99 inside the ingest path**, with the storage side at 55 ms. So
the D20 conclusion is now confirmed on clean state with a substantially better
operating point than when it was first measured.

**Not yet closed:** `benthos_to_nats_latency_ms` returns zero series, so the
intra-ingest split itself is still not visible. The consumer already computes
it when `benthos_entry_ts` is present (`consumer.py:152-153`), but the consumer
pods show `restart=0` and may still be running a pre-field build. Next step is
to roll the consumer and confirm the derived series appears. Until then the
suspect remains "EMQX or Benthos", not narrowed to one.

### D24 outcome: unresolved, and one of my corrections was wrong

This is recorded at length because the process was as informative as the
result, and because I retracted a correct diagnosis before reproducing it.

Four forms were tried against the **live** pipeline:

| form | result |
|---|---|
| `root.benthos_entry_ts = <ms>` | **Pipeline broken.** `stored=0`, `devices=0`, 0% efficiency, every pod Running, preflight green. |
| `root = this.parse_json()` | Logs `expected string value, got object from field this` on every message; pipeline still healthy (message passes through unmodified), but no field added. |
| `root = this.parse_json().assign("k", v)` | Rejected by `benthos lint` — `assign` takes one argument in 4.1.0. |
| `benthos test` unit tests | Pass on **both** the first and second forms. |

**I retracted the original diagnosis and was wrong to.** The standing comment
claimed the MQTT root is raw payload bytes. The logs seemed to contradict that —
`got object from field this` reads as `this` being an object, not bytes — so I
declared the comment wrong, switched to the bare `root.benthos_entry_ts = ...`
form, and reproduced the original failure exactly: `stored=0`, `devices=0`.

So the error text and the observed behaviour genuinely disagree. `this` is not a
string, yet assigning to `root` still collapses the message to a single field.
That contradiction is not resolved, and the pipeline is back to
`processors: []` — verified healthy at 95.72%.

The lesson is about method rather than Benthos: I twice changed a live
production path on the strength of an inference, and twice the cheap
verification existed — reproduce the failure, or read the input type — and would
have settled it. `benthos lint` caught form three for free. Nothing was
available for free on forms one and two except actually running them, which
takes about a minute.

Also worth recording: applying the ConfigMap does not restart Benthos. It does
not watch its config without `-w`, and it is a Deployment, so `kubectl rollout
status ds/benthos` fails `NotFound` while the pipeline looks perfectly healthy
on the old config. Any future change to `benthos.yaml` needs an explicit pod
delete, which is an easy thing to forget and gives no signal when you do.

**Still open, unchanged:** the 97%-of-p99 ingest tail is not yet split between
EMQX and Benthos. `stage_latency.py` remains accurate for the coarse split
(ingest vs storage) and is what the paper should cite.

---

## D25 — Three inert NetworkPolicies, and I broke the pipeline fixing them

Detector 12 was extended to resolve `podSelector` as well as
`namespaceSelector`, and immediately found three policies that selected nothing:

| policy | declared selector | pods actually carry |
|---|---|---|
| `benthos/benthos-network-policy` | `app.kubernetes.io/name: benthos` | `app: benthos` |
| `emqx/emqx-network-policy` | live had `app.kubernetes.io/name: emqx` | `app: emqx` |
| `victoriametrics/...` | live had `app.kubernetes.io/name: victoria-metrics-single` | `app: victoriametrics` |

A policy whose podSelector matches nothing is **inert in both directions**: it
permits everything while its author believes it restricts traffic. Those three
namespaces have had no network isolation at all.

The benthos one was a repeat. A previous fix had removed `app: benthos` on the
stated grounds that "the deployed Benthos carries app.kubernetes.io/name only" —
which is false. It read as fixed because `kubectl apply` said *unchanged* and the
object looked plausible.

Turning enforcement on is what broke the pipeline. With the benthos selector
corrected, the data path went to `stored=0, devices=0` **while all 15 preflight
checks still passed** — these policies had never been enforced, so they had
never been tested. Two further defects surfaced only once enforced:

- **No egress rule for Benthos → EMQX on 1883.** The file comment says "FIX:
  1883"; the rule was never written. Only the ingress side mentions 1883.
- **Egress to the NATS ClusterIP was unreachable.** The output URL is hardcoded
  to `nats://10.43.203.115:4222`, and Calico evaluates egress against the
  pre-DNAT destination. A ClusterIP is not a pod IP, so it matches no
  namespaceSelector. Same post-DNAT trap as the API-server egress in BUG 6, one
  layer over.
- **DNS was a bare podSelector**, selecting CoreDNS in the benthos namespace,
  where none exists.

### Current state: the pipeline is DOWN

`stored=0`. Benthos logs `Failed to connect to nats: no servers available`.
**I did not manage to restore it and I ran out of context before diagnosing it
properly.** What is known:

- It is **not** the benthos policy — that was deleted, and it still fails.
- It is **not** the nats policy — that was deleted, and it still fails.
- DNS from a pod in the benthos namespace resolves `nats.nats.svc.cluster.local`
  to `10.43.203.115` correctly, and that is still the Service ClusterIP.
- `nats-0` is Running with 3d19h uptime.

So the failure is downstream of NetworkPolicy and DNS. **Next thing to check is
NATS itself** — whether it is still accepting client connections on 4222 at
all, and whether `nats-0` is listening where the ClusterIP points.

### Live drift introduced

`benthos-network-policy` and `nats-network-policy` were **deleted live** during
the attempt to isolate the cause. Git still declares both. They should be
restored with:

    kubectl apply -f manifests/namespaces/network-policies.yaml

— but note the benthos one now has the corrected `app: benthos` selector, so it
**will** be enforced, and the two egress gaps above must be closed first or the
pipeline will break again.

### The lesson

This is the third instance of one pattern, and it is now expensive enough to be
worth naming plainly. **A check that reports "green" is not the same as a check
that would have noticed.** Fifteen preflight checks passed while the data path
was completely dead. The inert policies were invisible for months precisely
because they were never enforced, so their contents were never exercised — and
the fix that made them visible also activated months of untested configuration
at once.

The cheap discipline that would have caught this: after enabling any inert
policy, verify the *data path* end to end before treating it as a win, and never
bundle an enforcement change with a correctness change to the same object.

---

## D26 — The policies were never enforced, and ArgoCD was hiding it

Resolution of D25. The pipeline is back (95.28%, 15/15 preflight) with **all six
NetworkPolicies genuinely enforced for the first time in the project's life**.

### What actually caused the outage

Not NATS. Three separate things, found in this order:

**1. ArgoCD reverts live changes because it syncs from the *remote* Git, and
nothing had been pushed.** The remote was still at `b12be68` while local was 11
commits ahead. ArgoCD's repo-server reads GitHub, so every manual `kubectl apply`
this session — the corrected NetPols, the corrected Benthos config, the D23
emqx/MetalLB work — was faithfully reverted. That is why the live Benthos ConfigMap
kept showing `sensors/#` with `max_in_flight: 100` after I had "applied" the fixed
version. It looked like drift; it was ArgoCD doing exactly its job against a stale
remote.

The remote is SSH (`git@github.com`) and the local key is not registered, so
pushes failed. `gh auth setup-git` plus an explicit HTTPS push fixed it:
`b12be68..6af607c`, all 11 commits.

**2. `kubectl apply` merges `matchLabels` instead of replacing them.** Correcting
the benthos selector to `app: benthos` produced
`{app: benthos, app.kubernetes.io/name: benthos}` — which matches nothing, the
AND-semantics bug the file's own comment describes. Three-way merge adds keys; it
does not remove them. Needed an explicit JSON-patch `remove`.

**3. The node LAN IPs moved.** `raspberrypi`'s wlan0 is now `192.168.1.162`
(dynamic), and the broker resolved to `192.168.1.136`. Everything hardcoded to
`192.168.1.50` was stale. The probe-based broker discovery absorbed this; my
hardcoded `HARNESS_BROKER=192.168.1.241` did not, and I spent several runs
publishing into a dead VIP before noticing I was forcing the variable myself.

### The two rules that were actually missing

Once the policies were truly enforced, two real gaps appeared — neither had ever
been exercised:

- **Benthos → EMQX on 1883 had no egress rule.** The comment said "FIX: 1883"; the
  rule was never written.
- **VictoriaMetrics had no LAN ingress.** The benchmark harness runs on a laptop
  *outside* the cluster and reads results over the NodePort. With the policy inert
  that worked by accident. Enforcing it produced `stored=0` on every run while the
  consumer wrote happily to the ClusterIP and all 15 preflight checks stayed green
  — the measurement apparatus, not the pipeline, was severed.

Also fixed: the DNS rule I added used two separate `to:` peers, which is **OR**.
The bare-podSelector peer matched CoreDNS in the benthos namespace (none there) and
the namespaceSelector peer permitted *every* pod in kube-system. It worked and was
far wider than intended. One peer carrying both `namespaceSelector` and
`podSelector` is the AND form that "CoreDNS, and only CoreDNS" means.

And a self-inflicted one: I pushed `busybox:1.36` to the mirror from an amd64
Docker without running the platform check I had run for every other image. Nodes
served `exec format error` until I re-pushed via `crane pull --platform
linux/arm64`, which bypasses the local daemon entirely and is the better path.
`files/check_image_platform.py` exists precisely for this and I skipped it.

### The lesson, stated once

Four separate times in this session I concluded something was fixed or broken and
was wrong, every time because I trusted a status line instead of a data path:

| believed | actually |
|---|---|
| Benthos ConfigMap applied | ArgoCD reverted it (stale remote) |
| `kubectl apply` set the selector | merge added a key, still matched nothing |
| NATS was refusing connections | NATS was fine; the *harness read* path was severed |
| `stored=0` meant the pipeline was dead | pipeline fine, VM reads blocked by a newly-enforced policy |

`stored=0` is ambiguous — it means "no samples were read back", which conflates
"nothing was produced" with "nothing could be read". Splitting those into separate
failures would have shortened this entire investigation. That is a change to
`harness/run.py` and it is the obvious next one.

---

## D27 — Client ceiling measured, and a flaw in the efficiency denominator

Before quoting any 5k or 10k msg/s number, the load generator's own ceiling had
to be established. This is the check whose absence produced the fake "QoS
serialization ceiling" in paper §V-C — a client-side artifact recorded as a
system limit. It should not be optional.

### The publisher's ceiling

Single process, free-running (no delay), against EMQX over the LAN:

    achieved = 25,830 / 25,375 / 26,673 msg/s   (delay 10000 / 5000 / 2500 us)

So one publisher process saturates at roughly **26k msg/s**. 10k aggregate is
comfortably inside client capability — the client will not be the bottleneck at
the rates proposed.

### The delay mechanism works, with a caveat that matters

My first attempt passed `dev100` where `delay-us` belongs, so `atoi` returned 0
and the publisher free-ran. That looked like a load-generator bug and was my own
argument-order error. With correct ordering:

| per-device target | achieved | accuracy |
|---|---|---|
| 20 msg/s | 19.96 | 99.80% |
| 50 msg/s | 49.78 | 99.56% |
| 100 msg/s | 99.17 | 99.17% |
| 200 msg/s | 196.79 | 98.40% |

**The generator falls short, and the shortfall grows with per-device rate.**

### The flaw this exposes

`harness/report.py:186`:

    efficiency = (throughput / target_rate * 100.0)

`target_rate` is the **nominal** requested rate. The publisher already reports
what it actually achieved, and the harness never parses it. So every microsecond
the generator falls short is charged to the pipeline as packet loss, and reported
efficiency is capped by generator accuracy, not by the system under test.

Consequences for the proposed runs:

| scenario | per-device | ceiling on reported efficiency |
|---|---|---|
| 100c @ 5000/s | 50/s | 99.56% |
| 100c @ 10000/s | 100/s | 99.17% |

At 10k the reported figure could not distinguish a 99.2% pipeline from a 99.17%
generator artifact. That is not a usable measurement.

It also slightly taints existing results. The corrected matrices used 100c @
2000r, i.e. 20/s per device, ceiling 99.80%, against a measured mean of 98.43% —
so roughly 1.4 points of genuine loss with under 0.2 points of generator error.
The QoS conclusion (−0.66 points) is larger than that error but not enormously
so, and should be re-checked once the denominator is fixed.

### Two fixes, both worth doing

1. **Use the achieved rate as the denominator.** The publisher reports it; parse
   it. This is the principled fix and it makes every future number honest.
2. **Hold per-device rate low by adding clients.** 10k/s across 500 clients at
   20/s each returns the ceiling to 99.80%. Needs a check that the harness can
   supervise 500 processes.

Fix 1 is a change to `harness/runner.py` and `harness/report.py`. Until it lands,
any rate above ~2000 msg/s should be reported as "throughput and drop rate
against a nominal target, generator accurate to X%", not as efficiency.

### D27 resolution — the achieved-rate denominator, and a second metric

Three separate defects between "the publisher reports its rate" and "the report
is honest". Each had to be found separately because each masked the next.

**1. stdout went to DEVNULL.** `spawn_publishers` used
`stdout=subprocess.DEVNULL, stderr=subprocess.PIPE`, and the summary line
(`published=... achieved=... msg/s`) is printed to **stdout**. The harness
physically could not read it. Now captured.

**2. The publisher had no SIGTERM handler at all.** The harness stops publishers
with SIGTERM the instant the window closes. Default SIGTERM action killed the
process, so the summary on the normal exit path never ran. I had written a code
comment asserting the line "is reached from the SIGTERM handler" — there was no
such handler. Added one, plus file-scope summary state and a shared
`print_summary()`.

`fflush(stdout)` after the summary is required, not tidy: stdout is
block-buffered whenever it is not a tty, and under the harness it never is.

**3. `stop_publishers` reaped against a shared 5s budget.** It signalled and
waited on each process in turn, so with 100 publishers the first straggler
consumed the budget and the other 99 were SIGKILLed before printing. Now all are
signalled first, then reaped against one 30s deadline.

Verified on the cluster: 100/100 processes reporting, achieved 1997.94 of a
nominal 2000 (99.9% generator accuracy).

### The metric that was never measuring loss

With the denominator fixed, one run read `published=89390`, `stored=89390` —
identical — and efficiency still read **91.49%**. Nothing had been lost. The
entire gap was the observation window: throughput divides by the span of the
VictoriaMetrics samples, which includes the settle period and write lag, and so
is longer than the interval the generator actually published over.

That makes `efficiency` a window-alignment metric wearing a loss metric's name.
Added `delivery_ratio_pct` = stored / generator_published, which compares counts
and is therefore immune to window definitions:

    published by generator:  89526
    stored in VM:            89526
    DELIVERY RATIO:          100.0%   <- actual loss: none
    efficiency:              99.44%   (window 45.06s vs a 45s run)

Read `delivery_ratio_pct` first. `efficiency_pct` is retained because it is what
the earlier corrected matrices reported, and `efficiency_pct_nominal_basis` is
retained so those stay readable. Ten regression tests cover the basis selection,
the fallback when no generator data exists, zero-division, and the
perfect-delivery-despite-low-efficiency case.

---

## D28 — Saturation ladder: the ingest path fails between 3000 and 5000 msg/s

Run with the client count **held at 100** so rate is the only variable, and with
`delivery_ratio_pct` (D27) as the loss metric rather than efficiency.

| rate | generator achieved | accuracy | published | stored | **delivery** | p99 |
|---|---|---|---|---|---|---|
| 2000/s | 1997.9 / 2000 | 99.90% | 89,526 | 89,526 | **100.0%** | 3769 ms |
| 3000/s | — | ~99.7% | 134,174 | 134,174 | **100.0%** | 4987 ms |
| 5000/s | 4982.9 / 5000 | 99.66% | 222,966 | 88,957 | **39.9%** | 3661 ms |
| 10000/s @400c | — | — | — | 2,823 | **0.64%** | 12035 ms |

**The load generator is not the constraint.** At 5000/s it delivered 99.66% of
nominal while the pipeline delivered 39.9%, and the run at 3000/s was lossless
with the same client count. This is the check that was missing when the fake QoS
ceiling entered paper §V-C.

### Where the loss is, and where it is not

Not in NATS. `nats_retained` (88,957) equals `stored` (88,957) exactly, with
`unconsumed=0` and `nats_drained=True`. So the consumer is not dropping and not
lagging — every message that reached NATS was stored.

Not in the consumer's batching either: `BATCH_SIZE=5000` with an unbounded
`batch_queue`, which would grow memory rather than drop.

**The loss is between the MQTT broker and NATS.** 134,009 messages the publisher
sent never reached the JetStream stream. Benthos's input shows repeated
`Connection lost due to: EOF`, so the leading candidate is Benthos losing its
EMQX subscription under load — but that is not yet proven, and an earlier
attempt to localize it using Benthos's `input_received`/`output_sent` counters
was **invalid**: those counters are cumulative since pod start, not per run, so
the numbers I first read as this run's traffic were cumulative. The conclusion
happened to point the same way; the reasoning did not.

### What would settle it

1. **Benthos counter deltas, not absolutes.** Sample `input_received` and
   `output_sent` immediately before and after a run. The distinction between
   "Benthos never received them" and "Benthos received and failed to forward"
   is the whole question, and cumulative counters cannot answer it.
2. **EMQX's own dropped-message metric.** Its dashboard is currently
   LAN-blocked by its own NetworkPolicy (18083/8083 reachable only from the
   `monitoring` namespace), so the broker cannot account for what it dropped.
   That is the same class of gap as the VictoriaMetrics LAN ingress fixed in D26.
3. **Bracket the knee** at 3500/s and 4000/s, and 250c vs 100c at 5000/s, since
   the 5000/s run above is the only one that varied anything other than rate.

### Latency note

p99 does not degrade monotonically with load — 3769 ms at 2000/s, 4987 ms at
3000/s, 3661 ms at 5000/s, 12035 ms at 10000/s. The 5000/s figure is lower
because most messages were dropped rather than delayed, which is the expected
shape when a path fails by discarding instead of queueing. Latency percentiles
are only meaningful while delivery is near 100%, and should always be reported
next to the delivery ratio rather than on their own.
