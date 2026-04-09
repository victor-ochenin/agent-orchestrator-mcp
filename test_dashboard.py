"""Quick test: start dashboard web server and verify API endpoints."""
import sys, time, urllib.request, json, threading, _thread

# Add parent dir to path
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))

from orchestrator.web_server import start_dashboard_server

# Start dashboard
thread = start_dashboard_server(host="127.0.0.1", port=8765)
time.sleep(1)

print("=== Testing Dashboard API ===\n")

# Test /api/summary
try:
    resp = urllib.request.urlopen("http://127.0.0.1:8765/api/summary")
    data = json.loads(resp.read())
    print(f"GET /api/summary → {json.dumps(data, indent=2)}")
except Exception as e:
    print(f"FAIL /api/summary: {e}")

# Test /api/agents
try:
    resp = urllib.request.urlopen("http://127.0.0.1:8765/api/agents")
    data = json.loads(resp.read())
    print(f"\nGET /api/agents → {data}")
except Exception as e:
    print(f"FAIL /api/agents: {e}")

# Test /api/tasks
try:
    resp = urllib.request.urlopen("http://127.0.0.1:8765/api/tasks")
    data = json.loads(resp.read())
    print(f"\nGET /api/tasks → {len(data)} tasks")
except Exception as e:
    print(f"FAIL /api/tasks: {e}")

# Test /api/messages
try:
    resp = urllib.request.urlopen("http://127.0.0.1:8765/api/messages")
    data = json.loads(resp.read())
    print(f"\nGET /api/messages → {len(data)} messages")
except Exception as e:
    print(f"FAIL /api/messages: {e}")

# Test POST /api/tasks/clear
try:
    req = urllib.request.Request("http://127.0.0.1:8765/api/tasks/clear", method="POST", data=b"{}")
    req.add_header("Content-Type", "application/json")
    resp = urllib.request.urlopen(req)
    data = json.loads(resp.read())
    print(f"\nPOST /api/tasks/clear → {data}")
except Exception as e:
    print(f"FAIL /api/tasks/clear: {e}")

# Test GET /index.html
try:
    resp = urllib.request.urlopen("http://127.0.0.1:8765/")
    html = resp.read().decode()
    print(f"\nGET / → {len(html)} bytes, title present: {'Agent Orchestrator' in html}")
except Exception as e:
    print(f"FAIL GET /: {e}")

print("\n=== All tests done ===")
