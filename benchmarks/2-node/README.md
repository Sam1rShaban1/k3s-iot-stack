# 2-Node K3s IoT Stack Benchmark Results

## Test Configuration
- **Nodes**: 2 (raspberrypi + pi7)
- **Scenario**: 100 clients @ 500 msg/s
- **Protocol**: MQTT → EMQX → Benthos → NATS → VictoriaMetrics
- **Duration**: 73118.8s

## Results Summary
- **Total Messages**: 100
- **Throughput**: 0.00 msg/s
- **Average Latency**: 73114106.70 ms
- **P95 Latency**: 73117397.60 ms
- **P99 Latency**: 73117954.80 ms
- **Max Latency**: 73118628.00 ms

## Files
- `benchmark_scenario_summary.csv` - Overall scenario metrics
- `benchmark_per_device.csv` - Per-device detailed metrics
- `benchmark_latency_distribution.csv` - Latency bucket distribution
- `benchmark_timeseries_sampled.csv` - Raw timeseries data
