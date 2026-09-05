from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import asyncio
import json
import logging
import uuid

from backend.adapter.standard_request import StandardRequest
from backend.adapter.cli_proxy import CLIProxy
from backend.core.config import resolve_model, settings
from backend.core.request_logging import new_request_id, request_context, update_request_context
from backend.runtime import stream_presenter
from backend.runtime.execution import (
    build_tool_directive,
    cleanup_runtime_resources,
    collect_completion_run,
    collect_completion_run_with_recovery,
    evaluate_retry_directive,
    request_max_attempts,
)
from backend.services.auth_quota import resolve_auth_context
from backend.services.context_attachment_manager import prepare_context_attachments, derive_session_key
from backend.services.attachment_preprocessor import preprocess_attachments
from backend.services.prompt_builder import CLAUDE_CODE_OPENAI_PROFILE, messages_to_prompt
from backend.services.qwen_client import QwenClient
from backend.services.task_session import (
    build_anthropic_assistant_history_message,
    build_retry_rebase_prompt,
    clear_invalidated_session_chat,
    log_session_plan_reuse_cancelled,
    persist_session_turn,
    plan_persistent_session_turn,
)
from backend.services.token_calc import count_tokens
from backend.toolcall.normalize import build_tool_name_registry

log = logging.getLogger("qwen2api.anthropic")
router = APIRouter()


class _AnthropicStreamState:
    def __init__(self, *, msg_id: str, model_name: str, prompt: str):
        self.msg_id = msg_id
        self.model_name = model_name
        self.prompt = prompt
        self.pending_chunks: list[str] = []
        self.answer_text_buffer: list[tuple[int, str]] = []
        self.block_index = 0
        self.current_block: dict[str, object] = {"type": None, "index": None, "tool_call_id": None}
        self.opened_tool_calls: set[str] = set()

    def ensure_message_start(self) -> None:
        if not self.pending_chunks:
            self.pending_chunks.append(_message_start_event(self.msg_id, self.model_name, self.prompt, ""))

    def close_current_block(self) -> None:
        index = self.current_block.get("index")
        if index is None:
            return
        self.pending_chunks.append(stream_presenter.anthropic_content_block_stop(index))
        self.current_block = {"type": None, "index": None, "tool_call_id": None}

    def open_textual_block(self, block_type: str) -> int:
        current_type = self.current_block.get("type")
        current_index = self.current_block.get("index")
        if current_type == block_type and isinstance(current_index, int):
            return current_index
        self.close_current_block()
        index = self.block_index
        self.block_index += 1
        if block_type == "thinking":
            content_block = {"type": "thinking", "thinking": ""}
        else:
            content_block = {"type": "text", "text": ""}
        self.pending_chunks.append(stream_presenter.anthropic_content_block_start(index, content_block))
        self.current_block = {"type": block_type, "index": index, "tool_call_id": None}
        return index

    def open_tool_block(self, tool_call_id: str, tool_name: str) -> int:
        current_index = self.current_block.get("index")
        if (
            self.current_block.get("type") == "tool_use"
            and self.current_block.get("tool_call_id") == tool_call_id
            and isinstance(current_index, int)
        ):
            return current_index
        self.close_current_block()
        index = self.block_index
        self.block_index += 1
        self.pending_chunks.append(
            f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': index, 'content_block': {'type': 'tool_use', 'id': tool_call_id, 'name': tool_name, 'input': {}}}, ensure_ascii=False)}\n\n"
        )
        self.current_block = {"type": "tool_use", "index": index, "tool_call_id": tool_call_id}
        self.opened_tool_calls.add(tool_call_id)
        return index

    def append_thinking_delta(self, text_chunk: str) -> None:
        index = self.open_textual_block("thinking")
        self.pending_chunks.append(
            stream_presenter.anthropic_content_block_delta(index, {"type": "thinking_delta", "thinking": text_chunk})
        )

    def buffer_answer_text(self, text_chunk: str) -> None:
        index = self.open_textual_block("text")
        self.answer_text_buffer.append((index, text_chunk))

    def append_tool_delta(self, *, tool_call_id: str, tool_name: str, partial_json: str) -> None:
        index = self.open_tool_block(tool_call_id, tool_name)
        if partial_json:
            self.pending_chunks.append(
                stream_presenter.anthropic_content_block_delta(index, {"type": "input_json_delta", "partial_json": partial_json})
            )

    def flush_answer_text(self) -> None:
        if not self.answer_text_buffer:
            return
        for index, text_chunk in self.answer_text_buffer:
            self.pending_chunks.append(
                stream_presenter.anthropic_content_block_delta(index, {"type": "text_delta", "text": text_chunk})
            )
        self.answer_text_buffer = []

    def clear_answer_text(self) -> None:
        self.answer_text_buffer = []


