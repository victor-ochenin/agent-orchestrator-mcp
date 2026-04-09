"""MCP Server for Agent Orchestration."""

import json
import asyncio
import os
import re
from pathlib import Path
from typing import Optional
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from orchestrator.registry import AgentRegistry
from orchestrator.task_manager import TaskManager
from orchestrator.message_bus import MessageBus
from orchestrator.web_server import start_dashboard_server
from orchestrator.utils import generate_slug
from orchestrator.acp_client import ACPClient

app = Server("agent-orchestrator-mcp")

# Shared state
registry = AgentRegistry()
task_manager = TaskManager()
message_bus = MessageBus()

# Track which agents are Qwen agents (for auto-close on task complete)
# Format: agent_id -> {"task_id": str, "agent_name": str, "queue": list[str]}
_qwen_agents: dict[str, dict] = {}  # agent_id -> {task_id, agent_name, queue}

DASHBOARD_PORT = int(os.environ.get("ORCHESTRATOR_DASHBOARD_PORT", 8765))


def _resolve_agent_name(agent_id: Optional[str]) -> Optional[str]:
    """Resolve agent ID to human-readable name."""
    if agent_id is None:
        return None
    # Special names
    if agent_id == "orchestrator":
        return "orchestrator"
    # Check Qwen agents first
    agent_data = _qwen_agents.get(agent_id)
    if agent_data:
        return agent_data.get("agent_name", agent_id[:8])
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


async def _run_acp_agent(agent_id: str, agent_name: str, prompts: list[str], task_ids: list[str], cwd: str):
    """Background task: run ACP client as a virtual agent, processing tasks sequentially."""
    client = ACPClient(cwd=cwd)
    try:
        # Start ACP session
        agent_info_resp = await client.start()
        session_id = await client.new_session(cwd=cwd)

        _send_msg(
            content=f"ACP-агент {agent_name} подключён, сессия {session_id[:8]}",
            from_agent=agent_id,
            to_agent="orchestrator",
            task_id=task_ids[0],
        )

        # Process each task
        for i, prompt_text in enumerate(prompts):
            task_id = task_ids[i]
            task_manager.update_status(task_id, status="running")

            _send_msg(
                content=f"Начинаю задачу {i+1}/{len(prompts)}: {prompts[i][:60]}",
                from_agent=agent_id,
                to_agent="orchestrator",
                task_id=task_id,
            )

            result = await client.run_task(prompt_text, session_id=session_id, request_id=10 + i)

            answer = result.get("answer", "")
            stop_reason = result.get("stop_reason", "")

            # Update task
            task_manager.update_status(
                task_id,
                status="completed" if stop_reason else "failed",
                result=answer[:500] if answer else f"(stop: {stop_reason})",
            )

            # Send result as the agent
            if answer:
                _send_msg(
                    content=f"Задача {i+1}/{len(prompts)} завершена\n\n{answer[:400]}",
                    from_agent=agent_id,
                    to_agent="orchestrator",
                    task_id=task_id,
                )
            else:
                _send_msg(
                    content=f"Задача {i+1}/{len(prompts)} завершена (stop: {stop_reason}, ответ пуст)",
                    from_agent=agent_id,
                    to_agent="orchestrator",
                    task_id=task_id,
                )

        # All tasks done
        registry._agents[agent_id].status = "stopped"
        _send_msg(
            content=f"ACP-агент {agent_name} завершил все {len(prompts)} задач",
            from_agent=agent_id,
        )

    except Exception as e:
        # Mark remaining tasks as failed
        for task_id in task_ids:
            t = task_manager.get(task_id)
            if t and t.get("status") == "running":
                task_manager.update_status(task_id, status="failed", result=str(e)[:500])

        registry._agents[agent_id].status = "failed"
        registry._agents[agent_id].error = str(e)
        _send_msg(
            content=f"ACP-агент {agent_name} ошибка: {e}",
            from_agent=agent_id,
            to_agent="orchestrator",
        )
    finally:
        await client.stop()
        # Remove from Qwen agents tracking
        _qwen_agents.pop(agent_id, None)


