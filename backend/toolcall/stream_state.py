import json
from dataclasses import dataclass
from typing import Any


@dataclass
class _ToolCallBuffer:
    name: str = ""
    args: str = ""
    emitted: bool = False


class StreamingToolCallState:
    def __init__(self) -> None:
        self._calls: dict[str, _ToolCallBuffer] = {}
        self._anonymous_call_seq = 0

    def process_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        if event.get("type") != "delta" or event.get("phase") != "tool_call":
            return []

        tool_call_id = self._resolve_tool_call_id(event)
        buffer = self._calls.setdefault(tool_call_id, _ToolCallBuffer())
        content = event.get("content", "")

        self._apply_chunk(buffer, content)

        if buffer.emitted or not buffer.name or not self.is_complete_json_object(buffer.args):
            return []

        parsed_input = json.loads(buffer.args) if buffer.args else {}
        buffer.emitted = True
        return [{"type": "tool_use", "id": tool_call_id, "name": buffer.name, "input": parsed_input}]

    @staticmethod
    def is_complete_json_object(text: str) -> bool:
        if not text:
            return False
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
        return isinstance(parsed, dict)

    def _resolve_tool_call_id(self, event: dict[str, Any]) -> str:
        extra = event.get("extra") or {}
        tool_call_id = extra.get("tool_call_id")
        if tool_call_id:
            return tool_call_id

        index = extra.get("index")
        if index is not None:
            return f"tc_idx_{index}"

        self._anonymous_call_seq += 1
        return f"tc_anonymous_{self._anonymous_call_seq}"

    @staticmethod
    def _apply_chunk(buffer: _ToolCallBuffer, content: str) -> None:
        try:
            chunk = json.loads(content)
        except (json.JSONDecodeError, TypeError, ValueError):
            buffer.args += content
            return

        if isinstance(chunk, dict):
            name = chunk.get("name")
            if name and not buffer.name:
                buffer.name = name
            arguments = chunk.get("arguments")
            if isinstance(arguments, str):
                buffer.args += arguments
