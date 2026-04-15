"""MCP Server for Agent Orchestration."""

import json
import asyncio
import os
from pathlib import Path
from typing import Optional
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from orchestrator.registry import AgentRegistry, set_workspace_cwd, get_workspace_cwd
from orchestrator.task_manager import TaskManager
from orchestrator.message_bus import MessageBus
from orchestrator.web_server import start_dashboard_server
from orchestrator.utils import pick_role, generate_agent_name
from orchestrator.acp_client import ACPClient, PersistentACPSession

app = Server("agent-orchestrator-mcp")

# Shared state
registry = AgentRegistry()
task_manager = TaskManager()
message_bus = MessageBus()

# Track ACP agents (virtual agents managed by _run_acp_agent)
# Format: agent_id -> {"agent_name": str, "role": dict}
_acp_agents: dict[str, dict] = {}

# Persistent sessions that survive across multiple run_task calls
# Format: agent_id -> PersistentACPSession
_persistent_sessions: dict[str, PersistentACPSession] = {}

# Rate limiting: track total agents spawned per session
_MAX_AGENTS_PER_SESSION = int(os.environ.get("ORCHESTRATOR_MAX_AGENTS", 5))
_spawned_agents_count = 0

DASHBOARD_PORT = int(os.environ.get("ORCHESTRATOR_DASHBOARD_PORT", 8765))


def _resolve_agent_name(agent_id: Optional[str]) -> Optional[str]:
    """Resolve agent ID to human-readable name (role-based)."""
    if agent_id is None:
        return None
    # Special names
    if agent_id == "orchestrator":
        return "orchestrator"
    # Check ACP agents first — use role name
    agent_data = _acp_agents.get(agent_id)
    if agent_data:
        role = agent_data.get("role", {})
        role_name = role.get("role", "agent")
        return role_name
    # Check registry
    info = registry._agents.get(agent_id)
    if info:
        return info.name
    return agent_id[:8]  # fallback to short ID


def _send_msg(content: str, from_agent: Optional[str] = None, to_agent: Optional[str] = None,
            task_id: Optional[str] = None):
    """Send message with auto-resolved agent names."""
    message_bus.send(
        content=content,
        from_agent=from_agent,
        to_agent=to_agent,
        task_id=task_id,
        from_agent_name=_resolve_agent_name(from_agent),
        to_agent_name=_resolve_agent_name(to_agent),
    )


def _broadcast_msg(content: str, from_agent: Optional[str] = None, task_id: Optional[str] = None):
    """Broadcast message with auto-resolved agent names."""
    return message_bus.broadcast(
        content=content,
        from_agent=from_agent,
        task_id=task_id,
        from_agent_name=_resolve_agent_name(from_agent),
    )


