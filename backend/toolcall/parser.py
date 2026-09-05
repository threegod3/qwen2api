from __future__ import annotations

import json

from backend.toolcall.fallback_textkv import parse_textkv_format
from backend.toolcall.formats_json import parse_json_format
from backend.toolcall.formats_xml import parse_xml_format


def _has_top_level_json_tool_syntax(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```").strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()

    if not stripped.startswith("{"):
        return False

    repaired = stripped.replace('"name="', '"name": "')
    if '"name=' in repaired:
        return True

    try:
        payload = json.loads(repaired)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False

    if not isinstance(payload, dict):
        return False

    if isinstance(payload.get("tool_calls"), list):
        return True

    has_name = isinstance(payload.get("name"), str) and bool(payload.get("name"))
    has_args = any(key in payload for key in ("input", "arguments", "args", "parameters"))
    return has_name and has_args


def _has_xml_like_tool_syntax(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("<invoke", "<tool_call", "</tool_call>"))


def parse_tool_calls_detailed(text: str, allowed_names: set[str]) -> dict[str, object]:
    candidates = [
        ("json", parse_json_format(text, allowed_names)),
        ("xml", parse_xml_format(text, allowed_names)),
        ("textkv", parse_textkv_format(text, allowed_names)),
    ]

    for source, calls in candidates:
        if calls:
            return {
                "calls": calls,
                "source": source,
                "saw_tool_syntax": True,
            }

    return {
        "calls": [],
        "source": None,
        "saw_tool_syntax": (
            _has_top_level_json_tool_syntax(text)
            or _has_xml_like_tool_syntax(text)
            or any(marker in text for marker in ("function.name:", "function.arguments:", '"name="'))
        ),
    }