async def _monitor_qwen_agent(agent_id: str, task_id: str):
    """Background task to monitor Qwen agent and send results to message bus.
    Automatically picks up next task from queue when current one completes."""
    # Wait for agent to finish
    while True:
        await asyncio.sleep(2)
        output = registry.get_output(agent_id, last_n=200)
        agent_status = output.get("status", "unknown")
        if agent_status in ("stopped", "failed"):
            # Agent finished current task, send results
            lines = output.get("lines", [])
            result_text = "\n".join(lines).strip() if lines else "(пустой вывод)"

            # Strip markdown formatting
            result_text = re.sub(r'^#{1,6}\s*', '', result_text, flags=re.MULTILINE)
            result_text = re.sub(r'\*\*(.+?)\*\*', r'\1', result_text)
            result_text = re.sub(r'\*(.+?)\*', r'\1', result_text)
            result_text = re.sub(r'\n{3,}', '\n\n', result_text)

            # Update current task
            tasks = task_manager.list_tasks()
            for t in tasks:
                if t["id"] == task_id:
                    if t["status"] in ("pending", "running"):
                        task_manager.update_status(task_id, status="completed", result=result_text[:500])
                    break

            # Send result to message bus
            if agent_status == "failed":
                error = output.get("error", "Неизвестная ошибка")
                _send_msg(
                    content=f"Агент завершился с ошибкой\nЗадача: {task_id}\nОшибка: {error}",
                    from_agent=agent_id,
                    to_agent="orchestrator",
                    task_id=task_id,
                )
            else:
                _send_msg(
                    content=f"Агент завершил задачу\n\n{result_text}",
                    from_agent=agent_id,
                    to_agent="orchestrator",
                    task_id=task_id,
                )

            # Check if there are more tasks in queue
            agent_info = _qwen_agents.get(agent_id)
            if agent_info and agent_info.get("queue"):
                # Get next task from queue
                next_task_id = agent_info["queue"].pop(0)
                next_task = task_manager.get(next_task_id)
                
                if next_task:
                    _send_msg(
                        content=f"Агент берёт следующую задачу из очереди: {next_task.get('title')}",
                        from_agent="orchestrator",
                        to_agent=agent_id,
                        task_id=next_task_id,
                    )
                    
                    # Update agent's current task
                    _qwen_agents[agent_id]["task_id"] = next_task_id
                    task_manager.assign(next_task_id, agent_id)
                    registry.update_task(agent_id, next_task_id)
                    
                    # Restart agent with new task
                    info = registry._agents.get(agent_id)
                    if info:
                        info.status = "starting"
                        info.error = None
                        spawn_result = await registry.spawn_process(agent_id)
                        
                        # Send new task description to agent
                        if next_task.get("description"):
                            await asyncio.sleep(1)  # Wait for agent to start
                            await registry.send_input(agent_id, next_task["description"])
                        
                        # Continue monitoring for new task
                        asyncio.create_task(_monitor_qwen_agent(agent_id, next_task_id))
                        return  # Exit current monitoring cycle

            # No more tasks in queue, auto-close Qwen agent
            if agent_id in _qwen_agents:
                await registry.stop(agent_id)
                del _qwen_agents[agent_id]

            break


