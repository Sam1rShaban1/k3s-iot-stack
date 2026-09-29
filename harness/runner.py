"""Benchmark runner: build the publisher, drive scenarios, collect, report."""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config as cfg
from . import nats as natsctl
from .collect import Sample, VMClient
from .report import build_scenario_report

PUBLISHER_BUILD = ["gcc", "publisher.c", "-o", "publisher", "-lpaho-mqtt3c"]


@dataclass
class RunPaths:
    root: Path
    raw: Path
    results: Path
    run_id: str
    run_date: str

    @classmethod
    def create(cls, output_root: str, now: Optional[datetime.datetime] = None) -> "RunPaths":
        now = now or datetime.datetime.now(datetime.timezone.utc)
        stamp = now.strftime("%Y%m%d_%H%M%S")
        root = Path(output_root) / stamp
        raw = root / "raw_data"
        results = root / "results"
        raw.mkdir(parents=True, exist_ok=True)
        results.mkdir(parents=True, exist_ok=True)
        return cls(
            root=root,
            raw=raw,
            results=results,
            run_id=f"run_{stamp}",
            run_date=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )


def ensure_publisher(src: str, binary: str) -> Path:
    """Compile the publisher if the binary is missing or older than the source.

    Returns an ABSOLUTE path. Path("./publisher") stringifies to "publisher",
    and a bare name makes subprocess look it up in PATH rather than the working
    directory -- which is why the first end-to-end run raised FileNotFoundError
    even though the binary had just been built.
    """
    bin_path = Path(binary)
    src_path = Path(src)
    if not src_path.exists():
        raise FileNotFoundError(f"publisher source not found: {src}")
    needs_build = not bin_path.exists() or (
        bin_path.stat().st_mtime < src_path.stat().st_mtime
    )
    if needs_build:
        print(f"  building publisher: {' '.join(PUBLISHER_BUILD)}")
        subprocess.run(PUBLISHER_BUILD, check=True)
    resolved = bin_path.resolve()
    if not os.access(resolved, os.X_OK):
        raise RuntimeError(f"publisher not executable: {resolved}")
    return resolved


def spawn_publishers(
    binary: Path, conf: cfg.HarnessConfig, clients: int, rate: int, run_id: str,
    qos: int, duration_s: int,
) -> list[subprocess.Popen]:
    """One process per simulated device, as paper §IV-E's pseudocode implies.

    The per-device interval is derived from the aggregate target rate, so total
    offered load equals the requested rate.
    """
    per_device_delay_us = int(1_000_000 * clients / rate) if rate else 0
    procs: list[subprocess.Popen] = []
    for i in range(clients):
        device = f"sensor_{cfg.HarnessConfig.scenario_name(clients, rate)}_{i}_{run_id}"
        cmd = [
            str(binary),
            "--host", conf.broker_host,
            "--port", str(conf.broker_port),
            "--device-prefix", device,
            "--delay-us", str(per_device_delay_us),
            "--topic", conf.topic,
            "--qos", str(qos),
            "--duration", str(duration_s),
        ]
        procs.append(
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        )
    return procs


