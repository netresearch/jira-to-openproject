#!/usr/bin/env python3
import socket

host = "sobol.nr"
port = 22

try:
    sock = socket.socket()
    sock.settimeout(5)
    result = sock.connect_ex((host, port))
    if result == 0:
        print(f"✓ Port {port} is open on {host}")
    else:
        print(f"✗ Port {port} is closed/filtered on {host} (error code: {result})")
    sock.close()
except Exception as e:
    print(f"✗ Error checking {host}:{port} - {e}")
