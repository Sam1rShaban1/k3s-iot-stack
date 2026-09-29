"""NATS JetStream control for the benchmark harness.

Why this exists
---------------
The harness originally cleared only VictoriaMetrics before each scenario. That
is not sufficient, and the omission silently invalidates every measurement.

JetStream retains published messages until they are acknowledged. The consumer
acknowledges after writing, so a message that has not yet been consumed is
still in the stream. Clearing VictoriaMetrics does not touch that backlog. On a
cluster where the consumer is running at fewer replicas than configured, or
where a previous run was interrupted, the stream accumulates thousands of
unconsumed messages. The next scenario's consumer then spends its time
replaying that backlog while the fresh messages queue behind it, so:

  * measured latency reflects replay, not the offered load;
  * measured throughput is depressed by work that belongs to an earlier run;
  * the latency distribution is bimodal for reasons that have nothing to do
    with the pipeline.

Observed directly during P0.6: a 500 msg/s scenario reported 36% efficiency
because the consumer was draining a backlog accumulated across several earlier
runs, not because the pipeline was slow. After purging the stream, the same
scenario delivered 99.68%.

So purging the stream is part of establishing a clean measurement, alongside
clearing the database. The report records which of the two happened, so a run
whose stream could not be purged is distinguishable from a clean one rather
than quietly misread.

How the purge is performed
-------------------------
NATS is exposed ClusterIP-only, which a harness on a laptop cannot resolve or
route to. Adding a NodePort would change the system under test, so the harness
tries two routes in order:

  1. direct  -- nats-py from the harness process, if HARNESS_NATS_URL is
     reachable from wherever the harness runs.
  2. in-cluster -- ``kubectl exec`` a small purge snippet into an existing
     consumer pod, which already has nats-py and cluster DNS. This is the
     normal path for a laptop-driven harness.

Force either one with HARNESS_NATS_MODE=direct|incluster.

Credentials
-----------
The IOT_DATA stream in this deployment is not behind NATS auth -- the consumer
connects with no credentials. The optional HARNESS_NATS_USER /
HARNESS_NATS_PASSWORD / HARNESS_NATS_TOKEN variables exist for deployments that
do enable it, and are never stored in the repository. The in-cluster path reads
the same NATS_URL / NATS_USER / NATS_PASSWORD variables the consumer container
already has, so it needs no credentials on the harness host at all.

If neither path succeeds the purge is skipped and the run is recorded as
unclean. That is deliberate: silently skipping it is what produced the bad
measurements in the first place.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

STREAM = "IOT_DATA"

# Pod namespace searched by the in-cluster path.
CONSUMER_NAMESPACE = "nats-consumer"


@dataclass
class NatsResult:
    purged: bool
    messages_before: int | None = None
    messages_after: int | None = None
    detail: str = ""

    def as_metadata(self) -> dict:
        return {
            "nats_purged": self.purged,
            "nats_messages_before": self.messages_before,
            "nats_messages_after": self.messages_after,
            "nats_purge_detail": self.detail,
        }


def _config(stream: str) -> dict:
    return {
        "servers": [os.environ.get("HARNESS_NATS_URL", f"nats://nats.{CONSUMER_NAMESPACE}.svc.cluster.local:4222")],
        "user": os.environ.get("HARNESS_NATS_USER") or None,
        "password": os.environ.get("HARNESS_NATS_PASSWORD") or None,
        "token": os.environ.get("HARNESS_NATS_TOKEN") or None,
        "stream": os.environ.get("HARNESS_NATS_STREAM", stream),
    }


# Snippet run inside the consumer pod. It reads connection settings from the
# container's own environment, exactly as consumer.py does, so it works whether
# or not auth is enabled and without copying credentials onto the harness host.
#
# The bounded-reconnect arguments matter: without them an unreachable NATS
# emits a retry traceback about once a second until the call is killed, flooding
# benchmark output and hiding the real error.
_IN_CLUSTER_SNIPPET = """\
import asyncio, os
from nats.aio.client import Client as NATS

STREAM = os.environ.get("HARNESS_NATS_STREAM", "IOT_DATA")


