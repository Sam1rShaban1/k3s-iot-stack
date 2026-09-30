#!/usr/bin/env python3
"""Minimal MQTT 3.1.1 broker stub, just enough to exercise publisher.c.

Accepts CONNECT, replies CONNACK, counts PUBLISH packets, completes the QoS 1
handshake with PUBACK, and answers PINGREQ.

QoS 1 is fully implemented because its cost is a headline result: it adds an
acknowledgement round trip per publish, and this stub is the only place that
cost can be attributed to the publisher rather than to the cluster. The earlier
version accepted QoS 1 at the protocol level but never acknowledged, so a QoS 1
publisher filled its in-flight window, blocked inside publish(), and never
reached its --duration check. QoS 2 is deliberately not implemented and says so
rather than pretending, since publisher.c does not use it.

Used by harness/tests/test_publisher_rate.py.
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
import time


def read_remaining_length(sock: socket.socket) -> int:
    multiplier = 1
    value = 0
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("closed while reading length")
        value += (b[0] & 0x7F) * multiplier
        if not (b[0] & 0x80):
            return value
        multiplier *= 128
        if multiplier > 128**3:
            raise ValueError("malformed remaining length")


def handle(conn: socket.socket, stats: dict, lock: threading.Lock) -> None:
    # PUBACK is a 4-byte write. Without TCP_NODELAY, Nagle holds it back waiting
    # for more data to coalesce with, and the peer sees a ~40-100 ms stall per
    # acknowledged publish. That showed up as a hard ceiling of exactly 10
    # publishes per second at QoS 1, independent of the requested rate, which
    # looked like a protocol cost and was not: it was this stub's socket.
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        while True:
            header = conn.recv(1)
            if not header:
                break
            ptype = header[0] >> 4
            flags = header[0] & 0x0F
            length = read_remaining_length(conn)
            body = b""
            while len(body) < length:
                chunk = conn.recv(length - len(body))
                if not chunk:
                    raise ConnectionError("short body")
                body += chunk

            if ptype == 1:  # CONNECT
                conn.sendall(b"\x20\x02\x00\x00")
                with lock:
                    stats["connects"] += 1

            elif ptype == 3:  # PUBLISH
                qos = (flags >> 1) & 0x03
                topic_len = struct.unpack(">H", body[:2])[0]
                topic = body[2 : 2 + topic_len].decode(errors="replace")
                rest = body[2 + topic_len :]
                packet_id = None
                if qos > 0:
                    if len(rest) < 2:
                        with lock:
                            stats["errors"].append(
                                f"QoS {qos} PUBLISH with no packet id"
                            )
                        continue
                    packet_id = rest[:2]
                    rest = rest[2:]
                payload = rest
                with lock:
                    stats["publishes"] += 1
                    stats["payload_bytes"] += len(payload)
                    stats["payloads"].append(payload)
                    stats["first_ts"] = stats["first_ts"] or time.time()
                    stats["last_ts"] = time.time()
                    stats["topics"].add(topic)
                    stats["qos_seen"].add(qos)
                    if flags & 0x08:  # DUP
                        stats["redelivered"] += 1
                if qos == 1:
                    # PUBACK is what makes the QoS 1 handshake complete. Without
                    # it the publisher's in-flight window fills, its publishes
                    # stop being accepted, and it blocks inside publish() -- so
                    # it never reaches its --duration check and hangs. That made
                    # the QoS 1 path untestable here, and left the throughput
                    # cost of QoS 1 unmeasurable.
                    conn.sendall(b"\x40\x02" + packet_id)
                elif qos == 2:
                    # QoS 2 needs PUBREC/PUBREL/PUBCOMP. Not implemented; the
                    # publisher does not use it, and silently pretending
                    # otherwise would hide that.
                    with lock:
                        stats["errors"].append("QoS 2 is not implemented")

            elif ptype == 12:  # PINGREQ
                conn.sendall(b"\xd0\x00")

            elif ptype == 8:  # SUBSCRIBE (paho may send on connect for some opts)
                # Reply with SUBACK for the granted qos of each filter.
                if len(body) >= 2:
                    pid = body[:2]
                    idx = 2
                    qoss = b""
                    while idx < len(body):
                        flen = struct.unpack(">H", body[idx : idx + 2])[0]
                        idx += 2 + flen + 1
                        qoss += b"\x00"
                    conn.sendall(b"\x90" + bytes([3 + len(qoss)]) + pid + qoss)
            else:
                with lock:
                    stats["other"].add(ptype)
    except Exception as e:  # noqa: BLE001
        with lock:
            stats["errors"].append(str(e))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def serve(host: str, port: int) -> tuple[socket.socket, dict, threading.Lock]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(8)
    stats = {
        "connects": 0,
        "publishes": 0,
        "payload_bytes": 0,
        "payloads": [],
        "topics": set(),
        "qos_seen": set(),
        "redelivered": 0,
        "other": set(),
        "errors": [],
        "first_ts": None,
        "last_ts": None,
    }
    lock = threading.Lock()

    def acceptor():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn, stats, lock), daemon=True).start()

    threading.Thread(target=acceptor, daemon=True).start()
    return srv, stats, lock


def snapshot(stats: dict) -> dict:
    """JSON-serialisable view of the counters, for the subprocess test harness."""
    n = max(1, stats["publishes"])
    return {
        "connects": stats["connects"],
        "publishes": stats["publishes"],
        "payload_bytes": stats["payload_bytes"],
        "avg_payload_bytes": round(stats["payload_bytes"] / n, 2),
        "min_payload_bytes": min((len(p) for p in stats["payloads"]), default=0),
        "max_payload_bytes": max((len(p) for p in stats["payloads"]), default=0),
        "sample_payload": (
            stats["payloads"][0].decode(errors="replace") if stats["payloads"] else ""
        ),
        "topics": sorted(stats["topics"]),
        "qos_seen": sorted(stats["qos_seen"]),
        "redelivered": stats["redelivered"],
        "span_s": round((stats["last_ts"] or 0) - (stats["first_ts"] or 0), 4),
        "errors": stats["errors"][:5],
    }


if __name__ == "__main__":
    import json as _json

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18830
    s, st, _l = serve("127.0.0.1", port)
    print(_json.dumps({"listening": port}), flush=True)
    try:
        while True:
            time.sleep(0.5)
            with _l:
                print(_json.dumps(snapshot(st)), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        s.close()
        with _l:
            print(_json.dumps(snapshot(st)), flush=True)
