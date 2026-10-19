"""Pipeline preflight.

The previous implementation (run_test.sh:verify_pipeline) checked EMQX with
`-l app=emqx`. That label exists only in the orphaned manifests/emqx/*.yaml,
which was never rendered by ArgoCD. The chart-managed EMQX labels pods
`app.kubernetes.io/name: emqx`. So the check either matched nothing or, if it
matched, matched a resource ArgoCD no longer owned -- and the script tolerated
a benthos pod in CrashLoopBackOff, which is precisely the state a preflight
exists to catch.

Checks here are explicit about *why* they can fail, so a false negative is
distinguishable from an unreachable cluster.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# Component -> (namespace, candidate label selectors, in preference order).
#
# Two label conventions coexist in this cluster and both are legitimate:
#   app.kubernetes.io/name=<x>   Helm charts (EMQX, NATS, VictoriaMetrics)
#   app=<x>                      hand-written manifests (benthos, nats-consumer)
# Hardcoding one style produced false negatives during the P0.6 recovery --
# preflight reported "no pods match" for three components that were Running.
# So each component lists candidates and the first that matches wins; if none
# match, every candidate is reported so the label can be corrected rather than
# guessed at.
PIPELINE_COMPONENTS = {
    "emqx": (
        "emqx",
        ["app.kubernetes.io/name=emqx", "app=emqx", "app=emqx-host"],
    ),
    "benthos": ("benthos", ["app=benthos", "app.kubernetes.io/name=benthos"]),
    "nats": ("nats", ["app.kubernetes.io/name=nats", "app=nats", "app=nats-box"]),
    "victoriametrics": (
        "victoriametrics",
        ["app=victoriametrics", "app.kubernetes.io/name=victoria-metrics-single"],
    ),
    "nats-consumer": ("nats-consumer", ["app=nats-consumer"]),
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fatal: bool = True

    def line(self) -> str:
        mark = "PASS" if self.ok else ("FAIL" if self.fatal else "WARN")
        return f"[{mark}] {self.name}: {self.detail}"


@dataclass
class PreflightResult:
    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> None:
        self.checks.append(check)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.fatal)

    def render(self) -> str:
        return "\n".join(c.line() for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [
                {"name": c.name, "ok": c.ok, "fatal": c.fatal, "detail": c.detail}
                for c in self.checks
            ],
        }


def _kubectl(args: list[str], timeout: int = 20,
             stdin: str | None = None) -> tuple[int, str, str]:
    try:
        res = subprocess.run(
            ["kubectl", *args],
            capture_output=True,
            text=True,
            input=stdin,
            timeout=timeout,
            check=False,
        )
        return res.returncode, res.stdout, _clean_stderr(res.stderr)
    except FileNotFoundError:
        return 127, "", "kubectl not found"
    except subprocess.TimeoutExpired:
        return 124, "", "kubectl timed out"


def _clean_stderr(stderr: str) -> str:
    """Reduce client-go's multi-line discovery noise to one useful line.

    An unreachable cluster makes kubectl emit a client-go memcache warning per
    discovery attempt, so a single failing command can produce 20 lines. The
    actionable part is always the last non-empty line.
    """
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    return lines[-1]


def _diagnose_kubectl(err: str) -> str:
    """Turn a kubectl failure into something actionable.

    The vague version of this message cost real time. A kubeconfig that pointed
    at a file which had been deleted reported only "the server could not find
    the requested resource", which reads like a broken cluster rather than a
    broken client config. The two are distinguished here.
    """
    text = (err or "").strip()
    low = text.lower()

    if "the server could not find the requested resource" in low:
        probe = subprocess.run(
            ["kubectl", "config", "view", "--minify", "-o",
             "jsonpath={.clusters[0].cluster.server}"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        server = probe.stdout.strip()
        detail = (
            "kubectl reached no API group. That is a client-side problem, not a "
            "down cluster"
        )
        if probe.returncode != 0 or not server:
            detail += (
                ": kubectl cannot read a usable context (KUBECONFIG="
                f"{os.environ.get('KUBECONFIG', '<unset, using ~/.kube/config>')}"
                "). Check that the file exists and contains a current-context."
            )
        else:
            detail += (
                f". Context points at {server}; if that is right, the API server "
                "may be down or the token may have expired."
            )
        return detail

    if "connection refused" in low or "connection timed out" in low or "no route to host" in low:
        return (
            f"cannot reach the API server ({text}). Check that the control-plane "
            "node is up and reachable from this host."
        )

    if "unauthorized" in low or "forbidden" in low:
        return f"authenticated but not authorised ({text}); the kubeconfig token may have expired."

    return f"kubectl could not reach the API server: {text or 'unknown error'}"


def check_cluster_reachable(result: PreflightResult) -> bool:
    """Returns True when the cluster answered, so callers can short-circuit."""
    code, out, err = _kubectl(["get", "nodes", "-o", "json", "--request-timeout=8s"])
    if code != 0:
        result.add(
            Check(
                "cluster-reachable",
                False,
                _diagnose_kubectl(err)
                + " Component checks skipped -- they would all report the same cause.",
            )
        )
        return False
    context = subprocess.run(
        ["kubectl", "config", "current-context"],
        capture_output=True, text=True, timeout=20, check=False,
    ).stdout.strip()
    result.add(
        Check(
            "cluster-reachable",
            True,
            f"kubectl can reach the API server (context: {context or 'unknown'})",
        )
    )

    try:
        nodes = json.loads(out).get("items", [])
    except Exception as e:
        result.add(Check("cluster-parsable", False, f"could not parse node list: {e}"))
        return False

    not_ready = []
    versions = set()
    for n in nodes:
        meta = n.get("metadata", {})
        name = meta.get("name")
        for cond in n.get("status", {}).get("conditions", []):
            if cond.get("type") == "Ready" and cond.get("status") != "True":
                not_ready.append(name)
        versions.add(n.get("status", {}).get("nodeInfo", {}).get("kubeletVersion"))

    result.add(
        Check(
            "nodes-ready",
            not not_ready,
            f"{len(nodes) - len(not_ready)}/{len(nodes)} Ready"
            + (f"; not ready: {', '.join(not_ready)}" if not_ready else ""),
        )
    )

    # Detector class 4: version skew. Paper §V-E1(d) attributes a real
    # performance gap to mixed K3s versions, and it is statically checkable.
    if len(versions) > 1:
        result.add(
            Check(
                "node-version-consistent",
                False,
                f"MIXED KUBERLET VERSIONS {sorted(v for v in versions if v)} "
                "-- paper V-E1(d): causes intermittent ClusterIP resolution failures",
            )
        )
    else:
        result.add(
            Check(
                "node-version-consistent",
                True,
                f"single version {sorted(v for v in versions if v)}",
            )
        )

    # Must be an explicit True: this function's return value gates whether the
    # per-component checks run at all. Falling off the end returns None, which
    # is falsy, and silently reduces the whole preflight to three checks while
    # still exiting 0 -- a false pass.
    return True


def check_component(
    result: PreflightResult, name: str, namespace: str, selectors: list[str]
) -> None:
    """Try each candidate selector; report the first that matches any pod."""
    found_selector = None
    pods: list = []
    tried: list[str] = []

    for selector in selectors:
        tried.append(selector)
        code, out, err = _kubectl(
            ["get", "pods", "-n", namespace, "-l", selector, "-o", "json",
             "--request-timeout=8s"]
        )
        if code != 0:
            continue
        try:
            items = json.loads(out).get("items", [])
        except Exception:
            items = []
        if items:
            found_selector, pods = selector, items
            break

    if not pods:
        result.add(
            Check(
                name,
                False,
                f"no pods in namespace {namespace} match any of: {', '.join(tried)}",
            )
        )
        return

    ready = []
    bad = []
    for p in pods:
        pname = p.get("metadata", {}).get("name")
        phase = p.get("status", {}).get("phase")
        for cs in p.get("status", {}).get("containerStatuses", []) or []:
            state = (cs.get("state") or {}).get("waiting", {}).get("reason")
            if state:
                bad.append(f"{pname}:{state}")
            elif cs.get("ready"):
                ready.append(pname)
            else:
                bad.append(f"{pname}:{phase}/notready")

    detail = f"{len(ready)} ready ({', '.join(ready)}) [selector {found_selector}]"
    if ready and not bad:
        result.add(Check(name, True, detail))
    elif ready:
        result.add(Check(name, False, f"partially ready: {', '.join(bad)}", fatal=True))
    else:
        result.add(Check(name, False, f"none ready: {', '.join(bad) or 'unknown'}"))


def check_network_policies(result: PreflightResult) -> None:
    """Detector class 3.

    The May 14 13:34 incident (paper §V-E1c) was a NetworkPolicy whose DNS
    egress rule used a bare podSelector, which only matches CoreDNS pods in the
    policy's own namespace. CoreDNS lives in kube-system, so DNS egress was
    denied everywhere. The same mistake is statically detectable, so it is
    checked here rather than waiting for a 77-million-ms P99 to reveal it.
    """
    code, out, err = _kubectl(["get", "networkpolicy", "-A", "-o", "json"])
    if code != 0:
        result.add(
            Check("networkpolicy-dns", False, f"could not list NetworkPolicies: {err.strip()}")
        )
        return
    try:
        policies = json.loads(out).get("items", [])
    except Exception as e:
        result.add(Check("networkpolicy-dns", False, f"could not parse: {e}"))
        return

    offenders = []
    for p in policies:
        ns = p.get("metadata", {}).get("namespace")
        for rule in (p.get("spec") or {}).get("egress") or []:
            for peer in rule.get("to") or []:
                ps = peer.get("podSelector")
                if (
                    ps
                    and "namespaceSelector" not in peer
                    and (ps.get("matchLabels") or {}).get("k8s-app") == "kube-dns"
                ):
                    offenders.append(f"{ns}/{p['metadata']['name']}")

    if offenders:
        result.add(
            Check(
                "networkpolicy-dns",
                False,
                f"{len(offenders)} policy/policies allow DNS with a bare podSelector "
                f"(matches only the policy's own namespace): {', '.join(offenders)}. "
                "CoreDNS is in kube-system, so these deny DNS egress.",
            )
        )
    else:
        result.add(
            Check("networkpolicy-dns", True, f"no bare-podSelector DNS rules across {len(policies)} policies")
        )

    # Detector class 6: ephemeral observability state.
    code, out, _ = _kubectl(
        ["get", "statefulset", "prometheus", "-A", "-o", "json"]
    )
    if code == 0:
        try:
            sts = json.loads(out).get("items", [])
        except Exception:
            sts = []
        ephemeral = []
        for s in sts:
            for v in (s.get("spec", {}).get("template", {}).get("spec", {}).get("volumes") or []):
                if "emptyDir" in v:
                    ephemeral.append(f"{s['metadata']['name']}:{v['name']}")
        if ephemeral:
            result.add(
                Check(
                    "prometheus-durable",
                    False,
                    f"Prometheus uses ephemeral storage: {', '.join(ephemeral)}. "
                    "Paper §VIII-A: all historical infrastructure metrics were lost.",
                )
            )


def check_jetstream_retention(result: PreflightResult) -> None:
    """Detector class 7: unbounded stream growth.

    The IOT_DATA stream is created with retention=limits and no max_msgs,
    max_age or max_bytes. Nothing is ever evicted and acknowledging a message
    does not remove it, so the stream grows without limit.

    Measured on this cluster: 18,236 messages occupy 3.1 MB, about 170 B each.
    At a sustained 1000 msg/s that is roughly 14.7 GB/day, against a 57 GB SD
    card on pi7 that also carries K3s, Longhorn and the JetStream store. The
    failure mode is therefore a slow disk exhaustion, not a visible failure --
    which is exactly the class of problem worth detecting rather than
    discovering. See docs/findings-p0.md D13.
    """
    try:
        from . import nats as natsctl
    except Exception as e:  # noqa: BLE001
        result.add(Check("jetstream-retention", False, f"could not load nats probe: {e}"))
        return

    found = natsctl.stats()
    if found is None:
        result.add(
            Check("jetstream-retention", False, "could not read JetStream state")
        )
        return

    detail = (
        f"stream {natsctl._resolve_stream(natsctl.STREAM)}: "
        f"{found.retained} messages / {found.bytes_stored / 1e6:.1f} MB retained, "
        f"{found.unconsumed} unconsumed, retention={found.retention} "
        f"max_msgs={found.max_msgs} max_age={found.max_age} max_bytes={found.max_bytes}"
    )
    if found.unbounded:
        result.add(
            Check(
                "jetstream-retention",
                False,
                detail + " -- no limit is set, so stored data grows without bound "
                "and the node's disk will eventually be exhausted",
            )
        )
    else:
        result.add(Check("jetstream-retention", True, detail))

    # Unconsumed work is a separate question from retention, and it is the one
    # that invalidates a benchmark: it means a consumer is behind.
    if found.unconsumed:
        result.add(
            Check(
                "jetstream-drained",
                False,
                f"{found.unconsumed} messages published but not acknowledged "
                f"(num_pending={found.num_pending}, "
                f"num_ack_pending={found.num_ack_pending}); a consumer is behind",
            )
        )
    else:
        result.add(
            Check("jetstream-drained", True, "no unacknowledged messages")
        )


def check_airgap_images(result: PreflightResult) -> None:
    """Detector class 8: pods stuck pulling an image on an air-gapped cluster.

    The cluster has no registry route to the internet and Harbor is empty, so
    every image has to be side-loaded into each node's containerd by hand.
    Nothing enforces that, and scheduling does not care: a pod can land on a
    node that does not have its image and sit in ImagePullBackOff indefinitely.

    Observed on 2026-09-30: kube-state-metrics had been CrashLoopBackOff for
    ~15,000 restarts on `raspberrypi`. Deleting the pod to get a clean look at
    the real error rescheduled it to `pi3`, which does not have the image, and
    it became ImagePullBackOff -- so the crash loop had been hiding a second,
    different fault. ArgoCD's application-controller and Longhorn's
    instance-manager are in the same state.

    The check reports which images are in ImagePullBackOff and on which nodes,
    so the side-load list is derivable instead of guessed.
    """
    code, out, err = _kubectl(["get", "pods", "-A", "-o", "json"])
    if code != 0:
        result.add(Check("airgap-image-pull", False, f"could not list pods: {err.strip()}"))
        return
    try:
        pods = json.loads(out).get("items", [])
    except Exception as e:  # noqa: BLE001
        result.add(Check("airgap-image-pull", False, f"could not parse pods: {e}"))
        return

    stuck: dict[str, set] = {}
    for p in pods:
        statuses = p.get("status", {}).get("containerStatuses") or []
        pulling = any(
            (s.get("state") or {}).get("waiting", {}).get("reason")
            in ("ImagePullBackOff", "ErrImagePull")
            for s in statuses
        )
        if not pulling:
            continue
        for c in p["spec"].get("containers", []):
            stuck.setdefault(c.get("image", "?"), set()).add(p.get("spec", {}).get("nodeName", "?"))

    if not stuck:
        result.add(Check("airgap-image-pull", True, "no pods in ImagePullBackOff"))
        return

    detail = "; ".join(
        f"{img} on {', '.join(sorted(nodes))}" for img, nodes in sorted(stuck.items())
    )
    result.add(
        Check(
            "airgap-image-pull",
            False,
            f"{len(stuck)} image(s) cannot be pulled: {detail}. The cluster is "
            "air-gapped, so each image must be side-loaded into every node's "
            "containerd; a pod scheduled onto a node without it will not start.",
        )
    )


def _service_cidr() -> str:
    """The /16 containing the kubernetes Service ClusterIP, or '' if unknown.

    This is the range a NetworkPolicy must NOT rely on for API egress: Calico
    evaluates policy after kube-proxy's DNAT, so the destination address at
    evaluation time is the endpoint, not the ClusterIP.
    """
    code, out, _ = _kubectl(
        ["get", "svc", "kubernetes", "-n", "default",
         "-o", "jsonpath={.spec.clusterIP}"]
    )
    if code != 0 or not out.strip():
        return ""
    ip = out.strip()
    parts = ip.split(".")
    if len(parts) != 4:
        return ""
    return f"{parts[0]}.{parts[1]}.0.0/16"


def check_apiserver_egress(result: PreflightResult) -> None:
    """Detector class 9: policy must actually permit reaching the API server.

    Two plausible-looking rules were both wrong here, and each left every
    in-cluster API client broken with the same opaque error:

      * allowing `kube-system:6443` -- the kubernetes Service listens on 443,
        and has no backing pods to select, so this matches nothing;
      * allowing the service CIDR `10.43.0.0/16` on 443 -- Calico evaluates
        policy in the FORWARD chain, after kube-proxy's DNAT, so the
        destination it sees is the endpoint address, not the ClusterIP.

    The symptom is `dial tcp 10.43.0.1:443: connect: connection refused` from
    kube-state-metrics and the Prometheus Operator, and it is indistinguishable
    from a down API server unless you know to look at the policy.

    This checks that each egress policy carries an ipBlock covering a node
    network on 443/6443, which is the form that works with Calico.
    """
    code, out, err = _kubectl(["get", "networkpolicy", "-A", "-o", "json"])
    if code != 0:
        result.add(Check("apiserver-egress", False, f"could not list NetworkPolicies: {err.strip()}"))
        return
    try:
        policies = json.loads(out).get("items", [])
    except Exception as e:  # noqa: BLE001
        result.add(Check("apiserver-egress", False, f"could not parse: {e}"))
        return

    # The service CIDR is exactly the range that does NOT work, because Calico
    # sees the post-DNAT endpoint address. Derive it from the live Service
    # rather than hardcoding 10.43.0.0/16, so the check keeps working if the
    # service CIDR is ever renumbered.
    svc_cidr = _service_cidr()

    def _has_api_rule(spec: dict) -> bool:
        for rule in (spec.get("egress") or []):
            ports = {p.get("port") for p in (rule.get("ports") or [])}
            if not ({443, 6443} & ports):
                continue
            for peer in rule.get("to") or []:
                block = peer.get("ipBlock")
                if not block:
                    continue
                cidr = block.get("cidr", "")
                if svc_cidr and cidr == svc_cidr:
                    # Would only match the ClusterIP, which policy never sees.
                    continue
                if cidr.startswith(("10.", "192.168.")):
                    return True
        return False

    offenders = [
        f"{p['metadata'].get('namespace')}/{p['metadata']['name']}"
        for p in policies
        if not _has_api_rule(p.get("spec") or {})
    ]
    if offenders:
        result.add(
            Check(
                "apiserver-egress",
                False,
                f"{len(offenders)} policy/policies do not allow egress to the API "
                f"server on a node network: {', '.join(offenders)}. Components "
                "using inClusterConfig will fail with 'connection refused' to "
                "10.43.0.1:443. Needs an ipBlock on 443/6443; a namespaceSelector "
                "or the service CIDR alone does not work under Calico.",
            )
        )
    else:
        result.add(
            Check(
                "apiserver-egress",
                True,
                f"all {len(policies)} policies allow API server egress on a node network",
            )
        )


def check_benthos_subscription(result: PreflightResult) -> None:
    """Detector class 11: the bridge must use a shared MQTT subscription.

    Found on 2026-09-30, and it is the worst kind of fault: silent, and it
    corrupts every measurement rather than breaking the pipeline.

    The live ConfigMap had drifted from Git to subscribe on `sensors/#` instead
    of `$share/benthos/sensors/#`, and to use a fixed `client_id` shared by all
    five replicas. Without the shared-subscription group every replica receives
    every message instead of the group splitting them, so each scenario stored
    roughly twice its expected volume: 24,930 messages for a 25 s, 500 msg/s run
    that should have stored 12,500, reported as 187% efficiency. Throughput,
    latency and every derived statistic were wrong, and nothing failed -- all
    pods stayed Running, preflight was green, and the report looked plausible.

    Two things make this worth pinning:
      * a bare topic filter with multiple replicas duplicates delivery;
      * a client_id that is not unique per replica is worse still, because the
        broker disconnects the previous session each time a new one arrives.

    This checks the live ConfigMap rather than the Git copy, because the whole
    failure was that the two disagreed.
    """
    # The ConfigMap is generated by kustomize and therefore carries a content
    # hash suffix (`benthos-config-<hash>`), so it cannot be fetched by a fixed
    # name. It is resolved through the Deployment's volume reference instead,
    # which is the only authoritative link: it is the ConfigMap the pods
    # actually mount.
    code, out, err = _kubectl(
        ["get", "deployment", "benthos", "-n", "benthos",
         "-o", "jsonpath={.spec.template.spec.volumes[?(@.name==\"config\")].configMap.name}"]
    )
    cm_name = out.strip()
    if code != 0 or not cm_name:
        result.add(
            Check("benthos-shared-subscription", False,
                  f"could not resolve the benthos ConfigMap from the Deployment "
                  f"volume: {err.strip() or 'no volume named config'}")
        )
        return
    code, out, err = _kubectl(["get", "cm", cm_name, "-n", "benthos", "-o", "json"])
    if code != 0:
        result.add(
            Check("benthos-shared-subscription", False,
                  f"could not read the benthos ConfigMap {cm_name}: {err.strip()}")
        )
        return
    try:
        data = json.loads(out).get("data") or {}
    except Exception as e:  # noqa: BLE001
        result.add(Check("benthos-shared-subscription", False, f"could not parse: {e}"))
        return

    config = data.get("benthos.yaml") or ""
    replicas = 1
    code, out, _ = _kubectl(["get", "deploy", "benthos", "-n", "benthos",
                             "-o", "jsonpath={.spec.replicas}"])
    if code == 0 and out.strip().isdigit():
        replicas = int(out.strip())

    topics = re.findall(r"^\s*-\s*(\S+)\s*$", config, re.M)
    filters = [t for t in topics if "/" in t and not t.startswith("http")]
    shared = [t for t in filters if t.startswith("$share/")]
    client_id = ""
    m = re.search(r"client_id:\s*(\S+)", config)
    if m:
        client_id = m.group(1)

    problems = []
    if replicas > 1 and not shared:
        problems.append(
            f"{replicas} replicas subscribe to {filters or ['<none>']} with no "
            "$share/<group>/ prefix, so every replica receives every message "
            "and each scenario stores multiples of its true volume"
        )
    if replicas > 1 and client_id and "${" not in client_id and client_id != "$share":
        problems.append(
            f"client_id {client_id!r} is identical on all {replicas} replicas, so "
            "each connection displaces the previous session"
        )

    if problems:
        result.add(
            Check(
                "benthos-shared-subscription",
                False,
                "; ".join(problems) + ". The live ConfigMap has drifted from "
                "Git -- compare `kubectl get cm " + cm_name + " -n benthos -o "
                "yaml` with manifests/benthos/benthos.yaml.",
            )
        )
    else:
        result.add(
            Check(
                "benthos-shared-subscription",
                True,
                f"{len(shared) or 1} shared subscription(s), unique client_id "
                f"({client_id or 'unset'}) across {replicas} replicas",
            )
        )


def check_policy_selectors_resolve(result: PreflightResult) -> None:
    """Detector class 12: a policy rule that selects nothing is not a policy.

    Found on 2026-09-30 while fixing Prometheus. The monitoring policy had a
    rule selecting `namespaceSelector: {matchLabels: {name: kube-system}}`, and
    kube-system carries only `kubernetes.io/metadata.name` -- `name` is a
    convention this project applied to its own namespaces, not something
    Kubernetes puts on the ones it creates. The rule parsed, applied, and
    permitted nothing, so coredns metrics stayed at 0/3 while Prometheus
    reported itself healthy.

    The general hazard: from the API, a rule whose selector matches zero
    namespaces is indistinguishable from one that is working. It is accepted,
    it is enforced, and it allows nothing. Every earlier check here validated
    rule *shape* -- bare podSelector, API-server egress -- and never whether the
    selectors resolve, which is why this survived.

    This evaluates each egress rule's namespaceSelector against the live
    namespace labels and reports any that select nothing.
    """
    code, out, err = _kubectl(["get", "namespace", "-o", "json"])
    if code != 0:
        result.add(Check("policy-selectors-resolve", False,
                         f"could not list namespaces: {err.strip()}"))
        return
    try:
        namespaces = json.loads(out).get("items", [])
    except Exception as e:  # noqa: BLE001
        result.add(Check("policy-selectors-resolve", False, f"could not parse: {e}"))
        return
    labelled = [
        (n["metadata"]["name"], (n["metadata"].get("labels") or {}))
        for n in namespaces
    ]

    code, out, err = _kubectl(["get", "networkpolicy", "-A", "-o", "json"])
    if code != 0:
        result.add(Check("policy-selectors-resolve", False,
                         f"could not list NetworkPolicies: {err.strip()}"))
        return
    try:
        policies = json.loads(out).get("items", [])
    except Exception as e:  # noqa: BLE001
        result.add(Check("policy-selectors-resolve", False, f"could not parse: {e}"))
        return

    def matches(selector: dict, labels: dict) -> bool:
        """Does one namespace's label set satisfy this namespaceSelector?"""
        for key, want in (selector.get("matchLabels") or {}).items():
            if labels.get(key) != want:
                return False
        for expr in selector.get("matchExpressions") or []:
            op, key, values = expr.get("operator"), expr.get("key"), expr.get("values") or []
            present = key in labels
            if op == "In" and (not present or labels[key] not in values):
                return False
            if op == "NotIn" and present and labels[key] in values:
                return False
            if op == "Exists" and not present:
                return False
            if op == "DoesNotExist" and present:
                return False
        return True

    code, out, err = _kubectl(["get", "pods", "-A", "-o", "json"])
    pods: list[tuple[str, dict]] = []
    if code == 0:
        try:
            for pod in json.loads(out).get("items", []):
                pods.append((pod["metadata"].get("namespace", ""),
                             pod["metadata"].get("labels") or {}))
        except Exception:  # noqa: BLE001
            pods = []

    inert: list[str] = []
    total = 0
    for policy in policies:
        ns = policy["metadata"].get("namespace")
        name = policy["metadata"].get("name")
        for rule in ((policy.get("spec") or {}).get("egress") or []):
            for peer in (rule.get("to") or []):
                sel = peer.get("namespaceSelector")
                if sel is None:
                    continue
                total += 1
                # An empty selector means "all namespaces" by definition, and
                # that is a deliberate choice, not an accident.
                if not sel:
                    continue
                if not any(matches(sel, labels) for _, labels in labelled):
                    inert.append(f"{ns}/{name} rule namespaceSelector "
                                 f"{json.dumps(sel.get('matchLabels') or sel)} "
                                 f"selects no namespace")

        # A podSelector that matches nothing makes the whole policy inert. This
        # is the same failure as an unresolvable namespaceSelector and it is
        # worse, because it hides in both directions: an ingress policy that
        # selects no pod silently permits everything, while the author believes
        # it is restricting traffic. Found live on the emqx policy, where Git
        # declared podSelector: {} (all pods) but the cluster carried
        # matchLabels {app.kubernetes.io/name: emqx} -- a label the EMQX chart
        # does not put on its pods (it uses app: emqx).
        psel = (policy.get("spec") or {}).get("podSelector") or {}
        if pods and psel:
            if not any(ns == (policy["metadata"].get("namespace")) and matches(psel, labels)
                       for ns, labels in pods):
                inert.append(f"{ns}/{name} podSelector "
                             f"{json.dumps(psel.get('matchLabels') or psel)} "
                             f"selects no pod in its namespace")

    if inert:
        result.add(
            Check(
                "policy-selectors-resolve",
                False,
                f"{len(inert)} inert selector(s) out of {total} checked "
                f"namespaceSelector rules: {'; '.join(inert)}. Namespaces "
                "created by Kubernetes carry `kubernetes.io/metadata.name`, not "
                "`name`, so a selector on the latter is silently inert.",
            )
        )
    else:
        result.add(
            Check("policy-selectors-resolve", True,
                  f"all {total} namespaceSelector rules resolve to at least one namespace")
        )


