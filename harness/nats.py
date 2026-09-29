"""NATS JetStream control for the benchmark harness.

Why purging exists
------------------
JetStream can retain messages a consumer has already acknowledged, and this
stream is configured to do exactly that. IOT_DATA is created with
``retention: limits`` and no ``max_msgs``, ``max_age`` or ``max_bytes``, so
nothing is ever evicted and ``stream.state.messages`` is a count of everything
ever published, not a backlog.

That has two consequences the harness has to respect.

1. **Bounded storage per run.** Without a purge, retained history from every
   earlier scenario is still resident when the next one starts, and the
   measurements have to work around it.
2. **Consumer replay after a durable is recreated.** A pull consumer with
   ``deliver_policy: all`` starts at the beginning of the stream. If the
   durable is deleted or recreated -- a pod restart with a fresh name, a
   config change, a rebuilt JetStream store -- it replays the entire retained
   history while the new scenario's messages queue behind it. Measured
   throughput then reflects replay rather than the offered load.

Purge is therefore part of establishing a clean measurement, alongside clearing
the database, and the report records whether it happened so an unclean run is
distinguishable from a clean one.

What "backlog" means here
-------------------------
Two different numbers, and conflating them caused a real error. See D13.

* ``unconsumed()`` -- ``num_pending + num_ack_pending``: published but not yet
  acknowledged. This is the number that says whether a consumer is replaying.
* ``retained()`` -- ``stream.state.messages``: what the server is still
  storing. It never falls on ack, and it grows without bound on this stream.

An early version of this module reported ``state.messages`` as "backlog" and
used it to decide a run was contaminated. That is wrong: it flagged every run
after the first, including ones whose consumer was provably idle
(``num_pending=0``, ``delivered.stream_seq == stream.last_seq``).

How the purge is performed
-------------------------
NATS is exposed ClusterIP-only, which a harness on a laptop cannot resolve or
route to. Adding a NodePort would change the system under test, so the harness
tries two routes in order:

  1. direct  -- nats-py from the harness process, if HARNESS_NATS_URL is
     reachable from wherever the harness runs.
  2. in-cluster -- ``kubectl exec`` a small snippet into an existing consumer
     pod, which already has nats-py and cluster DNS. This is the normal path
     for a laptop-driven harness.

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

import json
import os
import subprocess
from dataclasses import dataclass

STREAM = "IOT_DATA"
DURABLE = "metrics-consumer-pull"

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


@dataclass
class JetStreamStats:
    """Stream retention and consumer position, which are different things.

    retained  -- messages the server still stores. With retention=limits and no
                 max_msgs/max_age/max_bytes this grows forever; acking does not
                 remove a message.
    unconsumed-- num_pending + num_ack_pending, i.e. published but not yet
                 acknowledged. This is the number that says whether a consumer
                 is replaying earlier work.
    """

    retained: int
    num_pending: int
    num_ack_pending: int
    num_redelivered: int
    bytes_stored: int
    retention: str
    max_msgs: int
    max_age: float
    max_bytes: int

    @property
    def unconsumed(self) -> int:
        """Published but not yet acknowledged.

        Derived rather than stored so it cannot disagree with the two fields it
        is defined from.
        """
        return self.num_pending + self.num_ack_pending

    def as_metadata(self) -> dict:
        return {
            "nats_retained": self.retained,
            "nats_unconsumed": self.unconsumed,
            "nats_num_pending": self.num_pending,
            "nats_num_ack_pending": self.num_ack_pending,
            "nats_num_redelivered": self.num_redelivered,
            "nats_bytes_stored": self.bytes_stored,
            "nats_retention": self.retention,
            "nats_max_msgs": self.max_msgs,
            "nats_max_age": self.max_age,
            "nats_max_bytes": self.max_bytes,
        }

    @property
    def unbounded(self) -> bool:
        """True when nothing will ever evict a message from this stream."""
        return (
            self.max_msgs in (-1, 0)
            and self.max_age in (0, 0.0)
            and self.max_bytes in (-1, 0)
        )


async def _read_stats(js, stream: str, durable: str) -> JetStreamStats:
    """Build JetStreamStats from a live JetStream context.

    Shared by the direct and in-cluster routes so both read identical fields.

    A missing consumer is reported as zero unconsumed rather than an error: the
    question the harness asks is "is there work outstanding for the consumer",
    and no consumer means none is. The purge is what a run needs then.
    """
    info = await js.stream_info(stream)
    state = info.state
    num_pending = num_ack = num_redelivered = 0
    try:
        ci = await js.consumer_info(stream, durable)
        num_pending = int(ci.num_pending or 0)
        num_ack = int(ci.num_ack_pending or 0)
        num_redelivered = int(ci.num_redelivered or 0)
    except Exception:  # noqa: BLE001
        pass
    cfg = info.config
    return JetStreamStats(
        retained=int(state.messages or 0),
        num_pending=num_pending,
        num_ack_pending=num_ack,
        num_redelivered=num_redelivered,
        bytes_stored=int(state.bytes or 0),
        retention=str(getattr(cfg, "retention", "") or ""),
        max_msgs=int(getattr(cfg, "max_msgs", -1) or -1),
        max_age=float(getattr(cfg, "max_age", 0) or 0),
        max_bytes=int(getattr(cfg, "max_bytes", -1) or -1),
    )


def _durable() -> str:
    return os.environ.get("HARNESS_NATS_DURABLE", DURABLE)


def _config(stream: str) -> dict:
    return {
        "servers": [os.environ.get("HARNESS_NATS_URL", f"nats://nats.{CONSUMER_NAMESPACE}.svc.cluster.local:4222")],
        "user": os.environ.get("HARNESS_NATS_USER") or None,
        "password": os.environ.get("HARNESS_NATS_PASSWORD") or None,
        "token": os.environ.get("HARNESS_NATS_TOKEN") or None,
        "stream": _resolve_stream(stream),
        "durable": _durable(),
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

# Read-only counterpart of _IN_CLUSTER_SNIPPET: stream retention and consumer
# position. Emits the same fields as JetStreamStats so the two routes agree.
# Stream and durable names arrive via the environment, never spliced into the
# source.
_STATS_SNIPPET = """\
import asyncio, json, os
from nats.aio.client import Client as NATS

