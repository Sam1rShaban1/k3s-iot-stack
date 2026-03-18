#include "MQTTClient.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

/*
 * Build command:
 * gcc publisher.c -o publisher -lpaho-mqtt3c
 */

int main(int argc, char *argv[]) {
  // Check for required arguments
  if (argc < 5) {
    printf("Usage: %s <Broker_IP> <Base_ClientID> <Delay_US> <Topic>\n",
           argv[0]);
    printf("Example: %s 192.168.1.50 sensor_node 200000 sensors/data\n",
           argv[0]);
    return 1;
  }

  char *broker_ip = argv[1];
  char *base_id = argv[2];
  int delay_us = atoi(argv[3]);
  char *topic = argv[4];

  // Create unique Client ID using PID to allow high concurrency
  int pid = (int)getpid();
  char final_client_id[64];
  sprintf(final_client_id, "%s_%d", base_id, pid);

  // Setup MQTT Connection
  char broker_address[128];
  sprintf(broker_address, "tcp://%s:1883", broker_ip);

  MQTTClient client;
  MQTTClient_connectOptions conn_opts = MQTTClient_connectOptions_initializer;
  MQTTClient_message pubmsg = MQTTClient_message_initializer;
  MQTTClient_deliveryToken token;
  int rc;

  if ((rc = MQTTClient_create(&client, broker_address, final_client_id,
                              MQTTCLIENT_PERSISTENCE_NONE, NULL)) !=
      MQTTCLIENT_SUCCESS) {
    fprintf(stderr, "Failed to create client, return code %d\n", rc);
    return rc;
  }

  conn_opts.keepAliveInterval = 30;
  conn_opts.cleansession = 1;

  if ((rc = MQTTClient_connect(client, &conn_opts)) != MQTTCLIENT_SUCCESS) {
    fprintf(stderr, "Failed to connect, return code %d\n", rc);
    return rc;
  }

  // Seed random number generator with time + pid for unique data streams
  srand(time(NULL) + pid);

  printf("Client [%s] connected to %s. Publishing to %s...\n", final_client_id,
         broker_address, topic);

  while (1) {
    char payload[512];
    struct timeval tv;
    gettimeofday(&tv, NULL);

    // 1. Generate High-Resolution Timestamp (Milliseconds)
    long long ts_ms =
        (long long)(tv.tv_sec) * 1000 + (long long)(tv.tv_usec) / 1000;

    // 2. Generate Randomized Sensor Data
    // PM values
    float pm1 = (float)(rand() % 3001) / 10.0; // 0.0 - 300.0
    float pm25 = pm1 + (float)(rand() % 2001) / 10.0;
    float pm10 = pm25 + (float)(rand() % 5001) / 10.0;

    // Temperature: Range -20.0 to +50.0
    float temp = ((float)(rand() % 701) / 10.0) - 20.0;

    // Humidity: Range 0.0 to 100.0
    float hum = (float)(rand() % 1001) / 10.0;

    // 3. Format Payload as JSON
    // Using %lld for long long timestamp
    sprintf(payload,
            "{\"device_id\":\"%s\",\"ts\":%lld,\"pm1\":%.2f,\"pm25\":%.2f,"
            "\"pm10\":%.2f,\"temp\":%.2f,\"hum\":%.2f}",
            final_client_id, ts_ms, pm1, pm25, pm10, temp, hum);

    pubmsg.payload = payload;
    pubmsg.payloadlen = (int)strlen(payload);
    pubmsg.qos = 2; // QoS 2 for guaranteed delivery in benchmarks
    pubmsg.retained = 0;

    // 4. Publish
    if ((rc = MQTTClient_publishMessage(client, topic, &pubmsg, &token)) !=
        MQTTCLIENT_SUCCESS) {
      fprintf(stderr, "Failed to publish message, return code %d\n", rc);
      // Attempt to reconnect if connection lost
      if (rc == MQTTCLIENT_DISCONNECTED) {
        MQTTClient_connect(client, &conn_opts);
      }
    }

    // 5. Precise Inter-message Delay
    if (delay_us > 0) {
      usleep(delay_us);
    }
  }

  // Cleanup (though while(1) keeps it running until killed)
  MQTTClient_disconnect(client, 10000);
  MQTTClient_destroy(&client);
  return 0;
}
