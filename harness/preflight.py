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


def check_cluster_reachable(result: PreflightResult) -> bool:
    """Returns True when the cluster answered, so callers can short-circuit."""
    code, out, err = _kubectl(["get", "nodes", "-o", "json", "--request-timeout=8s"])
    if code != 0:
        result.add(
            Check(
                "cluster-reachable",
                False,
                f"kubectl could not reach the API server: {err or 'unknown error'}. "
                "Component checks skipped -- they would all report the same cause.",
            )
        )
        return False
    result.add(Check("cluster-reachable", True, "kubectl can reach the API server"))

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


def run_preflight(components: bool = True) -> PreflightResult:
    result = PreflightResult()
    reachable = check_cluster_reachable(result)
    if not reachable:
        return result
    if components:
        for name, (ns, selectors) in PIPELINE_COMPONENTS.items():
            check_component(result, name, ns, selectors)
        check_network_policies(result)
    return result
