"""CLI entry point:  python3 -m harness <command>"""

from __future__ import annotations

import argparse
import json
import sys

from . import config as cfg
from . import preflight as pf
from . import runner


def _parse_list(value: str | None) -> list[int] | None:
    if not value:
        return None
    return [int(x) for x in value.replace(" ", "").split(",") if x]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m harness",
        description="Benchmark harness for the K3s IoT pipeline.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run one or more load scenarios")
    run_p.add_argument("--clients", type=_parse_list, help="e.g. 10,100")
    run_p.add_argument("--rates", type=_parse_list, help="e.g. 100,500,1000,2000")
    run_p.add_argument("--broker", help="host or host:port; else discovered")
    run_p.add_argument("--vm-url", help="VictoriaMetrics base URL")
    run_p.add_argument("--topic", help="MQTT topic")
    run_p.add_argument("--qos", type=int, default=0, choices=(0, 1, 2))
    run_p.add_argument("--duration", type=int, help="test duration seconds")
    run_p.add_argument("--cooldown", type=int, help="cooldown seconds")
    run_p.add_argument("--output-root", help="where to write run directories")
    run_p.add_argument("--no-clear", action="store_true",
                        help="do not clear VictoriaMetrics before each scenario")

    pre_p = sub.add_parser("preflight", help="verify the pipeline before running")
    pre_p.add_argument("--no-components", action="store_true",
                       help="cluster-level checks only")
    pre_p.add_argument("--json", action="store_true", help="machine-readable output")

    cfg_p = sub.add_parser("config", help="print resolved configuration")
    cfg_p.add_argument("--broker")
    cfg_p.add_argument("--vm-url")

    sub.add_parser("validate", help="static checks over the declarative config")

    args = parser.parse_args(argv)

    if args.command == "run":
        conf = cfg.HarnessConfig.resolve(
            broker=args.broker,
            vm_url=args.vm_url,
            topic=args.topic,
            clients=args.clients,
            rates=args.rates,
            test_duration_s=args.duration,
            cooldown_s=args.cooldown,
            output_root=args.output_root,
        )
        print("resolved configuration:")
        print(f"  broker      : {conf.broker_address}")
        print(f"  topic       : {conf.topic}")
        print(f"  vm          : {conf.vm_url}")
        print(f"  nodes       : {conf.nodes} ({conf.node_count})")
        print(f"  duration    : {conf.test_duration_s}s   cooldown {conf.cooldown_s}s")
        print(f"  scenarios   : {[cfg.HarnessConfig.scenario_name(c,r) for c,r in conf.scenarios()]}")
        print()
        summary = runner.run(
            conf,
            qos=args.qos,
            clear_before_each=not args.no_clear,
        )
        print()
        print(json.dumps(summary["scenarios"], indent=2))
        return 0

    if args.command == "preflight":
        result = pf.run_preflight(components=not args.no_components)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(result.render())
        return 0 if result.ok else 1

    if args.command == "config":
        conf = cfg.HarnessConfig.resolve(
            broker=args.broker, vm_url=args.vm_url
        )
        print(json.dumps({
            "broker_host": conf.broker_host,
            "broker_port": conf.broker_port,
            "topic": conf.topic,
            "vm_url": conf.vm_url,
            "nodes": conf.nodes,
            "node_count": conf.node_count,
            "scenarios": [cfg.HarnessConfig.scenario_name(c, r) for c, r in conf.scenarios()],
        }, indent=2))
        return 0

    if args.command == "validate":
        from . import validate as vd

        result = vd.validate()
        print(result.render())
        return 0 if result.ok else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