def _build_standard_request(req_data: dict) -> StandardRequest:
    """使用 CLIProxy 进行协议转换"""
    standard_request = CLIProxy.from_anthropic(req_data, client_profile=CLAUDE_CODE_OPENAI_PROFILE)
    CLIProxy.log_conversion("anthropic", standard_request.response_model, len(standard_request.prompt), len(standard_request.tools))
    return standard_request


def _anthropic_usage(prompt: str, answer_text: str) -> dict[str, int]:
    return {"input_tokens": len(prompt), "output_tokens": len(answer_text)}


def _message_start_event(msg_id: str, model_name: str, prompt: str, answer_text: str) -> str:
    return stream_presenter.anthropic_message_start(msg_id, model_name, _anthropic_usage(prompt, answer_text))


async def _run_anthropic_attempt(
    *,
    client: QwenClient,
    standard_request: StandardRequest,
    current_prompt: str,
    history_messages: list[dict],
    stream_attempt: int,
    max_attempts: int,
):
    update_request_context(stream_attempt=stream_attempt + 1)
    execution = await collect_completion_run(client, standard_request, current_prompt)
    retry = evaluate_retry_directive(
        request=standard_request,
        current_prompt=current_prompt,
        history_messages=history_messages,
        attempt_index=stream_attempt,
        max_attempts=max_attempts,
        state=execution.state,
        allow_after_visible_output=True,
    )
    return execution, retry


def _visible_answer_text_length(*, directive, execution, stream_state: _AnthropicStreamState | None = None) -> int:
    if directive.stop_reason == "tool_use":
        return 0
    if stream_state is not None:
        return sum(len(text_chunk) for _, text_chunk in stream_state.answer_text_buffer)
    return len(execution.state.answer_text)


async def _add_used_tokens_for_prompt(*, users_db, token: str, prompt_text: str, answer_text_length: int) -> None:
    users = await users_db.get()
    for user in users:
        if user["id"] == token:
            user["used_tokens"] += answer_text_length + len(prompt_text)
            break
    await users_db.save(users)


async def _reacquire_bound_account_if_needed(*, client: QwenClient, standard_request: StandardRequest) -> None:
    preferred_email = getattr(standard_request, "bound_account_email", None)
    if preferred_email:
        standard_request.bound_account = await client.account_pool.acquire_wait_preferred(preferred_email, timeout=60)
    else:
        standard_request.bound_account = None


@router.post("/messages/count_tokens")
@router.post("/v1/messages/count_tokens")
@router.post("/anthropic/v1/messages/count_tokens")
async def anthropic_count_tokens(request: Request):
    try:
        req_data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})
    prompt_result = messages_to_prompt(req_data, client_profile=CLAUDE_CODE_OPENAI_PROFILE)
    base_tokens = count_tokens(prompt_result.prompt)
    # Context Pressure Inflation:
    # Claude Code 假设 context window=200K，到 ~80%(160K) 触发自动压缩。
    # 但 Qwen 实际上游 window 只有 ~150K，到 ~120K 时就开始挤压输出预算。
    # 虚增 input_tokens 1.35x 让 CC 提前触发压缩，避免爆 window。
    inflation = 1.35
    inflated = int(base_tokens * inflation)
    return JSONResponse({"input_tokens": inflated})