def stop_publishers(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
    deadline = time.time() + 5
    for p in procs:
        remaining = max(0.1, deadline - time.time())
        try:
            p.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            p.kill()
    # Drain stderr so writers do not block on a full pipe.
    for p in procs:
        if p.stderr:
            try:
                p.stderr.read()
            except Exception:
                pass


def collect_scenario(
    vm: VMClient,
    paths: RunPaths,
    scenario: str,
    metrics: list[str],
    start_s: float | None = None,
    end_s: float | None = None,
    device_pattern: str | None = None,
) -> dict[str, list[Sample]]:
    """Export samples for one scenario, scoped to its window and devices.

    The window and device filter are what make a scenario's numbers mean
    something. Querying by metric name alone returns the whole retained
    history, so series from earlier runs are counted as this scenario's
    traffic, and their older timestamps stretch the observed write window
    across the entire retention period. That combination produced a reported
    throughput of 30 msg/s for a run that actually delivered ~494 msg/s.
    """
    out: dict[str, list[Sample]] = {}
    for metric in metrics:
        payload = vm.export(
            metric, start_s=start_s, end_s=end_s, device_pattern=device_pattern
        )
        if payload is None:
            print(f"  WARN could not export {metric}")
            out[metric] = []
            continue
        (paths.raw / f"{scenario}_{metric}.json").write_text(payload)
        from .collect import parse_export

        out[metric] = parse_export(payload)
    return out


def pick_latency(
    collected: dict[str, list[Sample]]
) -> tuple[list[Sample], str]:
    """Choose the best available latency source, preferring the new definition.

    Order:
      1. iot_sensor_latency_ms        -- end-to-end, computed by the consumer
      2. legacy join of sensor_ts and nats_exit_ts on (device_id, value)
    """
    if collected.get(cfg.METRIC_LATENCY):
        return collected[cfg.METRIC_LATENCY], cfg.METRIC_LATENCY

    ts = collected.get(cfg.METRIC_SENSOR_TS) or []
    ex = collected.get(cfg.METRIC_NATS_EXIT) or []
    if not ts or not ex:
        return [], "none"

    # Legacy join. With msg_id removed there is no per-message key, so join on
    # device_id and the sensor timestamp, pairing each sensor_ts sample with the
    # nearest nats_exit_ts for the same device at or after it. This is strictly
    # better than the previous behaviour, which collapsed everything into one
    # bucket, but it is still an approximation and is labelled as legacy.
    by_device: dict[str, list[Sample]] = {}
    for s in ex:
        by_device.setdefault(s.device_id, []).append(s)
    for v in by_device.values():
        v.sort(key=lambda s: s.timestamp_ms)

    joined: list[Sample] = []
    for s in ts:
        candidates = by_device.get(s.device_id) or []
        best = None
        for c in candidates:
            if c.timestamp_ms >= s.timestamp_ms:
                best = c
                break
        if best is not None:
            joined.append(
                Sample(
                    series="iot_sensor_latency_ms_legacy",
                    device_id=s.device_id,
                    value=best.value - s.value,
                    timestamp_ms=best.timestamp_ms,
                )
            )
    return joined, "legacy_join(sensor_ts -> nats_exit_ts)"


def run(
    conf: cfg.HarnessConfig,
    scenarios: Optional[list[tuple[int, int]]] = None,
    qos: int = 0,
    clear_before_each: bool = True,
) -> dict:
    scenarios = scenarios or conf.scenarios()
    vm = VMClient(conf.vm_url)
    paths = RunPaths.create(conf.output_root)

    print(f"run      : {paths.run_id}")
    print(f"broker   : {conf.broker_address}  topic={conf.topic}  qos={qos}")
    print(f"vm       : {conf.vm_url}")
    print(f"scenarios: {len(scenarios)}  duration={conf.test_duration_s}s "
          f"cooldown={conf.cooldown_s}s")
    print()

    reports = []
    nats_result = natsctl.NatsResult(
        False, detail="not attempted (clearing disabled for this run)"
    )
    # Use the absolute path ensure_publisher resolved. Re-deriving it from the
    # config value yields a bare "publisher", which subprocess looks for in
    # PATH instead of the working directory.
    publisher_bin = ensure_publisher(conf.publisher_src, conf.publisher_bin)
    for clients, rate in scenarios:
        name = cfg.HarnessConfig.scenario_name(clients, rate)
        print(f"[{name}] target {rate} msg/s across {clients} clients")
        if clear_before_each:
            # Clear BOTH stores. Clearing only VictoriaMetrics leaves the
            # JetStream retained history in place, and a pull consumer whose
            # durable is recreated replays all of it -- see harness/nats.py and
            # docs/findings-p0.md D13.
            accepted = vm.delete_series("iot_.*")
            if not accepted:
                print("  WARNING VictoriaMetrics rejected delete_series")
            pre = natsctl.stats()
            if pre is not None and pre.unconsumed:
                # Only genuinely outstanding work invalidates a measurement.
                # Retained-but-acknowledged messages do not.
                print(
                    f"  WARNING {pre.unconsumed} messages published but not "
                    f"acknowledged before this scenario"
                )
            nats_result = natsctl.purge_stream()
            if nats_result.purged:
                print(
                    f"  cleared: VM + NATS stream "
                    f"(had {nats_result.messages_before} retained, "
                    f"{(pre.unconsumed if pre else 0)} unconsumed)"
                )
            else:
                print(f"  WARNING {nats_result.detail}")
            # delete_series is applied in the background, so a 200 does not
            # mean the data is already gone. Wait for it, because the run's
            # own numbers depend on the database starting empty.
            cleared, remaining = vm.wait_for_delete(cfg.METRIC_LATENCY)
            if cleared:
                print("  VM confirmed empty")
            elif remaining is None:
                print(
                    "  WARNING could not verify the VictoriaMetrics delete; "
                    "measurements are still scoped to this run's window and "
                    "devices, but the database may hold earlier data"
                )
            else:
                print(
                    f"  WARNING VM still holds {remaining} series for "
                    f"{cfg.METRIC_LATENCY}; measurements are scoped to this "
                    f"run's window and devices, but the database is not cleared"
                )
            time.sleep(5)

        # Record the measurement window before publishing, and include the
        # settle period at the end, so every sample this scenario produces
        # falls inside the query bounds.
        window_start = time.time()
        procs = spawn_publishers(
            publisher_bin, conf, clients, rate, paths.run_id, qos,
            conf.test_duration_s,
        )
        time.sleep(conf.test_duration_s)
        stop_publishers(procs)
        print("  publishers stopped, settling 15s")
        time.sleep(15)
        window_end = time.time()

        # Device IDs are 'sensor_<scenario>_<index>_<run_id>_<pid>_<nanos>'.
        # The trailing .* is required: VictoriaMetrics anchors a label regex to
        # the whole value, so a pattern without it matches nothing at all and
        # the scenario silently reports zero messages.
        device_pattern = (
            f".*_{re.escape(name)}_[0-9]+_{re.escape(paths.run_id)}_.*"
        )
        collected = collect_scenario(
            vm, paths, name, cfg.ALL_METRICS,
            start_s=window_start,
            end_s=window_end + 1,
            device_pattern=device_pattern,
        )
        latency_samples, latency_metric = pick_latency(collected)
        inter = collected.get(cfg.METRIC_LATENCY) or collected.get(cfg.METRIC_NATS_EXIT)

        report = build_scenario_report(
            run_id=paths.run_id,
            run_date=paths.run_date,
            scenario=name,
            clients=clients,
            target_rate=rate,
            nodes=conf.nodes,
            node_count=conf.node_count,
            latency_samples=latency_samples,
            latency_metric=latency_metric,
            inter_arrival_samples=inter,
            test_duration_s=conf.test_duration_s,
        )
        report["results"].update(nats_result.as_metadata())
        # Record where the stream ended up. unconsumed>0 after the settle means
        # the pipeline had not drained when the window closed, which makes the
        # scenario's throughput a lower bound rather than a measurement.
        post = natsctl.stats()
        if post is not None:
            report["results"].update(post.as_metadata())
            report["results"]["nats_drained"] = post.unconsumed == 0
        report_path = paths.results / f"{name}_report.json"
        report_path.write_text(json.dumps(report, indent=2))
        reports.append(report)

        res = report["results"]
        lat = res["latency"]
        print(
            f"  stored={res['total_messages']}  devices={res['unique_devices']}  "
            f"throughput={res['throughput_msg_s']} msg/s  "
            f"eff={res['efficiency_pct']}%  p99={lat.get('p99_ms')} ms  "
            f"[{latency_metric}]"
        )

        if conf.cooldown_s:
            print(f"  cooldown {conf.cooldown_s}s")
            time.sleep(conf.cooldown_s)

    summary = {
        "run_id": paths.run_id,
        "run_date": paths.run_date,
        "broker": conf.broker_address,
        "topic": conf.topic,
        "qos": qos,
        "vm_url": conf.vm_url,
        "nodes": conf.nodes,
        "node_count": conf.node_count,
        "latency_metric": reports[0]["results"]["latency_metric"] if reports else None,
        "nats_purged_all_scenarios": all(
            r["results"].get("nats_purged") for r in reports
        ) if reports else False,
        "nats_drained_all_scenarios": all(
            r["results"].get("nats_drained") for r in reports
        ) if reports else False,
        "total_scenarios": len(reports),
        "scenarios": [
            {
                "name": r["scenario"],
                "total_messages": r["results"]["total_messages"],
                "unique_devices": r["results"]["unique_devices"],
                "throughput_msg_s": r["results"]["throughput_msg_s"],
                "efficiency_pct": r["results"]["efficiency_pct"],
                "drop_rate_pct": r["results"]["drop_rate_pct"],
                "avg_latency_ms": r["results"]["latency"]["avg_ms"],
                "p50_latency_ms": r["results"]["latency"]["p50_ms"],
                "p95_latency_ms": r["results"]["latency"]["p95_ms"],
                "p99_latency_ms": r["results"]["latency"]["p99_ms"],
                "latency_metric": r["results"]["latency_metric"],
            }
            for r in reports
        ],
    }
    (paths.results / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nwrote {paths.results / 'summary.json'}")
    return summary