async def main():
    nc = NATS()
    await nc.connect(
        os.environ.get("NATS_URL", "nats://nats.nats.svc.cluster.local:4222"),
        user=os.environ.get("NATS_USER") or None,
        password=os.environ.get("NATS_PASSWORD") or None,
        connect_timeout=5,
        max_reconnect_attempts=0,
        allow_reconnect=False,
    )
    try:
        js = nc.jetstream()
        before = (await js.stream_info(STREAM)).state.messages
        await js.purge_stream(STREAM)
        after = (await js.stream_info(STREAM)).state.messages
        print("RESULT %d %d" % (before, after))
    finally:
        await nc.close()


asyncio.run(main())
"""

# Read-only counterpart of _IN_CLUSTER_SNIPPET, used to size the backlog without
# mutating it. The stream name is passed through the environment rather than
# interpolated into the source, so there is no string-splicing into Python.
_BACKLOG_SNIPPET = """\
import asyncio, os
from nats.aio.client import Client as NATS

STREAM = os.environ.get("HARNESS_NATS_STREAM", "IOT_DATA")


async def main():
    nc = NATS()
    await nc.connect(
        os.environ.get("NATS_URL", "nats://nats.nats.svc.cluster.local:4222"),
        user=os.environ.get("NATS_USER") or None,
        password=os.environ.get("NATS_PASSWORD") or None,
        connect_timeout=5,
        max_reconnect_attempts=0,
        allow_reconnect=False,
    )
    try:
        info = await nc.jetstream().stream_info(STREAM)
        print("RESULT %d" % info.state.messages)
    finally:
        await nc.close()


