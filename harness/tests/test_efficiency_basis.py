"""Efficiency must divide by what the generator produced, not what it was asked for.

The publisher falls short of nominal, and increasingly so with per-device rate:
measured 20/s -> 99.80%, 50/s -> 99.56%, 100/s -> 99.17%, 200/s -> 98.40%.
Dividing by the nominal rate therefore caps reported efficiency at the
generator's accuracy and charges the remainder to the pipeline as packet loss.
At 100 clients and 10000 msg/s that ceiling is 99.17%, which cannot tell a good
cluster from a bad one.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from harness.report import build_scenario_report  # noqa: E402
from harness.collect import Sample  # noqa: E402


def _samples(n: int, step_ms: float = 1000.0):
    """n samples one second apart, so duration is exactly n seconds."""
    return [Sample(series="iot_sensor_latency_ms", device_id=f"d{i}",
                   value=1.0, timestamp_ms=i * step_ms)
            for i in range(n)]


def _report(gen, target_rate=1000, n=900):
    return build_scenario_report(
        run_id="r", run_date="2026-10-05", scenario="100c_1000r", clients=100,
        target_rate=target_rate, nodes="5x pi4b", node_count=5,
        latency_samples=_samples(n), latency_metric="iot_sensor_latency_ms",
        test_duration_s=60, generator=gen,
    )


class TestEfficiencyBasis(unittest.TestCase):
    def test_uses_achieved_rate_when_generator_reports(self):
        """900 samples ~1 msg/s observed; generator delivered 1 msg/s, not 1000.

        Nominal basis would report 0.1% and blame the cluster for the
        generator's shortfall. Achieved basis must report ~100%.
        """
        r = _report({"achieved_msg_s": 1.0, "published": 900, "reported": 100})
        cfg, res = r["configuration"], r["results"]
        self.assertEqual(cfg["efficiency_basis"], "generator_achieved")
        self.assertEqual(cfg["achieved_rate_msg_s"], 1.0)
        self.assertGreater(res["efficiency_pct"], 99.0)
        # Nominal basis (1000 msg/s) would have reported ~0.1% here.
        self.assertLess(res["efficiency_pct_nominal_basis"], 1.0)

    def test_reports_generator_accuracy_separately(self):
        """The shortfall must stay visible, not be silently absorbed."""
        r = _report({"achieved_msg_s": 99.17, "published": 900, "reported": 100},
                    target_rate=100)
        self.assertAlmostEqual(r["results"]["generator_accuracy_pct"], 99.17, places=1)

    def test_keeps_nominal_basis_for_comparability(self):
        """Older reports used nominal. The figure is retained so they stay readable."""
        r = _report({"achieved_msg_s": 99.17, "published": 900, "reported": 100},
                    target_rate=100)
        self.assertIn("efficiency_pct_nominal_basis", r["results"])
        self.assertLess(r["results"]["efficiency_pct_nominal_basis"],
                        r["results"]["efficiency_pct"])

    def test_falls_back_to_nominal_and_says_so(self):
        """No generator data must not silently produce a flattering number."""
        r = _report(None, target_rate=1000, n=900)
        cfg, res = r["configuration"], r["results"]
        self.assertEqual(cfg["efficiency_basis"], "nominal_target_fallback")
        self.assertIsNone(cfg["achieved_rate_msg_s"])
        self.assertEqual(res["efficiency_pct"], res["efficiency_pct_nominal_basis"])

    def test_zero_achieved_does_not_divide_by_zero(self):
        r = _report({"achieved_msg_s": 0.0, "published": 0, "reported": 0},
                    target_rate=0, n=10)
        self.assertEqual(r["results"]["efficiency_pct"], 0.0)

    def test_generator_shortfall_is_not_charged_as_pipeline_loss(self):
        """The point of the change: identical samples, different denominators."""
        gen = {"achieved_msg_s": 95.0, "published": 950, "reported": 100}
        fixed = _report(gen, target_rate=100, n=950)
        nominal = _report(None, target_rate=100, n=950)
        # 950 samples over 950s = 1 msg/s observed.
        self.assertEqual(fixed["results"]["throughput_msg_s"],
                         nominal["results"]["throughput_msg_s"])
        self.assertGreater(fixed["results"]["efficiency_pct"],
                           nominal["results"]["efficiency_pct"])


class TestDeliveryRatio(unittest.TestCase):
    """delivered/published is the honest loss metric; efficiency is not.

    Observed on a clean run: publisher reported published=89390, VictoriaMetrics
    returned stored=89390 -- nothing lost -- yet efficiency read 91.49%, because
    throughput divides by the span of the VM samples, which includes the settle
    period. Delivery ratio compares counts and is immune to that.
    """

    def test_counts_messages_against_generator_published(self):
        r = _report({"achieved_msg_s": 1.0, "published": 900, "reported": 100}, n=900)
        self.assertAlmostEqual(r["results"]["delivery_ratio_pct"], 100.0, places=2)

    def test_detects_actual_loss(self):
        r = _report({"achieved_msg_s": 1.0, "published": 1000, "reported": 100}, n=900)
        self.assertAlmostEqual(r["results"]["delivery_ratio_pct"], 90.0, places=2)

    def test_none_when_generator_count_unknown(self):
        r = _report(None, n=900)
        self.assertIsNone(r["results"]["delivery_ratio_pct"])

    def test_perfect_delivery_despite_sub_100_efficiency(self):
        """The real case: nothing lost, yet efficiency reads below 100."""
        r = _report({"achieved_msg_s": 1.0, "published": 900, "reported": 100}, n=900)
        self.assertEqual(r["results"]["delivery_ratio_pct"], 100.0)
        self.assertGreater(r["results"]["efficiency_pct"], 99.0)
