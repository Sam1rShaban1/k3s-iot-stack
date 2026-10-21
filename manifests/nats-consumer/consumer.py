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
# Whether to publish the STAGE series -- the three extra series that make the
# latency attribution possible:
#
#   benthos_entry_ts   stamped by Benthos, carried as a NATS header
#   nats_exit_ts       when the consumer fetched the message
#   consumer_queue_ms  how long it then waited in the consumer's own queue
#
# They cost 3 of the 11 series written per message, i.e. ~27% of the write
# volume, and the write path is the constrained side of the pipeline. A
# throughput run does not need them; an attribution run does. So they are
# switchable, and the default is ON because the paper's stage attribution
# depends on them and a missing series is worse than a slow one.
#
# Set EMIT_STAGE_METRICS=0 for a pure throughput measurement.
EMIT_STAGE_METRICS = os.getenv("EMIT_STAGE_METRICS", "1") not in ("0", "false", "no")
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
# Counts VM writes so the timing log can be printed every Nth batch.
write_counter = [0]

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
    # `nats_exit_ts` is stamped when the message is FETCHED from JetStream, not
    # when it is processed. The two are not the same moment: fetched messages
    # wait in an in-process queue until the batch writer reaches them, and at
    # 8000 msg/s that wait is large enough to dominate the measurement. Stamping
    # at process time made the "Benthos -> NATS" stage absorb the consumer's
    # own backlog, which is how a 19-second p99 got attributed to the wrong
    # place. The fetch-time stamp is what the name says it is.
    #
    # The gap between the two is the consumer's own queueing, and it is
    # published separately as `consumer_queue_ms`.
    if EMIT_STAGE_METRICS:
        fetch_ts = data.get("nats_exit_ts")
        if fetch_ts is None:
            data["nats_exit_ts"] = nats_exit_ts
        else:
            data["consumer_queue_ms"] = nats_exit_ts - fetch_ts
    # Only `benthos_entry_ts` and `consumer_queue_ms` are added. The other
    # stage series are derivable from the raw timestamps by
    # harness/stage_latency.py, which is where the split is actually analysed.
    # Emitting them as well cost four extra series per message -- and the
    # consumer, not the broker, is what runs out of headroom first: at 8000
    # msg/s it went from 1352m to 2507m of CPU.
    return _render(data, device)

def format_metrics_ack(data, ack_ts):
    """Second pass: the acknowledgement stamps, written after the POST returns.

    Emits ONLY vm_write_ack_ts and latency_ms. The payload is already stored
    by the time this runs, so a failure here costs the latency series rather
    than the measurement.

    It must not re-render the rest of the payload. An earlier version called
    the same _render(data, device) as pass 1, which re-emitted every field:
    VictoriaMetrics then held exactly twice as many samples of iot_sensor_ts
    and iot_sensor_nats_exit_ts as of latency_ms, for the same messages. The
    consumer wrote twice the series it needed, doubling the write volume and
    the ingestion cost for no additional information. The symptom was visible
    only by counting samples per series -- no error, and the reported latency
    and throughput were unaffected, so it would have gone unnoticed.
    """
    device = data.get("device_id", "unknown")
    data["vm_write_ack_ts"] = ack_ts
    sensor_ts = data.get("ts")
    if sensor_ts is not None:
        data["latency_ms"] = ack_ts - sensor_ts
    return _render(data, device, only=("vm_write_ack_ts", "latency_ms"))

def _render(data, device, only=None):
    lines = []
    # The stage series are skipped when the switch is off, so the switch is
    # authoritative at the point of writing rather than only at the point of
    # collection. Otherwise a field that arrived some other way would still
    # be written, and the write volume would not actually drop.
    stage_series = ("benthos_entry_ts", "nats_exit_ts", "consumer_queue_ms")
    for k, v in data.items():
        if only is not None and k not in only:
            continue
        if not EMIT_STAGE_METRICS and k in stage_series:
            continue
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
    write_started = time.monotonic()
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
    finally:
        write_ms = (time.monotonic() - write_started) * 1000.0
        # The write duration, with the size of the body, is the other half of
        # the localisation: how long a batch of samples takes to be accepted,
        # and how many samples that was. Printed sparingly -- one line per
        # batch would drown the logs at 8000 msg/s.
        write_counter[0] += 1
        if write_counter[0] % 20 == 1:
            print("vm write: %d msgs, %d bytes, %.0f ms"
                  % (len(stamped), len(body), write_ms))

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

