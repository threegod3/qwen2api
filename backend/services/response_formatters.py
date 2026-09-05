from __future__ import annotations

import json
from typing import Any

from backend.runtime.execution import build_tool_directive


def build_openai_completion_payload(*, completion_id: str, created: int, model_name: str, prompt: str, execution, standard_request) -> dict[str, Any]:
    directive = build_tool_directive(standard_request, execution.state)
    if directive.stop_reason == "tool_use":
        oai_tool_calls = [
            {
                "id": block["id"],
                "type": "function",
                "function": {
                    "name": block["name"],
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            }
            for block in directive.tool_blocks
            if block.get("type") == "tool_use"
        ]
        msg: dict[str, Any] = {"role": "assistant", "content": None, "tool_calls": oai_tool_calls}
        finish_reason = "tool_calls"
    else:
        oai_tool_calls = []
        msg = {"role": "assistant", "content": execution.state.answer_text}
        finish_reason = "stop"

    log_payload = [
        {
            "id": call["id"],
            "name": call["function"]["name"],
            "arguments": call["function"]["arguments"],
        }
        for call in oai_tool_calls
    ]
    import logging
    logging.getLogger("qwen2api.chat").info(
        "[OAI] response finish_reason=%s tool_calls=%s text_preview=%r",
        finish_reason,
        log_payload,
        execution.state.answer_text[:300],
    )

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": len(prompt),
            "completion_tokens": len(execution.state.answer_text),
            "total_tokens": len(prompt) + len(execution.state.answer_text),
        },
    }


def build_anthropic_message_payload(*, msg_id: str, model_name: str, prompt: str, execution, standard_request) -> dict[str, Any]:
    directive = build_tool_directive(standard_request, execution.state)
    content_blocks: list[dict[str, Any]] = []
    if execution.state.reasoning_text:
        content_blocks.append({"type": "thinking", "thinking": execution.state.reasoning_text})
    content_blocks.extend(directive.tool_blocks)
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model_name,
        "content": content_blocks,
        "stop_reason": directive.stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": len(prompt), "output_tokens": len(execution.state.answer_text)},
    }


def build_gemini_generate_payload(*, execution) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": execution.state.answer_text}],
                    "role": "model",
                }
            }
        ]
    }