asyncio.run(main())
"""


def _find_pod() -> str:
    """Return a Running consumer pod name, or '' if none is available."""
    pod = os.environ.get("HARNESS_NATS_POD")
    if pod:
        return pod
    out = subprocess.run(
        [
            "kubectl", "get", "pods",
            "-n", CONSUMER_NAMESPACE,
            "--field-selector", "status.phase=Running",
            "-o", "jsonpath={.items[0].metadata.name}",
        ],
        capture_output=True, text=True, timeout=20, check=False,
    )
    return out.stdout.strip()


def _in_cluster_purge(stream: str) -> NatsResult:
    """Purge by running _IN_CLUSTER_SNIPPET inside a consumer pod."""
    pod = _find_pod()
    if not pod:
        return NatsResult(
            False,
            detail=(
                f"no Running pod found in namespace {CONSUMER_NAMESPACE}; "
                f"set HARNESS_NATS_POD or make NATS reachable directly. "
                f"Stream {stream} NOT purged."
            ),
        )

    # The snippet is a fixed literal -- no interpolation, no credentials, no
    # stream name spliced into a command line. The stream name is handed over
    # through the environment via `env`, so it never becomes Python source.
    proc = subprocess.run(
        [
            "kubectl", "exec", "-n", CONSUMER_NAMESPACE, pod,
            "--", "env", f"HARNESS_NATS_STREAM={stream}",
            "python3", "-c", _IN_CLUSTER_SNIPPET,
        ],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0:
        return NatsResult(
            False,
            detail=(
                f"in-cluster purge via pod {pod} failed: "
                f"{(proc.stderr or 'no stderr').strip()[-200:]}. "
                f"Stream {stream} NOT purged."
            ),
        )

    for line in (proc.stdout or "").splitlines():
        if line.startswith("RESULT "):
            _, before, after = line.split()
            return NatsResult(
                True,
                messages_before=int(before),
                messages_after=int(after),
                detail=f"purged {stream} in-cluster via {pod}/{CONSUMER_NAMESPACE}",
            )

    return NatsResult(
        False,
        detail=(
            f"in-cluster purge via pod {pod} produced no result line "
            f"(stdout={proc.stdout!r}); stream {stream} NOT purged."
        ),
    )


def _direct_purge(cfg: dict) -> NatsResult:
    """Purge from the harness process. Fails fast and quietly if unreachable."""
    try:
        import asyncio

        from nats.aio.client import Client as NATS
    except ImportError:
        return NatsResult(False, detail="nats-py is not installed on the harness host")

    async def run() -> NatsResult:
        async def _quiet(_err: Exception) -> None:
            return None

        nc = NATS()
        await nc.connect(
            cfg["servers"][0],
            user=cfg["user"],
            password=cfg["password"],
            token=cfg["token"],
            connect_timeout=5,
            max_reconnect_attempts=0,
            allow_reconnect=False,
            error_cb=_quiet,
        )
        try:
            js = nc.jetstream()
            before = (await js.stream_info(cfg["stream"])).state.messages
            await js.purge_stream(cfg["stream"])
            after = (await js.stream_info(cfg["stream"])).state.messages
            return NatsResult(
                True,
                messages_before=before,
                messages_after=after,
                detail=f"purged {cfg['stream']} directly",
            )
        finally:
            await nc.close()

    try:
        return asyncio.run(run())
    except Exception as e:  # noqa: BLE001
        return NatsResult(
            False,
            detail=f"direct purge failed ({type(e).__name__}: {e})",
        )


def _resolve_stream(stream: str) -> str:
    """Effective stream name: env override wins over the argument.

    Resolved once and passed to both routes. Previously only the direct route
    consulted HARNESS_NATS_STREAM, so an operator override silently purged the
    wrong stream in-cluster and then reported the wrong name in the result.
    """
    return os.environ.get("HARNESS_NATS_STREAM") or stream


def purge_stream(stream: str = STREAM) -> NatsResult:
    """Purge the JetStream stream. Never raises."""
    mode = os.environ.get("HARNESS_NATS_MODE", "auto").lower()
    target = _resolve_stream(stream)
    failures: list[str] = []

    if mode in ("auto", "direct"):
        result = _direct_purge(_config(target))
        if result.purged:
            return result
        failures.append(result.detail)

    if mode in ("auto", "incluster"):
        result = _in_cluster_purge(target)
        if result.purged:
            return result
        failures.append(result.detail)

    return NatsResult(
        False,
        detail=(
            f"stream {target} NOT purged; latency and throughput for this "
            f"scenario include replay of unconsumed backlog. "
            + " | ".join(failures)
        ),
    )


def _direct_backlog(cfg: dict) -> int | None:
    try:
        import asyncio

        from nats.aio.client import Client as NATS
    except ImportError:
        return None

    async def run() -> int:
        nc = NATS()
        await nc.connect(
            cfg["servers"][0],
            user=cfg["user"],
            password=cfg["password"],
            token=cfg["token"],
            connect_timeout=5,
            max_reconnect_attempts=0,
            allow_reconnect=False,
        )
        try:
            return (await nc.jetstream().stream_info(cfg["stream"])).state.messages
        finally:
            await nc.close()

    try:
        return asyncio.run(run())
    except Exception:  # noqa: BLE001
        return None


def _in_cluster_backlog(stream: str = STREAM) -> int | None:
    pod = _find_pod()
    if not pod:
        return None
    proc = subprocess.run(
        [
            "kubectl", "exec", "-n", CONSUMER_NAMESPACE, pod,
            "--", "env", f"HARNESS_NATS_STREAM={stream}",
            "python3", "-c", _BACKLOG_SNIPPET,
        ],
        capture_output=True, text=True, timeout=60, check=False,
    )
    for line in (proc.stdout or "").splitlines():
        if line.startswith("RESULT "):
            return int(line.split()[1])
    return None


def backlog_size(stream: str = STREAM) -> int | None:
    """Messages currently retained in the stream, or None if unreachable.

    Honours HARNESS_NATS_MODE exactly as purge_stream does, so an operator who
    forces a single route gets a consistent answer from both functions rather
    than one silently falling back and the other not.
    """
    mode = os.environ.get("HARNESS_NATS_MODE", "auto").lower()
    target = _resolve_stream(stream)

    if mode in ("auto", "direct"):
        size = _direct_backlog(_config(target))
        if size is not None:
            return size
    if mode in ("auto", "incluster"):
        return _in_cluster_backlog(target)
    return None
