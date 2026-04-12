# Agent Orchestrator MCP Server

MCP-сервер для локальной оркестрации мультиагентных систем. Агенты запускаются через ACP (Agent Client Protocol) — чистый JSON-RPC API над stdin/stdout.

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

**Agent Orchestrator** решает это, позволяя агенту-оркестратору (LLM через MCP-клиент) создавать, управлять и координировать несколько виртуальных агентов через ACP:

- Каждый агент — независимая ACP-сессия с собственным контекстом
- Задачи сохраняются в JSON между сессиями
- Шина сообщений для меж-агентной коммуникации
- **Никаких внешних API-ключей** — всё работает локально через Qwen Code

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
│   ACP clients     tasks.json   messages.json  │
│   (virtual agents)                            │
└─────────────────────────────────────────────┘
         │
    ┌────┼────────────┐
    ▼    ▼            ▼
 Agent1 Agent2     Agent3
  (ACP)   (ACP)     (ACP)
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

#### Основной инструмент запуска задач

| Инструмент | Описание |
|---|---|
| `run_task` | Запустить одну или несколько задач через ACP в одной сессии. Поддерживает переиспользование агента и подключение внешних MCP-серверов |

#### Управление агентами

| Инструмент | Описание |
|---|---|
| `list_agents` | Список всех агентов с статусами |
| `stop_agent` | Остановить агента |
| `get_agent_output` | Получить вывод агента (stdout/stderr) |
| `send_to_agent` | Отправить текст на stdin агента |
| `list_persistent_agents` | Список активных сессий агентов |
| `stop_persistent_agent` | Остановить сессию агента (завершить ACP-сессию и subprocess) |

#### Управление задачами

| Инструмент | Описание |
|---|---|
| `create_task` | Создать задачу с приоритетом и зависимостями |
| `list_tasks` | Список задач с фильтрацией |
| `update_task` | Обновить статус/результат задачи |
| `assign_task` | Назначить задачу агенту |
| `task_summary` | Статистика по задачам |
| `clear_tasks` | Очистить все задачи |

#### Коммуникация

| Инструмент | Описание |
|---|---|
| `send_message` | Отправить сообщение конкретному агенту |
| `get_messages` | Прочитать сообщения из шины (по умолчанию до 500 последних) |
| `broadcast_message` | Отправить сообщение всем агентам |
| `clear_messages` | Очистить все сообщения |

#### Сессия и рабочее пространство

| Инструмент | Описание |
|---|---|
| `clear_session` | Очистить все сообщения и задачи |
| `set_workspace` | Установить рабочую директорию |
| `get_workspace` | Получить текущую рабочую директорию |

### Web Dashboard

Оркестратор запускает встроенный веб-дашборд для мониторинга в реальном времени.

- **URL:** `http://127.0.0.1:8765`
- **Порт** настраивается через переменную окружения `ORCHESTRATOR_DASHBOARD_PORT`

**Возможности дашборда:**
- Таблица агентов с ролями, статусами и действиями (Stop, Delete, View Messages)
- Таблица задач с приоритетами и статусами
- Лента сообщений оркестратора с поддержкой Markdown
- Все сообщения шины с фильтрацией по агенту
- Модальное окно для просмотра сообщений конкретного агента
- Автоматическое обновление каждые 3 секунды
- Тёмная тема

> **Сообщения отображаются полностью** — без сокращений и обрезок. Длинные тексты автоматически переносятся на новую строку.

#### Примеры использования

