from __future__ import annotations

import json
import re
from typing import Any

from .normalize import normalize_arguments, normalize_tool_name


JSON_INPUT_KEYS = ("input", "arguments", "args", "parameters")


def _repair_loose_json(text: str) -> str:
    repaired = text.strip()
    if not repaired:
        return repaired
    repaired = repaired.replace('"name="', '"name": "')
    repaired = re.sub(r'"name=([^",}]+)"', r'"name": "\1"', repaired)
    repaired = re.sub(r'"name=([^",}]+)', r'"name": "\1"', repaired)
    repaired = re.sub(r'"name\s*=\s*"', '"name": "', repaired)
    repaired = re.sub(r'"(name|input|arguments|args|parameters)"\s*=\s*', r'"\1": ', repaired)
    return repaired


def _extract_call(payload: object, allowed_names: set[str]) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None

    # Qwen / OpenAI 官方单对象格式：{"id":"...","type":"function","function":{"name":...,"arguments":"..."}}
    # name 和 arguments 嵌在 function 子字段里，不是顶层。向下穿透一层再提取。
    nested_function = payload.get("function")
    if isinstance(nested_function, dict) and nested_function.get("name"):
        payload = nested_function

    name = payload.get("name")
    if not name:
        return None

    raw_input = payload.get("input")
    if "input" not in payload:
        for key in JSON_INPUT_KEYS[1:]:
            if key in payload:
                raw_input = payload[key]
                break
        else:
            raw_input = {}
    return {
        "name": name if isinstance(name, str) and name in allowed_names else normalize_tool_name(name, allowed_names),
        "input": normalize_arguments(raw_input),
    }


def parse_json_format(text: str, allowed_names: set[str]) -> list[dict[str, Any]]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```").strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()

    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, TypeError, ValueError):
        repaired = _repair_loose_json(stripped)
        if repaired == stripped:
            return []
        try:
            payload = json.loads(repaired)
        except (json.JSONDecodeError, TypeError, ValueError):
            return []

    if isinstance(payload, dict) and isinstance(payload.get("tool_calls"), list):
        calls = []
        for item in payload["tool_calls"]:
            if not isinstance(item, dict):
                continue
            function_payload = item.get("function")
            if not isinstance(function_payload, dict):
                continue
            call = _extract_call(function_payload, allowed_names)
            if call:
                calls.append(call)
        return calls

    call = _extract_call(payload, allowed_names) if isinstance(payload, dict) else None
    return [call] if call else []
