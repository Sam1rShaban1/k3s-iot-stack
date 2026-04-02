#!/bin/bash

# --- CONFIGURATION ---
BROKER_IP="localhost"
BROKER_PORT="1883"
TOPIC="sensors/data"
PUBLISHER_BIN="./publisher"
TEST_DURATION=60
COOLDOWN=30

CLIENT_COUNTS=(10 100)
TOTAL_RATES=(100 500)

ulimit -n 10000

cleanup() {
    echo "Cleaning up..."
    pkill -f $PUBLISHER_BIN
    sleep 2
}

if [ ! -f $PUBLISHER_BIN ]; then
    gcc publisher.c -o publisher -lpaho-mqtt3c || exit 1
fi

echo "Starting Test..."
for CLIENTS in "${CLIENT_COUNTS[@]}"; do
    for RATE in "${TOTAL_RATES[@]}"; do
        DELAY=$(( (1000000 * CLIENTS) / RATE ))
        
        echo "============================================"
        echo "TEST: $CLIENTS Clients | $RATE msg/s | Delay: $DELAY us"
        echo "============================================"
        
        for i in $(seq 1 $CLIENTS); do
            $PUBLISHER_BIN $BROKER_IP $BROKER_PORT "sensor_c${CLIENTS}_r${RATE}_$i" $DELAY $TOPIC > /dev/null 2>&1 &
        done
        
        sleep $TEST_DURATION
        cleanup
        sleep $COOLDOWN
    done
done

echo "Tests Complete!"
