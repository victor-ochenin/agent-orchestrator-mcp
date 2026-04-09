"""MCP Server for Agent Orchestration."""

import json
import asyncio
import os
from pathlib import Path
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from orchestrator.registry import AgentRegistry
from orchestrator.task_manager import TaskManager
from orchestrator.message_bus import MessageBus
from orchestrator.web_server import start_dashboard_server

app = Server("agent-orchestrator-mcp")

# Shared state
registry = AgentRegistry()
task_manager = TaskManager()
message_bus = MessageBus()

# Track which agents are Qwen agents (for auto-close on task complete)
_qwen_agents: dict[str, str] = {}  # agent_id -> task_id

DASHBOARD_PORT = int(os.environ.get("ORCHESTRATOR_DASHBOARD_PORT", 8765))


async def _monitor_qwen_agent(agent_id: str, task_id: str):
    """Background task to monitor Qwen agent and send results to message bus."""
    # Wait for agent to finish
    while True:
        await asyncio.sleep(2)
        output = registry.get_output(agent_id, last_n=200)
        agent_status = output.get("status", "unknown")
        if agent_status in ("stopped", "failed"):
            # Agent finished, send results
            lines = output.get("lines", [])
            result_text = "\n".join(lines).strip() if lines else "(пустой вывод)"
            
            # Update task
            tasks = task_manager.list_tasks()
            for t in tasks:
                if t["id"] == task_id:
                    if t["status"] in ("pending", "running"):
                        task_manager.update_status(task_id, status="completed", result=result_text[:500])
                    break
            
            # Send result to message bus
            if agent_status == "failed":
                error = output.get("error", "Неизвестная ошибка")
                message_bus.send(
                    content=f"❌ Агент завершился с ошибкой\nЗадача: {task_id}\nОшибка: {error}",
                    from_agent=agent_id,
                    to_agent="orchestrator",
                    task_id=task_id,
                )
            else:
                message_bus.send(
                    content=f"✅ Агент завершил задачу\n\n{result_text}",
                    from_agent=agent_id,
                    to_agent="orchestrator",
                    task_id=task_id,
                )
            
            # Auto-close Qwen agent
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
                    message_bus.send(
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
                message_bus.send(
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
        msg = message_bus.send(
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
        msg = message_bus.broadcast(
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
        # Step 2: Spawn Qwen agent in non-interactive mode
        cwd = arguments.get("cwd", ".")
        prompt = arguments["description"]

        # Use -p for non-interactive one-shot mode and -o text for parseable output
        info = registry.spawn(
            name=f"qwen-{task.id}",
            command="qwen",
            args=["-p", prompt, "-o", "text", "--max-session-turns", "10"],
            cwd=cwd,
        )
        spawn_result = await registry.spawn_process(info.id)
        spawn_result["name"] = info.name

        # Step 3: Track as Qwen agent
        _qwen_agents[info.id] = task.id

        # Step 4: Assign task to agent
        task_manager.assign(task.id, info.id)
        registry.update_task(info.id, task.id)

        message_bus.send(
            content=f"🚀 Агент запущен: {info.name}\nЗадача: {task.title}",
            from_agent="orchestrator",
            to_agent=info.id,
            task_id=task.id,
        )

        # Step 5: Start background task to monitor agent and send results
        asyncio.create_task(_monitor_qwen_agent(info.id, task.id))

        return _result({
            "task_id": task.id,
            "agent_id": info.id,
            "agent_pid": spawn_result.get("pid"),
            "status": task.status,
            "prompt_sent": True,
        })

    elif name == "clear_tasks":
        task_manager._tasks.clear()
        task_manager._save()
        message_bus.send(content="Все задачи очищены", from_agent="orchestrator")
        return _result({"ok": True, "cleared": "tasks"})

    elif name == "clear_messages":
        message_bus._messages.clear()
        message_bus._save()
        return _result({"ok": True, "cleared": "messages"})

    elif name == "auto_close_agent":
        if arguments["agent_id"] in _qwen_agents:
            result = await registry.stop(arguments["agent_id"])
            del _qwen_agents[arguments["agent_id"]]
            return _result({**result, "qwen_tracking_removed": True})
        else:
            result = await registry.stop(arguments["agent_id"])
            return _result(result)

    else:
        return _result({"error": f"Unknown tool: {name}"})


async def main():
    """Run the MCP server via stdio."""
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


if __name__ == "__main__":
    asyncio.run(main())
