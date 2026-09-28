"""Sample collection from VictoriaMetrics.

The previous collector (run_test.sh, load_data()) stored samples in a dict
keyed by `msg_id`:

    results[msg_id] = {...}          # overwrites on every repeated key

Two distinct defects followed from that, and only the first is obvious:

1. The `msg_id` label was removed from the deployed consumer in the paper's
   §VI-C cardinality fix. With it gone, every sample hashed to the single key
   'unknown', so each metric collapsed to one entry and every scenario
   reported total_messages: 1. This is visible in
   benchmarks/20260519_130906/results/summary.json.

2. Even with msg_id present, the dict could only ever hold the LAST sample per
   identity. That happened to be correct only because msg_id was unique per
   message. Keying on anything bounded (device_id) would again retain one
   sample per device and silently discard the rest of the distribution.

The correct model is a flat list of samples, one per (series, timestamp).
Percentiles are then computed over all samples, and message counts are the
length of the list. This is what the paper's tables actually report.
"""

from __future__ import annotations

import json
import subprocess
import urllib.parse
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class Sample:
    """One observation of one series at one instant."""

    series: str
    device_id: str
    value: float
    timestamp_ms: float

    @property
    def key(self) -> tuple[str, str, float]:
        return (self.series, self.device_id, self.timestamp_ms)


def _iter_export_records(payload: str) -> Iterable[dict]:
    """Yield records from either a single JSON doc or NDJSON."""
    text = payload.strip()
    if not text:
        return
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
        return

    # /api/v1/export returns either {"metric":..,"values":[..]} per line, or a
    # single doc wrapping resultType/result depending on Content-Type handling.
    if isinstance(doc, dict) and "metric" in doc:
        yield doc
        return
    if isinstance(doc, dict) and "data" in doc:
        for rec in (doc.get("data") or {}).get("result", []) or []:
            yield rec


def parse_export(payload: str) -> list[Sample]:
    """Parse a VictoriaMetrics export into a flat sample list.

    Handles all three shapes the corpus contains:
      * NDJSON, one {"metric","values","timestamps"} per line  (5-node runs)
      * {"data":{"resultType":"matrix","result":[...]}}        (some 2-node runs)
      * {"data":{"resultType":"vector","result":[...]}}        (query API)
    """
    samples: list[Sample] = []
    for rec in _iter_export_records(payload):
        metric = rec.get("metric") or {}
        series = metric.get("__name__", "unknown")
        device_id = metric.get("device_id", "unknown")

        values = rec.get("values")
        timestamps = rec.get("timestamps")
        if values is not None:
            for i, val in enumerate(values):
                ts = timestamps[i] if timestamps and i < len(timestamps) else val
                samples.append(
                    Sample(series, device_id, float(val), float(ts))
                )
            continue

        value_pair = rec.get("value")
        if value_pair and len(value_pair) == 2:
            val, ts = value_pair
            samples.append(Sample(series, device_id, float(val), float(ts)))
    return samples


class VMClient:
    """Thin VictoriaMetrics client over the export and query APIs."""

    def __init__(self, base_url: str, timeout_s: int = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _run(self, url: str) -> Optional[str]:
        try:
            res = subprocess.run(
                ["curl", "-sS", "--max-time", str(self.timeout_s), url],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            raise RuntimeError("curl not found; cannot reach VictoriaMetrics")
        if res.returncode != 0:
            return None
        return res.stdout

    def export(self, metric: str) -> Optional[str]:
        """Raw export payload for one metric, or None on failure."""
        match = urllib.parse.quote('{__name__="%s"}' % metric)
        return self._run(f"{self.base_url}/api/v1/export?match[]={match}")

    def collect(self, metric: str) -> list[Sample]:
        payload = self.export(metric)
        if payload is None:
            return []
        return parse_export(payload)

    def delete_series(self, name_pattern: str = "iot_.*") -> bool:
        data = urllib.parse.urlencode({"match[]": '__name__=~"%s"' % name_pattern})
        res = subprocess.run(
            [
                "curl",
                "-sS",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "-X",
                "POST",
                f"{self.base_url}/api/v1/admin/tsdb/delete_series",
                "-d",
                data,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return res.stdout.strip() in ("200", "204")

    def total_series(self) -> Optional[int]:
        payload = self._run(f"{self.base_url}/api/v1/status/tsdb")
        if not payload:
            return None
        try:
            return int(json.loads(payload)["data"]["totalSeries"])
        except Exception:
            return None
