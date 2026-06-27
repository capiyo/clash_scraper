import socket
import ssl

host = "ac-bgxem5n-shard-00-00.omepeze.mongodb.net"
port = 27017

print(f"Testing raw TCP connect to {host}:{port} ...")
try:
    with socket.create_connection((host, port), timeout=10) as s:
        print("TCP connect OK")
        ctx = ssl.create_default_context()
        try:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                print("TLS OK:", ss.version())
        except Exception as e:
            print("TLS handshake FAILED:", e)
except Exception as e:
    print("TCP connect FAILED:", e)