@app.list_tools()
async def list_tools() -> list[Tool]:
    """Return list of available tools."""
    return [
        Tool(
            name="spawn_agent",
            description="Запустить нового агента как subprocess. "
                        "Агент может быть любой CLI-инструмент или скрипт. "
                        "Возвращает agent_id для дальнейшего управления.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Имя агента (например, 'backend-worker', 'test-runner').",
                    },
                    "command": {
                        "type": "string",
                        "description": "Команда для запуска (например, 'python', 'node', 'qwen').",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Аргументы командной строки.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Рабочая директория агента. По умолчанию — текущая.",
                    },
                },
                "required": ["name", "command"],
            },
        ),
        Tool(
            name="list_agents",
            description="Список всех зарегистрированных агентов с их статусами.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="stop_agent",
            description="Остановить работающего агента.",
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
            name="get_agent_output",
            description="Получить вывод subprocess агента (stdout/stderr).",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "ID агента.",
                    },
                    "last_n": {
                        "type": "integer",
                        "description": "Количество последних строк (по умолчанию 50).",
                        "default": 50,
                    },
                },
                "required": ["agent_id"],
            },
        ),
        Tool(
            name="send_to_agent",
            description="Отправить текст на stdin работающего агента.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "ID агента.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Текст для отправки.",
                    },
                },
                "required": ["agent_id", "text"],
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
                        "description": "Количество последних сообщений (по умолчанию 50).",
                        "default": 50,
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
            name="run_qwen_task",
            description="Создать задачу и автоматически запустить Qwen-агента для её выполнения. "
                        "Агент будет закрыт автоматически после завершения задачи.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Заголовок задачи.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Описание задачи — будет отправлено Qwen как промпт.",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "description": "Приоритет задачи.",
                        "default": "normal",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Рабочая директория для Qwen-агента.",
                    },
                },
                "required": ["title", "description"],
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
            name="auto_close_agent",
            description="Автоматически закрыть Qwen-агент после завершения его задачи. "
                        "Вызывается при update_task(status='completed/failed').",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "ID агента для закрытия.",
                    },
                },
                "required": ["agent_id"],
            },
        ),
        Tool(
            name="queue_task",
            description="Добавить задачу в очередь существующего Qwen-агента для автоматического выполнения. "
                        "Агент автоматически берёт следующую задачу из очереди после завершения текущей.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "ID задачи для добавления в очередь.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "ID агента (опционально, если не указан — выбирается первый активный).",
                    },
                },
                "required": ["task_id"],
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
            name="run_qwen_acp_task",
            description="Запустить задачу через ACP (Agent Client Protocol) — чистый JSON-RPC API. "
                        "Создаёт одну сессию, отправляет промпт, ждёт ответ. "
                        "Можно указать несколько задач — они выполнятся в одной сессии. "
                        "Быстрее чем run_qwen_task, так как не перезапускает процесс.",
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
                },
                "required": ["prompts"],
            },
        ),
    ]