async def _run_acp_agent_persistent(agent_id: str, agent_name: str, prompts: list[str], task_ids: list[str], session: PersistentACPSession):
    """Run tasks sequentially in an existing persistent session without restarting.

    Unlike _run_acp_agent, this does NOT create/destroy the subprocess.
    It reuses the existing session and keeps the agent alive after tasks complete.
    """
    try:
        # Ensure session is started
        if not session.is_alive:
            await session.start()

        # Process each task
        for i, prompt_text in enumerate(prompts):
            task_id = task_ids[i]
            task_manager.update_status(task_id, status="running")
            registry.update_task(agent_id, task_id)

            _send_msg(
                content=prompt_text,
                from_agent="orchestrator",
                to_agent=agent_id,
                task_id=task_id,
            )

            result = await session.run_task(prompt_text)

            answer = result.get("answer", "")
            stop_reason = result.get("stop_reason", "")

            # Check for pending confirmation
            confirmation_keywords = [
                "продолжить", "confirm", "are you sure", "you sure",
                "proceed", "permission", "разрешен", "подтвержд",
                "permanently", "удалить", "delete", "уничтож",
            ]
            is_pending_confirmation = any(
                kw in answer.lower() for kw in confirmation_keywords
            )

            if is_pending_confirmation and stop_reason:
                _send_msg(
                    content=f"Запрос подтверждения — отправляю 'да'",
                    from_agent=agent_id,
                    to_agent="orchestrator",
                    task_id=task_id,
                )
                result = await session.run_task("yes")
                answer = result.get("answer", "")
                stop_reason = result.get("stop_reason", "")

            task_manager.update_status(
                task_id,
                status="completed" if stop_reason else "failed",
                result=answer if answer else f"(stop: {stop_reason})",
            )

            if answer:
                _send_msg(
                    content=answer,
                    from_agent=agent_id,
                    to_agent="orchestrator",
                    task_id=task_id,
                )
            else:
                _send_msg(
                    content=f"Задача не выполнена | stop: {stop_reason or 'N/A'}",
                    from_agent=agent_id,
                    to_agent="orchestrator",
                    task_id=task_id,
                )

        # All tasks done — agent goes IDLE (stays alive, waiting for next task)
        if agent_id in registry._agents:
            registry._agents[agent_id].status = "idle"
        _send_msg(
            content="Задачи выполнены. Агент ожидает следующие задачи.",
            from_agent=agent_id,
            to_agent="orchestrator",
        )

    except Exception as e:
        for task_id in task_ids:
            t = task_manager.get(task_id)
            if t and t.get("status") == "running":
                task_manager.update_status(task_id, status="failed", result=str(e))

        registry._agents[agent_id].status = "failed"
        registry._agents[agent_id].error = str(e)
        _send_msg(
            content=f"Ошибка агента\n{e}",
            from_agent=agent_id,
            to_agent="orchestrator",
        )
        # Remove dead session from tracking
        _persistent_sessions.pop(agent_id, None)