DNS_PROBE_TIMEOUT_S = 150


def check_dns_from_every_node(result: PreflightResult) -> None:
    """Detector class 13: can a pod on EACH node actually resolve a name?

    Found on 2026-10-08. A node's `wlan0` dropped, NetworkManager wrote an empty
    `/etc/resolv.conf`, and that node's CoreDNS replica died with
    `plugin/forward: no nameservers found`. But the `kube-dns` Service still had
    three endpoints, so roughly one lookup in three was load-balanced onto a
    replica that could not forward.

    The symptom was maddeningly intermittent -- external lookups succeeded and
    failed on consecutive attempts -- and every other check stayed green. This
    is the failure rate of one dead replica of three, which is what gave it away
    once it was measured rather than guessed at.

    Every other check here runs from the control-plane node or from outside the
    cluster, so all of them can pass while a quarter of the cluster's pods cannot
    resolve anything. A check that only ever exercises the happy path cannot
    see that.

    Runs a throwaway pod on every node and asks it to resolve an external name,
    which exercises that node's DNS path end to end: pod -> kube-dns ->
    CoreDNS -> upstream resolver.
    """
    nodes = _kubectl(["get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"])[1].split()
    nodes = [n for n in nodes if n]
    if not nodes:
        result.add(Check("dns-per-node", False, "could not list nodes"))
        return

    IMAGE = "longhornio/longhorn-manager:v1.7.2"  # already side-loaded on all nodes
    def manifest_for(node: str) -> str:
        # nodeName goes in the manifest, not a follow-up patch: it is immutable
        # once the pod is scheduled, and the scheduler binds immediately.
        return f"""\
apiVersion: v1
kind: Pod
metadata:
  name: dnscheck-{node.replace('.', '-')}
  namespace: default
spec:
  nodeName: {node}
  restartPolicy: Never
  containers:
    - name: c
      image: {IMAGE}
      command: ["sh", "-c", "nslookup github.com >/dev/null 2>&1 && echo RESOLVED || echo FAILED; sleep 300"]
"""

    # All probes run concurrently. Doing them one node at a time meant the
    # later nodes were still Pending when the first one's result came back, so
    # the whole check took minutes and reported Pending as "could not test".
    pods = {n: f"dnscheck-{n.replace('.', '-')}" for n in nodes}
    for node, name in pods.items():
        _kubectl(["delete", "pod", name, "-n", "default",
                  "--ignore-not-found", "--wait=true", "--timeout=15s"])
    for node, name in pods.items():
        _kubectl(["apply", "-f", "-"], stdin=manifest_for(node), timeout=40)

    results: dict[str, str] = {}
    # Module-level so tests can shorten it; at 150s the "probe never ran" case
    # alone makes the suite take two and a half minutes.
    deadline = time.time() + DNS_PROBE_TIMEOUT_S
    while time.time() < deadline and len(results) < len(pods):
        for node, name in pods.items():
            if node in results:
                continue
            code, logs, _ = _kubectl(["logs", name, "-n", "default", "--tail=5"])
            if code == 0 and "RESOLVED" in logs:
                results[node] = "ok"
            elif code == 0 and "FAILED" in logs:
                results[node] = "broken"
        if len(results) < len(pods):
            time.sleep(3)

    broken: list[str] = []
    unchecked: list[str] = []
    for node, name in pods.items():
        _kubectl(["delete", "pod", name, "-n", "default",
                  "--ignore-not-found", "--wait=false"])
        if node not in results:
            code, out, _ = _kubectl(
                ["get", "pod", name, "-n", "default", "-o", "jsonpath={.status.phase}"]
            )
            code2, logs2, _ = _kubectl(["logs", name, "-n", "default", "--tail=3"])
            why = "no logs"
            if code2 == 0 and logs2.strip():
                why = logs2.strip().splitlines()[-1][:50]
            unchecked.append(f"{node} (phase={out.strip() or 'gone'}, {why})")
        elif results[node] == "broken":
            broken.append(node)

    parts = []
    if broken:
        parts.append(
            f"cannot resolve from: {', '.join(broken)} -- their pods have no working "
            "DNS path even though every other check passes"
        )
    if unchecked:
        parts.append(f"could not test: {', '.join(unchecked)}")
    if parts:
        result.add(Check("dns-per-node", False, "; ".join(parts)))
    else:
        result.add(
            Check("dns-per-node", True,
                  f"pods on all {len(nodes)} nodes resolve external names")
        )


