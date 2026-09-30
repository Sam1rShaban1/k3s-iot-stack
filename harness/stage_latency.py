#!/usr/bin/env python3
"""Split a benchmark run's end-to-end latency into its stages.

Answers "where does the latency actually come from" for one scenario, using the
timestamps the consumer already publishes:

    ts                 set by the publisher, before MQTTClient_publish
    nats_exit_ts       taken by the consumer when it pulls the message
    vm_write_ack_ts    taken after VictoriaMetrics accepts the write

so

    ingest = nats_exit_ts - ts          publisher -> EMQX -> Benthos -> NATS
    write  = vm_write_ack_ts - nats_exit_ts   consumer -> VictoriaMetrics
    total  = vm_write_ack_ts - ts

Messages are paired by the VictoriaMetrics sample timestamp, which is identical
across every series one message produces because they are written in the same
request. Pairing by list position instead is wrong and was wrong here first:
the series are returned in independent orders, so a positional zip pairs
unrelated messages and produces confident nonsense (it reported a p99 of 0 ms
for a run whose real p99 was 9.4 s).

Usage:
    stage_latency.py <run_dir> [scenario]
    stage_latency.py --all <run_dir>
"""

from __future__ import annotations

import argparse
import json
import os
import sys

SERIES = ("ts", "nats_exit_ts", "vm_write_ack_ts", "latency_ms")

STAGE_LABELS = {
    "ingest": "publisher -> EMQX -> Benthos -> NATS",
    "write": "consumer -> VictoriaMetrics",
    "total": "end to end",
}


def load(raw_dir: str, scenario: str) -> dict:
    out = {}
    for name in SERIES:
        path = os.path.join(raw_dir, f"{scenario}_iot_sensor_{name}.json")
        per = {}
        if not os.path.exists(path):
            out[name] = per
            continue
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                device = rec["metric"]["device_id"]
                stamps = rec.get("timestamps") or []
                values = rec.get("values") or []
                per[device] = dict(zip(stamps, values))
        out[name] = per
    return out


def pct(values: list[float], frac: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[int(frac * (len(ordered) - 1))]


def analyse(raw_dir: str, scenario: str) -> dict:
    data = load(raw_dir, scenario)
    ingest: list[float] = []
    write: list[float] = []
    total: list[float] = []
    reported: list[float] = []

    for device, stamps in data["ts"].items():
        nats = data["nats_exit_ts"].get(device) or {}
        ack = data["vm_write_ack_ts"].get(device) or {}
        lat = data["latency_ms"].get(device) or {}
        for wtime, ts_ms in stamps.items():
            exit_ms = nats.get(wtime)
            if exit_ms is not None:
                delta = exit_ms - ts_ms
                if delta >= 0:
                    ingest.append(delta)
            ack_ms = ack.get(wtime)
            if ack_ms is not None:
                if exit_ms is not None and ack_ms - exit_ms >= 0:
                    write.append(ack_ms - exit_ms)
                if ack_ms - ts_ms >= 0:
                    total.append(ack_ms - ts_ms)
            lat_ms = lat.get(wtime)
            if lat_ms is not None:
                reported.append(lat_ms)

    return {
        "scenario": scenario,
        "aligned": len(ingest),
        "ingest": ingest,
        "write": write,
        "total": total,
        "reported": reported,
    }


def render(result: dict) -> str:
    lines = [f"scenario {result['scenario']}  aligned messages: {result['aligned']}"]
    if not result["aligned"]:
        return "\n".join(lines) + "\n  (no aligned samples; was this run's pipeline healthy?)"

    width = max(len(v) for v in STAGE_LABELS.values())
    # No "share of p99" column on purpose. The stages are different
    # distributions, and a stage can have a HIGHER p99 than the end-to-end total
    # because most messages are fast and the tail comes from one stage alone.
    # The ratio exceeds 100% and reads like a bug. Comparing p99s is the
    # meaningful operation.
    header = f"  {'stage':<{width}} {'p50':>8} {'p90':>8} {'p99':>9} {'max':>9}"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for key in ("ingest", "write"):
        values = result[key]
        if not values:
            continue
        lines.append(
            f"  {STAGE_LABELS[key]:<{width}} {pct(values, 0.5):>8.0f} "
            f"{pct(values, 0.90):>8.0f} {pct(values, 0.99):>9.0f} {max(values):>9.0f}"
        )
    lines.append(
        f"  {STAGE_LABELS['total']:<{width}} {pct(result['total'], 0.5):>8.0f} "
        f"{pct(result['total'], 0.90):>8.0f} {pct(result['total'], 0.99):>9.0f} "
        f"{max(result['total']):>9.0f}"
    )
    if result["reported"]:
        lines.append(
            f"  (consumer-reported latency_ms p99: {pct(result['reported'], 0.99):.0f} ms)"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir")
    ap.add_argument("scenario", nargs="?", default=None)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    raw = os.path.join(args.run_dir, "raw_data")
    if not os.path.isdir(raw):
        print(f"error: no raw_data under {args.run_dir}", file=sys.stderr)
        return 2

    if args.all:
        names = sorted(
            {f.split("_iot_sensor_")[0] for f in os.listdir(raw) if f.endswith(".json")}
        )
    else:
        names = [args.scenario] if args.scenario else []

    if not names:
        print("error: give a scenario name or pass --all", file=sys.stderr)
        return 2

    for name in names:
        print(render(analyse(raw, name)))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
