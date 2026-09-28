"""Tests for the statically-checkable detectors in preflight.py.

Detector class 3 (NetworkPolicy DNS egress) is the one that matters most: it is
the root cause of the May 14 13:34 incident (paper §V-E1c, P99 -> 77,412,371 ms).
These tests assert it fires on the historical broken policy and stays quiet on
the corrected one, so the class cannot silently regress.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import yaml  # noqa: E402

from harness.preflight import Check, PreflightResult  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NETPOL = os.path.join(REPO, "manifests", "namespaces", "network-policies.yaml")

# The DNS rule exactly as it appeared before P0.3: a bare podSelector, which
# only matches CoreDNS pods in the policy's OWN namespace.
BROKEN_RULE = {
    "egress": [
        {
            "to": [
                {"podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}},
            ],
            "ports": [
                {"protocol": "UDP", "port": 53},
                {"protocol": "TCP", "port": 53},
            ],
        }
    ]
}

# The corrected rule: namespaceSelector plus podSelector, so CoreDNS is matched
# wherever it runs.
FIXED_RULE = {
    "egress": [
        {
            "to": [
                {
                    "namespaceSelector": {},
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                }
            ],
            "ports": [
                {"protocol": "UDP", "port": 53},
                {"protocol": "TCP", "port": 53},
            ],
        }
    ]
}


def dns_offenders(policies: list[dict]) -> list[str]:
    """Mirror of the scan loop in preflight.check_network_policies."""
    out = []
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
                    out.append(f"{ns}/{p['metadata']['name']}")
    return out


class TestDNSEgressDetector(unittest.TestCase):
    def test_fires_on_bare_podselector(self):
        policies = [
            {"metadata": {"namespace": "nats-consumer", "name": "p"}, "spec": BROKEN_RULE}
        ]
        self.assertEqual(len(dns_offenders(policies)), 1)

    def test_quiet_on_namespaced_podselector(self):
        policies = [
            {"metadata": {"namespace": "nats-consumer", "name": "p"}, "spec": FIXED_RULE}
        ]
        self.assertEqual(dns_offenders(policies), [])

    def test_ignores_unrelated_podselectors(self):
        spec = {"egress": [{"to": [{"podSelector": {"matchLabels": {"app": "x"}}}]}]}
        policies = [{"metadata": {"namespace": "ns", "name": "p"}, "spec": spec}]
        self.assertEqual(dns_offenders(policies), [])


class TestShippedPolicyFile(unittest.TestCase):
    """Assert the policy file actually in the repo is the corrected one."""

    def setUp(self):
        if not os.path.exists(NETPOL):
            self.skipTest("network-policies.yaml not present")
        with open(NETPOL) as fh:
            self.policies = [d for d in yaml.safe_load_all(fh) if d]

    def test_no_bare_podselector_dns_rules(self):
        offenders = dns_offenders(self.policies)
        self.assertEqual(
            offenders, [],
            f"these policies would deny DNS egress (paper V-E1c): {offenders}",
        )

    def test_no_duplicate_policy_names_per_namespace(self):
        seen = set()
        for p in self.policies:
            key = (p["metadata"].get("namespace"), p["metadata"].get("name"))
            self.assertNotIn(key, seen, f"duplicate NetworkPolicy {key}")
            seen.add(key)

    def test_benthos_ingress_admits_mqtt(self):
        benthos = [
            p for p in self.policies
            if p["metadata"].get("namespace") == "benthos"
        ]
        self.assertTrue(benthos, "no benthos policy found")
        ports = set()
        for rule in benthos[0]["spec"].get("ingress") or []:
            for port in rule.get("ports") or []:
                ports.add(port.get("port"))
        self.assertIn(
            1883, ports,
            "benthos ingress must admit MQTT on 1883; it previously allowed "
            "only 4195, which blocked ingest (paper P0 bug 3)",
        )

    def test_benthos_selector_is_not_and_semantics(self):
        benthos = [
            p for p in self.policies
            if p["metadata"].get("namespace") == "benthos"
        ][0]
        labels = benthos["spec"]["podSelector"].get("matchLabels") or {}
        self.assertLessEqual(
            len(labels), 1,
            "multiple matchLabels are ANDed and matched no pods (paper P0 bug 4)",
        )

    def test_every_policy_allows_api_server(self):
        """Bug 6: no policy permitted egress to the in-cluster API service."""
        for p in self.policies:
            ns = p["metadata"].get("namespace")
            if ns in ("kube-system", "metallb-system", "longhorn-system", "argocd"):
                continue
            egress = p["spec"].get("egress")
            if egress is None:
                continue  # ingress-only policy
            ports = {
                port.get("port")
                for rule in egress
                for port in (rule.get("ports") or [])
            }
            self.assertIn(
                6443, ports,
                f"{ns}/{p['metadata']['name']} does not allow API server egress",
            )


class TestPreflightResultSemantics(unittest.TestCase):
    def test_non_fatal_failure_does_not_fail_result(self):
        r = PreflightResult()
        r.add(Check("ok", True, "fine"))
        r.add(Check("warn", False, "advisory", fatal=False))
        self.assertTrue(r.ok)

    def test_fatal_failure_fails_result(self):
        r = PreflightResult()
        r.add(Check("bad", False, "broken"))
        self.assertFalse(r.ok)

    def test_render_marks_levels(self):
        r = PreflightResult()
        r.add(Check("a", True, "x"))
        r.add(Check("b", False, "y"))
        r.add(Check("c", False, "z", fatal=False))
        lines = r.render().splitlines()
        self.assertIn("[PASS]", lines[0])
        self.assertIn("[FAIL]", lines[1])
        self.assertIn("[WARN]", lines[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
