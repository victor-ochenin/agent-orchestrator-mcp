"""Message bus — file-based communication between agents."""

import json
import os
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional


@dataclass
class Message:
    id: str
    from_agent: Optional[str]  # None = system/orchestrator
    to_agent: Optional[str]  # None = broadcast to all
    content: str
    timestamp: float = field(default_factory=time.time)
    task_id: Optional[str] = None
    from_agent_name: Optional[str] = None  # Human-readable name
    to_agent_name: Optional[str] = None  # Human-readable name


class MessageBus:
    """File-based message bus for inter-agent communication.

    Uses file locking (msvcrt on Windows, fcntl on Unix) to prevent
    race conditions when multiple agents write messages concurrently.
    """

    def __init__(self, state_dir: Optional[Path] = None):
        if state_dir is None:
            state_dir = Path(".orchestrator")
        self._state_dir = state_dir
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._messages_file = self._state_dir / "messages.json"
        self._messages: list[Message] = []
        self._load()

    def _lock_file(self, file_obj, exclusive: bool = True):
        """Acquire a file lock. Works on Windows (msvcrt) and Unix (fcntl)."""
        import platform
        if platform.system() == "Windows":
            import msvcrt
            mode = msvcrt.LK_NBLCK if not exclusive else msvcrt.LK_LOCK
            # msvcrt.locking needs the file position at the start
            file_obj.seek(0)
            # Lock the first 1MB — enough for our JSON files
            try:
                msvcrt.locking(file_obj.fileno(), msvcrt.LK_LOCK, 1024 * 1024)
            except OSError:
                pass  # Best-effort locking
        else:
            import fcntl
            flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(file_obj.fileno(), flag)

    def _unlock_file(self, file_obj):
        """Release a file lock."""
        import platform
        if platform.system() == "Windows":
            import msvcrt
            file_obj.seek(0)
            try:
                msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1024 * 1024)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)

    def _load(self):
        """Load messages from file with locking."""
        if self._messages_file.exists():
            with open(self._messages_file, "r", encoding="utf-8") as f:
                self._lock_file(f, exclusive=False)
                try:
                    data = json.loads(f.read())
                    for item in data:
                        msg = Message(**item)
                        self._messages.append(msg)
                except (json.JSONDecodeError, TypeError):
                    self._messages = []
                finally:
                    self._unlock_file(f)

    def _save(self):
        """Save messages to file with locking to prevent race conditions."""
        data = [asdict(m) for m in self._messages]
        with open(self._messages_file, "w", encoding="utf-8") as f:
            self._lock_file(f, exclusive=True)
            try:
                f.write(json.dumps(data, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            finally:
                self._unlock_file(f)

    def send(
        self,
        content: str,
        from_agent: Optional[str] = None,
        to_agent: Optional[str] = None,
        task_id: Optional[str] = None,
        from_agent_name: Optional[str] = None,
        to_agent_name: Optional[str] = None,
    ) -> Message:
        """Send a message."""
        msg = Message(
            id=str(uuid.uuid4())[:8],
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            task_id=task_id,
            from_agent_name=from_agent_name,
            to_agent_name=to_agent_name,
        )
        self._messages.append(msg)
        self._save()
        return msg

    def get(
        self,
        agent_id: Optional[str] = None,
        last_n: int = 50,
        after_timestamp: Optional[float] = None,
    ) -> list[dict]:
        """
        Get messages.

        If agent_id is specified:
          - Returns messages TO that agent (direct or broadcast)
          - Returns messages FROM that agent
        If agent_id is None, returns all messages.
        """
        result = []
        for msg in self._messages:
            if agent_id:
                if msg.to_agent not in (None, agent_id) and msg.from_agent != agent_id:
                    continue
            if after_timestamp and msg.timestamp <= after_timestamp:
                continue
            result.append(asdict(msg))

        # Newest first
        result.sort(key=lambda x: x["timestamp"], reverse=True)
        return result[:last_n]

    def broadcast(self, content: str, from_agent: Optional[str] = None, task_id: Optional[str] = None) -> Message:
        """Send a broadcast message to all agents."""
        return self.send(content=content, from_agent=from_agent, to_agent=None, task_id=task_id)

    def count(self) -> int:
        """Total number of messages."""
        return len(self._messages)

    def clear(self, older_than: Optional[float] = None) -> int:
        """Clear old messages. If older_than specified, only clear messages before that timestamp."""
        if older_than:
            self._messages = [m for m in self._messages if m.timestamp > older_than]
        else:
            self._messages = []
        self._save()
        return len(self._messages)
