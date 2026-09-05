from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from backend.adapter.standard_request import StandardRequest
from backend.runtime.execution import build_tool_directive, cleanup_runtime_resources, collect_completion_run, evaluate_retry_directive
from backend.services.auth_quota import add_used_tokens
from backend.services.task_session import build_retry_rebase_prompt
from backend.services.token_calc import calculate_usage


@dataclass(slots=True)
class CompletionBridgeResult:
    execution: Any
    usage: dict[str, int]
    prompt: str
    attempt_index: int
    directive: Any | None = None


async def _reacquire_bound_account_if_needed(*, client, standard_request: StandardRequest) -> None:
    preferred_email = getattr(standard_request, 'bound_account_email', None)
    if preferred_email:
        standard_request.bound_account = await client.account_pool.acquire_wait_preferred(preferred_email, timeout=60)
    else:
        standard_request.bound_account = None


async def run_completion_bridge(
    *,
    client,
    standard_request: StandardRequest,
    prompt: str,
    users_db,
    token: str,
    usage_delta: int | None = None,
    capture_events: bool = True,
    on_delta: Callable[[dict[str, Any], str | None, list[dict[str, Any]] | None], Awaitable[None]] | None = None,
) -> CompletionBridgeResult:
    execution = await collect_completion_run(
        client,
        standard_request,
        prompt,
        capture_events=capture_events,
        on_delta=on_delta,
    )
    usage = calculate_usage(prompt, execution.state.answer_text)
    await add_used_tokens(users_db, token, usage_delta if usage_delta is not None else usage["total_tokens"])
    await cleanup_runtime_resources(
        client,
        execution.acc,
        execution.chat_id,
        preserve_chat=bool(getattr(standard_request, 'persistent_session', False)),
    )
    return CompletionBridgeResult(execution=execution, usage=usage, prompt=prompt, attempt_index=0)


async def run_retryable_completion_bridge(
    *,
    client,
    standard_request: StandardRequest,
    prompt: str,
    users_db,
    token: str,
    history_messages: list[dict[str, Any]] | None,
    max_attempts: int,
    usage_delta_factory: Callable[[Any, str], int] | None = None,
    allow_after_visible_output: bool = False,
    capture_events: bool = True,
    on_delta: Callable[[dict[str, Any], str | None, list[dict[str, Any]] | None], Awaitable[None]] | None = None,
    on_retry: Callable[[], None] | None = None,
) -> CompletionBridgeResult:
    current_prompt = prompt
    if not getattr(standard_request, 'full_prompt', None):
        standard_request.full_prompt = prompt

    for attempt_index in range(max_attempts):
        execution = await collect_completion_run(
            client,
            standard_request,
            current_prompt,
            capture_events=capture_events,
            on_delta=on_delta,
        )
        retry = evaluate_retry_directive(
            request=standard_request,
            current_prompt=current_prompt,
            history_messages=history_messages,
            attempt_index=attempt_index,
            max_attempts=max_attempts,
            state=execution.state,
            allow_after_visible_output=allow_after_visible_output,
        )
        if retry.retry:
            # 丢弃本轮已缓冲但尚未 finalize 的流式输出，避免重试结果与上一轮叠加。
            if on_retry is not None:
                on_retry()
            preserve_chat = bool(getattr(standard_request, 'persistent_session', False))
            await cleanup_runtime_resources(client, execution.acc, execution.chat_id, preserve_chat=preserve_chat)

            reused_persistent_chat = bool(getattr(standard_request, 'persistent_session', False) and getattr(standard_request, 'upstream_chat_id', None))
            if reused_persistent_chat:
                current_prompt = build_retry_rebase_prompt(standard_request, reason=retry.reason)
            else:
                current_prompt = retry.next_prompt

            if not preserve_chat:
                await asyncio.sleep(0.15)
            await _reacquire_bound_account_if_needed(client=client, standard_request=standard_request)
            continue

        usage = calculate_usage(current_prompt, execution.state.answer_text)
        usage_delta = usage_delta_factory(execution, current_prompt) if usage_delta_factory is not None else usage["total_tokens"]
        directive = build_tool_directive(standard_request, execution.state)
        await add_used_tokens(users_db, token, usage_delta)
        await cleanup_runtime_resources(
            client,
            execution.acc,
            execution.chat_id,
            preserve_chat=bool(getattr(standard_request, 'persistent_session', False)),
        )
        return CompletionBridgeResult(
            execution=execution,
            usage=usage,
            prompt=current_prompt,
            attempt_index=attempt_index,
            directive=directive,
        )

    raise RuntimeError("Retryable completion bridge exhausted attempts")