ARGOCD_DELETION_STUCK_S = 900
ARGOCD_UNKNOWN_STUCK_S = 3600


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an RFC3339 timestamp from the Kubernetes API, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _human_age(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 60:.0f}m"


def check_argocd_applications(result: PreflightResult) -> None:
    """Detector class 14: an Application wedged in deletion wedges the controller.

    Found on 2026-10-16, after six months of a silently broken GitOps pipeline.
    A `longhorn` Application had carried `deletionTimestamp: 2026-04-02` since
    April. Its `longhorn-uninstall` pre-delete hook would never succeed --
    Longhorn refuses to uninstall while `deleting-confirmation-flag` is false --
    so the finalizer was never removed and the object could not be collected.

    The damage was not limited to that Application. The controller retried the
    doomed delete forever and the reconcile queue filled behind it: every other
    Application drifted to `Unknown`, and auto-sync was skipped with
    "Skipping auto-sync: application status is Unknown". The cluster looked
    fine -- pods running, requests served -- while GitOps had been dead for
    half a year.

    Nothing in this file looked at ArgoCD at all, so no check could see it.
    `kubectl get applications` showed `Unknown` for ten objects, which reads
    like a transient reconciliation blip rather than a six-month stall.

    Two conditions are reported:

      * deleting for longer than ARGOCD_DELETION_STUCK_S -- fatal. A delete
        that has not completed in fifteen minutes is not going to; the object
        needs its finalizers inspected by hand. This is the exact signature of
        the longhorn zombie.
      * `Unknown` with no reconcile for longer than ARGOCD_UNKNOWN_STUCK_S --
        non-fatal. Some Applications sit Unknown for legitimate reasons (this
        cluster's own `argocd` app reports a 404 on its Helm repo index), so
        failing here would mask the real signal. It is still worth surfacing:
        a genuinely wedged controller shows this on every Application at once.
    """
    # Absence of the CRD is not a fault: preflight also runs on clusters where
    # ArgoCD is not installed.
    code, _, _ = _kubectl(["get", "crd", "applications.argoproj.io"])
    if code != 0:
        result.add(Check("argocd-apps-settle", True,
                         "ArgoCD is not installed; nothing to inspect"))
        return

    code, out, err = _kubectl(
        ["get", "applications.argoproj.io", "-n", "argocd", "-o", "json"]
    )
    if code != 0:
        result.add(Check("argocd-apps-settle", False,
                         f"could not list Applications: {err.strip()}", fatal=False))
        return
    try:
        apps = json.loads(out).get("items", [])
    except Exception as e:  # noqa: BLE001
        result.add(Check("argocd-apps-settle", False,
                         f"could not parse Applications: {e}", fatal=False))
        return

    now = datetime.now(timezone.utc)
    stuck_deleting: list[str] = []
    stale_unknown: list[str] = []

    for app in apps:
        meta = app.get("metadata") or {}
        name = meta.get("name", "?")
        status = app.get("status") or {}

        deleted_at = _parse_ts(meta.get("deletionTimestamp"))
        if deleted_at is not None:
            age = (now - deleted_at).total_seconds()
            if age > ARGOCD_DELETION_STUCK_S:
                finals = ",".join(meta.get("finalizers") or []) or "none"
                stuck_deleting.append(
                    f"{name} (deleting {_human_age(age)}, finalizers: {finals})"
                )
            # A recent deletion is normal churn; do not also judge its sync state.
            continue

        if (status.get("sync") or {}).get("status") != "Unknown":
            continue

        # Prefer the controller's own reconcile stamp. When it is absent, fall
        # back to when the sync condition last changed, so an Application that
        # has been Unknown since creation is still caught.
        ref = _parse_ts(status.get("reconciledAt"))
        if ref is None:
            stamps = [
                _parse_ts(c.get("lastTransitionTime"))
                for c in (status.get("conditions") or [])
                if c.get("type") in ("Synced", "ComparisonError")
            ]
            stamps = [t for t in stamps if t is not None]
            ref = max(stamps) if stamps else None
        if ref is not None and (now - ref).total_seconds() > ARGOCD_UNKNOWN_STUCK_S:
            reason = ""
            for c in (status.get("conditions") or []):
                if c.get("message"):
                    reason = c["message"].splitlines()[0][:120]
                    break
            stale_unknown.append(
                f"{name} (Unknown {_human_age((now - ref).total_seconds())}"
                + (f": {reason}" if reason else "") + ")"
            )

    if stuck_deleting:
        result.add(Check(
            "argocd-apps-settle", False,
            f"{len(stuck_deleting)} Application(s) wedged in deletion: "
            f"{'; '.join(stuck_deleting)}. A finalizer that cannot succeed keeps "
            "the object alive and the controller retries it forever, which wedges "
            "reconciliation for every other Application. Inspect the finalizer "
            "before removing it.",
        ))
    elif stale_unknown:
        result.add(Check(
            "argocd-apps-settle", False,
            f"{len(stale_unknown)} Application(s) have not reconciled in over "
            f"{_human_age(ARGOCD_UNKNOWN_STUCK_S)}: {'; '.join(stale_unknown)}",
            fatal=False,
        ))
    else:
        result.add(Check(
            "argocd-apps-settle", True,
            f"all {len(apps)} Applications settled (none wedged in deletion)",
        ))


def run_preflight(components: bool = True) -> PreflightResult:
    result = PreflightResult()
    reachable = check_cluster_reachable(result)
    if not reachable:
        return result
    if components:
        for name, (ns, selectors) in PIPELINE_COMPONENTS.items():
            check_component(result, name, ns, selectors)
        check_network_policies(result)
        check_apiserver_egress(result)
        check_policy_selectors_resolve(result)
        check_dns_from_every_node(result)
        check_benthos_subscription(result)
        check_jetstream_retention(result)
        check_airgap_images(result)
        check_argocd_applications(result)
    return result
