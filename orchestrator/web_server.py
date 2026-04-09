"""Web dashboard server — HTTP API + static files for Agent Orchestrator."""

import asyncio
import json
import time
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from threading import Thread
from typing import Optional

STATE_DIR = Path(__file__).parent.parent / ".orchestrator"


def _read_json(path: Path) -> list:
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, Exception):
            return []
    return []


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


class DashboardAPI(SimpleHTTPRequestHandler):
    """HTTP handler for dashboard API and static files."""

    # Class-level reference to registry for live agent data
    registry = None
    task_manager = None
    message_bus = None

    def do_GET(self):
        if self.path == "/api/agents":
            self._json_response(self._get_agents())
        elif self.path == "/api/tasks":
            self._json_response(self._get_tasks())
        elif self.path == "/api/messages":
            self._json_response(self._get_messages())
        elif self.path.startswith("/api/messages/agent/"):
            # /api/messages/agent/<agent_id>
            agent_id = self.path.split("/")[-1]
            self._json_response(self._get_agent_messages(agent_id))
        elif self.path == "/api/summary":
            self._json_response(self._get_summary())
        elif self.path == "/api/stream":
            self._handle_sse()
        else:
            # Serve static files from web/ directory
            if self.path == "/" or self.path == "/index.html":
                self.path = "/web/index.html"
            original_path = self.path
            self.path = str(Path(__file__).parent.parent / "web" / self.path.lstrip("/"))
            if Path(self.path).is_file():
                SimpleHTTPRequestHandler.do_GET(self)
            else:
                self.path = original_path
                SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
        data = json.loads(body) if body else {}

        if self.path == "/api/tasks/clear":
            result = self._clear_tasks()
            self._json_response(result)
        elif self.path == "/api/messages/clear":
            result = self._clear_messages()
            self._json_response(result)
        elif self.path == "/api/tasks/update_status":
            result = self._update_task_status(data)
            self._json_response(result)
        else:
            self._json_response({"error": "Not found"}, 404)

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _handle_sse(self):
        """Server-Sent Events for real-time updates."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                agents = self._get_agents()
                tasks = self._get_tasks()
                event = json.dumps({"agents": agents, "tasks": tasks}, ensure_ascii=False)
                self.wfile.write(f"data: {event}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _get_agents(self):
        if self.registry:
            return self.registry.list_agents()
        return []

    def _get_tasks(self):
        tasks_file = STATE_DIR / "tasks.json"
        return _read_json(tasks_file)

    def _get_messages(self):
        msgs_file = STATE_DIR / "messages.json"
        return _read_json(msgs_file)

    def _get_agent_messages(self, agent_id: str):
        """Get messages for a specific agent (sent to or from this agent)."""
        all_messages = self._get_messages()
        agent_messages = []
        for msg in all_messages:
            if msg.get("to_agent") == agent_id or msg.get("from_agent") == agent_id:
                agent_messages.append(msg)
        # Sort by timestamp ascending
        agent_messages.sort(key=lambda x: x.get("timestamp", 0))
        return agent_messages

    def _get_summary(self):
        tasks = self._get_tasks()
        counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0}
        for t in tasks:
            s = t.get("status", "pending")
            if s in counts:
                counts[s] += 1
        agents = self._get_agents()
        return {
            "total_tasks": len(tasks),
            "by_status": counts,
            "active_agents": len([a for a in agents if a.get("status") == "running"]),
            "total_agents": len(agents),
        }

    def _clear_tasks(self):
        tasks_file = STATE_DIR / "tasks.json"
        _write_json(tasks_file, [])
        # Also clear in-memory task manager if available
        if self.task_manager:
            self.task_manager._tasks.clear()
            self.task_manager._save()
        return {"ok": True, "cleared": "tasks"}

    def _clear_messages(self):
        msgs_file = STATE_DIR / "messages.json"
        _write_json(msgs_file, [])
        # Also clear in-memory message bus if available
        if self.message_bus:
            self.message_bus._messages.clear()
            self.message_bus._save()
        return {"ok": True, "cleared": "messages"}

    def _update_task_status(self, data):
        tasks_file = STATE_DIR / "tasks.json"
        tasks = _read_json(tasks_file)
        task_id = data.get("task_id")
        for t in tasks:
            if t.get("id") == task_id:
                if "status" in data:
                    t["status"] = data["status"]
                if "result" in data:
                    t["result"] = data["result"]
                if data.get("status") in ("completed", "failed", "cancelled"):
                    t["completed_at"] = time.time()
                _write_json(tasks_file, tasks)
                return {"ok": True, "task": t}
        return {"error": f"Task {task_id} not found"}, 404

    def log_message(self, format, *args):
        # Suppress default logging to keep console clean
        pass


def start_dashboard_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    registry=None,
    task_manager=None,
    message_bus=None,
) -> Optional[Thread]:
    """Start dashboard HTTP server in a background thread."""
    DashboardAPI.registry = registry
    DashboardAPI.task_manager = task_manager
    DashboardAPI.message_bus = message_bus

    server = HTTPServer((host, port), DashboardAPI)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread
