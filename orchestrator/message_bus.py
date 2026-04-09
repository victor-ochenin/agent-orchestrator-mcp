"""Message bus — file-based communication between agents."""

import json
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


class MessageBus:
    """File-based message bus for inter-agent communication."""

    def __init__(self, state_dir: Optional[Path] = None):
        if state_dir is None:
            state_dir = Path(".orchestrator")
        self._state_dir = state_dir
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._messages_file = self._state_dir / "messages.json"
        self._messages: list[Message] = []
        self._load()

    def _load(self):
        """Load messages from file."""
        if self._messages_file.exists():
            try:
                data = json.loads(self._messages_file.read_text("utf-8"))
                for item in data:
                    msg = Message(**item)
                    self._messages.append(msg)
            except (json.JSONDecodeError, TypeError):
                self._messages = []

    def _save(self):
        """Save messages to file."""
        data = [asdict(m) for m in self._messages]
        self._messages_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")

    def send(
        self,
        content: str,
        from_agent: Optional[str] = None,
        to_agent: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Message:
        """Send a message."""
        msg = Message(
            id=str(uuid.uuid4())[:8],
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            task_id=task_id,
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
