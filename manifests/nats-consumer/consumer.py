#!/usr/bin/env python3
"""IoT pipeline consumer: JetStream -> VictoriaMetrics.

SOURCE OF TRUTH. The ConfigMap the Deployment mounts is generated from this file
by kustomize (`configMapGenerator` in kustomization.yaml), so editing this file
and applying the kustomization is the whole workflow.

This replaces an earlier arrangement in which the script was embedded as a
`data['consumer.py']` key inside configmap.yaml, with a second copy checked in
next to it for readability. That had two failure modes, both observed:

  * the copies drifted, and the "readable" copy was the one people reviewed;
  * editing configmap.yaml did not change the pod template, so `kubectl apply`
    reported "successfully rolled out" while every pod kept running the old
    code. The bounded-retention fix below was deployed that way and silently
    did nothing.

Kustomize now derives the ConfigMap from this file and stamps a content hash
into the pod template, so an edit here always produces a real rollout.

Notes on the measurement contract, which is what this file is really about:

  * Latency is sensor timestamp -> VictoriaMetrics acknowledging the write.
    That requires two passes: the acknowledgement timestamp does not exist
    until the request carrying the payload has returned, so the payload goes
    first and the stamps follow in a second request. Emitting the stamp in the
    first request would silently measure sensor -> write-start.
  * ack() happens only after a successful write, so a failed write is nak'd
    and redelivered rather than lost. Acking on enqueue, as an earlier
    version did, made the paper's "a clean pod restart loses nothing" false.
  * Every emitted series is keyed on device_id alone. Nothing here is
    unbounded; that is the point of the paper's cardinality fix, and an earlier
    version carried a per-message msg_id that had to be removed.
  * A pull consumer shares a durable between replicas so JetStream distributes
    batches. A push consumer allows only one bound subscription, so with
    replicas: 3 two of the three blocked in subscribe() and never consumed.
  * The JetStream stream is created with explicit retention limits, because
    the server otherwise retains every message forever on a node whose disk is
    a 57 GB SD card.
"""
import asyncio
import json
import time
import os
import aiohttp
from nats.aio.client import Client as NATS
from nats.js.api import AckPolicy, ConsumerConfig

# nats-py 2.14 puts TimeoutError in nats.errors, not nats.js.errors, and
# has no EmptyError at all. Import defensively so a version change in the
# base image cannot crash the consumer at import time.
try:
    from nats.errors import TimeoutError as NatsTimeoutError
except ImportError:  # pragma: no cover
    NatsTimeoutError = asyncio.TimeoutError

NATS_URL = os.getenv("NATS_URL", "nats://nats.nats.svc.cluster.local:4222")
VM_URL = os.getenv(
    "VICTORIA_METRICS_URL",
    "http://victoriametrics-victoria-metrics-single-server."
    "victoriametrics.svc.cluster.local:8428/api/v1/import/prometheus",
)
STREAM = "IOT_DATA"
SUBJECT = "iot.data"
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5000"))
MAX_CONCURRENT_SENDS = int(os.getenv("MAX_CONCURRENT_SENDS", "32"))
WRITE_TIMEOUT_S = float(os.getenv("WRITE_TIMEOUT_S", "30"))
NAK_BACKOFF_S = float(os.getenv("NAK_BACKOFF_S", "1.0"))
NAK_BACKOFF_MAX_S = float(os.getenv("NAK_BACKOFF_MAX_S", "30.0"))
# Pull-consumer parameters. P0.6: the durable is shared by every replica so
# JetStream distributes batches between them.
DURABLE = os.getenv("CONSUMER_DURABLE", "metrics-consumer-pull")
FETCH_TIMEOUT_S = float(os.getenv("FETCH_TIMEOUT_S", "1.0"))
ACK_WAIT_S = float(os.getenv("ACK_WAIT_S", "60"))
MAX_ACK_PENDING = int(os.getenv("MAX_ACK_PENDING", "10000"))
# -1 == redeliver indefinitely, which is what paper section VII describes.
MAX_DELIVER = int(os.getenv("MAX_DELIVER", "-1"))
# D13: bound the JetStream stream. Both limits are set so that neither a
# sustained flood (bytes) nor a quiet period followed by a burst (age) can
# exhaust the node's disk. 3600 s of history is well beyond any benchmark
# window, and 512 MB is a fraction of pi7's free space.
STREAM_MAX_AGE_S = int(os.getenv("STREAM_MAX_AGE_S", "3600"))
STREAM_MAX_BYTES = int(os.getenv("STREAM_MAX_BYTES", str(512 * 1024 * 1024)))

