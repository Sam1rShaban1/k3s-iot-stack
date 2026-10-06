"""Report generation: percentiles, latency distribution, scenario and run summaries.

Latency semantics (P0.4)
------------------------
Paper §VIII-A records that the 1/2/3-node results used end-to-end latency
(sensor_ts -> nats_exit_ts) while the 5-node results used inter-arrival time
at the consumer, and that the two "are defined differently and should not be
compared directly across configurations."

That is fixed here. There is now one primary metric, `iot_sensor_latency_ms`,
which the consumer computes as:

    latency_ms = vm_write_ack_ts - ts

i.e. sensor publish -> VictoriaMetrics write acknowledged. It is written as a
bounded series (one per device), so no join is required and no unbounded label
is introduced. `latency_metric` records which definition a given report used, so
a legacy run is never silently compared against a new one.

`inter_arrival_ms` is still computed, but as a separate, explicitly named
metric. It is reported for continuity with the paper and is not a substitute for
end-to-end latency.
"""

from __future__ import annotations

import statistics
from typing import Optional, Sequence

from .collect import Sample

# Buckets retained from run_test.sh so that bucket-level counts remain
# comparable with the paper's tables. Extended upward because the paper's
# worst observed latency (1-node, 100c_500r) was ~36 hours.
BUCKETS: list[tuple[str, float, float]] = [
    ("0-10ms", 0, 10),
    ("10-50ms", 10, 50),
    ("50-100ms", 50, 100),
    ("100-200ms", 100, 200),
    ("200-500ms", 200, 500),
    ("500ms-1s", 500, 1000),
    ("1-2s", 1000, 2000),
    ("2-5s", 2000, 5000),
    ("5-10s", 5000, 10000),
    ("10-30s", 10000, 30000),
    ("30-60s", 30000, 60000),
    ("1-2min", 60000, 120000),
    ("2-5min", 120000, 300000),
    ("5-10min", 300000, 600000),
    ("10min-1h", 600000, 3600000),
    ("1h+", 3600000, float("inf")),
]


