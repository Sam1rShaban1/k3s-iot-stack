import time, subprocess, re

# Step 1: Get initial Benthos stats
result = subprocess.run(['curl', '-s', 'http://192.168.1.50:4195/stats'], capture_output=True, text=True)
initial = int(re.search(r'input_received\{label="[^"]*"\}[^}]*\} (\d+)', result.stdout).group(1))

# Step 2: Publish 5000 messages quickly
start = time.time()
for i in range(5000):
    # Use the C publisher with QoS 0, delay=0
    subprocess.Popen(['./publisher', '192.168.1.50', '1883', f'test_{i}', '0', 'sensors/test'])
    time.sleep(0.0001)  # 0.1ms between starts

# Step 3: Wait for Benthos to process them
time.sleep(10)

# Step 4: Get final Benthos stats
result = subprocess.run(['curl', '-s', 'http://192.168.1.50:4195/stats'], capture_output=True, text=True)
final = int(re.search(r'input_received\{label="[^"]*"\}[^}]*\} (\d+)', result.stdout).group(1))

duration = time.time() - start
delta = final - initial
rate = delta / duration

print(f'Initial: {initial}')
print(f'Final: {final}')
print(f'Delta: {delta} messages in {duration:.1f}s')
print(f'Rate: {rate:.1f} msg/s')
