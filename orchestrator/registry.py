"""Agent registry — manages subprocess agents."""

import asyncio
import os
import shutil
import uuid
import time
import platform
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# Default allowed cwd root — agents cannot escape this directory tree.
# Can be overridden via ORCHESTRATOR_ALLOWED_CWD_ROOT env var.
# IMPORTANT: This is updated dynamically when workspace is set via set_workspace_cwd().
# Initially set to current dir, but will be expanded to workspace parent.
_DEFAULT_CWD_ROOT = Path(os.getcwd()).resolve()
_ALLOWED_CWD_ROOT = Path(os.environ.get("ORCHESTRATOR_ALLOWED_CWD_ROOT", str(_DEFAULT_CWD_ROOT))).resolve()

# Workspace cwd — the directory where the orchestrator was launched from (set by MCP client).
# Agents will use this as their default cwd instead of the orchestrator project directory.
_WORKSPACE_CWD: Optional[Path] = None


def set_workspace_cwd(workspace_path: str) -> Path:
    """Set the workspace working directory for spawned agents.

    This allows agents to work in the directory where the orchestrator
    was launched from, rather than the orchestrator project directory.

    The allowed cwd root is automatically set to the workspace's parent
    directory, allowing agents to work in any sibling directory.

    Args:
        workspace_path: Absolute path to the workspace directory.

    Returns:
        Resolved workspace path.

    Raises:
        ValueError: If the path does not exist.
    """
    global _WORKSPACE_CWD, _ALLOWED_CWD_ROOT
    resolved = Path(workspace_path).resolve()

    if not resolved.exists():
        raise ValueError(f"Workspace directory does not exist: {workspace_path}")

    # Expand allowed cwd root to the workspace's parent directory
    # This allows agents to work in the workspace and any sibling directories
    _ALLOWED_CWD_ROOT = resolved.parent if resolved.parent != resolved else resolved

    _WORKSPACE_CWD = resolved
    return resolved


def get_workspace_cwd() -> Optional[Path]:
    """Get the current workspace cwd, or None if not set."""
    return _WORKSPACE_CWD


def validate_cwd(cwd: str) -> str:
    """Validate and resolve cwd to ensure it is within the allowed directory tree.

    Returns the resolved path if valid.
    Raises ValueError if cwd attempts to escape the allowed root.
    """
    resolved = Path(cwd).resolve()

    # On Windows, also check the drive letter matches
    if platform.system() == "Windows":
        if resolved.drive != _ALLOWED_CWD_ROOT.drive:
            raise ValueError(
                f"CWD '{cwd}' resolved to '{resolved}' which is on a different drive "
                f"than allowed root '{_ALLOWED_CWD_ROOT}'."
            )

    try:
        resolved.relative_to(_ALLOWED_CWD_ROOT)
    except ValueError:
        raise ValueError(
            f"CWD '{cwd}' resolved to '{resolved}' which is outside the allowed "
            f"directory tree '{_ALLOWED_CWD_ROOT}'. "
            f"Set ORCHESTRATOR_ALLOWED_CWD_ROOT to change the allowed root."
        )

    return str(resolved)


@dataclass
class AgentInfo:
    """Information about a spawned agent."""
    id: str
    name: str
    command: str
    args: list[str]
    cwd: str
    status: str  # "running", "stopped", "failed"
    pid: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    stopped_at: Optional[float] = None
    error: Optional[str] = None
    current_task: Optional[str] = None
    role: Optional[dict] = None  # {"role": "coder", "description": "..."}


def _resolve_command(command: str) -> str:
    """Resolve command path, handling Windows .cmd/.bat extensions."""
    found = shutil.which(command)
    if found:
        return found
    # On Windows, try adding .cmd/.bat extensions
    if platform.system() == "Windows":
        for ext in [".cmd", ".bat", ".ps1"]:
            found = shutil.which(command + ext)
            if found:
                return found
    return command  # Return original if not found


class AgentRegistry:
    """Registry of all spawned agents."""

    def __init__(self):
        self._agents: dict[str, AgentInfo] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._output_buffers: dict[str, list[str]] = {}

    def spawn(self, name: str, command: str, args: list[str], cwd: str, role: Optional[dict] = None) -> AgentInfo:
        """Register a new agent (asyncio process creation happens in spawn_process)."""
        # Validate cwd is within allowed directory tree
        validated_cwd = validate_cwd(cwd)

        agent_id = str(uuid.uuid4())[:8]
        # Resolve command path for Windows compatibility
        resolved_command = _resolve_command(command)
        info = AgentInfo(
            id=agent_id,
            name=name,
            command=resolved_command,
            args=args,
            cwd=validated_cwd,
            status="starting",
            role=role,
        )
        self._agents[agent_id] = info
        self._output_buffers[agent_id] = []
        return info

    async def spawn_process(self, agent_id: str) -> dict:
        """Actually start the subprocess for a registered agent."""
        info = self._agents.get(agent_id)
        if not info:
            return {"error": f"Agent {agent_id} not found"}

        cmd = [info.command] + info.args

        # Sanitize args — block path traversal patterns
        for arg in info.args:
            if ".." in arg and ("/" in arg or "\\" in arg):
                info.status = "failed"
                info.error = f"Blocked arg with path traversal: {arg}"
                return {"error": info.error}

        # Ensure cwd exists and is within allowed directory
        cwd_path = Path(info.cwd)
        if not cwd_path.exists():
            cwd_path.mkdir(parents=True, exist_ok=True)

        # On Windows, use shell=True for .cmd/.bat/.ps1 files
        use_shell = platform.system() == "Windows" and info.command.lower().endswith((".cmd", ".bat", ".ps1"))
        
        try:
            if use_shell:
                # For batch files, we need to use shell
                full_cmd = " ".join([info.command] + [f'"{a}"' if " " in str(a) else str(a) for a in info.args])
                process = await asyncio.create_subprocess_shell(
                    full_cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd_path,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd_path,
                )
            info.pid = process.pid
            info.status = "running"
            self._processes[agent_id] = process

            # Start background task to read output
            asyncio.create_task(self._read_output(agent_id, process))

            return {"agent_id": agent_id, "pid": process.pid, "status": "running"}
        except FileNotFoundError:
            info.status = "failed"
            info.error = f"Command not found: {info.command}"
            return {"error": info.error}
        except Exception as e:
            info.status = "failed"
            info.error = str(e)
            return {"error": str(e)}

    async def _read_output(self, agent_id: str, process: asyncio.subprocess.Process):
        """Background task to read subprocess stdout."""
        if process.stdout is None:
            return
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                self._output_buffers.setdefault(agent_id, []).append(decoded)
                # Keep buffer manageable
                if len(self._output_buffers[agent_id]) > 500:
                    self._output_buffers[agent_id] = self._output_buffers[agent_id][-200:]
        except Exception:
            pass
        finally:
            # Process exited
            if agent_id in self._agents:
                self._agents[agent_id].status = "stopped"
                self._agents[agent_id].stopped_at = time.time()
                rc = await process.wait()
                if rc != 0:
                    self._agents[agent_id].error = f"Exit code: {rc}"

    async def stop(self, agent_id: str) -> dict:
        """Stop a running agent."""
        info = self._agents.get(agent_id)
        if not info:
            return {"error": f"Agent {agent_id} not found"}

        process = self._processes.get(agent_id)
        if process and process.returncode is None:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            except Exception as e:
                return {"error": f"Failed to stop: {e}"}

        info.status = "stopped"
        info.stopped_at = time.time()
        self._processes.pop(agent_id, None)
        return {"agent_id": agent_id, "status": "stopped"}

    def list_agents(self) -> list[dict]:
        """List all registered agents."""
        result = []
        for info in self._agents.values():
            d = asdict(info)
            d["output_lines"] = len(self._output_buffers.get(info.id, []))
            result.append(d)
        return result

    def get_output(self, agent_id: str, last_n: int = 50) -> dict:
        """Get recent output from an agent."""
        info = self._agents.get(agent_id)
        if not info:
            return {"error": f"Agent {agent_id} not found"}

        buffer = self._output_buffers.get(agent_id, [])
        lines = buffer[-last_n:] if len(buffer) > last_n else buffer
        return {
            "agent_id": agent_id,
            "status": info.status,
            "lines": lines,
            "total_available": len(buffer),
        }

    def update_task(self, agent_id: str, task_id: Optional[str]):
        """Update the current task assignment for an agent."""
        info = self._agents.get(agent_id)
        if info:
            info.current_task = task_id

    async def send_input(self, agent_id: str, text: str) -> dict:
        """Send text to agent's stdin."""
        process = self._processes.get(agent_id)
        if not process or process.stdin is None:
            return {"error": f"Agent {agent_id} is not running or has no stdin"}

        try:
            process.stdin.write((text + "\n").encode("utf-8"))
            await process.stdin.drain()
            return {"sent": True}
        except Exception as e:
            return {"error": f"Failed to send input: {e}"}
