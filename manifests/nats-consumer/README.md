# nats-consumer

Consumes from the NATS JetStream stream `IOT_DATA` (subject `iot.data`) and writes
to VictoriaMetrics via Prometheus remote-write.

## Two implementations are present. Only one is deployed.

| Path | Files | Deployed? | Role |
|---|---|---|---|
| **Python** | `consumer.py`, `configmap.yaml`, `Dockerfile.consumer` | **Yes** — `deployment.yaml` runs `python3 /opt/consumer/consumer.py` | The single supported consumer from P0 onward |
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

`configmap.yaml` embeds the Python consumer source inline; it is the live definition
of the running consumer. `consumer.py` is kept alongside it as the readable copy and
must be kept in sync — this duplication is tracked and will be collapsed in P0.3.

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
