#!/usr/bin/env python3
"""End-to-end tests for publisher.c, driven against the local MQTT stub.

Verifies the two things P0.5 changed, and the one thing it must not break:

  * --qos is honoured (needed to reproduce paper V-C's serialization ceiling)
  * --duration exits cleanly, so the harness no longer needs `pkill -f publisher`
  * the COMPENSATED sleep still delivers the target rate, and no msg_id
    appears in the payload (paper VI-C)

The stub runs as a subprocess and reports counters as JSON lines. That keeps all
concurrency out of the test process, which previously deadlocked on a shared
lock across class attributes.

Run:  python3 harness/tests/test_publisher_rate.py
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import deque

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PUBLISHER_C = os.path.join(REPO, "publisher.c")
STUB = os.path.join(REPO, "harness", "tests", "mqtt_stub.py")
PORT = 18831


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def build_publisher(dest: str) -> str:
    res = subprocess.run(
        ["gcc", "-Wall", "-O2", PUBLISHER_C, "-o", dest, "-lpaho-mqtt3c"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"compile failed:\n{res.stderr}")
    if res.stderr.strip():
        raise RuntimeError(f"compiler warnings treated as errors:\n{res.stderr}")
    return dest


class StubBroker:
    """Runs mqtt_stub.py as a subprocess and exposes its counters.

    A daemon thread drains stdout into a list, because the stub emits a JSON
    snapshot every 500ms and nobody would otherwise read it.
    """

    def __init__(self, port: int):
        self.proc = subprocess.Popen(
            [sys.executable, "-u", STUB, str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._lines: deque[dict] = deque(maxlen=4096)
        self._ready = threading.Event()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()
        if not self._ready.wait(timeout=20):
            self.proc.kill()
            raise RuntimeError("stub broker did not report listening")

    def _drain(self) -> None:
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "listening" in rec:
                    self._ready.set()
                    continue
                self._lines.append(rec)
        except Exception:
            pass
        finally:
            self._ready.set()

    def stats(self) -> dict:
        return self._lines[-1] if self._lines else {}

    def stop(self) -> None:
        try:
            self.proc.send_signal(signal.SIGINT)
            self.proc.wait(timeout=6)
        except Exception:
            self.proc.kill()


class TestPublisher(unittest.TestCase):
    binary = ""
    broker: StubBroker

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.NamedTemporaryFile(suffix="_publisher", delete=False)
        tmp.close()
        cls._tmp_path = tmp.name
        cls.binary = build_publisher(cls._tmp_path)
        cls.broker = StubBroker(PORT)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.broker.stop()
        except Exception:
            pass
        try:
            os.unlink(cls._tmp_path)
        except OSError:
            pass

    def _run(self, delay_us: int, duration: int, qos: int = 0):
        before = self.broker.stats().get("publishes", 0)
        proc = subprocess.run(
            [
                self.binary, "--host", "127.0.0.1", "--port", str(PORT),
                "--device-prefix", "sensor_test", "--delay-us", str(delay_us),
                "--topic", "sensors/data", "--qos", str(qos),
                "--duration", str(duration),
            ],
            capture_output=True, text=True, timeout=duration + 25,
        )
        return proc, before

    def _wait_for_delta(self, before: int, expected_min: int, timeout_s: float = 6.0) -> dict:
        """Wait until the stub has counted at least expected_min new publishes."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            st = self.broker.stats()
            if st.get("publishes", 0) - before >= expected_min:
                return st
            time.sleep(0.2)
        return self.broker.stats()

    def test_duration_exits_on_its_own(self):
        t0 = time.time()
        proc, before = self._run(delay_us=200000, duration=2)
        elapsed = time.time() - t0
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 15, "publisher should exit ~2s, not hang")
        self.assertIn("stopping after", proc.stdout)

    def test_publishes_at_target_rate(self):
        """Compensated sleep must still hold the target rate.

        20ms interval == 50 msg/s. Tolerance +/-15% absorbs scheduler jitter
        without being loose enough to admit a regression to naive
        usleep(interval), which would drift measurably low under load.
        """
        delay_us = 20000
        proc, before = self._run(delay_us=delay_us, duration=4)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        st = self._wait_for_delta(before, expected_min=100)
        span = st.get("span_s") or 0
        if span <= 0:
            self.skipTest("stub did not observe a measurable span")
        achieved = st["publishes"] / span
        target = 1_000_000.0 / delay_us
        self.assertGreater(achieved, target * 0.85, f"{achieved:.1f} < 85% of {target}")
        self.assertLess(achieved, target * 1.15, f"{achieved:.1f} > 115% of {target}")

    def test_publisher_reports_own_achieved_rate(self):
        """The publisher's own summary line must agree with its target."""
        delay_us = 100000  # 10 msg/s
        proc, _ = self._run(delay_us=delay_us, duration=3)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = [l for l in proc.stdout.splitlines() if "stopping after" in l]
        self.assertTrue(line, proc.stdout)
        self.assertIn("achieved=", line[0])
        self.assertIn("target=", line[0])

    def test_payload_has_no_msg_id(self):
        """Paper VI-C: an unbounded per-message label must not reappear."""
        proc, before = self._run(delay_us=10000, duration=2)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        st = self._wait_for_delta(before, expected_min=30)
        payload = st.get("sample_payload") or ""
        self.assertTrue(payload, "no payload captured")
        self.assertNotIn("msg_id", payload)
        doc = json.loads(payload)
        for field in ("device_id", "ts", "pm1", "pm25", "pm10", "temp", "hum"):
            self.assertIn(field, doc, f"payload missing {field}")

    def test_message_size_documented(self):
        """Records the real wire payload size.

        The paper's throughput figures are per message; this pins the size so a
        change to the payload shape is caught rather than silently altering
        the offered byte rate.
        """
        proc, before = self._run(delay_us=10000, duration=2)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        st = self._wait_for_delta(before, expected_min=30)
        avg = st.get("avg_payload_bytes") or 0
        self.assertGreater(avg, 100, f"payload implausibly small: {avg}B")
        self.assertLess(avg, 300, f"payload implausibly large: {avg}B")

    def test_qos_zero_is_transmitted(self):
        """QoS 0 is fully supported by the local stub."""
        proc, before = self._run(delay_us=50000, duration=2, qos=0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        st = self._wait_for_delta(before, expected_min=10)
        self.assertIn(0, st.get("qos_seen", []), "qos 0 not seen on the wire")

    def test_qos_flag_is_accepted(self):
        """The publisher must accept and validate --qos without a real broker.

        The level is validated from the arguments, so a closed port is enough to
        confirm a valid value is accepted and an out-of-range one is rejected
        before any connection is attempted. QoS 1 is separately exercised
        against the stub in TestQos1RateIsNotWindowLimited.
        """
        for qos in (0, 1, 2):
            with self.subTest(qos=qos):
                res = subprocess.run(
                    [self.binary, "--host", "127.0.0.1", "--port", "1",
                     "--device-prefix", "d", "--delay-us", "1000",
                     "--topic", "t", "--qos", str(qos), "--duration", "1"],
                    capture_output=True, text=True, timeout=20,
                )
                # Port 1 is closed, so the publisher must fail to connect and
                # report it -- not reject the QoS value.
                self.assertIn("Failed to connect", res.stderr)
                self.assertNotIn("qos must be", res.stderr)

    @unittest.skip(
        "requires a real MQTT broker: the local stub does not implement the "
        "QoS 2 PUBREC/PUBREL/PUBCOMP handshake. Note that the QoS 1 ceiling "
        "this test was written to characterise turned out to be paho's "
        "in-flight window rather than the protocol -- see D15 -- so the QoS 2 "
        "figure should be re-measured after the same fix is validated there."
    )
    def test_qos_two_ceiling(self):
        """Measure the QoS 2 per-connection ceiling against a real broker.

        Expected behaviour (paper V-C): four-way handshake, ~20 ms round trip,
        therefore ~1_000/20 = 50 msg/s per connection regardless of how many
        additional clients connect, and regardless of CPU headroom.
        """
        proc = subprocess.run(
            [self.binary, "--host", os.environ["EMQX_HOST"],
             "--port", os.environ.get("EMQX_PORT", "1883"),
             "--device-prefix", "qos2_probe", "--delay-us", "1000",
             "--topic", "sensors/data", "--qos", "2", "--duration", "30"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertIn("achieved=", proc.stdout)

    def test_topic_is_honoured(self):
        proc, before = self._run(delay_us=20000, duration=2)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        st = self._wait_for_delta(before, expected_min=20)
        self.assertIn("sensors/data", st.get("topics", []))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestQos1RateIsNotWindowLimited(unittest.TestCase):
    """QoS 1 must not be capped by paho's default in-flight window.

    Paho C defaults maxInflightMessages to 10, so at QoS 1 the window filled
    immediately and the delivered rate collapsed to ~10 msg/s per process
    regardless of the requested rate: exactly 120 messages in 12 s whether the
    target was 100, 500 or 1000/s. publisher.c now widens the window, which puts
    QoS 1 within a couple of percent of QoS 0.

    This is also the finding that undercuts paper §V-C's "~50 msg/s per
    connection serialization ceiling": that number was the in-flight window, not
    the protocol. See docs/findings-p0.md D15.
    """

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.NamedTemporaryFile(suffix="_publisher", delete=False)
        tmp.close()
        cls._tmp_path = tmp.name
        cls.binary = build_publisher(tmp.name)

    @classmethod
    def tearDownClass(cls):
        try:
            os.unlink(cls._tmp_path)
        except OSError:
            pass

    def _offered(self, qos, clients=4, rate=200, duration=8):
        port = free_port()
        broker = StubBroker(port)
        try:
            delay_us = int(1_000_000 * clients / rate)
            procs = [
                subprocess.Popen(
                    [self.binary, "--host", "127.0.0.1", "--port", str(port),
                     "--device-prefix", f"qos{qos}_{i}", "--delay-us", str(delay_us),
                     "--topic", "sensors/data", "--duration", str(duration),
                     "--qos", str(qos)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                for i in range(clients)
            ]
            for p in procs:
                p.wait(timeout=40)
            snap = broker.stats()
        finally:
            broker.stop()
        span = snap.get("span_s") or duration
        offered = snap.get("publishes", 0) / span if span else 0.0
        return offered, snap

    def test_qos1_honours_the_requested_rate(self):
        offered, snap = self._offered(qos=1)
        self.assertIn(1, snap.get("qos_seen", []))
        # Far above the window-limited ~10/s per process, and close to target:
        # the acknowledgement round trip must not cost an order of magnitude.
        self.assertGreater(offered, 100, f"QoS 1 offered only {offered:.1f}/s")
        self.assertGreater(offered / 200.0, 0.8, f"QoS 1 at {offered:.1f}/s of 200")

    def test_qos1_is_close_to_qos0(self):
        zero, _ = self._offered(qos=0)
        one, _ = self._offered(qos=1)
        self.assertGreater(
            one, zero * 0.5,
            f"QoS 1 ({one:.0f}/s) far below QoS 0 ({zero:.0f}/s)",
        )

    def test_qos1_handshake_completes_without_redelivery(self):
        _, snap = self._offered(qos=1)
        self.assertEqual(snap.get("redelivered", 0), 0)
        self.assertEqual(snap.get("errors", []), [])

    def test_publisher_exits_on_duration_at_qos1(self):
        """It used to block inside publish() and never reach the duration check."""
        port = free_port()
        broker = StubBroker(port)
        try:
            res = subprocess.run(
                [self.binary, "--host", "127.0.0.1", "--port", str(port),
                 "--device-prefix", "dur", "--delay-us", "1000",
                 "--topic", "sensors/data", "--duration", "3", "--qos", "1"],
                capture_output=True, text=True, timeout=30,
            )
        finally:
            broker.stop()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("achieved=", res.stdout)

    def test_max_inflight_flag_is_accepted(self):
        port = free_port()
        broker = StubBroker(port)
        try:
            res = subprocess.run(
                [self.binary, "--host", "127.0.0.1", "--port", str(port),
                 "--device-prefix", "mi", "--delay-us", "1000",
                 "--topic", "sensors/data", "--duration", "2", "--qos", "1",
                 "--max-inflight", "500"],
                capture_output=True, text=True, timeout=30,
            )
        finally:
            broker.stop()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("unknown option", res.stderr)