def _result(data) -> list[TextContent]:
    """Helper to format result."""
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Invoke tool by name."""

    if name == "spawn_agent":
        info = registry.spawn(
            name=arguments["name"],
            command=arguments["command"],
            args=arguments.get("args", []),
            cwd=arguments.get("cwd", "."),
        )
        # Start the actual subprocess
        result = await registry.spawn_process(info.id)
        result["name"] = info.name
        return _result(result)

    elif name == "list_agents":
        return _result(registry.list_agents())

    elif name == "stop_agent":
        result = await registry.stop(arguments["agent_id"])
        return _result(result)

    elif name == "get_agent_output":
        result = registry.get_output(
            arguments["agent_id"],
            last_n=arguments.get("last_n", 50),
        )
        return _result(result)

    elif name == "send_to_agent":
        result = await registry.send_input(arguments["agent_id"], arguments["text"])
        return _result(result)

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

        # Auto-close Qwen agent when task is completed/failed/cancelled
        if arguments["status"] in ("completed", "failed", "cancelled"):
            for agent_id, task_id in list(_qwen_agents.items()):
                if task_id == arguments["task_id"]:
                    await registry.stop(agent_id)
                    del _qwen_agents[agent_id]
                    _send_msg(
                        content=f"Qwen-агент {agent_id} закрыт — задача {arguments['task_id']} завершена ({arguments['status']})",
                        from_agent="orchestrator",
                    )
                    break

        return _result(result)

    elif name == "assign_task":
        result = task_manager.assign(arguments["task_id"], arguments["agent_id"])
        if result is None:
            return _result({"error": f"Task {arguments['task_id']} not found"})
        # Also update the agent's current task
        registry.update_task(arguments["agent_id"], arguments["task_id"])

        # Auto-launch Qwen agent if this is a Qwen agent
        if arguments["agent_id"] in _qwen_agents:
            task = task_manager.get(arguments["task_id"])
            if task and task.get("description"):
                await registry.send_input(arguments["agent_id"], task["description"])
                _send_msg(
                    content=f"Qwen-агенту отправлена задача: {task.get('title')}",
                    from_agent="orchestrator",
                    to_agent=arguments["agent_id"],
                    task_id=arguments["task_id"],
                )
                return _result({**result, "qwen_prompt_sent": True})

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
            last_n=arguments.get("last_n", 50),
        )
        return _result(messages)

    elif name == "broadcast_message":
        msg = _broadcast_msg(
            content=arguments["content"],
            from_agent=arguments.get("from_agent"),
            task_id=arguments.get("task_id"),
        )
        return _result({"message_id": msg.id, "broadcast": True, "timestamp": msg.timestamp})

    elif name == "run_qwen_task":
        # Step 1: Create task
        task = task_manager.create(
            title=arguments["title"],
            description=arguments["description"],
            priority=arguments.get("priority", "normal"),
        )
        # Step 2: Reuse existing Qwen agent or create new one
        cwd = arguments.get("cwd", ".")
        prompt = arguments["description"]

        # Generate agent name from task title (Вариант 3.1)
        slug = generate_slug(task.title)
        agent_name = f"qwen-{slug}"

        # Try to find a stopped/failed agent to reuse
        existing_agent_id = None
        for agent_id, agent_data in list(_qwen_agents.items()):
            info = registry._agents.get(agent_id)
            if info and info.status in ("stopped", "failed"):
                existing_agent_id = agent_id
                break

        if existing_agent_id:
            # Restart the existing agent
            info = registry._agents[existing_agent_id]
            info.status = "starting"
            info.error = None
            info.current_task = task.id
            spawn_result = await registry.spawn_process(existing_agent_id)
            agent_id = existing_agent_id
            # Add to queue instead of replacing current task
            _qwen_agents[agent_id]["queue"].append(task.id)
            _send_msg(
                content=f"Задача '{task.title}' добавлена в очередь агента {agent_id}",
                from_agent="orchestrator",
                to_agent=agent_id,
                task_id=task.id,
            )
        else:
            # Spawn a new Qwen agent
            info = registry.spawn(
                name=agent_name,
                command="qwen",
                args=["-p", prompt, "-o", "text", "--max-session-turns", "10"],
                cwd=cwd,
            )
            spawn_result = await registry.spawn_process(info.id)
            agent_id = info.id

            # Track as Qwen agent with queue support
            _qwen_agents[agent_id] = {
                "task_id": task.id,
                "agent_name": agent_name,
                "queue": []  # Queue of additional tasks
            }

        spawn_result["name"] = info.name

        # Assign task to agent
        task_manager.assign(task.id, agent_id)
        registry.update_task(agent_id, task.id)

        _send_msg(
            content=f"Агент запущен: {agent_name}\nЗадача: {task.title}",
            from_agent="orchestrator",
            to_agent=agent_id,
            task_id=task.id,
        )

        # Start background task to monitor agent and send results
        asyncio.create_task(_monitor_qwen_agent(agent_id, task.id))

        return _result({
            "task_id": task.id,
            "agent_id": agent_id,
            "agent_pid": spawn_result.get("pid"),
            "agent_name": agent_name,
            "status": task.status,
            "prompt_sent": True,
        })

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
        # Also clear agent tracking
        _qwen_agents.clear()
        return _result({"ok": True, "cleared": ["tasks", "messages", "agents"]})

    elif name == "auto_close_agent":
        if arguments["agent_id"] in _qwen_agents:
            result = await registry.stop(arguments["agent_id"])
            del _qwen_agents[arguments["agent_id"]]
            return _result({**result, "qwen_tracking_removed": True})
        else:
            result = await registry.stop(arguments["agent_id"])
            return _result(result)

    elif name == "queue_task":
        """Add a task to an existing Qwen agent's queue for automatic execution."""
        task_id = arguments["task_id"]
        agent_id = arguments.get("agent_id")
        
        # If agent_id not specified, find a running Qwen agent
        if not agent_id:
            for aid, agent_data in _qwen_agents.items():
                info = registry._agents.get(aid)
                if info and info.status == "running":
                    agent_id = aid
                    break
        
        if not agent_id or agent_id not in _qwen_agents:
            return _result({"error": "No active Qwen agent found. Use run_qwen_task first."})
        
        # Get task
        task = task_manager.get(task_id)
        if not task:
            return _result({"error": f"Task {task_id} not found"})
        
        # Add to queue
        _qwen_agents[agent_id]["queue"].append(task_id)
        task_manager.assign(task_id, agent_id)
        
        _send_msg(
            content=f"Задача '{task['title']}' добавлена в очередь агента {_qwen_agents[agent_id]['agent_name']}",
            from_agent="orchestrator",
            to_agent=agent_id,
            task_id=task_id,
        )
        
        return _result({
            "task_id": task_id,
            "agent_id": agent_id,
            "agent_name": _qwen_agents[agent_id]["agent_name"],
            "queue_position": len(_qwen_agents[agent_id]["queue"]),
            "status": "queued"
        })

    elif name == "run_qwen_acp_task":
        prompts = arguments["prompts"]
        cwd = arguments.get("cwd", ".")

        # Generate agent name from first prompt (Вариант 3.1)
        slug = generate_slug(prompts[0])
        agent_name = f"qwen-acp-{slug}"

        # Register a "virtual" agent in registry (no subprocess)
        agent_info = registry.spawn(
            name=agent_name,
            command="qwen",  # not actually launched, ACP client handles it
            args=["--acp"],
            cwd=cwd,
        )
        agent_id = agent_info.id

        # Create tasks
        task_ids = []
        for prompt_text in prompts:
            task = task_manager.create(
                title=prompt_text[:50] + ("..." if len(prompt_text) > 50 else ""),
                description=prompt_text,
                assigned_to=agent_id,
                priority="normal",
            )
            task_ids.append(task.id)

        # Mark agent as running (even though it's virtual)
        registry._agents[agent_id].status = "running"

        # Track as ACP agent
        _qwen_agents[agent_id] = {
            "task_id": task_ids[0],
            "agent_name": agent_name,
            "queue": task_ids[1:],  # remaining tasks in queue
        }

        _send_msg(
            content=f"ACP-агент запущен: {agent_name}\nЗадач: {len(prompts)}",
            from_agent="orchestrator",
            to_agent=agent_id,
            task_id=task_ids[0],
        )

        # Launch background ACP runner
        asyncio.create_task(_run_acp_agent(agent_id, agent_name, prompts, task_ids, cwd))

        return _result({
            "mode": "acp",
            "agent_id": agent_id,
            "agent_name": agent_name,
            "tasks": task_ids,
            "status": "running",
        })

    else:
        return _result({"error": f"Unknown tool: {name}"})


async def main():
    """Run the MCP server via stdio."""
    try:
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
        # Cleanup on session close
        print("Session ended — clearing tasks and messages", flush=True)
        task_manager._tasks.clear()
        task_manager._save()
        message_bus._messages.clear()
        message_bus._save()
        _qwen_agents.clear()


if __name__ == "__main__":
    asyncio.run(main())
