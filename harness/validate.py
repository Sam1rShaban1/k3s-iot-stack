"""Static validation of the declarative configuration.

Everything the agent will later reason about is YAML, and every past incident
in this project was a YAML defect that ArgoCD could not see because the file
was orphaned, duplicated, or applied by hand. So the config tree gets the same
treatment as the code: syntax, structure, and a set of specific checks derived
from defects that actually occurred.

Run:  python3 -m harness validate
"""

from __future__ import annotations

import glob
import os
import sys
from dataclasses import dataclass

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files that must exist for the cluster to be describable. Several were
# orphaned at various points in P0, which is precisely the failure mode this
# project is about.
REQUIRED = [
    "argocd/root-application.yaml",
    "argocd/apps/kustomization.yaml",
    "manifests/namespaces/kustomization.yaml",
    "manifests/namespaces/namespaces.yaml",
    "manifests/namespaces/network-policies.yaml",
    "manifests/nats-consumer/kustomization.yaml",
    "manifests/nats-consumer/deployment.yaml",
    # The consumer script is the source of truth; the ConfigMap that the
    # Deployment mounts is generated from it by kustomize. A hand-written
    # configmap.yaml must NOT reappear: it is what allowed the script and the
    # running pods to diverge, and what made an edit fail to roll the pods.
    "manifests/nats-consumer/consumer.py",
    "manifests/benthos/kustomization.yaml",
    "publisher.c",
]

# ArgoCD Applications that exist but are never rendered, because they are
# absent from argocd/apps/kustomization.yaml. Detector class 9 (config drift).
KNOWN_ORPHANS = [
    "argocd/apps/loki",
    "argocd/apps/promtail",
    "argocd/apps/tempo",
    "argocd/apps/otel-collector",
    "argocd/apps/redpanda",
]


@dataclass
class Result:
    errors: list[str]
    warnings: list[str]
    infos: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        out = []
        for e in self.errors:
            out.append(f"[FAIL] {e}")
        for w in self.warnings:
            out.append(f"[WARN] {w}")
        for i in self.infos:
            out.append(f"[INFO] {i}")
        if not out:
            out.append("[PASS] configuration tree is consistent")
        return "\n".join(out)


def _load_all(path: str) -> list:
    with open(path) as fh:
        return [d for d in yaml.safe_load_all(fh) if d]


