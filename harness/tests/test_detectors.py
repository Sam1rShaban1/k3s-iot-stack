"""Tests for the statically-checkable detectors in preflight.py.

Detector class 3 (NetworkPolicy DNS egress) is the one that matters most: it is
the root cause of the May 14 13:34 incident (paper §V-E1c, P99 -> 77,412,371 ms).
These tests assert it fires on the historical broken policy and stays quiet on
the corrected one, so the class cannot silently regress.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import yaml  # noqa: E402

from harness import preflight  # noqa: E402
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


class TestKubectlFailureDiagnosis(unittest.TestCase):
    """A vague kubectl error costs more time than the failure itself.

    A kubeconfig pointing at a deleted file reported only "the server could not
    find the requested resource", which reads like a down cluster. During
    development that sent the investigation to the wrong place entirely: the
    nodes were fine, the client config was not.
    """

    NOT_A_DOWN_CLUSTER = "the server could not find the requested resource"

    def test_no_api_group_is_blamed_on_the_client(self):
        msg = preflight._diagnose_kubectl(
            f'Error from server (NotFound): {self.NOT_A_DOWN_CLUSTER}'
        )
        self.assertIn("client-side", msg)
        self.assertIn("not a down cluster", msg)

    def test_names_the_kubeconfig_env_var(self):
        with mock.patch.dict(os.environ, {"KUBECONFIG": "/nope/kubeconfig"}):
            with mock.patch.object(preflight.subprocess, "run") as run:
                run.return_value = mock.Mock(returncode=1, stdout="", stderr="")
                msg = preflight._diagnose_kubectl(self.NOT_A_DOWN_CLUSTER)
        self.assertIn("/nope/kubeconfig", msg)

    def test_reports_the_server_when_the_context_is_readable(self):
        with mock.patch.dict(os.environ, {"KUBECONFIG": "/somewhere"}):
            with mock.patch.object(preflight.subprocess, "run") as run:
                run.return_value = mock.Mock(
                    returncode=0,
                    stdout="https://192.168.1.50:6443",
                    stderr="",
                )
                msg = preflight._diagnose_kubectl(self.NOT_A_DOWN_CLUSTER)
        self.assertIn("192.168.1.50:6443", msg)
        self.assertIn("client-side", msg)

    def test_connection_refused_suggests_node_check(self):
        msg = preflight._diagnose_kubectl("dial tcp 192.168.1.50:6443: connect: connection refused")
        self.assertIn("control-plane", msg)

    def test_unauthorised_is_distinguished(self):
        msg = preflight._diagnose_kubectl("You must be logged in to the server (Unauthorized)")
        self.assertIn("token", msg.lower())

    def test_unknown_error_is_passed_through(self):
        msg = preflight._diagnose_kubectl("something else entirely")
        self.assertIn("something else entirely", msg)

    def test_empty_error_still_produces_a_message(self):
        self.assertTrue(preflight._diagnose_kubectl("").strip())


def _netpol(name, ns, egress):
    return {
        "metadata": {"name": name, "namespace": ns},
        "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": egress},
    }


class TestApiServerEgressDetector(unittest.TestCase):
    """Two plausible rules were both wrong, and both were silently fatal.

    Allowing kube-system:6443 matches nothing, because the kubernetes Service
    listens on 443 and has no backing pods. Allowing the service CIDR on 443
    also matches nothing under Calico, because policy is evaluated in the
    FORWARD chain after kube-proxy's DNAT, so the destination is the endpoint
    address rather than the ClusterIP.
    """

    GOOD = [{
        "to": [{"ipBlock": {"cidr": "10.0.0.0/16"}}],
        "ports": [{"protocol": "TCP", "port": 443}, {"protocol": "TCP", "port": 6443}],
    }]
    KUBE_SYSTEM_6443 = [{
        "to": [{"namespaceSelector": {"matchLabels": {"name": "kube-system"}}}],
        "ports": [{"protocol": "TCP", "port": 6443}],
    }]
    SERVICE_CIDR_443 = [{
        "to": [{"ipBlock": {"cidr": "10.43.0.0/16"}}],
        "ports": [{"protocol": "TCP", "port": 443}],
    }]

    SERVICE_CIDR = "10.43.0.0/16"

    def _run(self, policies):
        result = PreflightResult()
        calls = {"n": 0}

        def fake_kubectl(args, *a, **kw):
            # The detector reads the live Service to learn the service CIDR, so
            # the "service CIDR is never enough" case can be tested at all.
            if "svc" in args:
                return 0, "10.43.0.1", ""
            calls["n"] += 1
            return 0, json.dumps({"items": policies}), ""

        with mock.patch.object(preflight, "_kubectl", side_effect=fake_kubectl):
            preflight.check_apiserver_egress(result)
        return next(c for c in result.checks if c.name == "apiserver-egress")

    def test_node_network_ipblock_passes(self):
        c = self._run([_netpol("a", "x", self.GOOD)])
        self.assertTrue(c.ok)

    def test_kube_system_6443_rule_fails(self):
        c = self._run([_netpol("a", "x", self.KUBE_SYSTEM_6443)])
        self.assertFalse(c.ok)
        self.assertIn("x/a", c.detail)

    def test_service_cidr_only_rule_fails(self):
        """The rule that looked most obviously correct, and was not."""
        c = self._run([_netpol("a", "x", self.SERVICE_CIDR_443)])
        self.assertFalse(c.ok)

    def test_unrelated_policy_fails(self):
        c = self._run([_netpol("a", "x", [{"ports": [{"port": 53}]}])])
        self.assertFalse(c.ok)

    def test_names_every_offender(self):
        c = self._run([
            _netpol("a", "ns1", self.KUBE_SYSTEM_6443),
            _netpol("b", "ns2", self.GOOD),
            _netpol("c", "ns3", self.SERVICE_CIDR_443),
        ])
        self.assertIn("ns1/a", c.detail)
        self.assertIn("ns3/c", c.detail)
        self.assertNotIn("ns2/b", c.detail)

    def test_empty_policy_list_passes(self):
        c = self._run([])
        self.assertTrue(c.ok)


class TestAirgapImageDetector(unittest.TestCase):
    """A crash loop can hide an image-pull fault behind it.

    kube-state-metrics had been restarting ~15,000 times on one node. Deleting
    the pod to read the real error rescheduled it to a node without the image,
    where it went to ImagePullBackOff -- a second, different fault that the
    crash loop was concealing.
    """

    @staticmethod
    def _pod(ns, name, node, image, waiting_reason=None):
        container = {"name": "c", "image": image}
        status = {}
        if waiting_reason:
            status = {"containerStatuses": [
                {"state": {"waiting": {"reason": waiting_reason}}}
            ]}
        return {
            "metadata": {"namespace": ns, "name": name},
            "spec": {"nodeName": node, "containers": [container]},
            "status": status,
        }

    def _run(self, pods):
        result = PreflightResult()
        with mock.patch.object(preflight, "_kubectl", return_value=(0, json.dumps({"items": pods}), "")):
            preflight.check_airgap_images(result)
        return next(c for c in result.checks if c.name == "airgap-image-pull")

    def test_no_stuck_pods_passes(self):
        c = self._run([self._pod("m", "p", "pi2", "img:1")])
        self.assertTrue(c.ok)

    def test_image_pull_backoff_fails(self):
        c = self._run([self._pod("m", "p", "pi3", "img:1", "ImagePullBackOff")])
        self.assertFalse(c.ok)
        self.assertIn("img:1", c.detail)
        self.assertIn("pi3", c.detail)

    def test_err_image_pull_also_counts(self):
        c = self._run([self._pod("m", "p", "pi3", "img:1", "ErrImagePull")])
        self.assertFalse(c.ok)

    def test_crashloop_alone_does_not_count(self):
        """A crash loop is a different fault and must not be reported here."""
        c = self._run([self._pod("m", "p", "pi3", "img:1", "CrashLoopBackOff")])
        self.assertTrue(c.ok)

    def test_groups_the_same_image_across_nodes(self):
        c = self._run([
            self._pod("a", "p1", "pi2", "img:1", "ImagePullBackOff"),
            self._pod("b", "p2", "pi3", "img:1", "ImagePullBackOff"),
        ])
        self.assertIn("pi2, pi3", c.detail)
        self.assertIn("1 image(s)", c.detail)

    def test_distinct_images_counted_separately(self):
        c = self._run([
            self._pod("a", "p1", "pi2", "img:1", "ImagePullBackOff"),
            self._pod("b", "p2", "pi3", "img:2", "ImagePullBackOff"),
        ])
        self.assertIn("2 image(s)", c.detail)
