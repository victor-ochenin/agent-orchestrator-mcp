"""Task manager — persistent JSON storage for tasks."""

import json
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional


@dataclass
class Task:
    id: str
    title: str
    description: str
    assigned_to: Optional[str] = None
    status: str = "pending"  # pending, running, completed, failed, cancelled
    priority: str = "normal"  # low, normal, high
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Optional[str] = None
    dependencies: list[str] = field(default_factory=list)


class TaskManager:
    """Manages tasks with JSON file persistence."""

    def __init__(self, state_dir: Optional[Path] = None):
        if state_dir is None:
            state_dir = Path(".orchestrator")
        self._state_dir = state_dir
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._tasks_file = self._state_dir / "tasks.json"
        self._tasks: dict[str, Task] = {}
        self._load()

    def _load(self):
        """Load tasks from JSON file."""
        if self._tasks_file.exists():
            try:
                data = json.loads(self._tasks_file.read_text("utf-8"))
                for item in data:
                    task = Task(**item)
                    self._tasks[task.id] = task
            except (json.JSONDecodeError, TypeError):
                self._tasks = {}

    def _save(self):
        """Save tasks to JSON file."""
        data = [asdict(t) for t in self._tasks.values()]
        self._tasks_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")

    def create(
        self,
        title: str,
        description: str,
        assigned_to: Optional[str] = None,
        priority: str = "normal",
        dependencies: Optional[list[str]] = None,
    ) -> Task:
        """Create a new task."""
        task = Task(
            id=str(uuid.uuid4())[:8],
            title=title,
            description=description,
            assigned_to=assigned_to,
            priority=priority,
            dependencies=dependencies or [],
        )
        self._tasks[task.id] = task
        self._save()
        return task

    def get(self, task_id: str) -> Optional[dict]:
        """Get a task by ID."""
        task = self._tasks.get(task_id)
        if task:
            return asdict(task)
        return None

    def list_tasks(self, status: Optional[str] = None, assigned_to: Optional[str] = None) -> list[dict]:
        """List tasks with optional filters."""
        result = []
        for task in self._tasks.values():
            if status and task.status != status:
                continue
            if assigned_to and task.assigned_to != assigned_to:
                continue
            result.append(asdict(task))
        # Sort by creation time (newest first)
        result.sort(key=lambda x: x["created_at"], reverse=True)
        return result

    def update_status(self, task_id: str, status: str, result: Optional[str] = None) -> Optional[dict]:
        """Update task status."""
        task = self._tasks.get(task_id)
        if not task:
            return None

        task.status = status
        if result:
            task.result = result
        if status == "running" and not task.started_at:
            task.started_at = time.time()
        if status in ("completed", "failed", "cancelled"):
            task.completed_at = time.time()

        self._save()
        return asdict(task)

    def assign(self, task_id: str, agent_id: str) -> Optional[dict]:
        """Assign a task to an agent."""
        task = self._tasks.get(task_id)
        if not task:
            return None

        task.assigned_to = agent_id
        if task.status == "pending":
            task.status = "running"
            task.started_at = time.time()

        self._save()
        return asdict(task)

    def delete(self, task_id: str) -> bool:
        """Delete a task."""
        if task_id in self._tasks:
            del self._tasks[task_id]
            self._save()
            return True
        return False

    def get_summary(self) -> dict:
        """Get task statistics."""
        counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0}
        for task in self._tasks.values():
            if task.status in counts:
                counts[task.status] += 1
        return {
            "total": len(self._tasks),
            "by_status": counts,
        }

    def get_next_pending(self, exclude_agent: Optional[str] = None) -> Optional[dict]:
        """Get the next pending task (oldest first), optionally excluding tasks assigned to a specific agent."""
        pending_tasks = []
        for task in self._tasks.values():
            if task.status == "pending":
                # If exclude_agent is specified, skip tasks assigned to that agent
                if exclude_agent and task.assigned_to == exclude_agent:
                    continue
                pending_tasks.append(asdict(task))
        
        # Sort by creation time (oldest first)
        pending_tasks.sort(key=lambda x: x["created_at"])
        return pending_tasks[0] if pending_tasks else None

    def reassign_unassigned_pending(self) -> list[dict]:
        """Reassign all unassigned pending tasks in round-robin fashion to available agents.
        Returns list of reassigned tasks."""
        # Get all unassigned pending tasks
        unassigned = []
        for task in self._tasks.values():
            if task.status == "pending" and task.assigned_to is None:
                unassigned.append(asdict(task))
        
        # Sort by creation time (oldest first)
        unassigned.sort(key=lambda x: x["created_at"])
        return unassigned