def validate(repo: str = REPO) -> Result:
    errors: list[str] = []
    warnings: list[str] = []
    infos: list[str] = []

    for rel in REQUIRED:
        if not os.path.exists(os.path.join(repo, rel)):
            errors.append(f"required file missing: {rel}")

    # The generated ConfigMap must not be reintroduced as a hand-written file.
    # It is not in REQUIRED, so its absence is fine; its presence is the bug,
    # because kustomize generates it and a second source drifts from the first.
    stale = os.path.join(repo, "manifests/nats-consumer/configmap.yaml")
    if os.path.exists(stale):
        errors.append(
            "manifests/nats-consumer/configmap.yaml must not exist: the "
            "ConfigMap is generated from consumer.py by kustomize. A "
            "hand-written copy is how the script and the running pods "
            "diverged, and how an edit failed to roll the pods."
        )

    # 1. Every YAML parses.
    patterns = [
        "argocd/**/*.yaml",
        "manifests/**/*.yaml",
        "files/*.yaml",
        "files/*.yml",
    ]
    yaml_files: list[str] = []
    for pat in patterns:
        yaml_files.extend(glob.glob(os.path.join(repo, pat), recursive=True))
    for path in yaml_files:
        try:
            _load_all(path)
        except Exception as e:  # noqa: BLE001
            errors.append(f"YAML does not parse: {os.path.relpath(path, repo)}: {e}")
    infos.append(f"{len(yaml_files)} YAML files parsed")

    # 2. No duplicate (namespace, name) pairs within a kind. This is the class
    #    of defect that made the old network-policies.yaml silently ambiguous.
    for kind, keyfn in (
        ("NetworkPolicy", lambda d: (d["metadata"].get("namespace"), d["metadata"].get("name"))),
        ("Namespace", lambda d: (d["metadata"].get("name"),)),
    ):
        for rel in ("manifests/namespaces/network-policies.yaml", "manifests/namespaces/namespaces.yaml"):
            path = os.path.join(repo, rel)
            if not os.path.exists(path):
                continue
            try:
                docs = _load_all(path)
            except Exception:
                continue
            seen: dict = {}
            for d in docs:
                if d.get("kind") != kind:
                    continue
                k = keyfn(d)
                if k in seen:
                    errors.append(
                        f"duplicate {kind} {k} in {rel} "
                        "(on apply, the last definition silently wins)"
                    )
                seen[k] = True

    # 3. Namespace labels the NetworkPolicies select on must exist.
    #    Namespaces created by Kubernetes itself or by a chart are not ours to
    #    declare, so they are exempt.
    BUILTIN_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "default"}

    ns_path = os.path.join(repo, "manifests/namespaces/namespaces.yaml")
    if os.path.exists(ns_path):
        declared = set()
        for d in _load_all(ns_path):
            if d.get("kind") == "Namespace":
                declared.add(d["metadata"]["name"])
        netpol_path = os.path.join(repo, "manifests/namespaces/network-policies.yaml")
        if os.path.exists(netpol_path):
            referenced: set = set()
            for d in _load_all(netpol_path):
                if d.get("kind") != "NetworkPolicy":
                    continue
                for direction in ("ingress", "egress"):
                    for rule in (d["spec"] or {}).get(direction) or []:
                        key = "from" if direction == "ingress" else "to"
                        for peer in rule.get(key) or []:
                            sel = (peer.get("namespaceSelector") or {}).get("matchLabels") or {}
                            if "name" in sel:
                                referenced.add(sel["name"])
            for ns in sorted(referenced - declared - BUILTIN_NAMESPACES):
                warnings.append(
                    f"NetworkPolicy references namespace label name={ns} but no "
                    "Namespace declares it; the rule will never match"
                )
            infos.append(f"namespace labels referenced: {len(referenced)}")

    # 4. Orphaned ArgoCD Applications: defined but never rendered.
    kust_path = os.path.join(repo, "argocd/apps/kustomization.yaml")
    if os.path.exists(kust_path):
        kust = _load_all(kust_path)[0]
        rendered = set()
        for r in kust.get("resources", []) or []:
            if isinstance(r, str) and r.endswith("application.yaml"):
                rendered.add(os.path.basename(os.path.dirname(r)))
        for orphan in KNOWN_ORPHANS:
            name = orphan.split("/")[-1]
            app_file = os.path.join(repo, orphan, "application.yaml")
            if os.path.exists(app_file) and name not in rendered:
                warnings.append(
                    f"ArgoCD Application '{name}' exists at {orphan} but is not in "
                    "argocd/apps/kustomization.yaml, so it is never rendered "
                    "(detector class 9: config drift)"
                )
        infos.append(f"ArgoCD Applications rendered: {sorted(rendered)}")

    # 5. The consumer's embedded Python must compile.
    cm_path = os.path.join(repo, "manifests/nats-consumer/configmap.yaml")
    if os.path.exists(cm_path):
        import ast

        try:
            src = _load_all(cm_path)[0]["data"]["consumer.py"]
            ast.parse(src)
        except Exception as e:  # noqa: BLE001
            errors.append(f"embedded consumer.py does not parse: {e}")
        else:
            infos.append("embedded consumer.py parses")

            # The copy on disk must match the ConfigMap, or the readable
            # artifact and the deployed artifact disagree.
            copy_path = os.path.join(repo, "manifests/nats-consumer/consumer.py")
            if os.path.exists(copy_path):
                # The marker includes its trailing newline, so the generated
                # body starts at the first real character of the source.
                marker = "# --- BEGIN GENERATED FROM configmap.yaml ---\n"
                body = open(copy_path).read().split(marker, 1)
                if len(body) == 2 and body[1] != src:
                    errors.append(
                        "manifests/nats-consumer/consumer.py is out of sync with "
                        "configmap.yaml; resync before trusting the readable copy"
                    )
                else:
                    infos.append("consumer.py is in sync with configmap.yaml")

    return Result(errors, warnings, infos)


if __name__ == "__main__":
    r = validate()
    print(r.render())
    sys.exit(0 if r.ok else 1)