def percentile(sorted_values: Sequence[float], p: float) -> float:
    """Linear-interpolation percentile, matching run_test.sh's behaviour."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    k = (len(sorted_values) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1
    if c >= len(sorted_values):
        return float(sorted_values[f])
    return sorted_values[f] + (k - f) * (sorted_values[c] - sorted_values[f])


def latency_stats(values: Sequence[float]) -> dict:
    if not values:
        return {
            "samples": 0,
            "avg_ms": None,
            "min_ms": None,
            "max_ms": None,
            "median_ms": None,
            "p50_ms": None,
            "p75_ms": None,
            "p90_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "p999_ms": None,
            "stddev_ms": None,
        }
    s = sorted(values)
    n = len(s)
    return {
        "samples": n,
        "avg_ms": round(statistics.mean(s), 3),
        "min_ms": round(s[0], 3),
        "max_ms": round(s[-1], 3),
        "median_ms": round(statistics.median(s), 3),
        "p50_ms": round(percentile(s, 50), 3),
        "p75_ms": round(percentile(s, 75), 3),
        "p90_ms": round(percentile(s, 90), 3),
        "p95_ms": round(percentile(s, 95), 3),
        "p99_ms": round(percentile(s, 99), 3),
        "p999_ms": round(percentile(s, 99.9), 3),
        "stddev_ms": round(statistics.stdev(s), 3) if n > 1 else 0.0,
    }


def bucket_distribution(values: Sequence[float]) -> list[dict]:
    n = len(values)
    out = []
    for name, lo, hi in BUCKETS:
        count = sum(1 for v in values if lo <= v < hi)
        out.append(
            {
                "bucket": name,
                "min_ms": lo,
                "max_ms": None if hi == float("inf") else hi,
                "count": count,
                "percentage": round((count / n) * 100, 4) if n else 0.0,
            }
        )
    return out


def inter_arrival(samples: Sequence[Sample]) -> list[float]:
    """Gaps between consecutive samples, globally ordered by timestamp.

    Reported separately from end-to-end latency and never substituted for it.
    """
    ordered = sorted(samples, key=lambda s: s.timestamp_ms)
    gaps: list[float] = []
    for prev, cur in zip(ordered, ordered[1:]):
        gap = cur.timestamp_ms - prev.timestamp_ms
        if gap >= 0:
            gaps.append(gap)
    return gaps


def per_device_summary(samples: Sequence[Sample], limit: int = 50) -> list[dict]:
    """Worst devices by latency, for spotting a straggler client."""
    by_device: dict[str, list[float]] = {}
    for s in samples:
        by_device.setdefault(s.device_id, []).append(s.value)
    rows = []
    for device, values in by_device.items():
        s = sorted(values)
        rows.append(
            {
                "device_id": device,
                "messages": len(s),
                "avg_ms": round(statistics.mean(s), 3),
                "p50_ms": round(percentile(s, 50), 3),
                "p99_ms": round(percentile(s, 99), 3),
                "max_ms": round(s[-1], 3),
            }
        )
    rows.sort(key=lambda r: r["p99_ms"], reverse=True)
    return rows[:limit]


def build_scenario_report(
    *,
    run_id: str,
    run_date: str,
    scenario: str,
    clients: int,
    target_rate: int,
    nodes: str,
    node_count: int,
    latency_samples: Sequence[Sample],
    latency_metric: str,
    inter_arrival_samples: Optional[Sequence[Sample]] = None,
    test_duration_s: int = 60,
    generator: Optional[dict] = None,
) -> dict:
    latencies = [s.value for s in latency_samples]
    n = len(latencies)

    # Throughput. Prefer the observed write window when we have timestamps,
    # because it reflects the actual span of the data rather than the nominal
    # test duration. Fall back to the requested duration.
    window_ms = 0.0
    if n > 1:
        window_ms = max(s.timestamp_ms for s in latency_samples) - min(
            s.timestamp_ms for s in latency_samples
        )
    duration_s = window_ms / 1000.0 if window_ms > 0 else float(test_duration_s)
    throughput = n / duration_s if duration_s > 0 else 0.0

    unique_devices = len({s.device_id for s in latency_samples})

    # The denominator must be what the load generator ACTUALLY produced, not
    # what it was asked to produce.
    #
    # The publisher falls short of nominal, and the shortfall grows with
    # per-device rate: measured on this build, 20/s -> 99.80%, 50/s -> 99.56%,
    # 100/s -> 99.17%, 200/s -> 98.40%. Dividing by the nominal rate therefore
    # caps reported efficiency at the generator's accuracy and charges the
    # difference to the pipeline as loss. At 100c/10000rps that ceiling is
    # 99.17%, which cannot distinguish a good cluster from a bad one.
    #
    # Falls back to the nominal rate only when the generator reported nothing
    # (no parsable summary line), and says so in the report rather than
    # silently reverting to a flattering-but-wrong denominator.
    gen = generator or {}
    achieved_rate = float(gen.get("achieved_msg_s") or 0.0)
    gen_reported = int(gen.get("reported") or 0)
    denominator = achieved_rate if achieved_rate > 0 else float(target_rate or 0)
    basis = "generator_achieved" if achieved_rate > 0 else "nominal_target_fallback"

    expected = denominator * duration_s if denominator else 0
    efficiency = (throughput / denominator * 100.0) if denominator else 0.0
    # Against nominal, for comparison. Kept because it is what the earlier
    # corrected matrices reported and dropping it would make them unreadable.
    nominal_efficiency = (
        (throughput / target_rate * 100.0) if target_rate else 0.0
    )
    drop_rate = max(0.0, 100.0 - efficiency)

    # The honest loss metric, and the one that should be read first.
    #
    # `efficiency` compares observed throughput against a target rate, and
    # observed throughput divides by the span of the VictoriaMetrics samples.
    # That span includes the settle period and any write lag, so it is longer
    # than the interval the generator actually published over -- and the ratio
    # falls below 100% even when not a single message is lost.
    #
    # Observed on a clean 100c/2000rps run: the publisher reported
    # published=89390 and VictoriaMetrics returned stored=89390. Identical. The
    # pipeline dropped nothing, yet efficiency read 91.49%. The entire gap was
    # the window, not loss.
    #
    # delivered/published compares counts, so it is immune to window
    # definitions. It answers "did anything get lost", which is the question
    # efficiency appears to answer but does not.
    generator_published = int(gen.get("published") or 0)
    delivery_ratio = (
        (n / generator_published * 100.0) if generator_published > 0 else None
    )

    generator_accuracy = (
        (achieved_rate / target_rate * 100.0)
        if (target_rate and achieved_rate > 0) else None
    )

    stats = latency_stats(latencies)

    report = {
        "run_id": run_id,
        "scenario": scenario,
        "timestamp": run_date,
        "configuration": {
            "num_clients": clients,
            "total_rate_msg_s": target_rate,
            "achieved_rate_msg_s": achieved_rate or None,
            "generator_published": gen.get("published") or None,
            "generator_processes_reporting": gen_reported or None,
            "efficiency_basis": basis,
            "per_device_rate_msg_s": round(target_rate / clients, 2) if clients else 0,
            "test_duration_s": test_duration_s,
            "nodes": nodes,
            "node_count": node_count,
        },
        "results": {
            "total_messages": n,
            "unique_devices": unique_devices,
            "expected_messages": int(expected),
            "duration_s": round(duration_s, 2),
            "throughput_msg_s": round(throughput, 2),
            "efficiency_pct": round(efficiency, 2),
            "efficiency_pct_nominal_basis": round(nominal_efficiency, 2),
            "delivery_ratio_pct": (
                round(delivery_ratio, 2) if delivery_ratio is not None else None
            ),
            "generator_accuracy_pct": (
                round(generator_accuracy, 2) if generator_accuracy is not None else None
            ),
            "drop_rate_pct": round(drop_rate, 2),
            "latency": stats,
            "latency_metric": latency_metric,
            "latency_definition": (
                "end-to-end: sensor ts -> VictoriaMetrics write acknowledged"
                if latency_metric == "iot_sensor_latency_ms"
                else "LEGACY: sensor ts -> nats_exit_ts (pre-VM-write). "
                "Not comparable with iot_sensor_latency_ms; see paper VIII-A."
            ),
            "latency_distribution": bucket_distribution(latencies),
        },
    }

    if inter_arrival_samples is not None:
        gaps = inter_arrival(inter_arrival_samples)
        report["results"]["inter_arrival"] = latency_stats(gaps)
        report["results"]["inter_arrival_note"] = (
            "time between consecutive writes at the consumer; NOT end-to-end latency"
        )

    if n:
        report["results"]["devices"] = per_device_summary(latency_samples)
    return report
