# Agent Orchestrator MCP Server

MCP-сервер для локальной оркестрации мультиагентных систем. Не требует API-ключей — агенты запускаются как локальные subprocess-ы.

---

<!-- ==================== РУССКАЯ ВЕРСИЯ ==================== -->

## Оглавление

- [🇷🇺 Русская версия](#русская-версия)
  - [Зачем](#зачем)
  - [Архитектура](#архитектура)
  - [Установка](#установка)
  - [Подключение к MCP-клиенту](#подключение-к-mcp-клиенту)
  - [Доступные инструменты](#доступные-инструменты)
- [🇬🇧 English version](#english-version)
  - [Why](#why)
  - [Architecture](#architecture)
  - [Installation](#installation-1)
  - [Connecting to an MCP client](#connecting-to-an-mcp-client)
  - [Available tools](#available-tools)

---

## Русская версия

### Зачем

Когда LLM-агент работает над сложной задачей, он сталкивается с ограничениями:
- Один контекст — сложно держать несколько направлений работы
- Нет изоляции — сбой в одной части влияет на всё
- Нет параллелизма — задачи выполняются последовательно
- Теряется контекст между сессиями

**Agent Orchestrator** решает это, позволяя агенту-оркестратору (LLM через MCP-клиент) создавать, управлять и координировать несколько subprocess-агентов:

- Каждый агент — независимый процесс с собственным stdin/stdout
- Задачи сохраняются в JSON между сессиями
- Шина сообщений для меж-агентной коммуникации
- **Никаких внешних API-ключей** — всё работает локально

### Архитектура

```
┌─────────────────────────────────────────────┐
│           MCP-клиент (Qwen Code)             │
│         LLM-агент (оркестратор)              │
└──────────────┬──────────────────────────────┘
               │ stdio (MCP protocol)
               ▼
┌─────────────────────────────────────────────┐
│        Agent Orchestrator MCP Server         │
│  ┌─────────────┐  ┌──────────┐  ┌────────┐  │
│  │ Agent       │  │ Task     │  │Message │  │
│  │ Registry    │  │ Manager  │  │ Bus    │  │
│  └──────┬──────┘  └────┬─────┘  └───┬────┘  │
│         │              │            │        │
│         ▼              ▼            ▼        │
│   subprocesses    tasks.json   messages.json  │
└─────────────────────────────────────────────┘
         │
    ┌────┼────────────┐
    ▼    ▼            ▼
 Agent1 Agent2     Agent3
 (python) (node)   (bash script)
```

### Установка

#### Шаг 1: Клонируйте репозиторий

```bash
git clone https://github.com/victor-ochenin/agent-orchestrator-mcp.git
cd agent-orchestrator-mcp
```

#### Шаг 2: Установите зависимости

```bash
pip install -e .
```

### Подключение к MCP-клиенту

Добавьте сервер в конфигурацию вашего MCP-клиента (например, `settings.json` Qwen Code):

**С `python`:**
```json
{
  "mcpServers": {
    "agent-orchestrator": {
      "command": "python",
      "args": ["-m", "orchestrator.server"],
      "cwd": "C:\\Users\\user\\Desktop\\LocalMcp\\agent-orchestrator-mcp"
    }
  }
}
```

**С `uv`:**
```json
{
  "mcpServers": {
    "agent-orchestrator": {
      "command": "uv",
      "args": ["run", "-m", "orchestrator.server"],
      "cwd": "C:\\Users\\user\\Desktop\\LocalMcp\\agent-orchestrator-mcp"
    }
  }
}
```

> **Примечание:** замените `cwd` на абсолютный путь к директории проекта.

### Доступные инструменты

#### Управление агентами

| Инструмент | Описание |
|---|---|
| `spawn_agent` | Запустить нового агента как subprocess |
| `list_agents` | Список всех агентов с статусами |
| `stop_agent` | Остановить агента |
| `get_agent_output` | Получить вывод агента (stdout/stderr) |
| `send_to_agent` | Отправить текст на stdin агента |

#### Управление задачами

| Инструмент | Описание |
|---|---|
| `create_task` | Создать задачу с приоритетом и зависимостями |
| `list_tasks` | Список задач с фильтрацией |
| `update_task` | Обновить статус/результат задачи |
| `assign_task` | Назначить задачу агенту |
| `task_summary` | Статистика по задачам |

#### Коммуникация

| Инструмент | Описание |
|---|---|
| `send_message` | Отправить сообщение конкретному агенту |
| `get_messages` | Прочитать сообщения из шины |
| `broadcast_message` | Отправить сообщение всем агентам |

#### Примеры использования

```
# Запустить Python-агент
spawn_agent(name="worker-1", command="python", args=["-i"], cwd="C:\\project")

# Запустить Node.js-агент
spawn_agent(name="frontend", command="node", args=["-i"], cwd="C:\\project\\frontend")

# Посмотреть всех агентов
list_agents()

# Создать задачу
create_task(title="Реализовать API", description="Создать CRUD endpoints", priority="high")

# Назначить задачу агенту
assign_task(task_id="abc12345", agent_id="def67890")

# Отправить команду агенту
send_to_agent(agent_id="def67890", text="print('hello')")

# Получить вывод
get_agent_output(agent_id="def67890", last_n=20)

# Broadcast всем
broadcast_message(content="Начинаем работу над спринтом #5")
```

---
---

## English version

### Why

When an LLM agent tackles complex tasks, it faces limitations:
- Single context — hard to manage multiple work streams
- No isolation — a failure in one part affects everything
- No parallelism — tasks run sequentially
- Context is lost between sessions

**Agent Orchestrator** solves this by allowing an orchestrator agent (LLM via MCP client) to create, manage, and coordinate multiple subprocess agents:

- Each agent is an independent process with its own stdin/stdout
- Tasks persist in JSON between sessions
- Message bus for inter-agent communication
- **No external API keys** — everything runs locally

### Architecture

```
┌─────────────────────────────────────────────┐
│           MCP Client (Qwen Code)             │
│         LLM Agent (orchestrator)              │
└──────────────┬──────────────────────────────┘
               │ stdio (MCP protocol)
               ▼
┌─────────────────────────────────────────────┐
│        Agent Orchestrator MCP Server         │
│  ┌─────────────┐  ┌──────────┐  ┌────────┐  │
│  │ Agent       │  │ Task     │  │Message │  │
│  │ Registry    │  │ Manager  │  │ Bus    │  │
│  └──────┬──────┘  └────┬─────┘  └───┬────┘  │
│         │              │            │        │
│         ▼              ▼            ▼        │
│   subprocesses    tasks.json   messages.json  │
└─────────────────────────────────────────────┘
         │
    ┌────┼────────────┐
    ▼    ▼            ▼
 Agent1 Agent2     Agent3
 (python) (node)   (bash script)
```

### Installation

#### Step 1: Clone the repository

```bash
git clone https://github.com/victor-ochenin/agent-orchestrator-mcp.git
cd agent-orchestrator-mcp
```

#### Step 2: Install dependencies

```bash
pip install -e .
```

### Connecting to an MCP client

Add the server to your MCP client's configuration (e.g. Qwen Code's `settings.json`):

**With `python`:**
```json
{
  "mcpServers": {
    "agent-orchestrator": {
      "command": "python",
      "args": ["-m", "orchestrator.server"],
      "cwd": "/absolute/path/to/agent-orchestrator-mcp"
    }
  }
}
```

**With `uv`:**
```json
{
  "mcpServers": {
    "agent-orchestrator": {
      "command": "uv",
      "args": ["run", "-m", "orchestrator.server"],
      "cwd": "/absolute/path/to/agent-orchestrator-mcp"
    }
  }
}
```

> **Note:** replace `cwd` with the absolute path to the project directory.

### Available tools

#### Agent Management

| Tool | Description |
|---|---|
| `spawn_agent` | Launch a new agent as a subprocess |
| `list_agents` | List all agents with statuses |
| `stop_agent` | Stop an agent |
| `get_agent_output` | Get agent output (stdout/stderr) |
| `send_to_agent` | Send text to agent's stdin |

#### Task Management

| Tool | Description |
|---|---|
| `create_task` | Create a task with priority and dependencies |
| `list_tasks` | List tasks with filtering |
| `update_task` | Update task status/result |
| `assign_task` | Assign a task to an agent |
| `task_summary` | Task statistics |

#### Communication

| Tool | Description |
|---|---|
| `send_message` | Send a message to a specific agent |
| `get_messages` | Read messages from the bus |
| `broadcast_message` | Broadcast a message to all agents |

#### Usage examples

```
# Spawn a Python agent
spawn_agent(name="worker-1", command="python", args=["-i"], cwd="/home/user/project")

# Spawn a Node.js agent
spawn_agent(name="frontend", command="node", args=["-i"], cwd="/home/user/project/frontend")

# List all agents
list_agents()

# Create a task
create_task(title="Implement API", description="Create CRUD endpoints", priority="high")

# Assign task to agent
assign_task(task_id="abc12345", agent_id="def67890")

# Send command to agent
send_to_agent(agent_id="def67890", text="print('hello')")

# Get output
get_agent_output(agent_id="def67890", last_n=20)

# Broadcast to all
broadcast_message(content="Starting sprint #5")
```
