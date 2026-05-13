import socket, struct, time, threading, sys

def create_connect_packet(client_id):
    protocol_name = b'MQTT'
    protocol_level = 4
    connect_flags = 0x02
    keep_alive = 60
    var_header = struct.pack('!H', len(protocol_name)) + protocol_name
    var_header += struct.pack('B', protocol_level)
    var_header += struct.pack('B', connect_flags)
    var_header += struct.pack('!H', keep_alive)
    payload = struct.pack('!H', len(client_id)) + client_id.encode()
    remaining_length = len(var_header) + len(payload)
    fixed_header = struct.pack('B', 0x10) + struct.pack('B', remaining_length)
    return fixed_header + var_header + payload

def create_publish_packet(topic, message):
    topic_bytes = topic.encode()
    var_header = struct.pack('!H', len(topic_bytes)) + topic_bytes
    payload = message.encode()
    remaining_length = len(var_header) + len(payload)
    fixed_header = struct.pack('B', 0x30) + struct.pack('B', remaining_length)
    return fixed_header + var_header + payload

# Connect to EMQX
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(10)
sock.connect(('192.168.1.50', 1883))
sock.send(create_connect_packet('test-batch'))
sock.recv(1024)  # CONNACK

# Send 1000 messages as fast as possible
start_time = time.time()
for i in range(1000):
    msg = f'{{"device_id":"test-batch_{i}", "ts":{int(time.time()*1000)}, "temp":25.0, "hum":50.0}}'
    sock.send(create_publish_packet('sensors/test', msg))
end_time = time.time()

print(f'Published 1000 messages in {end_time - start_time:.2f}s')
print(f'Rate: {1000 / (end_time - start_time):.2f} msg/s')

sock.close()
