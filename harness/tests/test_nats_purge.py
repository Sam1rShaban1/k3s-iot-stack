"""Tests for the NATS purge requirement.

P0.6 established empirically that clearing only VictoriaMetrics leaves the
JetStream backlog in place, and that the consumer then spends the scenario
replaying it. A 500 msg/s scenario measured 36% efficiency because of it; after
purging, the same scenario delivered 99.68%.

These tests pin the behaviour that prevents a regression, and pin the honesty
requirement: when the purge cannot happen, the run must say so rather than
report numbers that look clean.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness import nats as natsctl  # noqa: E402

# A port nothing listens on, so the direct path fails fast and locally.
UNREACHABLE = "nats://127.0.0.1:1"


def _stats(**kw) -> natsctl.JetStreamStats:
    """JetStreamStats with sensible defaults, overridable per test."""
    base = dict(
        retained=0,
        num_pending=0,
        num_ack_pending=0,
        num_redelivered=0,
        bytes_stored=0,
        retention="limits",
        max_msgs=-1,
        max_age=0.0,
        max_bytes=-1,
    )
    base.update(kw)
    return natsctl.JetStreamStats(**base)


class TestNatsResultMetadata(unittest.TestCase):
    def test_reports_purged_flag(self):
        r = natsctl.NatsResult(True, 1234, 0, "purged IOT_DATA")
        m = r.as_metadata()
        self.assertTrue(m["nats_purged"])
        self.assertEqual(m["nats_messages_before"], 1234)
        self.assertEqual(m["nats_messages_after"], 0)

    def test_reports_failure_distinctly(self):
        r = natsctl.NatsResult(False, detail="no creds")
        m = r.as_metadata()
        self.assertFalse(m["nats_purged"])
        self.assertIn("no creds", m["nats_purge_detail"])


class TestPurgeNeverRaises(unittest.TestCase):
    """A purge failure must degrade the run, not abort the benchmark.

    HARNESS_NATS_MODE=direct confines these tests to the direct path. Without
    it the in-cluster kubectl fallback can succeed on a developer machine that
    happens to have the cluster running, which would make the failure branch
    untested and the result machine-dependent.
    """

    def _direct_and_offline(self):
        return mock.patch.dict(
            os.environ,
            {
                "HARNESS_NATS_URL": UNREACHABLE,
                "HARNESS_NATS_MODE": "direct",
            },
            clear=False,
        )

    def test_returns_result_even_when_nats_unreachable(self):
        with self._direct_and_offline():
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("HARNESS_NATS_USER", None)
                os.environ.pop("HARNESS_NATS_PASSWORD", None)
                os.environ.pop("HARNESS_NATS_TOKEN", None)
                result = natsctl.purge_stream("IOT_DATA")

        # The contract: never raises, always returns a result, and reports the
        # failure so the run cannot be mistaken for a clean one.
        self.assertIsInstance(result, natsctl.NatsResult)
        self.assertFalse(result.purged)
        self.assertTrue(result.detail.strip(), "failure detail must not be empty")
        self.assertIn("not purged", result.detail.lower())

    def test_stats_returns_none_when_unreachable(self):
        with self._direct_and_offline():
            self.assertIsNone(natsctl.stats("IOT_DATA"))

    def test_unconsumed_and_retained_are_none_when_unreachable(self):
        with self._direct_and_offline():
            self.assertIsNone(natsctl.unconsumed("IOT_DATA"))
            self.assertIsNone(natsctl.retained("IOT_DATA"))

    def test_purge_falls_back_to_incluster_when_direct_fails(self):
        """An unreachable direct path must not prevent the in-cluster purge."""
        expected = natsctl.NatsResult(True, 42, 0, "purged in-cluster")
        with mock.patch.dict(
            os.environ,
            {"HARNESS_NATS_URL": UNREACHABLE, "HARNESS_NATS_MODE": "auto"},
            clear=False,
        ):
            with mock.patch.object(natsctl, "_direct_purge") as direct:
                direct.return_value = natsctl.NatsResult(False, detail="direct failed")
                with mock.patch.object(natsctl, "_in_cluster_purge") as incl:
                    incl.return_value = expected
                    result = natsctl.purge_stream("IOT_DATA")
        self.assertIs(result, expected)
        self.assertTrue(result.purged)
        self.assertEqual(result.messages_before, 42)
        direct.assert_called_once()
        incl.assert_called_once_with("IOT_DATA")

    def test_purge_reports_both_failures_when_all_routes_fail(self):
        with mock.patch.dict(
            os.environ,
            {"HARNESS_NATS_URL": UNREACHABLE, "HARNESS_NATS_MODE": "auto"},
            clear=False,
        ):
            with mock.patch.object(natsctl, "_direct_purge") as direct:
                direct.return_value = natsctl.NatsResult(False, detail="direct failed")
                with mock.patch.object(natsctl, "_in_cluster_purge") as incl:
                    incl.return_value = natsctl.NatsResult(False, detail="incl failed")
                    result = natsctl.purge_stream("IOT_DATA")
        self.assertFalse(result.purged)
        # Both reasons must survive, so the operator knows which route to fix.
        self.assertIn("direct failed", result.detail)
        self.assertIn("incl failed", result.detail)


class TestStreamNameIsConfigurable(unittest.TestCase):
    def test_stream_name_from_env(self):
        with mock.patch.dict(os.environ, {"HARNESS_NATS_STREAM": "CUSTOM"}, clear=False):
            cfg = natsctl._config("IOT_DATA")
        self.assertEqual(cfg["stream"], "CUSTOM")

    def test_stream_name_defaults_to_argument(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_NATS_STREAM", None)
            cfg = natsctl._config("OTHER_STREAM")
        self.assertEqual(cfg["stream"], "OTHER_STREAM")


class TestStreamOverrideReachesEveryRoute(unittest.TestCase):
    """An env override must reach every route, not just the direct one.

    Regression: HARNESS_NATS_STREAM was honoured by the direct path only, so the
    in-cluster path purged the default stream and then reported success under
    the wrong name. A wrong-stream purge is worse than no purge, because the
    report claims the backlog was cleared when it was not.
    """

    def test_override_reaches_incluster_route(self):
        with mock.patch.dict(
            os.environ,
            {"HARNESS_NATS_MODE": "incluster", "HARNESS_NATS_STREAM": "CUSTOM"},
            clear=False,
        ):
            with mock.patch.object(natsctl, "_in_cluster_purge") as incl:
                incl.return_value = natsctl.NatsResult(True, 1, 0, "ok")
                natsctl.purge_stream("IOT_DATA")
            incl.assert_called_once_with("CUSTOM")

    def test_override_reaches_backlog_route(self):
        with mock.patch.dict(
            os.environ,
            {"HARNESS_NATS_MODE": "incluster", "HARNESS_NATS_STREAM": "CUSTOM"},
            clear=False,
        ):
            with mock.patch.object(natsctl, "_probe_in_cluster") as incl:
                incl.return_value = _stats(retained=99, num_pending=7)
                self.assertEqual(natsctl.unconsumed("IOT_DATA"), 7)
            incl.assert_called_once_with("CUSTOM")

    def test_failure_message_names_the_effective_stream(self):
        with mock.patch.dict(
            os.environ,
            {"HARNESS_NATS_MODE": "incluster", "HARNESS_NATS_STREAM": "CUSTOM"},
            clear=False,
        ):
            with mock.patch.object(natsctl, "_in_cluster_purge") as incl:
                incl.return_value = natsctl.NatsResult(False, detail="no pod")
                result = natsctl.purge_stream("IOT_DATA")
        self.assertIn("CUSTOM", result.detail)


class TestNoCredentialsInRepo(unittest.TestCase):
    """The purge must not require credentials committed to the repository."""

    def test_module_has_no_hardcoded_credentials(self):
        src = open(natsctl.__file__).read()
        # A literal password/token, or credentials embedded in a URL. Reading
        # a variable out of the environment is fine and is what the module does.
        for bad in ("nats://user:", "password=\"", "password='", "token=\""):
            self.assertNotIn(bad, src, f"hardcoded credential material: {bad}")

    def test_incluster_snippet_carries_no_credentials(self):
        """The snippet must read config from the pod, not embed secrets."""
        for bad in ("nats://user:", "password=\"", "token=\""):
            self.assertNotIn(bad, natsctl._IN_CLUSTER_SNIPPET)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestRetainedIsNotBacklog(unittest.TestCase):
    """Regression, D13: retained messages are not unconsumed work.

    The stream uses retention=limits with no max_msgs/max_age/max_bytes, so
    acknowledging a message does not remove it. An earlier version of this
    module reported stream.state.messages as "backlog" and used it to declare
    runs contaminated. A probe of a fully-consumed stream read 18,236 "backlog"
    while the consumer sat at num_pending=0 with
    delivered.stream_seq == stream.last_seq.
    """

    def test_retained_counts_everything_stored(self):
        s = _stats(retained=18236, num_pending=0, num_ack_pending=0)
        self.assertEqual(s.retained, 18236)

    def test_unconsumed_is_pending_plus_ack_pending(self):
        s = _stats(num_pending=100, num_ack_pending=5, retained=99999)
        self.assertEqual(s.unconsumed, 105)

    def test_fully_consumed_stream_is_not_flagged(self):
        s = _stats(retained=18236, num_pending=0, num_ack_pending=0)
        self.assertEqual(s.unconsumed, 0)

    def test_accessors_read_distinct_fields(self):
        s = _stats(retained=5000, num_pending=10, num_ack_pending=2)
        with mock.patch.object(natsctl, "stats", return_value=s):
            self.assertEqual(natsctl.retained("IOT_DATA"), 5000)
            self.assertEqual(natsctl.unconsumed("IOT_DATA"), 12)

    def test_metadata_exposes_both(self):
        m = _stats(retained=7, num_pending=3).as_metadata()
        self.assertEqual(m["nats_retained"], 7)
        self.assertEqual(m["nats_unconsumed"], 3)


class TestUnboundedRetentionDetectable(unittest.TestCase):
    """Nothing evicts from this stream, so storage grows without limit.

    Measured on the live cluster: 18,236 messages occupy 3.1 MB, about 170 B
    each. At 1000 msg/s that is roughly 14.7 GB/day against a 57 GB SD card
    shared with K3s, Longhorn and VictoriaMetrics.
    """

    def test_detects_unbounded(self):
        self.assertTrue(_stats().unbounded)

    def test_bounded_by_max_msgs(self):
        self.assertFalse(_stats(max_msgs=1_000_000).unbounded)

    def test_bounded_by_max_age(self):
        self.assertFalse(_stats(max_age=3600.0).unbounded)

    def test_bounded_by_max_bytes(self):
        self.assertFalse(_stats(max_bytes=1_000_000_000).unbounded)

    def test_metadata_carries_the_limits(self):
        m = _stats(max_msgs=-1, max_age=0.0, max_bytes=-1).as_metadata()
        self.assertEqual(m["nats_max_msgs"], -1)
        self.assertEqual(m["nats_max_age"], 0.0)
        self.assertEqual(m["nats_max_bytes"], -1)


class TestStatsSnippetFields(unittest.TestCase):
    """Both routes must report the same fields, or a comparison is meaningless."""

    def test_snippet_emits_every_field(self):
        import json

        for field in (
            "retained", "num_pending", "num_ack_pending",
            "num_redelivered", "bytes_stored", "retention", "max_msgs",
            "max_age", "max_bytes",
        ):
            self.assertIn(f'"{field}"', natsctl._STATS_SNIPPET)

    def test_snippet_output_parses_into_the_dataclass(self):
        import json

        payload = json.dumps(_stats(retained=5, num_pending=2).as_metadata())
        data = json.loads(payload)
        for k in list(data):
            data[k[len("nats_"):]] = data.pop(k)
        # as_metadata() publishes the derived field; the parser drops it, the
        # same way _probe_in_cluster does for an older snippet.
        data.pop("unconsumed", None)
        rebuilt = natsctl.JetStreamStats(**data)
        self.assertEqual(rebuilt.retained, 5)
        self.assertEqual(rebuilt.unconsumed, 2)

    def test_probe_drops_a_stray_unconsumed_field(self):
        """An older snippet's payload must still load."""
        import json

        fields = _stats(retained=5, num_pending=2).as_metadata()
        fields = {
            k[len("nats_"):]: v for k, v in fields.items()
        }
        fields["unconsumed"] = 999
        line = "RESULT " + json.dumps(fields)
        with mock.patch.object(
            natsctl, "_find_pod", return_value="nats-consumer-x"
        ), mock.patch.object(
            natsctl.subprocess, "run"
        ) as run:
            run.return_value = mock.Mock(returncode=0, stdout=line, stderr="")
            got = natsctl._probe_in_cluster("IOT_DATA")
        self.assertIsNotNone(got)
        self.assertEqual(got.unconsumed, 2)
