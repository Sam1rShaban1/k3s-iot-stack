/*
 * MQTT load generator for the K3s IoT benchmark pipeline.
 *
 * Build:
 *   gcc publisher.c -o publisher -lpaho-mqtt3c
 *
 * Model: ONE PROCESS PER SIMULATED DEVICE. The harness spawns N of these to
 * simulate N devices. (Paper §IV-E previously described this as "one POSIX
 * thread per simulated device"; the implementation has always been
 * process-per-device. The paper text is corrected -- see
 * docs/findings-p0.md D5.)
 *
 * The inter-message delay is COMPENSATED against publish time. This is the
 * paper's §IV-E fix and must not be "simplified" back to a bare usleep: naive
 * usleep(interval) drifts because it sleeps for the full interval in addition
 * to the time the publish itself consumed, so the delivered rate falls below
 * target and every efficiency figure becomes unreliable. The compensated form
 * below computes the target wake time, then sleeps only for the remainder.
 *
 * P0.5 additions (all backward compatible; positional form still works):
 *   --qos N         publish at QoS 0/1/2. QoS 2 is required to reproduce the
 *                   serialization ceiling in paper §V-C (four-way handshake,
 *                   ~50 msg/s per connection at ~20 ms round-trip).
 *   --duration N    exit cleanly after N seconds instead of running until
 *                   killed. Removes the harness's fragile `pkill -f publisher`.
 *   --host/--port/--topic/--delay-us/--device-prefix
 *                   named forms of the positional arguments.
 *   --max-inflight N
 *                   in-flight window for QoS > 0. Paho C defaults it to 10,
 *                   which pinned QoS 1 at ~10 msg/s per process; see the
 *                   comment where it is applied.
 */

#include "MQTTClient.h"

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

/* Wide enough that the broker, not this process, is the limit. Ten clients at
 * 2,000 msg/s is 2,000 outstanding publishes in the worst case. */
#define DEFAULT_MAX_INFLIGHT 1000

static void usage(const char *prog) {
  fprintf(stderr,
          "Usage: %s <host> <port> <device-prefix> <delay-us> <topic> [options]\n"
          "       %s --host H --port P --device-prefix D --delay-us U "
          "--topic T [options]\n"
          "\n"
          "Options:\n"
          "  --qos N            0, 1 or 2 (default 0). QoS 2 reproduces the\n"
          "                     serialization ceiling described in paper V-C.\n"
          "  --duration N       exit after N seconds (default: run forever)\n"
          "  --seed N           RNG seed (default: time+pid, i.e. random)\n"
          "  --max-inflight N   QoS>0 in-flight window (default %d). A load\n"
          "                     generator must not wait per acknowledgement, or\n"
          "                     it measures the round trip, not the broker.\n"
          "\n"
          "Example:\n"
          "  %s 192.168.1.241 1883 sensor_node 200000 sensors/data --qos 0 "
          "--duration 60\n",
          prog, prog, DEFAULT_MAX_INFLIGHT, prog);
}

