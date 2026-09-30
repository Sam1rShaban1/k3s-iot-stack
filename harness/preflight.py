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
import subprocess
from dataclasses import dataclass, field
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


def _kubectl(args: list[str], timeout: int = 20) -> tuple[int, str, str]:
    try:
        res = subprocess.run(
            ["kubectl", *args],
            capture_output=True,
            text=True,
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
        check_jetstream_retention(result)
        check_airgap_images(result)
    return result