async def create_pull_subscription(js):
    """Create (or re-create) the shared durable pull subscription.

    A JetStream subscription is bound to the CONNECTION that created it, not
    just to the durable. When the NATS server restarts -- which it did here,
    deliberately -- the durable survives with all of its messages, but this
    object does not, and every subsequent fetch fails with
    "ConnectionClosedError: nats: connection closed" forever.

    That is exactly what happened: the consumer logged that error once per
    second, indefinitely, and the pipeline silently stopped draining while
    every pod stayed Running. The pipeline was broken and nothing reported
    it -- the same shape of failure as the six-month ArgoCD zombie (D42) and
    the full Prometheus volume (D50): the system looks healthy because
    nothing checks whether the work is actually happening.

    So the subscription is re-created when the connection dies. If that fails
    too, the process exits non-zero and Kubernetes restarts it, which is
    slower but cannot leave the consumer wedged.
    """
    return await js.pull_subscribe(
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
    sub = await create_pull_subscription(js)

    batch_queue = []
    connector = aiohttp.TCPConnector(limit=200, limit_per_host=200)
    async with aiohttp.ClientSession(connector=connector) as session:
        sender = asyncio.create_task(batch_sender(session, batch_queue))
        try:
            fetches = 0
            while True:
                try:
                    fetch_started = time.monotonic()
                    msgs = await sub.fetch(
                        batch=BATCH_SIZE, timeout=FETCH_TIMEOUT_S
                    )
                    fetch_ms = (time.monotonic() - fetch_started) * 1000.0
                    fetches += 1
                    if fetches % 20 == 1:
                        # The fetch duration localises the bottleneck. If it is
                        # long, the consumer is waiting on the server
                        # (JetStream's delivery); if it is short, the consumer
                        # has messages in hand and the delay is in the write
                        # path. It was not clear which, and the difference
                        # decides whether the fix is more consumer replicas, a
                        # faster store, or a cheaper write.
                        print("fetch ok: %d msgs in %.0f ms (total fetches %d)"
                              % (len(msgs) if msgs else 0, fetch_ms, fetches))
                except (asyncio.TimeoutError, NatsTimeoutError):
                    continue
                except Exception as e:
                    # P0.6: nats-py raises its own TimeoutError, not
                    # asyncio's, so a quiet pull loop previously looked
                    # identical to a healthy idle one. Report the type.
                    print("fetch error %s: %s" % (type(e).__name__, e))
                    msg_text = str(e).lower()
                    gone = any(
                        marker in msg_text
                        for marker in (
                            "connection closed",
                            "consumer not found",
                            "no stream",
                            "stream not found",
                            "invalid durable",
                            # Deleting a stream makes JetStream answer a pull
                            # request with 503 Service Unavailable rather than
                            # "consumer not found" -- the durable is gone and
                            # so is the stream it belonged to. Measured, not
                            # assumed: the first version of this list did not
                            # include it, and the pipeline stored nothing at
                            # all with every pod Running and no errors
                            # anywhere.
                            "serviceunavailable",
                            "no responders",
                            "503",
                        )
                    )
                    if gone:
                        # Either the server went away (restart, network blip) or
                        # the stream and its durable were deleted underneath us
                        # (the benchmark harness recreates the stream between
                        # scenarios, which takes the durable with it).
                        #
                        # Either way the subscription is bound to something
                        # that no longer exists, so re-create it; otherwise
                        # every later fetch fails the same way forever and the
                        # pipeline stops draining while looking perfectly
                        # healthy -- every pod Running, no errors in the
                        # preflight, and no messages moving.
                        print("subscription lost (%s), re-subscribing"
                              % type(e).__name__)
                        try:
                            # The stream may be gone too, not just the
                            # durable: deleting a stream takes its consumers
                            # with it, and the benchmark harness recreates the
                            # stream between scenarios. Recreate it before
                            # subscribing, or the subscription fails with the
                            # same error forever.
                            try:
                                await js.add_stream(
                                    name=STREAM,
                                    subjects=[SUBJECT],
                                    max_age=STREAM_MAX_AGE_S,
                                    max_bytes=STREAM_MAX_BYTES,
                                    discard="old",
                                )
                            except Exception as e3:
                                if ("already in use" not in str(e3)
                                        and "stream name already" not in str(e3).lower()):
                                    raise
                            sub = await create_pull_subscription(js)
                            print("re-subscribed to %s" % SUBJECT)
                        except Exception as e2:
                            print("re-subscribe failed (%s); exiting so "
                                  "Kubernetes restarts the pod"
                                  % type(e2).__name__)
                            os._exit(1)
                    await asyncio.sleep(1.0)
                    continue
                if not msgs:
                    continue
                for msg in msgs:
                    try:
                        data = json.loads(msg.data.decode())
                        # The stage stamp arrives as a NATS header, not in the
                        # payload. Benthos adds it with
                        #   headers: {benthos_entry_ts: '${! timestamp_unix_nano() }'}
                        # so the payload is never re-serialised -- adding a
                        # payload field instead cost 47% of Benthos's CPU and
                        # took the pipeline from 86.4% delivery to 39.1% at
                        # 8000 msg/s. The header is nanoseconds; every other
                        # timestamp here is milliseconds.
                        if EMIT_STAGE_METRICS:
                            entry_ns = (getattr(msg, "headers", None) or {}).get(
                                "benthos_entry_ts")
                            if entry_ns is not None:
                                data["benthos_entry_ts"] = int(entry_ns) // 1000000
                            # Stamped here, at receipt, so the consumer's own
                            # queueing can be separated from the pipeline's.
                            data["nats_exit_ts"] = now_ms()
                        batch_queue.append((msg, data))
                    except Exception as e:
                        print("Err: %s" % e)
                        try:
                            await msg.nak()
                        except Exception:
                            pass
        finally:
            sender.cancel()

asyncio.run(main())
