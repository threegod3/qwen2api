from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
import asyncio
import json
import logging
from typing import Any

from backend.adapter.standard_request import StandardRequest
from backend.adapter.cli_proxy import CLIProxy
from backend.core.config import resolve_model
from backend.core.request_logging import new_request_id, request_context, update_request_context
from backend.runtime import stream_presenter
from backend.runtime.execution import collect_completion_run, cleanup_runtime_resources
from backend.services.auth_quota import resolve_auth_context
from backend.services.token_calc import calculate_usage

log = logging.getLogger("qwen2api.gemini")
router = APIRouter()

GEMINI_STREAM_MEDIA_TYPE = "application/json"


def _build_standard_request(model: str, body: dict, *, stream: bool | None = None) -> StandardRequest:
    """使用 CLIProxy 进行协议转换"""
    standard_request = CLIProxy.from_gemini(model, body, stream=stream)
    CLIProxy.log_conversion("gemini", standard_request.response_model, len(standard_request.prompt), len(standard_request.tools))
    return standard_request


def _gemini_chunk_payload(text: str) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text}],
                    "role": "model",
                }
            }
        ]
    }


async def _load_and_validate_request(request: Request, model: str, *, force_stream: bool | None = None):
    app = request.app
    users_db = app.state.users_db
    client = app.state.qwen_client

    auth = await resolve_auth_context(request, users_db)
    token = auth.token

    body = await request.json()
    standard_request = _build_standard_request(model, body, stream=force_stream)
    update_request_context(resolved_model=standard_request.resolved_model)
    return users_db, client, token, standard_request


@router.post("/v1beta/models/{model}:generateContent")
@router.post("/v1/models/{model}:generateContent")
@router.post("/models/{model}:generateContent")
async def gemini_generate_content(model: str, request: Request):
    with request_context(req_id=new_request_id(), surface="gemini", requested_model=model):
        users_db, client, token, standard_request = await _load_and_validate_request(request, model, force_stream=False)
        content = standard_request.prompt
        log.info(f"[Gemini] route=generateContent model={standard_request.resolved_model}, stream={standard_request.stream}, prompt_len={len(content)}")

        try:
            execution = await collect_completion_run(client, standard_request, content)
        except Exception as e:
            log.error(f"Gemini proxy failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

        usage = calculate_usage(content, execution.state.answer_text)
        users = await users_db.get()
        for u in users:
            if u["id"] == token:
                u["used_tokens"] += usage["total_tokens"]
                break
        await users_db.save(users)
        await cleanup_runtime_resources(client, execution.acc, execution.chat_id)

        log.info(f"[Gemini] Request complete. Generated {len(execution.state.answer_text)} characters.")
        return JSONResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": execution.state.answer_text}],
                            "role": "model",
                        }
                    }
                ]
            }
        )


@router.post("/v1beta/models/{model}:streamGenerateContent")
@router.post("/v1/models/{model}:streamGenerateContent")
@router.post("/models/{model}:streamGenerateContent")
async def gemini_stream_generate_content(model: str, request: Request):
    with request_context(req_id=new_request_id(), surface="gemini", requested_model=model):
        users_db, client, token, standard_request = await _load_and_validate_request(request, model, force_stream=True)
        content = standard_request.prompt
        log.info(f"[Gemini] route=streamGenerateContent model={standard_request.resolved_model}, stream={standard_request.stream}, prompt_len={len(content)}")

        async def generate():
            queue: asyncio.Queue[str | None] = asyncio.Queue()

            async def on_delta(evt, text_chunk, _):
                if text_chunk and evt.get("phase") == "answer":
                    await queue.put(stream_presenter.gemini_text_chunk(text_chunk))

            async def runner():
                execution = None
                try:
                    execution = await collect_completion_run(
                        client,
                        standard_request,
                        content,
                        capture_events=False,
                        on_delta=on_delta,
                    )

                    usage = calculate_usage(content, execution.state.answer_text)
                    users = await users_db.get()
                    for u in users:
                        if u["id"] == token:
                            u["used_tokens"] += usage["total_tokens"]
                            break
                    await users_db.save(users)
                    await cleanup_runtime_resources(client, execution.acc, execution.chat_id)
                    log.info(f"[Gemini] Request complete. Generated {len(execution.state.answer_text)} characters.")
                except Exception as e:
                    await queue.put(json.dumps({"error": str(e)}) + "\n")
                finally:
                    await queue.put(None)

            task = asyncio.create_task(runner())
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
            await task

        return StreamingResponse(generate(), media_type=GEMINI_STREAM_MEDIA_TYPE)