```
# Запустить задачу через ACP (создаётся новый агент)
run_task(prompts=["Реализовать CRUD API для пользователей"])

# Запустить несколько задач в одной сессии
run_task(prompts=[
  "Создать модель User с полями id, name, email",
  "Написать тесты для модели User",
  "Создать endpoint GET /users"
])

# Запустить задачу в конкретной директории
run_task(prompts=["Проанализировать код"], cwd="C:\\project")

# Переиспользовать существующего агента (сохраняет контекст сессии)
# Сначала создаём агента и запоминаем agent_id из ответа
run_task(prompts=["Создать файлы проекта"])
# → ответ: {"agent_id": "abc12345", ...}

# Затем используем того же агента для следующей задачи
run_task(agent_id="abc12345", prompts=["Добавить документацию"])

# Подключить внешние MCP-серверы к агенту
run_task(
  prompts=["Найди все конфиги в проекте"],
  mcp_servers=[{
    "name": "config-finder",
    "command": "python",
    "args": ["-m", "config_finder_mcp.server"],
    "cwd": "C:\\project\\config-finder-mcp",
    "type": "stdio",
    "env": [],
    "headers": []
  }]
)

# Посмотреть всех агентов
list_agents()

# Посмотреть активные сессии
list_persistent_agents()

# Остановить сессию агента
stop_persistent_agent(agent_id="abc12345")

# Создать задачу вручную
create_task(title="Реализовать API", description="Создать CRUD endpoints", priority="high")

# Назначить задачу агенту
assign_task(task_id="abc12345", agent_id="def67890")

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

**Agent Orchestrator** solves this by allowing an orchestrator agent (LLM via MCP client) to create, manage, and coordinate multiple virtual agents via ACP:

- Each agent is an independent ACP session with its own context
- Tasks persist in JSON between sessions
- Message bus for inter-agent communication
- **No external API keys** — everything runs locally via Qwen Code

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
│   ACP clients     tasks.json   messages.json  │
│   (virtual agents)                            │
└─────────────────────────────────────────────┘
         │
    ┌────┼────────────┐
    ▼    ▼            ▼
 Agent1 Agent2     Agent3
  (ACP)   (ACP)     (ACP)
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

#### Main task execution tool

| Tool | Description |
|---|---|
| `run_task` | Run one or more tasks via ACP in a single session. Supports agent reuse and external MCP servers |

#### Agent Management

| Tool | Description |
|---|---|
| `list_agents` | List all agents with statuses |
| `stop_agent` | Stop an agent |
| `get_agent_output` | Get agent output (stdout/stderr) |
| `send_to_agent` | Send text to agent's stdin |
| `list_persistent_agents` | List active agent sessions |
| `stop_persistent_agent` | Stop an agent session (terminate ACP session and subprocess) |

#### Task Management

| Tool | Description |
|---|---|
| `create_task` | Create a task with priority and dependencies |
| `list_tasks` | List tasks with filtering |
| `update_task` | Update task status/result |
| `assign_task` | Assign a task to an agent |
| `task_summary` | Task statistics |
| `clear_tasks` | Clear all tasks |

#### Communication

| Tool | Description |
|---|---|
| `send_message` | Send a message to a specific agent |
| `get_messages` | Read messages from the bus (up to 500 last by default) |
| `broadcast_message` | Broadcast a message to all agents |
| `clear_messages` | Clear all messages |

#### Session & Workspace

| Tool | Description |
|---|---|
| `clear_session` | Clear all messages and tasks |
| `set_workspace` | Set working directory |
| `get_workspace` | Get current working directory |

### Web Dashboard

The orchestrator launches a built-in web dashboard for real-time monitoring.

- **URL:** `http://127.0.0.1:8765`
- **Port** configurable via `ORCHESTRATOR_DASHBOARD_PORT` environment variable

**Dashboard features:**
- Agents table with roles, statuses and actions (Stop, Delete, View Messages)
- Tasks table with priorities and statuses
- Orchestrator message feed with Markdown support
- All bus messages with agent filtering
- Modal for viewing specific agent messages
- Auto-refresh every 3 seconds
- Dark theme

> **Messages are displayed in full** — no truncation. Long text automatically wraps.

#### Usage examples

```
# Run a task via ACP (creates a new agent)
run_task(prompts=["Implement CRUD API for users"])

# Run multiple tasks in one session
run_task(prompts=[
  "Create User model with id, name, email fields",
  "Write tests for User model",
  "Create GET /users endpoint"
])

# Run a task in a specific directory
run_task(prompts=["Analyze code"], cwd="/home/user/project")

# Reuse an existing agent (preserves session context)
# First, create an agent and note the agent_id from the response
run_task(prompts=["Create project files"])
# → response: {"agent_id": "abc12345", ...}

# Then reuse the same agent for the next task
run_task(agent_id="abc12345", prompts=["Add documentation"])

# Connect external MCP servers to the agent
run_task(
  prompts=["Find all config files in the project"],
  mcp_servers=[{
    "name": "config-finder",
    "command": "python",
    "args": ["-m", "config_finder_mcp.server"],
    "cwd": "/home/project/config-finder-mcp",
    "type": "stdio",
    "env": [],
    "headers": []
  }]
)

# List all agents
list_agents()

# List active persistent sessions
list_persistent_agents()

# Stop an agent session
stop_persistent_agent(agent_id="abc12345")

# Create a task manually
create_task(title="Implement API", description="Create CRUD endpoints", priority="high")

# Assign task to agent
assign_task(task_id="abc12345", agent_id="def67890")

# Broadcast to all
broadcast_message(content="Starting sprint #5")
```
