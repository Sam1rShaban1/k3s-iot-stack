"""Where do messages disappear between the publisher and NATS?

At 5000 msg/s the publisher emits ~223k messages and JetStream retains ~89k, so
roughly 60% vanish somewhere between the MQTT broker and the stream (D28). The
obvious instrument -- Benthos's `input_received` and `output_sent` -- answers
nothing when read as absolutes, because both are cumulative since pod start.
Read absolutely they look like this run's traffic and invert the conclusion.

So sample every Benthos replica before and after a run and report DELTAS:

  input_received  vs  published   did Benthos receive what the publisher sent?
  output_sent     vs  input_received   did Benthos forward what it received?

The first column localises the loss to EMQX->Benthos, the second to
Benthos->NATS. Absolutes cannot distinguish these; deltas can.

Usage:
    python3 harness/ingest_deltas.py snapshot out.json
    python3 harness/ingest_deltas.py delta before.json after.json
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

COUNTERS = ("input_received", "output_sent", "output_error", "input_connection_lost")
_LINE = re.compile(r"^(\w+)\{[^}]*\}\s+([0-9.e+-]+)$")


def _kubectl(args: list[str]) -> str:
    out = subprocess.run(
        ["kubectl"] + args, capture_output=True, text=True, timeout=60
    )
    return out.stdout


def snapshot() -> dict:
    """Total each counter across all Benthos replicas."""
    # Quoted, or the shell strips the braces and kubectl returns nothing.
    pods = _kubectl(["get", "pods", "-n", "benthos", "-l", "app=benthos",
                     "-o", "jsonpath={.items[*].metadata.name}"]).split()
    totals = {c: 0.0 for c in COUNTERS}
    per_pod = {}
    for pod in pods:
        metrics = _kubectl(["exec", "-n", "benthos", pod,
                            "--", "wget", "-qO-", "http://localhost:4195/metrics"])
        found = {c: 0.0 for c in COUNTERS}
        for line in metrics.splitlines():
            m = _LINE.match(line.strip())
            if m and m.group(1) in found:
                try:
                    found[m.group(1)] += float(m.group(2))
                except ValueError:
                    pass
        per_pod[pod] = found
        for k, v in found.items():
            totals[k] += v
    return {"pods": per_pod, "totals": totals}


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "snapshot":
        data = snapshot()
        path = argv[1] if len(argv) > 1 else "-"
        text = json.dumps(data, indent=2)
        if path == "-":
            print(text)
        else:
            with open(path, "w") as fh:
                fh.write(text)
            print(f"  snapshot -> {path}")
        return 0

    if len(argv) >= 3 and argv[0] == "delta":
        before = json.load(open(argv[1]))["totals"]
        after = json.load(open(argv[2]))["totals"]
        print(f"  {'counter':24s} {'before':>12s} {'after':>12s} {'delta':>12s}")
        for c in COUNTERS:
            b, a = before.get(c, 0.0), after.get(c, 0.0)
            print(f"  {c:24s} {b:12.0f} {a:12.0f} {a - b:12.0f}")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
