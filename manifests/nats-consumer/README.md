# nats-consumer

Consumes from the NATS JetStream stream `IOT_DATA` (subject `iot.data`) and writes
to VictoriaMetrics via Prometheus remote-write.

## Two implementations are present. Only one is deployed.

| Path | Files | Deployed? | Role |
|---|---|---|---|
| **Python** | `consumer.py`, `kustomization.yaml` (generates the ConfigMap), `Dockerfile.consumer` | **Yes** — `deployment.yaml` runs `python3 /opt/consumer/consumer.py` | The single supported consumer from P0 onward |
| **Go** | `consumer.go`, `go.mod`, `go.sum`, `Dockerfile` | **No** | Retained **unmodified** as the P5–P8 ablation reference |

### Why the Go path is retained but inactive

The Go consumer ran in the 1- and 2-node configurations; the Python consumer ran in
the 3- and 5-node configurations. Paper §VIII-A (Measurement Gaps) records that this
swap happened *at the same time as* adding the third node, which confounded the two
factors — so the paper cannot cleanly attribute the 2→3 node throughput jump.

Paper §VI-D separately establishes that the Python consumer's `asyncio` batching is
the mechanism that broke the QoS 2 serialization ceiling, so the Python path is the
one with a documented causal story and is therefore the one to keep.

**Do not delete `consumer.go` + `go.mod` + `go.sum` + `Dockerfile` during further
cleanup.** They are a self-consistent build unit kept for the ablation study. They are
intentionally excluded from `kustomization.yaml` and will not be rendered by ArgoCD.

To build the ablation image (requires a Go toolchain; not part of the default build):

```sh
cd manifests/nats-consumer && docker build -t nats-consumer:go .
```

## Layout note

`consumer.py` is the source of truth for the running Python consumer.
`kustomization.yaml` generates the `nats-consumer-script` ConfigMap from it, and
rewrites the Deployment's volume reference to the generated content-hashed
name, so an edit to the script always produces a real rollout.

This replaced an earlier arrangement in which the source was embedded inline in
a hand-written `configmap.yaml` with a second copy of the script kept beside it
for readability. Two failures came out of that, both observed rather than
hypothetical:

- the two copies drifted, so the file people read and reviewed was not
  necessarily the file that ran;
- editing `configmap.yaml` did not change the pod template, so `kubectl apply`
  reported `successfully rolled out` while every pod kept running the previous
  code. The bounded-retention fix was deployed that way and silently did
  nothing.

There is now one copy. `harness/validate.py` fails if a hand-written
`configmap.yaml` reappears, and `make render-manifests` fails if the
kustomizations do not render.

## Known defect (scheduled for P0.4)

The deployed consumer writes **no `msg_id` label**, but the benchmark reader in
`run_test.sh` (lines ~185-238) expects one and collapses every series to the key
`unknown`. This is the direct cause of the `total_messages: 1` result recorded in
`benchmarks/20260519_130906/results/summary.json`.

The reader will be made schema-agnostic (count series, never key on `msg_id`).
Separately, P0.4 will add a `vm_write_ack_ts` field so that end-to-end latency is
measured identically across all cluster configurations — see paper §VIII-A, which
notes that 5-node P99 was recorded as *inter-arrival time* rather than end-to-end
latency, making the columns incomparable.
