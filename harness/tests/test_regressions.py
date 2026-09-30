"""Regression tests for the measurement defects found in P0.

These exist because D4 (the "incomplete" May 19 run) sat undetected in the
corpus for the life of the paper: the reader silently produced a plausible
looking summary.json with total_messages: 1 rather than failing. A test that
only checks for exceptions would not have caught it.

Run with:  python3 -m pytest harness/tests/ -q
       or: python3 harness/tests/test_regressions.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness import config  # noqa: E402
from harness.collect import Sample, parse_export  # noqa: E402
from harness.report import build_scenario_report, latency_stats, percentile  # noqa: E402
from harness.runner import pick_latency  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestSampleCollectionIsListBased(unittest.TestCase):
    """D4: samples must not collapse into a single 'unknown' bucket."""

    def test_many_samples_one_device_are_all_retained(self):
        recs = [
            {
                "metric": {"__name__": "iot_sensor_ts", "device_id": "dev0"},
                "values": [1000.0 + i for i in range(50)],
                "timestamps": [2000.0 + i for i in range(50)],
            }
        ]
        payload = "\n".join(json.dumps(r) for r in recs)
        samples = parse_export(payload)
        self.assertEqual(len(samples), 50, "all 50 samples must survive parsing")
        self.assertEqual(len({s.device_id for s in samples}), 1)

    def test_missing_msg_id_does_not_collapse(self):
        """The exact D4 shape: no msg_id label, many samples."""
        payload = json.dumps(
            {
                "metric": {"__name__": "iot_sensor_nats_exit_ts", "device_id": "d1"},
                "values": [1.0, 2.0, 3.0, 4.0, 5.0],
                "timestamps": [1.0, 2.0, 3.0, 4.0, 5.0],
            }
        )
        samples = parse_export(payload)
        self.assertEqual(len(samples), 5)

    def test_ndjson_and_query_api_shapes_agree(self):
        nd = "\n".join(
            json.dumps(
                {
                    "metric": {"__name__": "m", "device_id": "d"},
                    "values": [7.0],
                    "timestamps": [8.0],
                }
            )
            for _ in range(3)
        )
        query = json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {"metric": {"__name__": "m", "device_id": "d"}, "value": [7.0, 8.0]},
                        {"metric": {"__name__": "m", "device_id": "d"}, "value": [7.0, 8.0]},
                        {"metric": {"__name__": "m", "device_id": "d"}, "value": [7.0, 8.0]},
                    ],
                },
            }
        )
        self.assertEqual(len(parse_export(nd)), 3)
        self.assertEqual(len(parse_export(query)), 3)


class TestLatencySelection(unittest.TestCase):
    def test_prefers_consumer_computed_end_to_end(self):
        collected = {
            "iot_sensor_latency_ms": [Sample("iot_sensor_latency_ms", "d", 42.0, 1.0)],
            "iot_sensor_nats_exit_ts": [Sample("x", "d", 1.0, 1.0)],
        }
        samples, metric = pick_latency(collected)
        self.assertEqual(metric, "iot_sensor_latency_ms")
        self.assertEqual(len(samples), 1)

    def test_falls_back_to_legacy_join_without_msg_id(self):
        collected = {
            "iot_sensor_nats_exit_ts": [
                Sample("x", "d", 110.0, 10.0),
                Sample("x", "d", 130.0, 20.0),
            ],
            "iot_sensor_ts": [
                Sample("x", "d", 100.0, 1.0),
                Sample("x", "d", 120.0, 11.0),
            ],
        }
        samples, metric = pick_latency(collected)
        self.assertTrue(metric.startswith("legacy_join"))
        self.assertEqual(len(samples), 2, "legacy join must still pair per device")
        self.assertEqual([s.value for s in samples], [10.0, 10.0])


class TestReportSemantics(unittest.TestCase):
    def test_latency_definition_is_recorded(self):
        """D7/paper VIII-A: the report must say which definition it used."""
        rep = build_scenario_report(
            run_id="r", run_date="d", scenario="10c_100r", clients=10,
            target_rate=100, nodes="n", node_count=1,
            latency_samples=[Sample("iot_sensor_latency_ms", "d", 5.0, 1.0)],
            latency_metric="iot_sensor_latency_ms",
        )
        self.assertIn("end-to-end", rep["results"]["latency_definition"])
        legacy = build_scenario_report(
            run_id="r", run_date="d", scenario="10c_100r", clients=10,
            target_rate=100, nodes="n", node_count=1,
            latency_samples=[Sample("legacy", "d", 5.0, 1.0)],
            latency_metric="legacy_join(sensor_ts -> nats_exit_ts)",
        )
        self.assertIn("LEGACY", legacy["results"]["latency_definition"])

    def test_message_count_is_sample_count(self):
        rep = build_scenario_report(
            run_id="r", run_date="d", scenario="10c_500r", clients=10,
            target_rate=500, nodes="n", node_count=1,
            latency_samples=[Sample("m", f"d{i}", 1.0, float(i)) for i in range(1000)],
            latency_metric="iot_sensor_latency_ms", test_duration_s=2,
        )
        self.assertEqual(rep["results"]["total_messages"], 1000)

    def test_empty_input_is_reported_not_crashed(self):
        rep = build_scenario_report(
            run_id="r", run_date="d", scenario="10c_100r", clients=10,
            target_rate=100, nodes="n", node_count=1,
            latency_samples=[], latency_metric="none",
        )
        self.assertEqual(rep["results"]["total_messages"], 0)
        self.assertIsNone(rep["results"]["latency"]["p99_ms"])

    def test_percentile_matches_run_test_sh_semantics(self):
        vals = list(range(1, 101))
        self.assertAlmostEqual(percentile(vals, 50), 50.5, places=6)
        self.assertAlmostEqual(percentile(vals, 99), 99.01, places=6)
        self.assertEqual(percentile([], 50), 0.0)

    def test_stats_of_single_sample(self):
        s = latency_stats([42.0])
        self.assertEqual(s["samples"], 1)
        self.assertEqual(s["stddev_ms"], 0.0)


class TestRealCorpusRegression(unittest.TestCase):
    """If the paper-1 corpus is present, assert the corrected numbers hold.

    This is the test that would have caught D4 at the time.
    """

    RAW = os.path.join(
        REPO, "benchmarks", "20260519_130906", "raw_data", "10c_500r_nats_exit.json"
    )

    def setUp(self):
        if not os.path.exists(self.RAW):
            self.skipTest("paper-1 corpus not present")

    def test_may19_10c_500r_is_not_single_message(self):
        with open(self.RAW) as fh:
            samples = parse_export(fh.read())
        self.assertGreater(
            len(samples), 29000,
            "D4 regression: May 19 10c_500r has ~29,770 samples, not 1",
        )
        self.assertGreaterEqual(len({s.device_id for s in samples}), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestBrokerDiscoveryNeverReturnsAProbeFailure(unittest.TestCase):
    """Discovery must not hand back an address it just proved unreachable.

    After pi7 moved to a static address on the wired segment, the only
    advertised node addresses (10.0.0.x) were unroutable from the workstation.
    The probe correctly rejected every candidate, and the function then returned
    the first candidate anyway -- the ClusterIP, which is in the service CIDR
    and never routable from outside the cluster. The result was a benchmark run
    that published nothing and reported 0 messages at 0% of target, which reads
    as a pipeline result rather than a harness that could not find the broker.

    Kubernetes only reports each node's InternalIP, so a node reachable solely
    on a secondary interface is invisible to discovery. The fallbacks cover that
    from the ansible inventory and the kubeconfig server address.
    """

    def test_returns_none_when_nothing_answers_and_no_fallback_works(self):
        with mock.patch.object(config, "kubectl_json", return_value={
            "spec": {"clusterIP": "10.43.81.69",
                     "ports": [{"name": "mqtt", "port": 1883, "nodePort": 31883}]},
            "status": {},
        }):
            with mock.patch.object(config, "_node_addresses", return_value=["10.0.0.2"]):
                with mock.patch.object(config, "_master_node_ip", return_value="10.0.0.1"):
                    with mock.patch.object(config, "_tcp_reachable", return_value=False):
                        self.assertIsNone(config.discover_emqx_address())

    def test_does_not_return_the_clusterip(self):
        with mock.patch.object(config, "kubectl_json", return_value={
            "spec": {"clusterIP": "10.43.81.69",
                     "ports": [{"name": "mqtt", "port": 1883, "nodePort": 31883}]},
            "status": {},
        }):
            with mock.patch.object(config, "_node_addresses", return_value=[]):
                with mock.patch.object(config, "_master_node_ip", return_value=None):
                    with mock.patch.object(config, "_tcp_reachable", return_value=False):
                        got = config.discover_emqx_address()
        self.assertNotEqual(got, ("10.43.81.69", 1883))

    def test_reachable_nodeport_still_wins(self):
        with mock.patch.object(config, "kubectl_json", return_value={
            "spec": {"clusterIP": "10.43.81.69",
                     "ports": [{"name": "mqtt", "port": 1883, "nodePort": 31883}]},
            "status": {},
        }):
            with mock.patch.object(config, "_node_addresses", return_value=["10.0.0.2"]):
                with mock.patch.object(config, "_master_node_ip", return_value=None):
                    with mock.patch.object(
                        config, "_tcp_reachable",
                        side_effect=lambda h, p, **k: h == "10.0.0.2",
                    ):
                        self.assertEqual(
                            config.discover_emqx_address(), ("10.0.0.2", 31883)
                        )

    def test_falls_back_to_the_kubeconfig_server(self):
        with mock.patch.object(config, "kubectl_json", return_value={
            "spec": {"clusterIP": "10.43.81.69",
                     "ports": [{"name": "mqtt", "port": 1883, "nodePort": 31883}]},
            "status": {},
        }):
            with mock.patch.object(config, "_node_addresses", return_value=[]):
                with mock.patch.object(config, "_master_node_ip", return_value=None):
                    with mock.patch.object(config, "_inventory_hosts", return_value=[]):
                        with mock.patch.object(
                            config, "_kubeconfig_server_host", return_value="192.168.1.50"
                        ):
                            with mock.patch.object(
                                config, "_tcp_reachable",
                                side_effect=lambda h, p, **k: h == "192.168.1.50",
                            ):
                                self.assertEqual(
                                    config.discover_emqx_address(),
                                    ("192.168.1.50", 31883),
                                )

    def test_kubeconfig_host_is_parsed_from_a_url(self):
        for url, want in [
            ("https://192.168.1.50:6443", "192.168.1.50"),
            ("https://node.example:6443/", "node.example"),
            ("", None),
        ]:
            with self.subTest(url=url):
                with mock.patch.object(
                    config.subprocess, "run",
                    return_value=mock.Mock(stdout=url, returncode=0),
                ):
                    self.assertEqual(config._kubeconfig_server_host(), want)

    def test_inventory_hosts_are_extracted(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "inventory.ini"
            fake.write_text(
                "# comment\n[all:vars]\nk3s_version=v1\n"
                "master ansible_host=192.168.1.50 ansible_user=master\n"
                "pi7 ansible_host=10.0.0.2 ansible_user=pi7\n"
            )
            with mock.patch.object(config, "_repo_file", return_value=fake):
                self.assertEqual(
                    config._inventory_hosts(), ["192.168.1.50", "10.0.0.2"]
                )
