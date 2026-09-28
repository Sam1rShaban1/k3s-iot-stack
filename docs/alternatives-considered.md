# Alternatives Considered and Rejected

This file preserves the evaluation rationale for components that were configured,
benchmarked against, and then **replaced**. It exists so the paper's §VII-B claim
("Alternative Components Not Tested") remains auditable after the Helm values files
were removed from the repository during the P0 cleanup.

The rejected values files were removed in P0 because they were never rendered by any
ArgoCD `Application`. Their substance is recorded below.

---

## Summary

| Candidate | Role it would have filled | Replaced by | Rejection reason |
|---|---|---|---|
| Apache Kafka (+ ZooKeeper) | Durable message buffer | **NATS JetStream** | 6 JVMs (3 brokers + 3 ZK) at 512 MiB heap each ≈ 2 GB of heap on ARM. Consumes a quarter of an 8 GB Pi before storing a single IoT reading. |
| Apache IoTDB | Time-series storage | **VictoriaMetrics** | 2 replicas × 768 MiB JVM heap. Comparable memory profile to Kafka; no InfluxDB-compatible PromQL, so it forfeits the existing Grafana ecosystem. |
| Apache NiFi | Stream processing | **Benthos** | 2 replicas × 768 MiB Java heap. Benthos is a single static Go binary under 50 MB. |
| InfluxDB | Time-series storage | **VictoriaMetrics** | Higher resident memory on ARM64 and poorer high-frequency write compression. |
| Mosquitto / HiveMQ | MQTT broker | **EMQX** | No shared-subscription support (`$share/…`), which Benthos needs for parallel consumption. EMQX also provides the Lua hook used for per-hop latency injection. |
| Standard Kubernetes (kubeadm) | Orchestration | **K3s** | 1–2 GB control plane vs. K3s's 300–400 MB, and it does not fit the per-node memory budget. |
| Layer-3 routed network | Inter-pod transport | **Layer-2 switch** | Adds routing overhead to every inter-pod packet, which is not free on ARM. |

---

## Retained rationale worth restating

Two of these rejections are load-bearing for the paper's central claim, and are worth
keeping in the repository in prose form rather than only in the PDF:

1. **Kafka is a memory problem, not a throughput problem.** The rejection is
   arithmetic (6 JVMs × 512 MiB on an 8 GB node), not a judgement about Kafka. Anyone
   re-reading the paper should be able to verify the claim without counting ZooKeeper
   pods. → See `files/` history at commit `c6faab5`.

2. **Protocol choice dominates compute.** The QoS 2 serialization ceiling
   (≈ 50 msg/s per connection at ~20 ms round-trip) was the single largest constraint
   in the 1- and 2-node configurations, and it was resolved by changing QoS 2 → QoS 0,
   *not* by adding nodes. This is the empirical seed for the follow-on work on
   automated configuration diagnosis.

---

## Rejected *after* deployment (different category)

These were live at some point and removed for cause, not for architecture:

| Component | Reason for removal |
|---|---|
| `files/emqx-values.yaml` | Superseded by inline `helm.values` in `argocd/apps/emqx/application.yaml`. Contained the EMQX factory-default dashboard password (`changeme`), which was never the deployed credential. |
| `manifests/nats/pod-nats-simple.yaml` | A single-Pod NATS manifest with no `kustomization.yaml`, never referenced by any ArgoCD `Application`. Applied out-of-band at least once, producing an unmanaged `nats-simple` Pod alongside the real chart-managed StatefulSet. |
| `manifests/emqx/{pod,statefulset,service,service-headless}.yaml` | Same failure mode: no `kustomization.yaml`, no external references, superseded by the EMQX Helm chart. |
| 7 of 8 `bench_*.py` scripts | Near-duplicate benchmark drivers, differing only in which metrics they queried and how they aggregated. Collapsed into a single `harness/` package. `bench_simple.py` was retained as the reference implementation. |
| Go consumer build path (`deployment-go.yaml`, `test.go`, `consumer.gz`, `go.mod`, `go.sum`, `Dockerfile`) | The Go consumer ran only in the 1- and 2-node configurations. Paper §VIII-A records that switching Go → Python at the same time as adding the third node confounded the two factors. The Python consumer is the single supported path from P0 onward; `consumer.go` is retained unmodified as the P5–P8 ablation reference. |

---

## Paper / repository discrepancies found during P0

Three claims in the paper are not supported by the repository as it stood. All three
were found while preparing the P0/P1 rebuild and are recorded here because paper 2
will be reviewed alongside paper 1.

### D1 — "default-deny ingress" never existed

Paper §III-I: *"All namespaces run with default-deny ingress. Explicit NetworkPolicy
objects permit only the required communication paths."*

There was no default-deny policy anywhere in the repository. Worse, the file that
*would* have contained them was absent from
`manifests/namespaces/kustomization.yaml`, so the policies that did exist were applied
by hand rather than reconciled by ArgoCD.

Resolved in P0.3: `manifests/namespaces/network-policies.yaml` corrected and wired
into the kustomization; `manifests/namespaces/default-deny.yaml` created but
**deliberately not enabled** until it can be verified against a live cluster (P1),
because turning it on is a behaviour change from the state the paper measured.

### D2 — the EMQX broker address could not have been what the paper says

Paper §III-C: EMQX is "exposed via MetalLB on 192.168.1.241:1883".

The repository said otherwise:
- `argocd/apps/emqx/application.yaml` declared `service.type: NodePort`, which
  MetalLB does not assign an address to, and which allocates from 30000–32767.
- The orphaned `manifests/emqx/service-emqx.yaml` pinned `nodePort: 31883`.
- `run_test.sh` connected to `192.168.1.50:1883` — the master's own IP on the
  standard MQTT port, which no declared Service produced.
- `manifests/emqx/statefulset-emqx.yaml` pinned `replicas: 2` to
  `nodeName: raspberrypi`, which cannot have been the source of a host-level 1883
  binding (two pods cannot share one host port).

So the port-1883 path the harness used is **not reproducible from the declared
configuration**. This is precisely the failure mode paper §III-A credits GitOps with
eliminating — "attributed to a specific commit rather than to ambient cluster drift" —
so it is worth being explicit that ambient drift was in fact still present in the
broker's exposure.

Resolved in P0.3: the EMQX app now declares `type: LoadBalancer` with
`loadBalancerIP: 192.168.1.241`, making the paper's claim true and giving the harness
a stable address. **Verify on first rebuild** that the chart version accepts
`loadBalancerIP`; if not, use the `metallb.io/loadBalancerIPs` annotation and record
the deviation here.

### D3 — the workload generator is described differently from how it is written

Paper §IV-E describes a publisher that "spawns one POSIX thread per simulated device",
each running `while (elapsed < duration)`.

`publisher.c` is **single-threaded**: one OS process per simulated device, running an
unbounded `while (1)` loop, terminated externally by `pkill -f publisher`. There is no
thread creation and no duration handling in the C source.

Not a correctness problem — the aggregate load shape is equivalent — but the released
artifact and the paper must agree. P0.5 adds `--duration` to the publisher and the
paper text is corrected to describe process-per-device.

