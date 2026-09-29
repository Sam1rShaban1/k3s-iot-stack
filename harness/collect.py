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
import time
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

    def export(
        self,
        metric: str,
        start_s: Optional[float] = None,
        end_s: Optional[float] = None,
        device_pattern: Optional[str] = None,
    ) -> Optional[str]:
        """Raw export payload for one metric, or None on failure.

        start_s/end_s and device_pattern are not optional conveniences. Without
        them the export returns the metric's entire retained history, so a
        scenario picks up series left by earlier runs. That is not a cosmetic
        problem: it inflates total_messages and unique_devices, and because
        throughput is derived from the observed (max-min) timestamp span, the
        stale samples stretch the window across the whole retention period and
        the reported throughput collapses to a meaningless figure.

        Every caller measuring a scenario must pass a window and a device
        pattern.
        """
        selector = '{__name__="%s"' % metric
        if device_pattern:
            selector += ',device_id=~"%s"' % device_pattern
        selector += "}"
        params = {"match[]": selector}
        if start_s is not None:
            params["start"] = f"{start_s:.3f}"
        if end_s is not None:
            params["end"] = f"{end_s:.3f}"
        return self._run(
            f"{self.base_url}/api/v1/export?{urllib.parse.urlencode(params)}"
        )

    def count_series(self, metric: str) -> Optional[int]:
        """Number of stored series for one metric, or None if the query fails.

        Deliberately unfiltered and windowless, so it is a valid check for
        whether a delete actually removed everything. It uses the /series API
        rather than an instant `count()` query because an instant query only
        sees the lookbehind window: stale data older than that would be
        invisible and the check would wrongly report success.

        An exact __name__ match is used. A regex across all iot_* metrics can
        exceed VictoriaMetrics' -search.max* limits and fail on a large
        database.
        """
        params = {"match[]": '{__name__="%s"}' % metric}
        payload = self._run(f"{self.base_url}/api/v1/series?{urllib.parse.urlencode(params)}")
        if payload is None:
            return None
        try:
            doc = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if doc.get("status") != "success":
            return None
        data = doc.get("data")
        return len(data) if isinstance(data, list) else None

    def collect(
        self,
        metric: str,
        start_s: Optional[float] = None,
        end_s: Optional[float] = None,
        device_pattern: Optional[str] = None,
    ) -> list[Sample]:
        payload = self.export(metric, start_s, end_s, device_pattern)
        if payload is None:
            return []
        return parse_export(payload)

    def delete_series(self, name_pattern: str = "iot_.*") -> bool:
        """Request deletion of matching series.

        The match must be a full selector with braces. VictoriaMetrics rejects
        a bare `__name__=~"..."` with HTTP 400 and the text 'unexpected token
        "=~"', and that rejection is silent from the caller's point of view
        because the old code discarded the return value. So the database was
        never actually cleared: every scenario inherited the entire retained
        history of every previous run.

        Returns True only if VictoriaMetrics accepted the request. It does NOT
        mean the data is gone: delete_series is applied in the background, so
        the series can still be returned by queries for a short while after a
        200. Callers that need the data to be absent must poll with
        count_samples, or scope their queries so that leftover series cannot
        affect the result.
        """
        selector = name_pattern if name_pattern.startswith("__name__") else f'__name__=~"{name_pattern}"'
        data = urllib.parse.urlencode({"match[]": "{%s}" % selector})
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

    def wait_for_delete(
        self,
        metric: str,
        timeout_s: int = 60,
        poll_s: float = 2.0,
    ) -> tuple[bool, Optional[int]]:
        """Poll until no series remain for `metric`, or the timeout expires.

        Returns (cleared, remaining_series). `cleared` is False when the wait
        timed out, which the runner reports as a warning. Measurement is still
        correct because queries are scoped by window and device, but a database
        that will not clear is a real problem worth surfacing.

        `remaining_series` is None when the check itself could not be
        performed, which is different from "empty" and must not be read as it.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = self.count_series(metric)
            if remaining is None:
                # Cannot verify. Report not-cleared rather than claiming success.
                return False, None
            if remaining == 0:
                return True, 0
            if time.monotonic() >= deadline:
                return False, remaining
            time.sleep(poll_s)

    def total_series(self) -> Optional[int]:
        payload = self._run(f"{self.base_url}/api/v1/status/tsdb")
        if not payload:
            return None
        try:
            return int(json.loads(payload)["data"]["totalSeries"])
        except Exception:
            return None