STREAM = os.environ.get("HARNESS_NATS_STREAM", "IOT_DATA")
DURABLE = os.environ.get("HARNESS_NATS_DURABLE", "metrics-consumer-pull")


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
        info = await js.stream_info(STREAM)
        state, cfg = info.state, info.config
        pending = ack = redelivered = 0
        try:
            ci = await js.consumer_info(STREAM, DURABLE)
            pending = int(ci.num_pending or 0)
            ack = int(ci.num_ack_pending or 0)
            redelivered = int(ci.num_redelivered or 0)
        except Exception:
            pass
        print("RESULT " + json.dumps({
            "retained": int(state.messages or 0),
            "num_pending": pending,
            "num_ack_pending": ack,
            "num_redelivered": redelivered,
            "bytes_stored": int(state.bytes or 0),
            "retention": str(getattr(cfg, "retention", "") or ""),
            "max_msgs": int(getattr(cfg, "max_msgs", -1) or -1),
            "max_age": float(getattr(cfg, "max_age", 0) or 0),
            "max_bytes": int(getattr(cfg, "max_bytes", -1) or -1),
        }))
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
            f"stream {target} NOT purged; retained messages from earlier runs "
            f"remain in the stream. See docs/findings-p0.md D13 for what that "
            f"does and does not affect. " + " | ".join(failures)
        ),
    )


def _probe_direct(cfg: dict) -> "JetStreamStats | None":
    try:
        import asyncio

        from nats.aio.client import Client as NATS
    except ImportError:
        return None

    async def run() -> JetStreamStats:
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
            return await _read_stats(nc.jetstream(), cfg["stream"], cfg["durable"])
        finally:
            await nc.close()

    try:
        return asyncio.run(run())
    except Exception:  # noqa: BLE001
        return None


def _probe_in_cluster(stream: str = STREAM) -> "JetStreamStats | None":
    pod = _find_pod()
    if not pod:
        return None
    proc = subprocess.run(
        [
            "kubectl", "exec", "-n", CONSUMER_NAMESPACE, pod,
            "--", "env",
            f"HARNESS_NATS_STREAM={stream}",
            f"HARNESS_NATS_DURABLE={_durable()}",
            "python3", "-c", _STATS_SNIPPET,
        ],
        capture_output=True, text=True, timeout=60, check=False,
    )
    for line in (proc.stdout or "").splitlines():
        if line.startswith("RESULT "):
            fields = json.loads(line.split(" ", 1)[1])
            # unconsumed is derived; ignore it if an older snippet sent it.
            fields.pop("unconsumed", None)
            return JetStreamStats(**fields)
    return None


def stats(stream: str = STREAM) -> "JetStreamStats | None":
    """Read stream retention and consumer position, or None if unreachable.

    Honours HARNESS_NATS_MODE exactly as purge_stream does, so an operator who
    forces a single route gets a consistent answer from both functions.
    """
    mode = os.environ.get("HARNESS_NATS_MODE", "auto").lower()
    target = _resolve_stream(stream)

    if mode in ("auto", "direct"):
        found = _probe_direct(_config(target))
        if found is not None:
            return found
    if mode in ("auto", "incluster"):
        return _probe_in_cluster(target)
    return None


def unconsumed(stream: str = STREAM) -> int | None:
    """Messages published but not yet acknowledged by the durable consumer.

    This is the number that determines whether a scenario's consumer is
    replaying earlier work: num_pending (not yet delivered) plus num_ack_pending
    (delivered, awaiting ack).

    This is NOT stream.state.messages. The IOT_DATA stream is created with
    retention=limits and no max_msgs, max_age or max_bytes, so the server
    retains every message indefinitely and acknowledging one does not remove
    it. Reading state.messages as a "backlog" therefore counts the entire
    retained history, which is why a probe of a fully-consumed stream reported
    18,236 while the consumer sat at num_pending=0. See D13.
    """
    found = stats(stream)
    return None if found is None else found.unconsumed


def retained(stream: str = STREAM) -> int | None:
    """Messages the server is still storing, regardless of ack state.

    Grows without bound on this stream. Useful for spotting unbounded storage
    growth, which is a disk-exhaustion risk on SD-card nodes.
    """
    found = stats(stream)
    return None if found is None else found.retained
