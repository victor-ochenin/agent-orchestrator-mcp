"""Utility functions for agent naming."""

import re
import unicodedata

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


def transliterate_cyrillic(text: str) -> str:
    """Transliterate Cyrillic characters to Latin."""
    # Mapping for Cyrillic to Latin transliteration
    cyrillic_to_latin = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
        'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'Yo',
        'Ж': 'Zh', 'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L', 'М': 'M',
        'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U',
        'Ф': 'F', 'Х': 'Kh', 'Ц': 'Ts', 'Ч': 'Ch', 'Ш': 'Sh', 'Щ': 'Shch',
        'Ъ': '', 'Ы': 'Y', 'Ь': '', 'Э': 'E', 'Ю': 'Yu', 'Я': 'Ya',
    }
    
    result = []
    for char in text:
        if char in cyrillic_to_latin:
            result.append(cyrillic_to_latin[char])
        else:
            result.append(char)
    return ''.join(result)


def generate_slug(text: str, max_length: int = 30) -> str:
    """Generate a URL-friendly slug from text.
    
    Args:
        text: The text to convert to a slug
        max_length: Maximum length of the slug (default 30)
    
    Returns:
        A lowercase slug with hyphens as separators
    """
    # Transliterate Cyrillic to Latin
    text = transliterate_cyrillic(text)
    # Normalize unicode characters
    text = unicodedata.normalize('NFKD', text)
    # Remove non-ASCII characters
    text = text.encode('ascii', 'ignore').decode('ascii')
    # Convert to lowercase
    text = text.lower()
    # Replace spaces and hyphens with single hyphen
    text = re.sub(r'[\s_]+', '-', text)
    # Remove non-alphanumeric characters (except hyphens)
    text = re.sub(r'[^a-z0-9-]', '', text)
    # Remove multiple consecutive hyphens
    text = re.sub(r'-{2,}', '-', text)
    # Strip leading/trailing hyphens
    text = text.strip('-')
    # Truncate to max_length
    if len(text) > max_length:
        text = text[:max_length].rstrip('-')
    return text if text else "task"
