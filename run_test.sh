#!/bin/bash

# --- CONFIGURATION ---
BROKER_IP="192.168.1.50"  # Change to your Cluster LB IP
TOPIC="sensors/data"
PUBLISHER_BIN="./publisher"
TEST_DURATION=300         # Each test runs for 5 minutes
COOLDOWN=30               # 30 seconds between tests to let cluster clear RAM

# Matrix Variables
CLIENT_COUNTS=(10 100 500 1000)
TOTAL_RATES=(100 500 1000 2500)

# Ensure system can handle high connection counts
ulimit -n 10000

cleanup() {
    echo "Cleaning up processes..."
    pkill -f $PUBLISHER_BIN
    sleep 5
}

# Ensure binary exists
if [ ! -f $PUBLISHER_BIN ]; then
    gcc publisher.c -o publisher -lpaho-mqtt3c || exit 1
fi

echo "Starting Full Evaluation Matrix..."
echo "Total Tests: $((${#CLIENT_COUNTS[@]} * ${#TOTAL_RATES[@]}))"

for CLIENTS in "${CLIENT_COUNTS[@]}"; do
    for RATE in "${TOTAL_RATES[@]}"; do
        
        # MATH: Calculate the microsecond delay per individual client
        # Delay = (1,000,000us * Number of Clients) / Total Target Rate
        DELAY=$(( (1000000 * CLIENTS) / RATE ))

        echo "-------------------------------------------------------"
        echo "TEST CASE: $CLIENTS Clients | Target: $RATE msg/s"
        echo "Calculated delay per client: $DELAY us"
        echo "-------------------------------------------------------"

        # Launch the fleet of clients
        for i in $(seq 1 $CLIENTS); do
            $PUBLISHER_BIN $BROKER_IP "sensor_c${CLIENTS}_r${RATE}_$i" $DELAY $TOPIC > /dev/null &
        done

        echo "Test in progress... (Duration: $TEST_DURATION seconds)"
        sleep $TEST_DURATION

        echo "Test case finished. Cleaning up..."
        cleanup
        
        echo "Cooldown for $COOLDOWN seconds..."
        sleep $COOLDOWN
    done
done

echo "======================================================"
echo "All Matrix Tests Completed Successfully."
echo "======================================================"
