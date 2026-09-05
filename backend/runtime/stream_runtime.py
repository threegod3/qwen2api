from __future__ import annotations

from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass, field
from typing import Any, TypeAlias, cast

from .finalizer import finalize_runtime_result

Event: TypeAlias = dict[str, Any]
EventSource: TypeAlias = Iterable[Event] | AsyncIterable[Event]


@dataclass
class RuntimeResult:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None


class StreamRuntime:
    def __init__(self, request: dict[str, Any]) -> None:
        self.request = request
        self.result = RuntimeResult()

    def _apply_event(self, event: Event) -> None:
        event_type = event.get("type")

        if event_type == "text_delta":
            self.result.text += event.get("text", "")
        elif event_type == "tool_call":
            self.result.tool_calls.append(
                {
                    "id": event.get("id"),
                    "name": event.get("name"),
                    "input": event.get("input", {}),
                }
            )
        elif event_type == "finish":
            self.result.finish_reason = event.get("finish_reason")

    async def collect(self, events: EventSource) -> RuntimeResult:
        if hasattr(events, "__aiter__"):
            async_events = cast(AsyncIterable[Event], events)
            async for event in async_events:
                self._apply_event(event)
        else:
            sync_events = cast(Iterable[Event], events)
            for event in sync_events:
                self._apply_event(event)

        return finalize_runtime_result(self.result)
