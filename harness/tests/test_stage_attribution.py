"""The four-way latency split must be arithmetic, not interpretation.

`stage_latency.py` pairs samples by their VictoriaMetrics sample timestamp,
which is identical across every series one message produces because they are
written in one request. Pairing by list position was tried first and produced
confident nonsense: the series come back in independent orders, so unrelated
messages get zipped together.

These tests use hand-built raw_data files with known deltas, so a sign error,
a swapped operand or a broken pairing fails loudly.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness import stage_latency

SCENARIO = "10c_500r"
WTIME = 100.0  # VictoriaMetrics sample time, identical for one message
TS = 1000
ENTRY = 1004
EXIT = 1817
ACK = 1821


def _write(raw: Path, name: str, value: float) -> None:
    rec = {
        "metric": {"__name__": f"iot_sensor_{name}", "device_id": "d1"},
        "timestamps": [WTIME],
        "values": [value],
    }
    (raw / f"{SCENARIO}_iot_sensor_{name}.json").write_text(
        json.dumps(rec) + "\n")


class TestStageAttribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self.tmp.name) / "raw_data"
        self.raw.mkdir()
        for name, value in (
            ("ts", TS),
            ("benthos_entry_ts", ENTRY),
            ("nats_exit_ts", EXIT),
            ("vm_write_ack_ts", ACK),
            ("latency_ms", ACK - TS),
        ):
            _write(self.raw, name, value)

    def tearDown(self):
        self.tmp.cleanup()

    def test_split_stages(self):
        r = stage_latency.analyse(str(self.raw), SCENARIO)
        self.assertEqual(r["publish_to_benthos"], [ENTRY - TS])
        self.assertEqual(r["benthos_to_nats"], [EXIT - ENTRY])
        self.assertEqual(r["ingest"], [EXIT - TS])
        self.assertEqual(r["write"], [ACK - EXIT])
        self.assertEqual(r["total"], [ACK - TS])

    def test_sub_stages_sum_to_ingest(self):
        r = stage_latency.analyse(str(self.raw), SCENARIO)
        self.assertEqual(
            r["publish_to_benthos"][0] + r["benthos_to_nats"][0],
            r["ingest"][0],
        )

    def test_missing_entry_falls_back_to_combined_ingest(self):
        """Runs from before the instrumentation must still report ingest."""
        (self.raw / f"{SCENARIO}_iot_sensor_benthos_entry_ts.json").unlink()
        r = stage_latency.analyse(str(self.raw), SCENARIO)
        self.assertEqual(r["publish_to_benthos"], [])
        self.assertEqual(r["benthos_to_nats"], [])
        self.assertEqual(r["ingest"], [EXIT - TS])

    def test_render_lists_the_new_stages(self):
        out = stage_latency.render(stage_latency.analyse(str(self.raw), SCENARIO))
        self.assertIn("publisher -> Benthos", out)
        self.assertIn("Benthos -> NATS", out)
        self.assertIn("end to end", out)


if __name__ == "__main__":
    unittest.main()
