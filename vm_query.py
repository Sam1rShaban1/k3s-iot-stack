#!/usr/bin/env python3
import sys, json, urllib.request

metric = sys.argv[1]
output = sys.argv[2]

url = f"http://192.168.1.50:30000/api/v1/export?match[]={{__name__=\"{metric}\"}}"
try:
    req = urllib.request.urlopen(url, timeout=30)
    data = req.read().decode()
    with open(output, 'w') as f:
        f.write(data)
except Exception as e:
    with open(output, 'w') as f:
        f.write('{"data":[]}')
