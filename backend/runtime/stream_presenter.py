import json
from typing import Any


def anthropic_message_start(msg_id: str, model_name: str, usage: dict[str, Any]) -> str:
    payload = {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model_name,
            "stop_reason": None,
            "usage": usage,
        },
    }
    return f"event: message_start\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def anthropic_content_block_start(index: int, content_block: dict[str, Any]) -> str:
    return f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': index, 'content_block': content_block}, ensure_ascii=False)}\n\n"


def anthropic_content_block_delta(index: int, delta: dict[str, Any]) -> str:
    return f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': index, 'delta': delta}, ensure_ascii=False)}\n\n"


def anthropic_content_block_stop(index: int) -> str:
    return f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': index}, ensure_ascii=False)}\n\n"


def anthropic_message_delta(stop_reason: str, output_tokens: int) -> str:
    return f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason}, 'usage': {'output_tokens': output_tokens}}, ensure_ascii=False)}\n\n"


def anthropic_message_stop() -> str:
    return f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'}, ensure_ascii=False)}\n\n"


def openai_chunk(completion_id: str, created: int, model_name: str, delta: dict[str, Any], finish_reason: str | None = None) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def openai_done() -> str:
    return "data: [DONE]\n\n"


def gemini_text_chunk(text: str) -> str:
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text}],
                    "role": "model",
                }
            }
        ]
    }
    return json.dumps(payload, ensure_ascii=False) + "\n"


def gemini_error_chunk(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False) + "\n"