@app.list_tools()
async def list_tools() -> list[Tool]:
    """Return list of available tools."""
    return [
        Tool(
            name="list_agents",
            description="Список всех зарегистрированных агентов с их статусами.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="create_task",
            description="Создать новую задачу для агента.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Заголовок задачи.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Описание задачи.",
                    },
                    "assigned_to": {
                        "type": "string",
                        "description": "ID агента для назначения (опционально).",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "description": "Приоритет задачи.",
                        "default": "normal",
                    },
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "ID задач-зависимостей.",
                    },
                },
                "required": ["title", "description"],
            },
        ),
        Tool(
            name="list_tasks",
            description="Список задач с фильтрацией по статусу и агенту.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "running", "completed", "failed", "cancelled"],
                        "description": "Фильтр по статусу.",
                    },
                    "assigned_to": {
                        "type": "string",
                        "description": "Фильтр по агенту.",
                    },
                },
            },
        ),
        Tool(
            name="update_task",
            description="Обновить статус или результат задачи.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "ID задачи.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "running", "completed", "failed", "cancelled"],
                        "description": "Новый статус.",
                    },
                    "result": {
                        "type": "string",
                        "description": "Результат выполнения (опционально).",
                    },
                },
                "required": ["task_id", "status"],
            },
        ),
        Tool(
            name="assign_task",
            description="Назначить задачу конкретному агенту.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "ID задачи.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "ID агента.",
                    },
                },
                "required": ["task_id", "agent_id"],
            },
        ),
        Tool(
            name="task_summary",
            description="Статистика по задачам (количество по статусам).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="send_message",
            description="Отправить сообщение агенту (сохраняется в шине сообщений).",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Текст сообщения.",
                    },
                    "from_agent": {
                        "type": "string",
                        "description": "ID отправителя (опционально, None = от оркестратора).",
                    },
                    "to_agent": {
                        "type": "string",
                        "description": "ID получателя (опционально, None = broadcast всем).",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "ID связанной задачи (опционально).",
                    },
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="get_messages",
            description="Получить сообщения из шины. Можно фильтровать по агенту.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "ID агента для фильтрации (опционально).",
                    },
                    "last_n": {
                        "type": "integer",
                        "description": "Количество последних сообщений (по умолчанию 500).",
                        "default": 500,
                    },
                },
            },
        ),
        Tool(
            name="broadcast_message",
            description="Отправить сообщение всем агентам (broadcast).",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Текст сообщения.",
                    },
                    "from_agent": {
                        "type": "string",
                        "description": "ID отправителя (опционально).",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "ID связанной задачи (опционально).",
                    },
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="clear_tasks",
            description="Очистить все задачи (удалить tasks.json).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="clear_messages",
            description="Очистить все сообщения (удалить messages.json).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="clear_session",
            description="Очистить все сообщения и задачи (завершение сессии).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="run_task",
            description="Выполнить задачу через ACP (Agent Client Protocol) — чистый JSON-RPC API. "
                        "Создаёт одну сессию, отправляет промпт, ждёт ответ. "
                        "Можно указать несколько задач — они выполнятся в одной сессии. "
                        "Это основной способ запуска задач в оркестраторе.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Список промптов (задач) для выполнения в одной сессии. "
                                       "Можно один — тогда будет выполнено последовательно.",
                        "minItems": 1,
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Рабочая директория для Qwen.",
                    },
                    "yolo": {
                        "type": "boolean",
                        "description": "Включить режим yolo — пропускать все подтверждения действий. "
                                       "Позволяет выполнять файловые операции, shell-команды и т.д. без подтверждений. "
                                       "По умолчанию всегда True.",
                        "default": True,
                    },
                    "mcp_servers": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Список конфигураций MCP-серверов для подключения агента. "
                                       "Каждый элемент — объект с полями name, command, args, cwd.",
                    },
                },
                "required": ["prompts"],
            },
        ),
        Tool(
            name="set_workspace",
            description="Установить рабочую директорию для запускаемых агентов. "
                        "По умолчанию агенты работают в директории проекта оркестратора. "
                        "Этот инструмент позволяет агентам работать в директории, "
                        "где был запущен MCP-клиент (оркестратор).",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Абсолютный путь к рабочей директории (например, C:\\Users\\user\\Desktop\\MyProject).",
                    },
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="get_workspace",
            description="Получить текущую рабочую директорию оркестратора. "
                        "Если не установлена, вернётся директория проекта оркестратора.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="stop_persistent_agent",
            description="Остановить сессию агента (завершить ACP-сессию и subprocess).",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "ID агента для остановки.",
                    },
                },
                "required": ["agent_id"],
            },
        ),
        Tool(
            name="list_persistent_agents",
            description="Список активных сессий агентов.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


def _result(data) -> list[TextContent]:
    """Helper to format result."""
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Invoke tool by name."""
    global _spawned_agents_count

    if name == "list_agents":
        return _result(registry.list_agents())

    elif name == "create_task":
        task = task_manager.create(
            title=arguments["title"],
            description=arguments["description"],
            assigned_to=arguments.get("assigned_to"),
            priority=arguments.get("priority", "normal"),
            dependencies=arguments.get("dependencies"),
        )
        return _result({"task_id": task.id, "title": task.title, "status": task.status})

    elif name == "list_tasks":
        tasks = task_manager.list_tasks(
            status=arguments.get("status"),
            assigned_to=arguments.get("assigned_to"),
        )
        return _result(tasks)

    elif name == "update_task":
        result = task_manager.update_status(
            arguments["task_id"],
            arguments["status"],
            arguments.get("result"),
        )
        if result is None:
            return _result({"error": f"Task {arguments['task_id']} not found"})

        return _result(result)

    elif name == "assign_task":
        result = task_manager.assign(arguments["task_id"], arguments["agent_id"])
        if result is None:
            return _result({"error": f"Task {arguments['task_id']} not found"})
        # Also update the agent's current task
        registry.update_task(arguments["agent_id"], arguments["task_id"])

        return _result(result)

    elif name == "task_summary":
        return _result(task_manager.get_summary())

    elif name == "send_message":
        msg = _send_msg(
            content=arguments["content"],
            from_agent=arguments.get("from_agent"),
            to_agent=arguments.get("to_agent"),
            task_id=arguments.get("task_id"),
        )
        return _result({"message_id": msg.id, "timestamp": msg.timestamp})

    elif name == "get_messages":
        messages = message_bus.get(
            agent_id=arguments.get("agent_id"),
            last_n=arguments.get("last_n", 500),
        )
        return _result(messages)

    elif name == "broadcast_message":
        msg = _broadcast_msg(
            content=arguments["content"],
            from_agent=arguments.get("from_agent"),
            task_id=arguments.get("task_id"),
        )
        return _result({"message_id": msg.id, "broadcast": True, "timestamp": msg.timestamp})

    elif name == "clear_tasks":
        task_manager._tasks.clear()
        task_manager._save()
        _send_msg(content="Все задачи очищены", from_agent="orchestrator")
        return _result({"ok": True, "cleared": "tasks"})

    elif name == "clear_messages":
        message_bus._messages.clear()
        message_bus._save()
        return _result({"ok": True, "cleared": "messages"})

    elif name == "clear_session":
        task_manager._tasks.clear()
        task_manager._save()
        message_bus._messages.clear()
        message_bus._save()
        # Also clear ACP agent tracking
        _acp_agents.clear()
        # And clear persistent sessions — stop all in parallel
        if _persistent_sessions:
            stop_coroutines = [sess.stop() for sess in _persistent_sessions.values() if sess.is_alive]
            if stop_coroutines:
                await asyncio.gather(*stop_coroutines, return_exceptions=True)
        _persistent_sessions.clear()
        return _result({"ok": True, "cleared": ["tasks", "messages", "agents", "persistent_sessions"]})

    elif name == "run_task":
        prompts = arguments["prompts"]
        # Use workspace cwd if not explicitly specified
        cwd = arguments.get("cwd")
        if cwd is None:
            workspace = get_workspace_cwd()
            if workspace:
                cwd = str(workspace)
            else:
                cwd = "."
        yolo = arguments.get("yolo", True)
        mcp_servers = arguments.get("mcp_servers")

        # Check if we should reuse an existing persistent agent
        existing_agent_id = arguments.get("agent_id")
        if existing_agent_id:
            session = _persistent_sessions.get(existing_agent_id)
            if not session:
                return _result({
                    "error": f"Persistent agent '{existing_agent_id}' not found. Create a new one first or use list_persistent_agents to see available agents."
                })

            # Reuse existing session — create new tasks, run in same session
            task_ids = []
            for prompt_text in prompts:
                task = task_manager.create(
                    title=prompt_text,
                    description=prompt_text,
                    assigned_to=existing_agent_id,
                    priority="normal",
                )
                task_ids.append(task.id)

            # Update agent status in registry
            registry._agents[existing_agent_id].current_task = task_ids[0]
            registry._agents[existing_agent_id].status = "running"

            agent_name = _acp_agents.get(existing_agent_id, {}).get("agent_name", "agent")

            _send_msg(
                content=f"Агент {agent_name} ({existing_agent_id}) получил задачу: {prompts[0]}",
                from_agent="orchestrator",
                to_agent=existing_agent_id,
                task_id=task_ids[0],
            )

            asyncio.create_task(_run_acp_agent_persistent(
                existing_agent_id,
                agent_name,
                prompts, task_ids, session,
            ))

            return _result({
                "mode": "acp-persistent-reuse",
                "agent_id": existing_agent_id,
                "agent_name": agent_name,
                "tasks": task_ids,
                "status": "running",
                "cwd": cwd,
            })

        # No agent_id — check rate limit and create a NEW persistent agent
        if _spawned_agents_count >= _MAX_AGENTS_PER_SESSION:
            return _result({
                "error": f"Rate limit: reached maximum {_MAX_AGENTS_PER_SESSION} agents per session"
            })

        # Pick role based on task content
        first_prompt = prompts[0]
        role = pick_role(first_prompt, first_prompt)

        # Generate agent name from role only
        agent_name = generate_agent_name(role["role"])

        # Register a "virtual" agent in registry (no subprocess)
        try:
            agent_info = registry.spawn(
                name=agent_name,
                command="qwen",  # not actually launched, ACP client handles it
                args=["--acp"],
                cwd=cwd,
                role=role,
            )
        except ValueError as e:
            return _result({"error": str(e)})
        agent_id = agent_info.id

        # Create a persistent session
        session = PersistentACPSession(agent_id=agent_id, cwd=cwd, yolo=yolo, mcp_servers=mcp_servers)
        _persistent_sessions[agent_id] = session

        # Create tasks
        task_ids = []
        for prompt_text in prompts:
            task = task_manager.create(
                title=prompt_text,
                description=prompt_text,
                assigned_to=agent_id,
                priority="normal",
            )
            task_ids.append(task.id)

        # Mark agent as running
        registry._agents[agent_id].status = "running"
        registry._agents[agent_id].current_task = task_ids[0]

        # Track as ACP agent
        _acp_agents[agent_id] = {
            "agent_name": agent_name,
            "role": role,
        }
        _spawned_agents_count += 1

        _send_msg(
            content=f"Агент запущен: {agent_name} | Задача: {first_prompt}",
            from_agent="orchestrator",
            to_agent=agent_id,
            task_id=task_ids[0],
        )

        # Launch background persistent ACP runner
        asyncio.create_task(_run_acp_agent_persistent(agent_id, agent_name, prompts, task_ids, session))

        return _result({
            "mode": "acp-persistent",
            "agent_id": agent_id,
            "agent_name": agent_name,
            "tasks": task_ids,
            "status": "running",
            "cwd": cwd,
        })

    elif name == "set_workspace":
        workspace_path = arguments["path"]
        try:
            resolved = set_workspace_cwd(workspace_path)
            _send_msg(
                content=f"Рабочая директория установлена: {resolved}",
                from_agent="orchestrator",
            )
            return _result({"ok": True, "workspace": str(resolved)})
        except ValueError as e:
            return _result({"error": str(e)})

    elif name == "get_workspace":
        workspace = get_workspace_cwd()
        if workspace:
            return _result({"workspace": str(workspace), "default": False})
        else:
            # Return the default (orchestrator project directory)
            from orchestrator.registry import _DEFAULT_CWD_ROOT
            return _result({"workspace": str(_DEFAULT_CWD_ROOT), "default": True})

    elif name == "stop_persistent_agent":
        agent_id = arguments["agent_id"]
        session = _persistent_sessions.pop(agent_id, None)
        if session:
            await session.stop()
            if agent_id in registry._agents:
                registry._agents[agent_id].status = "stopped"
            _send_msg(
                content=f"Агент {agent_id} остановлен",
                from_agent="orchestrator",
            )
            return _result({"ok": True, "agent_id": agent_id})
        return _result({"error": f"Persistent agent '{agent_id}' not found"})

    elif name == "list_persistent_agents":
        result = []
        for aid, sess in _persistent_sessions.items():
            agent_data = _acp_agents.get(aid, {})
            result.append({
                "agent_id": aid,
                "agent_name": agent_data.get("agent_name", "unknown"),
                "is_alive": sess.is_alive,
                "session_id": sess.session_id,
                "cwd": sess.cwd,
            })
        return _result({
            "persistent_agents": result,
            "total": len(result),
        })

    else:
        return _result({"error": f"Unknown tool: {name}"})


async def main():
    """Run the MCP server via stdio."""
    try:
        # Auto-set workspace to parent directory of orchestrator project
        # This allows agents to work in the directory where orchestrator was launched from
        orchestrator_project_dir = Path(__file__).resolve().parent.parent
        workspace_dir = orchestrator_project_dir.parent  # Parent of agent-orchestrator-mcp
        if workspace_dir.exists():
            set_workspace_cwd(str(workspace_dir))
            print(f"Workspace: {workspace_dir}", flush=True)

        # Start web dashboard
        dash_thread = start_dashboard_server(
            host="127.0.0.1",
            port=DASHBOARD_PORT,
            registry=registry,
            task_manager=task_manager,
            message_bus=message_bus,
        )
        print(f"Dashboard: http://127.0.0.1:{DASHBOARD_PORT}", flush=True)

        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())
    finally:
        # Cleanup on session close — stop all persistent agents in parallel
        if _persistent_sessions:
            stop_coroutines = [sess.stop() for sess in _persistent_sessions.values() if sess.is_alive]
            if stop_coroutines:
                await asyncio.gather(*stop_coroutines, return_exceptions=True)
            _persistent_sessions.clear()
        try:
            print("Session ended — clearing tasks and messages", flush=True)
        except (ValueError, OSError):
            pass  # stdout may be closed on shutdown
        task_manager._tasks.clear()
        task_manager._save()
        message_bus._messages.clear()
        message_bus._save()
        _acp_agents.clear()


if __name__ == "__main__":
    asyncio.run(main())
