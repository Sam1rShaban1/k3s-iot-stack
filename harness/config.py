"""Configuration for the benchmark harness.

Every network address and knob is resolved here, in priority order:

    1. explicit constructor argument
    2. environment variable
    3. discovery from the live cluster (Service / LoadBalancer)
    4. built-in default

The previous implementation (run_test.sh) hardcoded 192.168.1.50 in five
places. That is the reason the broker address in the repository was not
reproducible: see docs/alternatives-considered.md discrepancy D2.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Optional

# Metrics series the harness reads. Kept as constants because the report schema
# and the detectors (P3) both key off these names.
METRIC_LATENCY = "iot_sensor_latency_ms"
METRIC_SENSOR_TS = "iot_sensor_ts"
METRIC_NATS_EXIT = "iot_sensor_nats_exit_ts"
METRIC_VM_ACK = "iot_sensor_vm_write_ack_ts"
METRIC_TEMP = "iot_sensor_temp"
METRIC_HUM = "iot_sensor_hum"
METRIC_PM25 = "iot_sensor_pm25"
METRIC_PM10 = "iot_sensor_pm10"

ALL_METRICS = [
    METRIC_LATENCY,
    METRIC_SENSOR_TS,
    METRIC_NATS_EXIT,
    METRIC_VM_ACK,
    METRIC_TEMP,
    METRIC_HUM,
    METRIC_PM25,
    METRIC_PM10,
]


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def kubectl_json(*args: str) -> Optional[dict]:
    """Best-effort kubectl JSON. Returns None instead of raising.

    The harness must be able to run against a cluster it can only partly reach,
    and must report a clear preflight failure rather than a stack trace.
    """
    try:
        out = subprocess.run(
            ["kubectl", "-o", "json", *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        import json

        return json.loads(out.stdout)
    except Exception:
        return None


def discover_emqx_address(namespace: str = "emqx") -> Optional[tuple[str, int]]:
    """Resolve the broker's reachable address from the cluster.

    Prefers an explicit LoadBalancer ingress address (what the paper's
    192.168.1.241 claim describes), then a NodePort, and pairs the chosen
    ingress port with the Service's own targetPort so a renamed port such as
    `mqtt` still resolves.
    """
    svc = kubectl_json("get", "svc", "emqx", "-n", namespace)
    if not svc:
        return None

    status = svc.get("status") or {}
    ingresses = status.get("loadBalancer", {}).get("ingress") or []
    if ingresses:
        ip = ingresses[0].get("ip")
        if ip:
            return ip, 1883

    ports = (svc.get("spec") or {}).get("ports") or []
    mqtt = _find_mqtt_port(ports)
    if mqtt and mqtt.get("nodePort"):
        node_ip = _master_node_ip()
        if node_ip:
            return node_ip, int(mqtt["nodePort"])

    if mqtt:
        return "127.0.0.1", int(mqtt.get("port", 1883))
    return None


def _find_mqtt_port(ports: list[dict]) -> Optional[dict]:
    for p in ports:
        if p.get("name") in ("mqtt", "mqtt-tcp", "1883"):
            return p
    for p in ports:
        if int(p.get("port", 0)) == 1883:
            return p
    return None


def _master_node_ip() -> Optional[str]:
    nodes = kubectl_json("get", "nodes")
    if not nodes:
        return None
    for item in nodes.get("items", []):
        labels = (item.get("metadata") or {}).get("labels") or {}
        if "node-role.kubernetes.io/control-plane" in labels:
            for addr in (item.get("status") or {}).get("addresses", []):
                if addr.get("type") == "InternalIP":
                    return addr.get("address")
    return None


@dataclass
class HarnessConfig:
    """Resolved configuration for a benchmark session."""

    broker_host: str
    broker_port: int = 1883
    topic: str = "sensors/data"

    # VictoriaMetrics. Prefer the in-cluster Service via port-forward or an
    # explicit base URL; the NodePort is the fallback that run_test.sh used.
    vm_url: str = "http://192.168.1.50:30000"

    publisher_bin: str = "./publisher"
    publisher_src: str = "publisher.c"

    test_duration_s: int = 60
    cooldown_s: int = 30

    scenario_clients: list[int] = field(default_factory=lambda: [10, 100])
    scenario_rates: list[int] = field(default_factory=lambda: [100, 500, 1000, 2000])

    node_count: int = 5
    nodes: str = "raspberrypi,pi7,pi2,pi3,pi4"

    output_root: str = "./benchmarks"

    @classmethod
    def resolve(cls, **overrides) -> "HarnessConfig":
        """Build config from overrides, then env, then discovery, then default."""
        resolved: dict = {}

        broker = overrides.get("broker") or _env("HARNESS_BROKER")
        if broker:
            if ":" in broker:
                host, port = broker.rsplit(":", 1)
                resolved["broker_host"], resolved["broker_port"] = host, int(port)
            else:
                resolved["broker_host"] = broker
        else:
            discovered = discover_emqx_address()
            if discovered:
                resolved["broker_host"], resolved["broker_port"] = discovered
            else:
                resolved["broker_host"] = "127.0.0.1"

        resolved.setdefault("broker_port", 1883)
        resolved["topic"] = overrides.get("topic") or _env("HARNESS_TOPIC", "sensors/data")
        resolved["vm_url"] = (
            overrides.get("vm_url")
            or _env("HARNESS_VM_URL")
            or _env("VICTORIA_METRICS_URL", "http://192.168.1.50:30000")
        ).rstrip("/")

        for field_name in ("test_duration_s", "cooldown_s", "node_count"):
            env_name = f"HARNESS_{field_name.upper()}"
            raw = _env(env_name)
            if overrides.get(field_name) is not None:
                resolved[field_name] = overrides[field_name]
            elif raw is not None:
                resolved[field_name] = int(raw)

        if overrides.get("clients"):
            resolved["scenario_clients"] = list(overrides["clients"])
        if overrides.get("rates"):
            resolved["scenario_rates"] = list(overrides["rates"])

        resolved["nodes"] = overrides.get("nodes") or _env("HARNESS_NODES", cls.nodes)
        resolved["output_root"] = overrides.get("output_root") or _env(
            "HARNESS_OUTPUT_ROOT", "./benchmarks"
        )
        return cls(**resolved)

    @property
    def broker_address(self) -> str:
        return f"{self.broker_host}:{self.broker_port}"

    def scenarios(self) -> list[tuple[int, int]]:
        """Scenario matrix as (clients, total_rate_msg_s)."""
        return [(c, r) for c in self.scenario_clients for r in self.scenario_rates]

    @staticmethod
    def scenario_name(clients: int, rate: int) -> str:
        return f"{clients}c_{rate}r"
