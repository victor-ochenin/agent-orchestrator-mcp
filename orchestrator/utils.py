"""Utility functions for agent naming."""

# ─── Agent Roles ───────────────────────────────────────────────
AGENT_ROLES = [
    {"role": "coder", "description": "Написание и изменение кода"},
    {"role": "reviewer", "description": "Ревью кода и анализ"},
    {"role": "tester", "description": "Тестирование и валидация"},
    {"role": "researcher", "description": "Исследование и поиск информации"},
    {"role": "architect", "description": "Проектирование и планирование"},
    {"role": "debugger", "description": "Отладка и исправление ошибок"},
    {"role": "writer", "description": "Документация и тексты"},
    {"role": "optimizer", "description": "Оптимизация и улучшение"},
]

_ROLE_INDEX = 0  # Round-robin counter
_ROLE_COUNT: dict[str, int] = {}  # role_name -> count for unique naming


def pick_role(title: str, description: str) -> dict:
    """Pick an agent role based on task title/description keywords."""
    text = (title + " " + description).lower()

    keyword_map = {
        "reviewer": ["ревью", "review", "провер", "анализ", "quality", "качеств", "lint"],
        "tester": ["тест", "test", "проверк", "validate", "validate", "e2e", "unit"],
        "debugger": ["баг", "bug", "ошибк", "debug", "fix", "почин", "исправ", "проблем"],
        "researcher": ["исслед", "research", "най", "поиск", "узна", "find", "explor"],
        "architect": ["проект", "architect", "структур", "design", "план", "созда", "нов"],
        "writer": ["документ", "doc", "readme", "описан", "текст", "write", "стать"],
        "optimizer": ["оптимиз", "optimiz", "улучш", "ускор", "refactor", "рефактор"],
    }

    for role_slug, keywords in keyword_map.items():
        for kw in keywords:
            if kw in text:
                for r in AGENT_ROLES:
                    if r["role"] == role_slug:
                        return r

    # Default: round-robin
    global _ROLE_INDEX
    role = AGENT_ROLES[_ROLE_INDEX % len(AGENT_ROLES)]
    _ROLE_INDEX += 1
    return role


def generate_agent_name(role_name: str) -> str:
    """Generate a unique agent name from role only.

    Examples: coder, reviewer, tester, coder-2, reviewer-2
    """
    global _ROLE_COUNT
    _ROLE_COUNT.setdefault(role_name, 0)
    _ROLE_COUNT[role_name] += 1
    count = _ROLE_COUNT[role_name]
    if count == 1:
        return role_name
    return f"{role_name}-{count}"