_nak_backoff = NAK_BACKOFF_S

def now_ms():
    return int(time.time() * 1000)

async def setup():
    nc = NATS()
    await nc.connect(NATS_URL)
    js = nc.jetstream()
    # D13: the stream used to be created with no limits at all --
    # retention=limits with max_msgs=-1, max_age=0, max_bytes=-1. Nothing
    # was ever evicted and acking a message did not remove it, so stored
    # data grew without bound. Measured: 18,236 messages = 3.1 MB, so about
    # 170 B each, which is roughly 14.7 GB/day at a sustained 1000 msg/s
    # against a 57 GB SD card that also carries K3s, Longhorn and the
    # JetStream store. Bounded now, on both an age and a byte budget so
    # neither a quiet period nor a flood can fill the disk.
    stream_cfg = dict(
        name=STREAM,
        subjects=[SUBJECT],
        max_age=STREAM_MAX_AGE_S,
        max_bytes=STREAM_MAX_BYTES,
        discard="old",
    )
    for i in range(10):
        try:
            await js.add_stream(**stream_cfg)
            print(
                "Stream: %s ready (max_age=%ds max_bytes=%d)"
                % (STREAM, STREAM_MAX_AGE_S, STREAM_MAX_BYTES)
            )
            break
        except Exception as e:
            # Already exists: converge its config rather than leaving an
            # unbounded stream in place because it predates this fix.
            if "already in use" in str(e) or "stream name already" in str(e).lower():
                try:
                    await js.update_stream(**stream_cfg)
                    print(
                        "Stream: %s updated to bounded retention "
                        "(max_age=%ds max_bytes=%d)"
                        % (STREAM, STREAM_MAX_AGE_S, STREAM_MAX_BYTES)
                    )
                    break
                except Exception as e2:
                    print("Stream update attempt %d failed: %s" % (i + 1, e2))
            else:
                print("Stream attempt %d failed: %s" % (i + 1, e))
            await asyncio.sleep(1)
    return nc, js

def format_metrics(data, nats_exit_ts):
    """Render one message as a bounded set of Prometheus series.

    Every series is keyed on device_id alone. Nothing emitted here is
    unbounded -- that is the entire point of the paper's §VI-C fix.

    The acknowledgement timestamp is deliberately absent. It cannot be
    known until the write carrying these samples has returned, so emitting
    it here would mean stamping it *before* the POST. The previous version
    did exactly that while the field was documented as "stamped after the
    VictoriaMetrics POST returns", which made latency_ms measure
    sensor -> write-start and silently exclude the entire write -- the very
    quantity the paper claims to measure end to end. See format_metrics_ack.
    """
    device = data.get("device_id", "unknown")
    data["nats_exit_ts"] = nats_exit_ts
    if "benthos_entry_ts" in data:
        data["benthos_to_nats_latency_ms"] = nats_exit_ts - data["benthos_entry_ts"]
    return _render(data, device)

def format_metrics_ack(data, ack_ts):
    """Second pass: acknowledgement stamps, written after the POST returns.

    Carries vm_write_ack_ts and latency_ms only. The payload is already
    stored by the time this runs, so a failure here costs the latency
    series rather than the measurement.
    """
    device = data.get("device_id", "unknown")
    data["vm_write_ack_ts"] = ack_ts
    sensor_ts = data.get("ts")
    if sensor_ts is not None:
        data["latency_ms"] = ack_ts - sensor_ts
    return _render(data, device)

def _render(data, device):
    lines = []
    for k, v in data.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            lines.append('iot_sensor_%s{device_id="%s"} %s' % (k, device, v))
    return lines

