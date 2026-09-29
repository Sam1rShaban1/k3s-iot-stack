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

    def test_backlog_size_returns_none_on_failure(self):
        with self._direct_and_offline():
            result = natsctl.backlog_size("IOT_DATA")
        self.assertIsNone(result)

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
            with mock.patch.object(natsctl, "_in_cluster_backlog") as incl:
                incl.return_value = 7
                self.assertEqual(natsctl.backlog_size("IOT_DATA"), 7)
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
