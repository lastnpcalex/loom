"""Send a graceful shutdown request to the Loom test server."""
import urllib.request
import sys

port = int(sys.argv[1]) if len(sys.argv) > 1 else 3001
try:
    req = urllib.request.Request(f"http://localhost:{port}/shutdown", method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        print(resp.read().decode())
except Exception as e:
    print(f"Could not reach server on port {port}: {e}")
