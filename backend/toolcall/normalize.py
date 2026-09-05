from __future__ import annotations

import json
import re
from typing import Any, Iterable


def _tool_alias_key(value: str) -> str:
    lowered = value.strip().lower()
    if not lowered:
        return ""
    return re.sub(r"[^a-z0-9]+", "", lowered)


def build_tool_name_registry(allowed_names: Iterable[str]) -> dict[str, str]:
    registry: dict[str, str] = {}
    for allowed_name in allowed_names:
        if not isinstance(allowed_name, str):
            continue
        canonical = allowed_name.strip()
        if not canonical:
            continue
        for alias in {canonical, canonical.lower(), _tool_alias_key(canonical)}:
            key = _tool_alias_key(alias)
            if key and key not in registry:
                registry[key] = canonical
    return registry


def normalize_tool_name(name: str, allowed_names: Iterable[str]) -> str:
    if not isinstance(name, str) or not name:
        return name

    allowed_list = [candidate for candidate in allowed_names if candidate]
    if not allowed_list:
        return name

    for allowed_name in allowed_list:
        if allowed_name == name:
            return allowed_name

    registry = build_tool_name_registry(allowed_list)
    return registry.get(_tool_alias_key(name), name)


def normalize_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {"value": value}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    if value is None:
        return {}

    if isinstance(value, dict):
        return value

    return {"value": value}