async def process_chunk(session, chunk):
    """Write one chunk and settle every message in it exactly once."""
    global _nak_backoff

    # Per-message NATS exit stamp, taken before the write.
    stamped = [(msg, data, now_ms()) for (msg, data) in chunk]

    # Pass 1: the payload itself, with no acknowledgement timestamp.
    body = "\n".join(
        line
        for _msg, data, exit_ts in stamped
        for line in format_metrics(data, exit_ts)
    )

    ok = False
    ack_ts = None
    try:
        async with session.post(
            VM_URL,
            data=body,
            timeout=aiohttp.ClientTimeout(total=WRITE_TIMEOUT_S),
        ) as resp:
            if resp.status in (200, 204):
                ok = True
                # Taken here, after the server accepted the write.
                ack_ts = now_ms()
            else:
                text = await resp.text()
                print("VM error: %s - %s" % (resp.status, text[:200]))
    except Exception as e:
        print("VM error: %s" % e)

    if ok:
        _nak_backoff = NAK_BACKOFF_S
        # Pass 2: acknowledgement stamps, so latency_ms really is
        # sensor -> write acknowledged.
        ack_body = "\n".join(
            line
            for _msg, data, _exit_ts in stamped
            for line in format_metrics_ack(data, ack_ts)
        )
        try:
            async with session.post(
                VM_URL,
                data=ack_body,
                timeout=aiohttp.ClientTimeout(total=WRITE_TIMEOUT_S),
            ) as resp:
                if resp.status not in (200, 204):
                    print("VM ack-stamp write rejected: %s" % resp.status)
        except Exception as e:
            print("VM ack-stamp write failed: %s" % e)
    else:
        _nak_backoff = min(_nak_backoff * 2, NAK_BACKOFF_MAX_S)
        print(
            "write failed; nak batch of %d, backing off %.1fs"
            % (len(chunk), _nak_backoff)
        )
        await asyncio.sleep(_nak_backoff)

    for msg, _data in chunk:
        try:
            if ok:
                await msg.ack()
            else:
                await msg.nak()
        except Exception as e:
            print("settle failed: %s" % e)

async def batch_sender(session, batch_queue):
    """Single drain path. Polls the queue; chunks by BATCH_SIZE."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SENDS)

    async def guarded(chunk):
        async with semaphore:
            await process_chunk(session, chunk)

    while True:
        if not batch_queue:
            await asyncio.sleep(0.002)
            continue
        chunk = batch_queue[:BATCH_SIZE]
        del batch_queue[:BATCH_SIZE]
        await guarded(chunk)

async def main():
    print("Starting NATS consumer (pull mode, batch_size=%d)" % BATCH_SIZE)
    nc, js = await setup()

    # P0.6: this was a PUSH consumer (js.subscribe with a queue group).
    # A push consumer has exactly ONE bound subscription, so with replicas: 3
    # two of the three replicas block in subscribe() and never consume --
    # the pipeline silently scaled at 1x regardless of the replica count.
    # Symptoms seen during recovery: replicas stuck after "Subscribing to
    # iot.data", and JetStreamError "consumer is already bound to a
    # subscription" when a replica restarted.
    #
    # A PULL consumer lets every replica share the durable and have the
    # server distribute batches between them, which is the behaviour the
    # Deployment's replica count was always meant to provide.
    print("Creating pull subscription for %s (durable=%s)" % (SUBJECT, DURABLE))
    sub = await js.pull_subscribe(
        subject=SUBJECT,
        durable=DURABLE,
        stream=STREAM,
        config=ConsumerConfig(
            ack_policy=AckPolicy.EXPLICIT,
            max_deliver=MAX_DELIVER,
            # nats-py 2.14 takes ack_wait in SECONDS and multiplies by
            # 1e9 itself. Passing nanoseconds overflows to 6e19 and the
            # server rejects it; passing a timedelta fails in the client's
            # int() conversion. Verified empirically against 2.14.0.
            ack_wait=ACK_WAIT_S,
            max_ack_pending=MAX_ACK_PENDING,
        ),
    )

    batch_queue = []
    connector = aiohttp.TCPConnector(limit=200, limit_per_host=200)
    async with aiohttp.ClientSession(connector=connector) as session:
        sender = asyncio.create_task(batch_sender(session, batch_queue))
        try:
            fetches = 0
            while True:
                try:
                    msgs = await sub.fetch(
                        batch=BATCH_SIZE, timeout=FETCH_TIMEOUT_S
                    )
                    fetches += 1
                    if fetches % 20 == 1:
                        print("fetch ok: %d msgs (total fetches %d)"
                              % (len(msgs) if msgs else 0, fetches))
                except (asyncio.TimeoutError, NatsTimeoutError):
                    continue
                except Exception as e:
                    # P0.6: nats-py raises its own TimeoutError, not
                    # asyncio's, so a quiet pull loop previously looked
                    # identical to a healthy idle one. Report the type.
                    print("fetch error %s: %s" % (type(e).__name__, e))
                    await asyncio.sleep(1.0)
                    continue
                if not msgs:
                    continue
                for msg in msgs:
                    try:
                        batch_queue.append((msg, json.loads(msg.data.decode())))
                    except Exception as e:
                        print("Err: %s" % e)
                        try:
                            await msg.nak()
                        except Exception:
                            pass
        finally:
            sender.cancel()

asyncio.run(main())
