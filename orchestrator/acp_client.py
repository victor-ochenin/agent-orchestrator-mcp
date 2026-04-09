"""ACP (Agent Client Protocol) client for Qwen Code."""

import asyncio
import json
import platform
import shutil
from pathlib import Path
from typing import Optional


def _find_qwen() -> str:
    """Find the qwen executable, handling Windows .cmd extension."""
    cmd = shutil.which("qwen")
    if cmd:
        return cmd
    if platform.system() == "Windows":
        cmd = shutil.which("qwen.cmd")
        if cmd:
            return cmd
    return "qwen"  # fallback, will fail on some systems


class ACPClient:
    """Client for Qwen Code's Agent Client Protocol (ACP).

    ACP is a JSON-RPC 2.0 protocol over stdio. It allows:
    - Creating sessions that persist across multiple tasks
    - Sending tasks as session/prompt calls
    - Receiving streaming responses via session/update notifications
    - Auto-handling permission requests
    """

    def __init__(self, cwd: Optional[str] = None, output_format: str = "json"):
        """Initialize ACP client (does not start the process).

        Args:
            cwd: Working directory for Qwen sessions
            output_format: Output format flag for qwen CLI (default: "json")
        """
        self._qwen_cmd = _find_qwen()
        self._cwd = cwd or "."
        self._output_format = output_format
        self._process: Optional[asyncio.subprocess.Process] = None
        self._session_id: Optional[str] = None
        self._initialized = False
        self._task: Optional[asyncio.Task] = None
        self._msg_queue: asyncio.Queue = asyncio.Queue()

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    async def start(self) -> dict:
        """Start the Qwen ACP subprocess and initialize the connection.

        Returns:
            dict with agent info (name, version, etc.)
        """
        args = [self._qwen_cmd, "--acp", "-o", self._output_format]

        if platform.system() == "Windows":
            full_cmd = " ".join([f'"{a}"' if " " in a else a for a in args])
            self._process = await asyncio.create_subprocess_shell(
                full_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        # Start background reader
        self._task = asyncio.create_task(self._reader())

        # Send initialize
        resp = await self._request("initialize", {
            "protocolVersion": 1,
            "capabilities": {},
            "clientInfo": {"name": "agent-orchestrator", "version": "0.1"},
        }, request_id=1)

        if "error" in resp:
            await self.stop()
            raise RuntimeError(f"ACP init failed: {resp['error']}")

        agent_info = resp.get("result", {})

        # Send initialized notification
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

        self._initialized = True
        return agent_info

    async def new_session(self, cwd: Optional[str] = None, mcp_servers: Optional[list] = None) -> str:
        """Create a new ACP session.

        Args:
            cwd: Working directory (overrides constructor default)
            mcp_servers: List of MCP server configs (default: [])

        Returns:
            Session ID string
        """
        session_cwd = cwd or self._cwd
        resp = await self._request("session/new", {
            "cwd": session_cwd,
            "mcpServers": mcp_servers or [],
        }, request_id=2)

        if "error" in resp:
            raise RuntimeError(f"session/new failed: {resp['error']}")

        self._session_id = resp["result"]["sessionId"]
        return self._session_id

    async def run_task(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        timeout_per_message: float = 5.0,
        max_messages: int = 200,
        request_id: int = 10,
    ) -> dict:
        """Send a task (prompt) and wait for completion.

        Args:
            prompt: The task text to send
            session_id: Session ID (uses current session if None)
            timeout_per_message: Timeout for each response message
            max_messages: Maximum number of messages to wait for
            request_id: JSON-RPC request id (to match responses)

        Returns:
            dict with:
                - answer: collected response text
                - stop_reason: why the task stopped (end_turn, etc.)
                - messages_count: number of messages received
        """
        sid = session_id or self._session_id
        if not sid:
            raise RuntimeError("No active session. Call new_session() first.")

        # Send session/prompt directly (don't wait for response via _request,
        # we'll read it from the queue in the loop below)
        self._write({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/prompt",
            "params": {
                "sessionId": sid,
                "prompt": [{"type": "text", "text": prompt}],
            },
        })
        if self._process and self._process.stdin:
            await self._process.stdin.drain()

        answer = ""
        msg_count = 0
        stop_reason = None

        for _ in range(max_messages):
            msg = await self._read(timeout=timeout_per_message)
            if msg is None:
                break

            msg_count += 1

            msg_id = msg.get("id")
            if msg_id == request_id:
                result = msg.get("result", {})
                if result.get("stopReason"):
                    stop_reason = result["stopReason"]
                    break
                continue

            if "session/update" in msg.get("method", ""):
                update = msg.get("params", {}).get("update", {})
                session_update_type = update.get("sessionUpdate", "")
                content = update.get("content", {})

                if isinstance(content, dict):
                    text = content.get("text", "")
                else:
                    text = str(content) if content else ""

                if session_update_type in ("agent_thought_chunk", "agent_message_chunk"):
                    if text:
                        answer += text

            # Auto-respond to permission requests
            if "session/request_permission" in msg.get("method", ""):
                perm_id = msg.get("id")
                if perm_id:
                    self._write({
                        "jsonrpc": "2.0",
                        "id": perm_id,
                        "result": {"outcome": "allowAlways"},
                    })

        return {
            "answer": answer,
            "stop_reason": stop_reason,
            "messages_count": msg_count,
        }

    async def run_tasks(self, tasks: list[str], session_id: Optional[str] = None) -> list[dict]:
        """Run multiple tasks sequentially in the same session.

        Args:
            tasks: List of prompt strings
            session_id: Session ID (uses current if None)

        Returns:
            List of result dicts (same as run_task)
        """
        results = []
        for i, task_text in enumerate(tasks):
            result = await self.run_task(task_text, session_id=session_id, request_id=10 + i)
            results.append(result)
        return results

    async def stop(self):
        """Terminate the Qwen subprocess."""
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        self._process = None
        self._initialized = False
        self._session_id = None

    def _write(self, obj: dict):
        """Write a JSON-RPC message to stdin."""
        if self._process and self._process.stdin:
            self._process.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))

    async def _request(self, method: str, params: dict, request_id: int) -> dict:
        """Send a request and wait for the response with matching id."""
        self._write({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        })
        if self._process and self._process.stdin:
            await self._process.stdin.drain()

        # Read until we find a message with matching id,
        # pushing unrelated messages back to the queue
        while True:
            msg = await self._read(timeout=15.0)
            if msg is None:
                return {"error": {"message": f"Timeout waiting for {method}"}}
            # Match by id (response to our request)
            if msg.get("id") == request_id:
                return msg
            # Not our response — put it back in queue for the caller
            await self._msg_queue.put(msg)

    async def _read(self, timeout: float = 5.0) -> Optional[dict]:
        """Read the next JSON-RPC message from the message queue."""
        try:
            return await asyncio.wait_for(self._msg_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def _reader(self):
        """Background task: read stdout and push to message queue."""
        if not self._process or not self._process.stdout:
            return
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace").strip())
                    await self._msg_queue.put(msg)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass  # skip malformed lines
        except Exception:
            pass

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()