@router.post("/messages")
@router.post("/v1/messages")
@router.post("/anthropic/v1/messages")
async def anthropic_messages(request: Request):
    app = request.app
    users_db = app.state.users_db
    client: QwenClient = app.state.qwen_client

    auth = await resolve_auth_context(request, users_db)
    token = auth.token

    try:
        req_data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})

    session_key = derive_session_key("anthropic", token, req_data)
    original_history_messages = req_data.get("messages", [])

    async def prepare_locked_request(payload: dict) -> tuple[StandardRequest, dict, str, str, str, str]:
        file_store = getattr(app.state, "file_store", None)
        preprocessed = None
        working_payload = payload
        if file_store is not None:
            preprocessed = await preprocess_attachments(working_payload, file_store, owner_token=token)
            working_payload = preprocessed.payload
        context_prepared = await prepare_context_attachments(
            app=app,
            payload=working_payload,
            surface="anthropic",
            auth_token=token,
            client_profile=CLAUDE_CODE_OPENAI_PROFILE,
            existing_attachments=(preprocessed.attachments if preprocessed is not None else None),
        )
        working_payload = context_prepared["payload"]
        standard_request = _build_standard_request(working_payload)
        if preprocessed is not None:
            standard_request.attachments = preprocessed.attachments
            standard_request.uploaded_file_ids = preprocessed.uploaded_file_ids
        standard_request.upstream_files = context_prepared["upstream_files"]
        standard_request.session_key = context_prepared["session_key"]
        standard_request.context_mode = context_prepared["context_mode"]
        standard_request.bound_account_email = context_prepared["bound_account_email"]
        standard_request.bound_account = context_prepared["bound_account"]

        session_plan = await plan_persistent_session_turn(app=app, request=standard_request, payload=working_payload, surface="anthropic")
        if session_plan.enabled:
            standard_request.persistent_session = True
            standard_request.full_prompt = session_plan.full_prompt
            standard_request.prompt = session_plan.prompt
            standard_request.session_message_hashes = session_plan.current_hashes
            standard_request.upstream_chat_id = session_plan.existing_chat_id if session_plan.reuse_chat else None
            if standard_request.bound_account is None and session_plan.account_email:
                standard_request.bound_account = await app.state.account_pool.acquire_wait_preferred(session_plan.account_email, timeout=60)
                if standard_request.bound_account is not None:
                    standard_request.bound_account_email = standard_request.bound_account.email
            elif standard_request.bound_account is not None and not standard_request.bound_account_email:
                standard_request.bound_account_email = standard_request.bound_account.email
            if standard_request.upstream_chat_id and standard_request.bound_account is None:
                log_session_plan_reuse_cancelled(
                    request=standard_request,
                    planned_chat_id=session_plan.existing_chat_id,
                    reason="missing_bound_account",
                )
                standard_request.upstream_chat_id = None
                standard_request.prompt = standard_request.full_prompt or standard_request.prompt

        model_name = standard_request.response_model
        qwen_model = standard_request.resolved_model
        prompt = standard_request.prompt
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        return standard_request, working_payload, model_name, qwen_model, prompt, msg_id

    with request_context(req_id=new_request_id(), surface="anthropic", requested_model=req_data.get("model", "claude-3-5-sonnet"), resolved_model="-", api_key=auth.token):
        if request.headers.get("x-debug-session-key"):
            pass

        if req_data.get("stream", False):
            async def generate():
                async with app.state.session_locks.hold(session_key):
                    standard_request, effective_payload, model_name, qwen_model, prompt, msg_id = await prepare_locked_request(req_data)
                    update_request_context(requested_model=model_name, resolved_model=qwen_model)
                    log.info(f"[ANT] model={qwen_model}, stream={standard_request.stream}, tool_enabled={standard_request.tool_enabled}, tools={[t.get('name') for t in standard_request.tools]}, prompt_len={len(prompt)}")
                    history_messages = original_history_messages
                    current_prompt = prompt
                    max_attempts = request_max_attempts(standard_request)
                    for stream_attempt in range(max_attempts):
                        stream_state = _AnthropicStreamState(msg_id=msg_id, model_name=model_name, prompt=current_prompt)
                        try:
                            update_request_context(stream_attempt=stream_attempt + 1)

                            async def on_delta(evt, text_chunk, _):
                                stream_state.ensure_message_start()
                                phase = evt.get("phase")
                                if text_chunk and phase in ("think", "thinking_summary"):
                                    stream_state.append_thinking_delta(text_chunk)
                                    return
                                if text_chunk and phase == "answer":
                                    stream_state.buffer_answer_text(text_chunk)
                                    return
                                if phase == "tool_call":
                                    extra = evt.get("extra", {}) or {}
                                    tool_call_id = extra.get("tool_call_id")
                                    if tool_call_id is None:
                                        tool_call_id = f"tc_idx_{extra.get('index', 0)}"
                                    tool_name = extra.get("tool_name")
                                    if not tool_name:
                                        return
                                    stream_state.append_tool_delta(
                                        tool_call_id=str(tool_call_id),
                                        tool_name=str(tool_name),
                                        partial_json=evt.get("content", ""),
                                    )

                            execution = await collect_completion_run_with_recovery(
                                client,
                                standard_request,
                                current_prompt,
                                capture_events=False,
                                on_delta=on_delta,
                                max_continuation=2,
                                warmup_chars=64,
                                guard_chars=96,
                            )
                            retry = evaluate_retry_directive(
                                request=standard_request,
                                current_prompt=current_prompt,
                                history_messages=history_messages,
                                attempt_index=stream_attempt,
                                max_attempts=max_attempts,
                                state=execution.state,
                                allow_after_visible_output=True,
                            )
                            if retry.retry:
                                reused_persistent_chat = bool(standard_request.persistent_session and standard_request.upstream_chat_id)
                                # 如果正在复用会话，重试时保留会话，避免删除后重建导致上下文丢失
                                preserve_chat = reused_persistent_chat
                                await cleanup_runtime_resources(client, execution.acc, execution.chat_id, preserve_chat=preserve_chat)
                                if reused_persistent_chat:
                                    # 保留 upstream_chat_id，在同一会话中重试
                                    # standard_request.session_chat_invalidated = True
                                    # standard_request.upstream_chat_id = None
                                    current_prompt = build_retry_rebase_prompt(standard_request, reason=retry.reason)
                                else:
                                    current_prompt = retry.next_prompt
                                await _reacquire_bound_account_if_needed(client=client, standard_request=standard_request)
                                continue

                            if not stream_state.pending_chunks:
                                stream_state.pending_chunks.append(_message_start_event(msg_id, model_name, current_prompt, execution.state.answer_text))

                            stream_state.close_current_block()
                            directive = build_tool_directive(standard_request, execution.state)
                            if directive.stop_reason == "tool_use":
                                stream_state.clear_answer_text()
                                stream_state.current_block = {"type": None, "index": None, "tool_call_id": None}
                            else:
                                stream_state.flush_answer_text()
                            expected_tool_ids = {
                                block.get("id")
                                for block in directive.tool_blocks
                                if block.get("type") == "tool_use" and block.get("id")
                            }
                            for block in directive.tool_blocks:
                                if block.get("type") != "tool_use":
                                    continue
                                tool_id = block.get("id")
                                if tool_id in stream_state.opened_tool_calls:
                                    continue
                                index = stream_state.open_tool_block(str(tool_id), str(block.get("name", "")))
                                stream_state.pending_chunks.append(
                                    stream_presenter.anthropic_content_block_delta(index, {'type': 'input_json_delta', 'partial_json': json.dumps(block.get('input', {}), ensure_ascii=False)})
                                )
                                stream_state.close_current_block()

                            visible_answer_length = _visible_answer_text_length(
                                directive=directive,
                                execution=execution,
                                stream_state=stream_state,
                            )
                            stop_reason = "tool_use" if expected_tool_ids else "end_turn"
                            stream_state.pending_chunks.append(stream_presenter.anthropic_message_delta(stop_reason, visible_answer_length))
                            stream_state.pending_chunks.append(stream_presenter.anthropic_message_stop())

                            await _add_used_tokens_for_prompt(
                                users_db=users_db,
                                token=token,
                                prompt_text=current_prompt,
                                answer_text_length=len(execution.state.answer_text),
                            )
                            assistant_message = build_anthropic_assistant_history_message(
                                execution=execution,
                                request=standard_request,
                                directive=directive,
                            )
                            await persist_session_turn(
                                app=app,
                                request=standard_request,
                                surface="anthropic",
                                execution=execution,
                                assistant_message=assistant_message,
                            )
                            await cleanup_runtime_resources(
                                client,
                                execution.acc,
                                execution.chat_id,
                                preserve_chat=bool(standard_request.persistent_session),
                            )
                            for chunk in stream_state.pending_chunks:
                                yield chunk
                            return
                        except HTTPException as he:
                            await clear_invalidated_session_chat(app=app, request=standard_request)
                            yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': he.detail}}, ensure_ascii=False)}\n\n"
                            return
                        except Exception as e:
                            await clear_invalidated_session_chat(app=app, request=standard_request)
                            yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': str(e)}}, ensure_ascii=False)}\n\n"
                            return

            return StreamingResponse(
                generate(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        async with app.state.session_locks.hold(session_key):
            standard_request, effective_payload, model_name, qwen_model, prompt, msg_id = await prepare_locked_request(req_data)
            update_request_context(requested_model=model_name, resolved_model=qwen_model)
            log.info(f"[ANT] model={qwen_model}, stream={standard_request.stream}, tool_enabled={standard_request.tool_enabled}, tools={[t.get('name') for t in standard_request.tools]}, prompt_len={len(prompt)}")
            history_messages = original_history_messages
            current_prompt = prompt
            max_attempts = request_max_attempts(standard_request)
            for stream_attempt in range(max_attempts):
                try:
                    execution, retry = await _run_anthropic_attempt(
                        client=client,
                        standard_request=standard_request,
                        current_prompt=current_prompt,
                        history_messages=history_messages,
                        stream_attempt=stream_attempt,
                        max_attempts=max_attempts,
                    )
                    if retry.retry:
                        reused_persistent_chat = bool(standard_request.persistent_session and standard_request.upstream_chat_id)
                        # 如果正在复用会话，重试时保留会话，避免删除后重建导致上下文丢失
                        preserve_chat = reused_persistent_chat
                        await cleanup_runtime_resources(client, execution.acc, execution.chat_id, preserve_chat=preserve_chat)
                        if reused_persistent_chat:
                            # 保留 upstream_chat_id，在同一会话中重试
                            # standard_request.session_chat_invalidated = True
                            # standard_request.upstream_chat_id = None
                            current_prompt = build_retry_rebase_prompt(standard_request, reason=retry.reason)
                        else:
                            current_prompt = retry.next_prompt
                        await _reacquire_bound_account_if_needed(client=client, standard_request=standard_request)
                        continue

                    directive = build_tool_directive(standard_request, execution.state)
                    content_blocks: list[dict] = []
                    if execution.state.reasoning_text:
                        content_blocks.append({"type": "thinking", "thinking": execution.state.reasoning_text})
                    content_blocks.extend(directive.tool_blocks)

                    await _add_used_tokens_for_prompt(
                        users_db=users_db,
                        token=token,
                        prompt_text=current_prompt,
                        answer_text_length=len(execution.state.answer_text),
                    )
                    assistant_message = build_anthropic_assistant_history_message(
                        execution=execution,
                        request=standard_request,
                        directive=directive,
                    )
                    await persist_session_turn(
                        app=app,
                        request=standard_request,
                        surface="anthropic",
                        execution=execution,
                        assistant_message=assistant_message,
                    )
                    await cleanup_runtime_resources(
                        client,
                        execution.acc,
                        execution.chat_id,
                        preserve_chat=bool(standard_request.persistent_session),
                    )

                    return JSONResponse(
                        {
                            "id": msg_id,
                            "type": "message",
                            "role": "assistant",
                            "model": model_name,
                            "content": content_blocks,
                            "stop_reason": directive.stop_reason,
                            "stop_sequence": None,
                            "usage": _anthropic_usage(current_prompt, execution.state.answer_text),
                        }
                    )
                except Exception as e:
                    if stream_attempt == max_attempts - 1:
                        await clear_invalidated_session_chat(app=app, request=standard_request)
                        raise HTTPException(status_code=500, detail=str(e))