/* Monotonic seconds, for duration accounting. */
static double mono_sec(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

/* Summary state at file scope, so the SIGTERM handler can reach it.
 *
 * The harness stops publishers with SIGTERM the moment the test window closes
 * -- which is how it has always worked. Until now the process died on the
 * default SIGTERM action, so the closing summary on the normal exit path never
 * ran and the harness could not learn the achieved rate. It fell back to the
 * nominal denominator silently, which is the failure this exists to prevent.
 *
 * Read once on the way out; a torn long in a once-per-shutdown summary is not
 * a concern. */
static volatile long g_published = 0;
static volatile long g_reconnects = 0;
static double g_start_s = 0.0;
static int g_delay_us = 0;
static char g_client_id[128] = "unknown";

static void print_summary(void) {
  double elapsed = mono_sec() - g_start_s;
  double achieved = (elapsed > 0.0) ? (double)g_published / elapsed : 0.0;
  printf("Client [%s] stopping after %.1fs: published=%ld achieved=%.2f msg/s "
         "target=%.2f msg/s reconnects=%ld\n",
         g_client_id, elapsed, (long)g_published, achieved,
         g_delay_us > 0 ? 1000000.0 / (double)g_delay_us : 0.0,
         (long)g_reconnects);
  /* REQUIRED, not tidiness. stdout is block-buffered whenever it is not a tty,
   * and under the harness it never is. Without this the summary is discarded on
   * exit and the harness sees no achieved rate. */
  fflush(stdout);
}

static void on_terminate(int sig) {
  (void)sig;
  print_summary();
  _exit(0);
}

int main(int argc, char *argv[]) {
  const char *broker_ip = NULL;
  int broker_port = 0;
  const char *base_id = NULL;
  int delay_us = 0;
  const char *topic = NULL;
  int qos = 0;
  int max_inflight = DEFAULT_MAX_INFLIGHT;
  double duration_s = 0.0; /* 0 == run until killed */
  unsigned long seed = 0;
  int positional = 0;
  int i;

  /* Parse: accept positional args first, then --flags in any order. */
  char *pos[5];
  int npos = 0;
  for (i = 1; i < argc; i++) {
    if (strncmp(argv[i], "--", 2) != 0) {
      if (npos < 5)
        pos[npos++] = argv[i];
      continue;
    }
    if (!strcmp(argv[i], "--qos") && i + 1 < argc) {
      qos = atoi(argv[++i]);
    } else if (!strcmp(argv[i], "--duration") && i + 1 < argc) {
      duration_s = atof(argv[++i]);
    } else if (!strcmp(argv[i], "--max-inflight") && i + 1 < argc) {
      max_inflight = atoi(argv[++i]);
    } else if (!strcmp(argv[i], "--seed") && i + 1 < argc) {
      seed = strtoul(argv[++i], NULL, 10);
    } else if (!strcmp(argv[i], "--host") && i + 1 < argc) {
      broker_ip = argv[++i];
    } else if (!strcmp(argv[i], "--port") && i + 1 < argc) {
      broker_port = atoi(argv[++i]);
    } else if (!strcmp(argv[i], "--device-prefix") && i + 1 < argc) {
      base_id = argv[++i];
    } else if (!strcmp(argv[i], "--delay-us") && i + 1 < argc) {
      delay_us = atoi(argv[++i]);
    } else if (!strcmp(argv[i], "--topic") && i + 1 < argc) {
      topic = argv[++i];
    } else if (!strcmp(argv[i], "--help") || !strcmp(argv[i], "-h")) {
      usage(argv[0]);
      return 0;
    } else {
      fprintf(stderr, "unknown option: %s\n", argv[i]);
      usage(argv[0]);
      return 1;
    }
  }

  /* Fill any positional gaps that were not supplied as flags. */
  if (!broker_ip && npos > 0)
    broker_ip = pos[0];
  if (!broker_port && npos > 1)
    broker_port = atoi(pos[1]);
  if (!base_id && npos > 2)
    base_id = pos[2];
  if (!delay_us && npos > 3)
    delay_us = atoi(pos[3]);
  if (!topic && npos > 4)
    topic = pos[4];
  positional = npos;

  if (!broker_ip || broker_port <= 0 || !base_id || !topic) {
    usage(argv[0]);
    return 1;
  }
  if (qos < 0 || qos > 2) {
    fprintf(stderr, "qos must be 0, 1 or 2 (got %d)\n", qos);
    return 1;
  }
  (void)positional;

  /* Unique client id per process: base id + pid + nanoseconds. */
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  int pid = (int)getpid();
  char final_client_id[128];
  snprintf(final_client_id, sizeof(final_client_id), "%s_%d_%ld", base_id, pid,
           ts.tv_nsec);

  char broker_address[160];
  snprintf(broker_address, sizeof(broker_address), "tcp://%s:%d", broker_ip,
           broker_port);

  MQTTClient client;
  MQTTClient_connectOptions conn_opts = MQTTClient_connectOptions_initializer;
  MQTTClient_message pubmsg = MQTTClient_message_initializer;
  MQTTClient_deliveryToken token;
  int rc;

  if ((rc = MQTTClient_create(&client, broker_address, final_client_id,
                              MQTTCLIENT_PERSISTENCE_NONE,
                              NULL)) != MQTTCLIENT_SUCCESS) {
    fprintf(stderr, "Failed to create client, return code %d\n", rc);
    return rc;
  }

  conn_opts.keepAliveInterval = 30;
  conn_opts.cleansession = 1;

  /* In-flight window for QoS > 0.
   *
   * This is the fix for the QoS 1 ceiling. Paho C defaults this to 10, so at
   * QoS 1 the window filled almost immediately and the delivered rate
   * collapsed to roughly 10 msg/s per process regardless of the requested
   * rate -- 120 messages in 12 s whether the target was 100, 500 or 1000/s.
   * Against a broker that withheld the PUBACK the process also blocked inside
   * publish() and never reached its --duration check.
   *
   * A load generator must not serialise on acknowledgements either: waiting
   * per PUBACK would cap the rate at one per round trip and measure the round
   * trip rather than the broker. The window is therefore wide, so the limit
   * measured is the broker's rather than this process's. With this alone,
   * QoS 1 reaches ~8,200 msg/s per process on loopback.
   *
   * QoS 0 is left at paho's default: the window is unused there, and the
   * default measurement path is left exactly as it was. */
  if (qos > 0 && max_inflight > 1) {
    conn_opts.maxInflightMessages = max_inflight;
  }

  if ((rc = MQTTClient_connect(client, &conn_opts)) != MQTTCLIENT_SUCCESS) {
    fprintf(stderr, "Failed to connect, return code %d\n", rc);
    MQTTClient_destroy(&client);
    return rc;
  }

  srand(seed ? (unsigned)seed
              : (unsigned)(time(NULL) + (long)pid + ts.tv_nsec));

  /* Stagger connections so N processes do not all CONNECT in the same
   * millisecond, which would distort the broker's per-connection view. */
  usleep(rand() % 100000);

  printf("Client [%s] connected to %s. Publishing to %s (qos=%d, delay=%dus)%s\n",
         final_client_id, broker_address, topic, qos, delay_us,
         duration_s > 0 ? ", bounded duration" : "");

  const double start_s = mono_sec();

  /* Publish summary context before the loop so a SIGTERM at any point can
   * still report which device and target it was. */
  g_start_s = start_s;
  g_delay_us = delay_us;
  snprintf(g_client_id, sizeof(g_client_id), "%s", final_client_id);
  g_published = 0;
  g_reconnects = 0;
  signal(SIGTERM, on_terminate);
  signal(SIGINT, on_terminate);
  long published = 0;
  long reconnects = 0;

  struct timespec cycle_start;
  clock_gettime(CLOCK_MONOTONIC, &cycle_start);

  for (;;) {
    if (duration_s > 0 && (mono_sec() - start_s) >= duration_s)
      break;

    char payload[512];
    struct timeval tv;
    gettimeofday(&tv, NULL);

    /* 1. High-resolution publish timestamp (milliseconds, wall clock). */
    long long ts_ms =
        (long long)(tv.tv_sec) * 1000 + (long long)(tv.tv_usec) / 1000;

    /* 2. Randomised sensor readings. */
    float pm1 = (float)(rand() % 3001) / 10.0;
    float pm25 = pm1 + (float)(rand() % 2001) / 10.0;
    float pm10 = pm25 + (float)(rand() % 5001) / 10.0;
    float temp = ((float)(rand() % 701) / 10.0) - 20.0;
    float hum = (float)(rand() % 1001) / 10.0;

    /* 3. JSON payload. No msg_id: an unbounded per-message label is exactly
     *    what broke VictoriaMetrics in paper §VI-C. Per-message correlation
     *    is the consumer's job now (it publishes iot_sensor_latency_ms). */
    snprintf(payload, sizeof(payload),
             "{\"device_id\":\"%s\",\"ts\":%lld,\"pm1\":%.2f,\"pm25\":%.2f,"
             "\"pm10\":%.2f,\"temp\":%.2f,\"hum\":%.2f}",
             final_client_id, ts_ms, pm1, pm25, pm10, temp, hum);

    pubmsg.payload = payload;
    pubmsg.payloadlen = (int)strlen(payload);
    pubmsg.qos = qos;
    pubmsg.retained = 0;

    /* 4. Publish. */
    if ((rc = MQTTClient_publishMessage(client, topic, &pubmsg, &token)) !=
        MQTTCLIENT_SUCCESS) {
      fprintf(stderr, "Failed to publish message, return code %d\n", rc);
      if (rc == MQTTCLIENT_DISCONNECTED) {
        reconnects++;
        g_reconnects = reconnects;
        fprintf(stderr, "disconnected; reconnecting (#%ld)\n", reconnects);
        if (MQTTClient_connect(client, &conn_opts) != MQTTCLIENT_SUCCESS) {
          fprintf(stderr, "reconnect failed; aborting\n");
          break;
        }
      }
    } else {
      published++;
      g_published = published;
    }

    /* No network pump here, deliberately.
     *
     * An earlier version called MQTTClient_yield() each iteration to process
     * PUBACKs. In this Paho C build that call blocks for ~100 ms even when
     * nothing is pending, which pinned QoS 1 to 9.95 msg/s per process
     * regardless of the requested rate. The real cause of the original QoS 1
     * ceiling was paho's small default in-flight window, not a missing pump:
     * widening the window (see maxInflightMessages above) raises QoS 1 to
     * ~8,200 msg/s per process with no pump at all, because this Paho version
     * runs its own sender thread.
     *
     * Measured on this build, 1 client, 8 s, --delay-us 0, --qos 1:
     *   with MQTTClient_yield()   -> published=80     (9.95 msg/s)
     *   without                    -> published=66,001 (8,249 msg/s)
     *
     * Do not "fix" a QoS throughput problem by adding a yield call. */

    /* 5. COMPENSATED inter-message delay.
     *
     * Sleep only for the time remaining until the next scheduled wake, having
     * subtracted the time already spent in this iteration (publish + build).
     * A bare usleep(delay_us) here would over-sleep by the publish duration
     * and drift the delivered rate below target. Do not replace this with a
     * plain sleep: it is the paper's §IV-E fix. */
    if (delay_us > 0) {
      struct timespec now;
      clock_gettime(CLOCK_MONOTONIC, &now);
      long elapsed_us = (now.tv_sec - cycle_start.tv_sec) * 1000000 +
                        (now.tv_nsec - cycle_start.tv_nsec) / 1000;
      long remaining_us = delay_us - elapsed_us;
      if (remaining_us > 0)
        usleep((useconds_t)remaining_us);
      clock_gettime(CLOCK_MONOTONIC, &cycle_start);
    }
  }

  g_published = published;
  g_reconnects = reconnects;
  print_summary();

  MQTTClient_disconnect(client, 10000);
  MQTTClient_destroy(&client);
  return 0;
}
