# Отчёт: ACP Protocol для Agent Orchestrator

## Что такое ACP?

**ACP (Agent Client Protocol)** — это JSON-RPC 2.0 поверх stdio протокол, встроенный в Qwen Code (`qwen --acp`).

Qwen запускается как subprocess, а оркестратор обменивается с ним JSON-сообщениями:
- `initialize` → установка соединения
- `session/new` → создание сессии
- `session/prompt` → отправка задачи
- `session/update` → streaming ответов (чанки мыслей и текста)
- `session/request_permission` → запрос разрешения на инструменты (авто-ответ: `allowAlways`)

## Зачем это нам?

| Подход | Как сейчас (`-p`) | ACP Protocol |
|--------|-------------------|--------------|
| Запуск | Новый subprocess на каждую задачу | Один процесс → много задач |
| Контекст | Теряется между задачами | Сохраняется в сессии |
| Ответы | Парсинг stdout/stderr | Чистый JSON-RPC |
| Статус | Guess по exit code | Явный `stopReason` |
| Очередь | Restart агента | Следующий `session/prompt` |

## Время выполнения (измерено в тестах)

| Операция | Время |
|----------|-------|
| Запуск `qwen --acp -o json` | ~1 сек |
| `initialize` | ~1 сек |
| `session/new` | ~1 сек |
| Простая задача (математика) | **3–6 сек** |
| Переключение между задачами | **~0.5 сек** (просто следующий `session/prompt`) |

**Итого:** 2 задачи подряд = **~11 сек** (вместо ~14 сек с перезапуском).

Для сложных задач (анализ кода, документация) — время определяется LLM, а не протоколом. Overhead ACP — менее 1 секунды.

## Как работает (рабочий поток)

```python
# 1. Запуск
proc = subprocess("qwen --acp -o json")

# 2. Инициализация
write({"method": "initialize", "params": {"protocolVersion": 1, ...}})
read()  # ← agentInfo, capabilities

write({"method": "notifications/initialized"})

# 3. Сессия
write({"method": "session/new", "params": {"cwd": ".", "mcpServers": []}})
sid = read()["result"]["sessionId"]

# 4. Задачи (цикл)
for task in tasks:
    write({"method": "session/prompt", "params": {"sessionId": sid, "prompt": [...]}})
    
    while True:
        msg = read(timeout=5.0)
        if msg["result"]["stopReason"]: break  # DONE
        if "session/update" in msg["method"]:
            text = msg["params"]["update"]["content"]["text"]
            # собираем ответ
        if "request_permission" in msg["method"]:
            write({"id": msg["id"], "result": {"outcome": "allowAlways"}})
```

## Интеграция в agent-orchestrator-mcp

### Что нужно изменить

**Файл: `orchestrator/server.py`**

1. Заменить `_create_qwen_task` → запускать `qwen --acp -o json` вместо `qwen -p ...`
2. Добавить класс `ACPClient` для обёртки JSON-RPC коммуникации
3. `run_qwen_task` → `initialize` → `session/new` → `session/prompt` → чтение до `stopReason`
4. Очередь задач → просто цикл `session/prompt` в той же сессии

### Минимальный код (ACP клиент)

```python
class ACPClient:
    def __init__(self, process):
        self.proc = process
    
    async def init(self):
        await self._req("initialize", {"protocolVersion": 1, "capabilities": {}, 
            "clientInfo": {"name": "orchestrator", "version": "0.1"}})
        self._write({"method": "notifications/initialized"})
    
    async def new_session(self):
        r = await self._req("session/new", {"cwd": ".", "mcpServers": []})
        return r["result"]["sessionId"]
    
    async def run_task(self, session_id, prompt_text):
        await self._req("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": prompt_text}]
        })
        
        answer = ""
        while True:
            msg = await self._read(timeout=5.0)
            if not msg: break
            if msg.get("result", {}).get("stopReason"): break
            if "session/update" in msg.get("method", ""):
                u = msg["params"]["update"]
                if u.get("sessionUpdate") in ("agent_thought_chunk", "agent_message_chunk"):
                    answer += u.get("content", {}).get("text", "")
            if "request_permission" in msg.get("method", ""):
                self._write({"id": msg["id"], "result": {"outcome": "allowAlways"}})
        
        return answer
```

### Оценка усилий

| Что | Сложность |
|-----|-----------|
| ACPClient класс | ~50 строк, готово из теста |
| Замена run_qwen_task | ~30 строк изменений |
| Очередь задач (цикл session/prompt) | ~20 строк |
| Тесты | уже есть `test_acp_fast.py` |
| **Итого** | **~100 строк нового кода, 2-3 часа** |

**Риск:** минимальный. Текущий код (`-p` режим) остаётся как fallback. ACP — просто альтернативный способ запуска.

## Вывод

✅ **ACP работает стабильно** — протестировано с 2 задачами в одной сессии
✅ **Быстро** — overhead < 1 секунды, переключение между задачами мгновенное
✅ **Мало кода** — ~100 строк для интеграции
✅ **Не ломает текущее** — `-p` режим остаётся как fallback
✅ **Очередь задач** — естественная: цикл `session/prompt` в одной сессии
