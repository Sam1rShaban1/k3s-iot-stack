"""Tests for VictoriaMetrics query scoping.

A 10-client, 500 msg/s, 20 s scenario reported 22,335 stored messages across 20
devices at 30 msg/s. The pipeline had actually delivered 9,875 messages across
10 devices in ~494 msg/s. The collector was querying by metric name only, so it
received the metric's entire retained history, including 12,460 samples left by
an earlier run.

Two separate numbers were wrong, and both came from the same cause:

  * total_messages and unique_devices counted another run's series;
  * because throughput is derived from the observed (max - min) timestamp span,
    the stale samples stretched that span across the whole retention period,
    which is what turned 494 msg/s into 30 msg/s.

These tests pin the scoping that prevents a regression.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness import collect as collectmod  # noqa: E402
from harness.collect import VMClient  # noqa: E402


class RecordingClient(VMClient):
    """VMClient that records requested URLs and returns a canned payload."""

    def __init__(self, payload: str = ""):
        super().__init__("http://vm.invalid")
        self.payload = payload
        self.urls: list[str] = []

    def _run(self, url: str):
        self.urls.append(url)
        return self.payload


class TestExportIsScoped(unittest.TestCase):
    def test_export_includes_time_window(self):
        vm = RecordingClient("")
        vm.export("iot_sensor_latency_ms", start_s=1000.0, end_s=1035.0)
        url = vm.urls[0]
        self.assertIn("start=1000.000", url)
        self.assertIn("end=1035.000", url)

    def test_export_includes_device_filter(self):
        vm = RecordingClient("")
        vm.export("iot_sensor_latency_ms", device_pattern=".*_10c_500r_.*")
        self.assertIn("device_id", vm.urls[0])
        self.assertIn("10c_500r", vm.urls[0])

    def test_export_omits_bounds_when_not_given(self):
        vm = RecordingClient("")
        vm.export("iot_sensor_latency_ms")
        self.assertNotIn("start=", vm.urls[0])
        self.assertNotIn("end=", vm.urls[0])
        self.assertNotIn("device_id", vm.urls[0])

    def test_selector_is_closed_brace(self):
        """An unbalanced selector silently matches nothing in some backends."""
        vm = RecordingClient("")
        vm.export("iot_sensor_latency_ms", device_pattern=".*abc.*")
        self.assertIn("%7D", vm.urls[0])  # encoded closing brace


class TestCollectPassesScopeThrough(unittest.TestCase):
    def test_collect_forwards_window_and_pattern(self):
        vm = RecordingClient("")
        vm.collect("iot_sensor_latency_ms", start_s=1.0, end_s=2.0, device_pattern="p")
        self.assertIn("start=1.000", vm.urls[0])
        self.assertIn("end=2.000", vm.urls[0])
        self.assertIn("p", vm.urls[0])


class TestCountSeries(unittest.TestCase):
    def test_counts_series_list(self):
        payload = json.dumps(
            {
                "status": "success",
                "data": [
                    {"__name__": "m", "device_id": "a"},
                    {"__name__": "m", "device_id": "b"},
                ],
            }
        )
        vm = RecordingClient(payload)
        self.assertEqual(vm.count_series("m"), 2)

    def test_empty_data_is_zero(self):
        vm = RecordingClient(json.dumps({"status": "success", "data": []}))
        self.assertEqual(vm.count_series("m"), 0)

    def test_malformed_payload_is_none(self):
        vm = RecordingClient("not json")
        self.assertIsNone(vm.count_series("m"))

    def test_error_status_is_none(self):
        """A rejected query must not be read as 'empty'."""
        vm = RecordingClient(json.dumps({"status": "error", "error": "boom"}))
        self.assertIsNone(vm.count_series("m"))

    def test_uses_series_api_not_instant_query(self):
        """An instant count() only sees the lookbehind window.

        Stale data older than the window would be invisible and the check
        would wrongly report that the delete succeeded.
        """
        vm = RecordingClient("")
        vm.count_series("m")
        self.assertIn("/api/v1/series", vm.urls[0])
        self.assertNotIn("/api/v1/query", vm.urls[0])


class TestDeleteSelectorIsBraced(unittest.TestCase):
    """Regression: the unbraced match returned HTTP 400 for the whole project.

    VictoriaMetrics parses `match[]` as a full selector and rejects a bare
    `__name__=~"..."` with 'unexpected token "=~"'. The old code sent the
    unbraced form and discarded the return value, so the database was never
    cleared and every scenario inherited all retained history.
    """

    def _posted_match(self) -> str:
        with mock.patch("harness.collect.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="204", stderr="")
            VMClient("http://vm.invalid").delete_series("iot_.*")
        args, kwargs = run.call_args
        post = args[0]
        return post[post.index("-d") + 1]

    def test_match_value_has_braces(self):
        import urllib.parse

        data = self._posted_match()
        self.assertEqual(
            urllib.parse.parse_qs(data)["match[]"],
            ['{__name__=~"iot_.*"}'],
        )


class TestWaitForDelete(unittest.TestCase):
    """delete_series returns 200 before the data is actually gone."""

    def test_returns_true_once_empty(self):
        vm = RecordingClient("")
        with mock.patch.object(vm, "count_series", return_value=0):
            cleared, remaining = vm.wait_for_delete("m", timeout_s=1)
        self.assertTrue(cleared)
        self.assertEqual(remaining, 0)

    def test_times_out_with_remaining_count(self):
        vm = RecordingClient("")
        with mock.patch.object(vm, "count_series", return_value=30):
            with mock.patch("harness.collect.time.sleep"):
                cleared, remaining = vm.wait_for_delete("m", timeout_s=0, poll_s=0)
        self.assertFalse(cleared)
        self.assertEqual(remaining, 30)

    def test_unverifiable_is_not_success(self):
        """None means 'could not check', which must not read as 'cleared'."""
        vm = RecordingClient("")
        with mock.patch.object(vm, "count_series", return_value=None):
            cleared, remaining = vm.wait_for_delete("m", timeout_s=1)
        self.assertFalse(cleared)
        self.assertIsNone(remaining)

    def test_polls_until_data_disappears(self):
        vm = RecordingClient("")
        with mock.patch.object(vm, "count_series", side_effect=[30, 12, 0]):
            with mock.patch("harness.collect.time.sleep"):
                cleared, remaining = vm.wait_for_delete("m", timeout_s=30, poll_s=1)
        self.assertTrue(cleared)
        self.assertEqual(remaining, 0)


class TestStaleSeriesCannotInflateThroughput(unittest.TestCase):
    """The end-to-end failure this scoping prevents, at the report level.

    Mixes 9,875 fresh samples from this run with 12,460 stale ones from an
    earlier run. Without scoping, the stale samples dominate the count and
    stretch the timestamp span, collapsing throughput.
    """

    def _samples(self, device_prefix, n, t0_ms, step_ms, devices=10):
        """n samples spread over `devices` distinct device IDs.

        Real series carry many samples per device, so the device count has to
        be decoupled from the sample count.
        """
        return [
            collectmod.Sample(
                series="iot_sensor_latency_ms",
                device_id=f"{device_prefix}_{i % devices}",
                value=20.0 + (i % 10),
                timestamp_ms=t0_ms + i * step_ms,
            )
            for i in range(n)
        ]

    def test_fresh_only_reports_actual_throughput(self):
        from harness.report import build_scenario_report

        # 9,875 samples at a 2 ms step span ~19.7 s, so the implied rate is
        # ~500 msg/s, matching the scenario's target.
        fresh = self._samples("sensor_10c_500r_0_run_NEW", 9875, 1_700_000_000_000, 2.0)
        report = build_scenario_report(
            run_id="run_NEW", run_date="2026-09-29", scenario="10c_500r",
            clients=10, target_rate=500, nodes="5xPi4", node_count=5,
            latency_samples=fresh, latency_metric="iot_sensor_latency_ms",
            test_duration_s=20,
        )
        res = report["results"]
        self.assertEqual(res["total_messages"], 9875)
        self.assertEqual(res["unique_devices"], 10)
        # 9875 samples spread over ~20s of writes.
        self.assertGreater(res["throughput_msg_s"], 400)
        self.assertLess(res["throughput_msg_s"], 600)

    def test_stale_mix_would_corrupt_but_is_excluded_by_scope(self):
        """Documents the corruption, and shows the filter that removes it."""
        from harness.report import build_scenario_report

        fresh = self._samples("sensor_10c_500r_0_run_NEW", 9875, 1_700_000_000_000, 2.0)
        # An earlier run: same 10-device shape, 27 days older.
        stale = self._samples("cln_d0_340219", 12460, 1_699_000_000_000, 2.0)

        contaminated = build_scenario_report(
            run_id="run_NEW", run_date="2026-09-29", scenario="10c_500r",
            clients=10, target_rate=500, nodes="5xPi4", node_count=5,
            latency_samples=fresh + stale, latency_metric="iot_sensor_latency_ms",
            test_duration_s=20,
        )
        cres = contaminated["results"]
        # The corruption: far too many messages, too many devices, and a
        # throughput dragged down by the ~27-day timestamp span.
        self.assertEqual(cres["total_messages"], 22335)
        self.assertEqual(cres["unique_devices"], 20)
        self.assertLess(cres["throughput_msg_s"], 100)

        # The fix: keep only samples whose device belongs to this run.
        scoped = [s for s in fresh + stale if "_run_NEW" in s.device_id]
        clean = build_scenario_report(
            run_id="run_NEW", run_date="2026-09-29", scenario="10c_500r",
            clients=10, target_rate=500, nodes="5xPi4", node_count=5,
            latency_samples=scoped, latency_metric="iot_sensor_latency_ms",
            test_duration_s=20,
        )
        self.assertEqual(clean["results"]["total_messages"], 9875)
        self.assertEqual(clean["results"]["unique_devices"], 10)


class TestDevicePatternShape(unittest.TestCase):
    """The runner's device pattern must be fully anchored.

    VictoriaMetrics anchors a `device_id=~"..."` regex to the entire label
    value. A pattern ending in the run id with no trailing wildcard therefore
    matches nothing, and the scenario reports zero messages while the pipeline
    is in fact delivering them. This was observed live: `.*_10c_500r_.*_RUN`
    returned 0 bytes and the same pattern plus `.*` returned 175,603.
    """

    def _pattern(self, scenario: str, run_id: str) -> str:
        import re

        return f".*_{re.escape(scenario)}_[0-9]+_{re.escape(run_id)}_.*"

    def test_matches_this_runs_devices(self):
        import re

        pat = self._pattern("10c_500r", "run_20260929_103755")
        self.assertTrue(
            re.fullmatch(pat, "sensor_10c_500r_3_run_20260929_103755_375263_48751635")
        )

    def test_ends_with_wildcard(self):
        """The regression: no trailing .* matched nothing at all."""
        self.assertTrue(self._pattern("10c_500r", "run_1").endswith("_.*"))

    def test_excludes_earlier_run(self):
        import re

        pat = self._pattern("10c_500r", "run_20260929_103755")
        self.assertIsNone(
            re.fullmatch(pat, "sensor_10c_500r_3_run_20260929_090000_1_2")
        )

    def test_excludes_other_scenario_in_same_run(self):
        import re

        pat = self._pattern("10c_500r", "run_20260929_103755")
        self.assertIsNone(
            re.fullmatch(pat, "sensor_100c_1000r_3_run_20260929_103755_1_2")
        )

    def test_excludes_stale_cln_series(self):
        import re

        pat = self._pattern("10c_500r", "run_20260929_103755")
        self.assertIsNone(re.fullmatch(pat, "cln_d0_340219_356570171"))

    def test_scenario_name_is_not_treated_as_regex(self):
        """A '.' in a scenario name must not become a wildcard."""
        import re

        run_id = "run_1"
        pat = self._pattern("10c.500r", run_id)
        self.assertIsNone(re.fullmatch(pat, f"sensor_10cX500r_0_{run_id}_1_2"))
        self.assertTrue(re.fullmatch(pat, f"sensor_10c.500r_0_{run_id}_1_2"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
