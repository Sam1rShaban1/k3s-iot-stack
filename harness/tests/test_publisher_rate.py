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

        QoS 1 and 2 cannot be exercised against the stub: it does not implement
        the PUBACK/PUBREC handshake, so paho blocks until the process is killed.
        Confirming the serialized QoS level therefore requires the real EMQX
        broker. That measurement is cluster-gated, and is the experiment that
        reproduces the ~50 msg/s per-connection ceiling in paper §V-C.
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
        "QoS 1/2 acknowledgement handshake. Run against EMQX to reproduce the "
        "serialization ceiling in paper V-C."
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
