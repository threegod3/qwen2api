from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from backend.adapter.standard_request import CLAUDE_CODE_OPENAI_PROFILE, StandardRequest
from backend.core.config import settings
from backend.core.request_logging import update_request_context
from backend.runtime.stream_metrics import StreamMetrics
from backend.services import tool_parser
from backend.toolcall.normalize import normalize_tool_name
from backend.toolcall.stream_state import StreamingToolCallState


# Qwen 偶尔生成的毒性"工具不存在"或"无法继续"幻觉。
# 在流式收到前 20 字时识别，触发早期拦截 + retry 而不是流给客户端。
_TOXIC_REFUSAL_RE = re.compile(
    # 英文：工具不存在/不可用
    r"Tool\s+\S+\s+(?:does\s+not\s+exists?|is\s+not\s+(?:available|registered))"
    r"|I\s+cannot\s+execute\s+this\s+tool"
    # 英文：任务放弃/拒绝继续
    r"|I[''\u2019]?\s*m\s+sorry[,. ]"
    r"|I\s+cannot\s+(?:help|assist|proceed|continue|support|perform)"
    r"|I[''\u2019]?m\s+not\s+(?:able|designed)\s+to"
    r"|unable\s+to\s+(?:proceed|continue|perform|complete)"
    # 中文：工具/操作不存在或无法继续
    r"|该工具.{0,8}?不存在|工具.{0,12}?不存在"
    r"|我(?:无法|不能|不可以)(?:继续|进行|支持|完成|操作|执行)"
    r"|无法(?:进行|支持|完成|执行).{0,10}?操作"
    r"|抱歉.{0,20}?(?:无法|不能|不支持)",
    re.IGNORECASE,
)


log = logging.getLogger("qwen2api.runtime")


@dataclass(slots=True)
class RuntimeAttemptState:
    answer_text: str = ""
    reasoning_text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    blocked_tool_names: list[str] = field(default_factory=list)
    finish_reason: str = "stop"
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    emitted_visible_output: bool = False
    stage_metrics: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class RuntimeExecutionResult:
    state: RuntimeAttemptState
    chat_id: str | None
    acc: Any | None


@dataclass(slots=True)
class RuntimeToolDirective:
    tool_blocks: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "end_turn"


@dataclass(slots=True)
class RuntimeRetryDirective:
    retry: bool
    next_prompt: str
    reason: str | None = None


@dataclass(slots=True)
class RuntimeRetryContinuation:
    should_continue: bool
    next_prompt: str


@dataclass(slots=True)
class RuntimeRetryLoop:
    prompt: str
    max_attempts: int


@dataclass(slots=True)
class RuntimeAttemptPlan:
    loop: RuntimeRetryLoop
    prompt: str


@dataclass(slots=True)
class AnthropicStreamCompletionResult:
    chunks: list[str]


@dataclass(slots=True)
class AnthropicStreamSuccessResult:
    chunks: list[str]
    usage_delta: int


@dataclass(slots=True)
class RuntimeAttemptOutcome:
    execution: RuntimeExecutionResult
    continuation: RuntimeRetryContinuation


@dataclass(slots=True)
class RuntimeAttemptCursor:
    index: int
    number: int


TRAILING_IDLE_AFTER_TOOL_SECONDS = 2.0


__all__ = [
    "RuntimeAttemptState",
    "RuntimeExecutionResult",
    "RuntimeToolDirective",
    "RuntimeRetryDirective",
    "RuntimeRetryContinuation",
    "RuntimeRetryLoop",
    "RuntimeAttemptPlan",
    "AnthropicStreamCompletionResult",
    "AnthropicStreamSuccessResult",
    "RuntimeAttemptOutcome",
    "RuntimeAttemptCursor",
    "anthropic_stream_stop_reason",
    "anthropic_stream_usage_delta",
    "build_retry_loop",
    "build_tool_directive",
    "build_usage_delta_factory",
    "begin_runtime_attempt",
    "cleanup_runtime_resources",
    "collect_completion_run",
    "collect_completion_run_with_recovery",
    "continue_after_retry_directive",
    "evaluate_retry_directive",
    "extract_blocked_tool_names",
    "finalize_anthropic_stream_success",
    "complete_anthropic_stream_success",
    "has_recent_search_no_results",
    "has_recent_unchanged_read_result",
    "inject_assistant_message",
    "native_tool_calls_to_markup",
    "parse_tool_directive_once",
    "plan_runtime_attempts",
    "recent_same_tool_identity_count",
    "request_max_attempts",
    "retryable_usage_delta",
    "should_force_finish_after_tool_use",
    "tool_identity",
]


def begin_runtime_attempt(attempt_index: int) -> RuntimeAttemptCursor:
    cursor = RuntimeAttemptCursor(index=attempt_index, number=attempt_index + 1)
    update_request_context(stream_attempt=cursor.number)
    return cursor


def should_force_finish_after_tool_use(stop_reason: str, trailing_idle_seconds: float, visible_output_after_tool: bool) -> bool:
    return stop_reason == "tool_use" and trailing_idle_seconds >= TRAILING_IDLE_AFTER_TOOL_SECONDS and not visible_output_after_tool


def extract_blocked_tool_names(text: str, allowed_tool_names: list[str] | None = None) -> list[str]:
    if not text:
        return []
    if "does not exist" not in text.lower():
        return []
    blocked = re.findall(r"Tool\s+([A-Za-z0-9_.:-]+)\s+does not exists?\.?", text)
    if not blocked:
        return []
    if not allowed_tool_names:
        return blocked
    return [normalize_tool_name(name, allowed_tool_names) for name in blocked]


def _recent_message_texts(messages: list[dict[str, Any]] | None, *, limit: int = 10) -> list[str]:
    texts: list[str] = []
    checked = 0
    for msg in reversed(messages or []):
        checked += 1
        content = msg.get("content", "")
        parts: list[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        parts.append(part.get("text", ""))
                    elif part.get("type") == "tool_result":
                        inner = part.get("content", "")
                        if isinstance(inner, str):
                            parts.append(inner)
                        elif isinstance(inner, list):
                            for inner_part in inner:
                                if isinstance(inner_part, dict) and inner_part.get("type") == "text":
                                    parts.append(inner_part.get("text", ""))
                elif isinstance(part, str):
                    parts.append(part)
        merged = "\n".join(text for text in parts if text)
        if merged:
            texts.append(merged)
        if checked >= limit:
            break
    return texts


def has_recent_unchanged_read_result(messages: list[dict[str, Any]] | None) -> bool:
    return any("Unchanged since last read" in text for text in _recent_message_texts(messages))


def has_recent_search_no_results(messages: list[dict[str, Any]] | None) -> bool:
    for text in _recent_message_texts(messages):
        lowered = text.lower()
        if "websearch" not in lowered:
            continue
        if "did 0 searches" in lowered or '"results": []' in lowered or '"matches": []' in lowered:
            return True
    return False


def tool_identity(tool_name: str, tool_input: Any = None) -> str:
    try:
        if tool_name == "Read" and isinstance(tool_input, dict):
            return f"Read::{tool_input.get('file_path', '').strip()}"
        if tool_name == "read" and isinstance(tool_input, dict):
            return f"read::{tool_input.get('path', '').strip()}"
        return f"{tool_name}::{json.dumps(tool_input or {}, ensure_ascii=False, sort_keys=True)}"
    except Exception:
        return tool_name or ""


def recent_same_tool_identity_count(messages: list[dict[str, Any]] | None, tool_name: str, tool_input: Any = None) -> int:
    target = tool_identity(tool_name, tool_input)
    count = 0
    started = False
    for msg in reversed(messages or []):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            if started:
                break
            continue
        tools = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name")]
        if not tools:
            if started:
                break
            continue
        started = True
        if len(tools) == 1 and tool_identity(tools[0].get("name", ""), tools[0].get("input", {})) == target:
            count += 1
            continue
        break
    return count


def has_recent_openai_same_tool_call(history_messages: list[dict[str, Any]] | None, tool_name: str, tool_input: Any = None) -> bool:
    target = tool_identity(tool_name, tool_input)
    for msg in reversed(history_messages or []):
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            continue
        if len(tool_calls) != 1:
            return False
        fn = tool_calls[0].get("function", {}) if isinstance(tool_calls[0], dict) else {}
        name = fn.get("name", "")
        raw_args = fn.get("arguments", "{}")
        try:
            parsed_args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args else raw_args
        except (json.JSONDecodeError, ValueError):
            parsed_args = {"raw": raw_args}
        return tool_identity(name, parsed_args) == target
    return False


def has_invalid_textual_tool_contract(answer_text: str) -> bool:
    if not answer_text:
        return False
    if "##TOOL_CALL##" not in answer_text and "<tool_call>" not in answer_text:
        return False
    compact = answer_text.strip()
    tc_m = re.search(r'##TOOL_CALL##\s*(.*?)\s*##END_CALL##', compact, re.DOTALL | re.IGNORECASE)
    if tc_m:
        try:
            obj = json.loads(tc_m.group(1))
        except (json.JSONDecodeError, ValueError):
            return True
        tool_input = obj.get("input", obj.get("args", obj.get("arguments", obj.get("parameters", {}))))
        return isinstance(tool_input, str)
    xml_m = re.search(r'<tool_call>\s*(.*?)\s*</tool_call>', compact, re.DOTALL | re.IGNORECASE)
    if xml_m:
        try:
            obj = json.loads(xml_m.group(1))
        except (json.JSONDecodeError, ValueError):
            return True
        tool_input = obj.get("input", obj.get("args", obj.get("arguments", obj.get("parameters", {}))))
        return isinstance(tool_input, str)
    return False


def should_retry_textual_tool_contract(answer_text: str) -> bool:
    if not answer_text:
        return False
    if "##TOOL_CALL##" in answer_text or "<tool_call>" in answer_text:
        return True
    return False


def native_tool_calls_to_markup(tool_calls: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for tool_call in tool_calls:
        parts.append(
            f'<tool_call>{{"name": {json.dumps(tool_call["name"])}, "input": {json.dumps(tool_call.get("input", {}), ensure_ascii=False)}}}</tool_call>'
        )
    return "\n".join(parts)


async def run_runtime_attempt(
    *,
    client,
    request: StandardRequest,
    current_prompt: str,
    history_messages: list[dict[str, Any]] | None,
    attempt_index: int,
    max_attempts: int,
    allow_after_visible_output: bool = False,
    capture_events: bool = True,
    on_delta: Callable[[dict[str, Any], str | None, list[dict[str, Any]] | None], Awaitable[None]] | None = None,
) -> RuntimeAttemptOutcome:
    attempt_cursor = begin_runtime_attempt(attempt_index)
    execution = await collect_completion_run(
        client,
        request,
        current_prompt,
        capture_events=capture_events,
        on_delta=on_delta,
    )
    retry = evaluate_retry_directive(
        request=request,
        current_prompt=current_prompt,
        history_messages=history_messages,
        attempt_index=attempt_cursor.index,
        max_attempts=max_attempts,
        state=execution.state,
        allow_after_visible_output=allow_after_visible_output,
    )
    preserve_chat = bool(getattr(request, 'persistent_session', False))
    continuation = await continue_after_retry_directive(
        client=client,
        execution=execution,
        retry=retry,
        preserve_chat=preserve_chat,
    )
    return RuntimeAttemptOutcome(execution=execution, continuation=continuation)


async def collect_completion_run(
    client,
    request: StandardRequest,
    prompt: str,
    *,
    capture_events: bool = True,
    on_delta: Callable[[dict[str, Any], str | None, list[dict[str, Any]] | None], Awaitable[None]] | None = None,
) -> RuntimeExecutionResult:
    chat_id = None
    acc = None
    answer_fragments: list[str] = []
    reasoning_fragments: list[str] = []
    native_tool_calls: list[dict[str, Any]] = []
    tool_state = StreamingToolCallState()
    emitted_visible_output = False
    # 是否已向客户端流出 answer 正文。
    # 不能复用 emitted_visible_output：思考内容(phase=think)也会把它置 True，
    # 而思考总是先于正文到达，会导致下面的"毒性拒绝早期拦截"永久失效，
    # 让 "Tool X does not exists." 直接流给客户端。
    emitted_answer_output = False
    first_event_marked = False
    raw_events: list[dict[str, Any]] = []
    metrics = StreamMetrics()

    # 初始化 Tool Sieve 用于实时检测
    tool_sieve = None
    if request.tools:
        tool_sieve = tool_parser.ToolSieve(request.tool_names)
        log.info("[收集完成] 工具过滤器已启用，工具列表: %s", request.tool_names)

    def _finalize_result(*, reason: str | None = None) -> RuntimeExecutionResult:
        answer_text = "".join(answer_fragments)
        reasoning_text = "".join(reasoning_fragments)
        if native_tool_calls and not answer_text:
            answer_text = native_tool_calls_to_markup(native_tool_calls)

        # 关键修复：强制解析最终文本中的工具调用
        detected_tool_calls = native_tool_calls
        final_finish_reason = "tool_calls" if native_tool_calls else "stop"

        # 第一重：刷新 Tool Sieve
        if tool_sieve and not native_tool_calls:
            flush_events = tool_sieve.flush()
            for evt in flush_events:
                if evt.get("type") == "tool_calls":
                    calls = evt.get("calls", [])
                    if calls:
                        # 转换为标准格式
                        import uuid
                        detected_tool_calls = [{
                            "type": "tool_use",
                            "id": f"toolu_{uuid.uuid4().hex[:8]}",
                            "name": call["name"],
                            "input": call["input"]
                        } for call in calls]
                        final_finish_reason = "tool_calls"
                        log.info(
                            "[Collect] ✓ Tool Sieve 刷新检测到工具调用: tools=%s",
                            [t.get("name") for t in detected_tool_calls],
                        )
                        break
                elif evt.get("type") == "content":
                    # 剩余文本内容
                    pass

        # 第二重：解析最终文本
        if not detected_tool_calls and request.tools and answer_text:
            # 尝试从最终文本中解析工具调用
            tool_blocks, stop_reason = tool_parser.parse_tool_calls_silent(answer_text, request.tools)
            tool_use_blocks = [b for b in tool_blocks if b.get("type") == "tool_use"]

            if tool_use_blocks and stop_reason == "tool_use":
                # 找到工具调用！
                detected_tool_calls = tool_use_blocks
                final_finish_reason = "tool_calls"

                # 从文本中移除工具调用部分
                text_blocks = [b for b in tool_blocks if b.get("type") == "text"]
                if text_blocks:
                    answer_text = text_blocks[0].get("text", "")
                else:
                    answer_text = ""

                log.info(
                    "[Collect] ✓ 最终文本解析检测到工具调用: tools=%s, cleaned_text_len=%s",
                    [t.get("name") for t in detected_tool_calls],
                    len(answer_text),
                )

        # 检查空输出
        if not detected_tool_calls and not answer_text.strip() and not reasoning_text.strip():
            log.warning(
                "[收集完成] 上游返回空输出: 原因=%s 会话=%s",
                reason,
                chat_id,
            )
            # 如果有 reasoning 但没有 visible output，说明模型只输出了思考过程
            if reasoning_text.strip():
                log.warning("[收集完成] 模型只返回了推理内容，没有可见输出")
            # 空响应 flush 该账号池（同批次预热的可能都是坏的），下次走新建
            try:
                pool = getattr(client, "executor", None) and getattr(client.executor, "chat_id_pool", None)
                if pool is not None and acc is not None:
                    import asyncio as _aio
                    _aio.create_task(pool.flush_account(acc.email))
            except Exception:
                pass

        if reason:
            log.info(
                "[收集完成] 原因=%s 会话=%s 工具调用=%s 答复字数=%s 推理字数=%s 结束原因=%s",
                reason,
                chat_id,
                len(detected_tool_calls),
                len(answer_text),
                len(reasoning_text),
                final_finish_reason,
            )
        metrics.mark("stream_finish", float(len(raw_events)))
        state = RuntimeAttemptState(
            answer_text=answer_text,
            reasoning_text=reasoning_text,
            tool_calls=detected_tool_calls,
            blocked_tool_names=extract_blocked_tool_names(answer_text.strip(), request.tool_names),
            finish_reason=final_finish_reason,
            raw_events=raw_events,
            emitted_visible_output=emitted_visible_output,
            stage_metrics=metrics.summary(),
        )
        return RuntimeExecutionResult(state=state, chat_id=chat_id, acc=acc)

    async for item in client.chat_stream_events_with_retry(
        request.resolved_model,
        prompt,
        has_custom_tools=bool(request.tools),
        files=getattr(request, "upstream_files", None),
        fixed_account=getattr(request, "bound_account", None),
        existing_chat_id=getattr(request, "upstream_chat_id", None),
    ):
        if item.get("type") == "meta":
            chat_id = item.get("chat_id")
            acc = item.get("acc")
            update_request_context(chat_id=chat_id)
            metrics.mark("chat_created", float(len(raw_events)))
            continue
        if item.get("type") != "event":
            continue

        evt = item.get("event", {})
        if capture_events:
            raw_events.append(evt)
        if evt.get("type") != "delta":
            continue

        phase = evt.get("phase", "")
        content = evt.get("content", "")

        if phase in ("think", "thinking_summary") and content:
            reasoning_fragments.append(content)
            emitted_visible_output = True
            if not first_event_marked:
                metrics.mark("first_event", float(len(raw_events)))
                first_event_marked = True
            if on_delta is not None:
                await on_delta(evt, content, None)
            continue

        if phase == "answer" and content:
            answer_fragments.append(content)

            # 毒性拒绝早期拦截：Qwen 偶尔幻觉出 "Tool X does not exists." 之类文本。
            # 在标记 emitted_visible_output 之前识别并提前 finalize，让 evaluate_retry_directive
            # 的 blocked_tool_name 分支能正常触发重试（否则 emitted=True 后就不 retry 了）。
            if (
                request.tools
                and not emitted_answer_output
                and len("".join(answer_fragments)) >= 20
            ):
                early_answer = "".join(answer_fragments).strip()
                if _TOXIC_REFUSAL_RE.search(early_answer):
                    toxic_blocked = extract_blocked_tool_names(early_answer, request.tool_names)
                    blocked_name = toxic_blocked[0] if toxic_blocked else "unknown"
                    log.warning(
                        "[收集完成] 污染拦截 %r (未流出客户端，触发重试)",
                        early_answer[:80],
                    )
                    return _finalize_result(reason=f"blocked_tool_name:{blocked_name}")

            emitted_visible_output = True
            emitted_answer_output = True
            if not first_event_marked:
                metrics.mark("first_event", float(len(raw_events)))
                first_event_marked = True

            # Tool Sieve 实时检测
            if tool_sieve:
                sieve_events = tool_sieve.process_chunk(content)
                for sieve_evt in sieve_events:
                    if sieve_evt.get("type") == "tool_calls":
                        # 检测到工具调用！
                        calls = sieve_evt.get("calls", [])
                        if calls:
                            import uuid
                            detected_calls = [{
                                "type": "tool_use",
                                "id": f"toolu_{uuid.uuid4().hex[:8]}",
                                "name": call["name"],
                                "input": call["input"]
                            } for call in calls]
                            native_tool_calls.extend(detected_calls)
                            log.info(
                                "[Collect] ✓ Tool Sieve 实时检测到工具调用: tools=%s",
                                [c.get("name") for c in detected_calls],
                            )
                            return _finalize_result(reason="tool_sieve_detected")

            if on_delta is not None:
                await on_delta(evt, content, None)
            if request.tools:
                answer_text = "".join(answer_fragments)
                if len(answer_fragments) % 3 == 0 or "does not exist" in content.lower():
                    blocked_tool_names = extract_blocked_tool_names(answer_text.strip(), request.tool_names)
                    if blocked_tool_names:
                        return _finalize_result(reason=f"blocked_tool_name:{blocked_tool_names[0]}")
                if "##TOOL_CALL##" in answer_text or "<tool_call>" in answer_text:
                    directive = parse_tool_directive_once(
                        request,
                        RuntimeAttemptState(answer_text=answer_text, reasoning_text="".join(reasoning_fragments)),
                    )
                    if directive.stop_reason == "tool_use":
                        return _finalize_result(reason="textual_tool_use")
            continue

        if phase == "tool_call":
            emitted_visible_output = True
            if not first_event_marked:
                metrics.mark("first_event", float(len(raw_events)))
                first_event_marked = True
            completed_calls = tool_state.process_event(evt)
            if completed_calls:
                native_tool_calls.extend(completed_calls)
                if on_delta is not None:
                    await on_delta(evt, None, completed_calls)
                return _finalize_result(reason="native_tool_use")

    return _finalize_result(reason="stream_end")


def parse_tool_directive_once(request: StandardRequest, state: RuntimeAttemptState) -> RuntimeToolDirective:
    if state.tool_calls:
        return RuntimeToolDirective(
            tool_blocks=[
                {
                    "type": "tool_use",
                    "id": tool_call["id"],
                    "name": normalize_tool_name(tool_call["name"], request.tool_names),
                    "input": tool_call.get("input", {}),
                }
                for tool_call in state.tool_calls
            ],
            stop_reason="tool_use",
        )

    if request.tools and state.answer_text:
        tool_blocks, stop_reason = tool_parser.parse_tool_calls_silent(state.answer_text, request.tools)
        return RuntimeToolDirective(tool_blocks=tool_blocks, stop_reason=stop_reason)

    return RuntimeToolDirective(tool_blocks=[{"type": "text", "text": state.answer_text}], stop_reason="end_turn")


# ==================== 截断续写 + 流式 warmup（P2-6 & P2-10 接入）====================

async def collect_completion_run_with_recovery(
    client,
    request: StandardRequest,
    prompt: str,
    *,
    capture_events: bool = True,
    on_delta: Callable[[dict[str, Any], str | None, list[dict[str, Any]] | None], Awaitable[None]] | None = None,
    max_continuation: int = 2,
    warmup_chars: int = 0,
    guard_chars: int = 0,
) -> RuntimeExecutionResult:
    """collect_completion_run 的增强版，叠加两个能力：

    1. **截断自动续写**（P2-6）：初始流结束若 answer_text 含未闭合的 ##TOOL_CALL## 块，
       最多追加 max_continuation 次续写请求，每次丢弃工具定义省上下文预算，
       用 deduplicate_continuation 去除头尾重叠后拼接。

    2. **流式 warmup/guard**（P2-10）：当 warmup_chars>0 时，用 IncrementalTextStreamer
       包装 on_delta 的 text 路径——起步累积 warmup_chars 字符后才放行，
       任何时刻保留末尾 guard_chars 字符暂不发出，给跨 chunk 清洗预留空间。

    保留向后兼容：不传可选参数 → 行为与 collect_completion_run 完全一致。
    """
    from backend.services.truncation_recovery import (
        build_continuation_prompt,
        deduplicate_continuation,
        is_truncated,
    )
    from backend.services.incremental_text_streamer import IncrementalTextStreamer

    wrapped_on_delta = on_delta
    streamer: IncrementalTextStreamer | None = None
    if warmup_chars > 0 and on_delta is not None:
        streamer = IncrementalTextStreamer(
            warmup_chars=warmup_chars,
            guard_chars=max(guard_chars, 64),
        )

        async def _wrapped(evt, text_chunk, tool_calls):
            # 仅对纯文本 delta 做 warmup；tool_calls / thinking / native 直接透传
            if text_chunk is None or tool_calls is not None or evt.get("phase") not in ("answer", "text"):
                await on_delta(evt, text_chunk, tool_calls)
                return
            released = streamer.push(text_chunk)
            if released:
                await on_delta(evt, released, None)

        wrapped_on_delta = _wrapped

    result = await collect_completion_run(
        client, request, prompt,
        capture_events=capture_events,
        on_delta=wrapped_on_delta,
    )

    # 若 warmup 还保留着尾部，flush 出去
    if streamer is not None and on_delta is not None:
        tail = streamer.finish()
        if tail:
            await on_delta({"phase": "answer"}, tail, None)

    # 截断续写
    continues = 0
    while continues < max_continuation:
        state = result.state
        # 有已检出的工具调用就不续写（让客户端去执行那个 tool）
        if state.tool_calls:
            break
        # 没 tools 的场景不做续写（纯聊天，截断检测无 ##TOOL_CALL## 参考点会误触发）
        if not request.tools:
            break
        if not is_truncated(state.answer_text):
            break

        continues += 1
        log.info(
            "[TruncRecover] detected unclosed tool call, continuation attempt=%d chat_id=%s len=%d",
            continues, result.chat_id, len(state.answer_text),
        )

        assistant_ctx, followup = build_continuation_prompt(state.answer_text, anchor_chars=2000)
        # 续写 prompt = 原 prompt + assistant 已输出的锚点 + user 续写指令
        cont_prompt = (
            f"{prompt.rstrip()}\n\nAssistant: {assistant_ctx}\n\nHuman: {followup}\n\nAssistant:"
        )

        cont_result = await collect_completion_run(
            client, request, cont_prompt,
            capture_events=False,
            on_delta=on_delta,  # 不经过 streamer，续写内容直接透传
        )
        cont_text = cont_result.state.answer_text
        if not cont_text or not cont_text.strip():
            log.info("[TruncRecover] empty continuation, stopping")
            break

        deduped = deduplicate_continuation(state.answer_text, cont_text)
        if not deduped.strip():
            log.info("[TruncRecover] continuation fully overlapped existing, stopping")
            break

        merged_answer = state.answer_text + deduped
        merged_state = RuntimeAttemptState(
            answer_text=merged_answer,
            reasoning_text=state.reasoning_text,
            tool_calls=cont_result.state.tool_calls or state.tool_calls,
            blocked_tool_names=cont_result.state.blocked_tool_names or state.blocked_tool_names,
            finish_reason=cont_result.state.finish_reason or state.finish_reason,
            raw_events=state.raw_events,
            emitted_visible_output=state.emitted_visible_output or cont_result.state.emitted_visible_output,
            stage_metrics=state.stage_metrics,
        )
        result = RuntimeExecutionResult(state=merged_state, chat_id=result.chat_id, acc=result.acc)
        log.info(
            "[TruncRecover] continuation=%d produced %d new chars; total=%d",
            continues, len(deduped), len(merged_answer),
        )
        # 若续写完成后已闭合则收工
        if not is_truncated(merged_answer):
            break

    return result


def build_tool_directive(
    request: StandardRequest,
    state: RuntimeAttemptState,
) -> RuntimeToolDirective:
    directive = parse_tool_directive_once(request, state)
    log.info(
        f"[ToolDirective] tool_blocks={len(directive.tool_blocks)} stop_reason={directive.stop_reason} "
        f"has_tool_use={any(b.get('type') == 'tool_use' for b in directive.tool_blocks)}"
    )
    return directive


def anthropic_stream_usage_delta(prompt: str, answer_text: str) -> int:
    return len(answer_text) + len(prompt)


def anthropic_stream_stop_reason(request: StandardRequest, state: RuntimeAttemptState, pending_chunks: list[str]) -> str:
    if state.tool_calls or any('"type": "tool_use"' in chunk for chunk in pending_chunks):
        return "tool_use"
    return build_tool_directive(request, state).stop_reason


def finalize_anthropic_stream_success(*, request: StandardRequest, prompt: str, execution: RuntimeExecutionResult, translator) -> AnthropicStreamSuccessResult:
    stop_reason = anthropic_stream_stop_reason(request, execution.state, translator.pending_chunks)
    chunks = translator.finalize(answer_text=execution.state.answer_text, stop_reason=stop_reason)
    return AnthropicStreamSuccessResult(
        chunks=chunks,
        usage_delta=anthropic_stream_usage_delta(prompt, execution.state.answer_text),
    )


async def complete_anthropic_stream_success(*, users_db, token: str, client, prompt: str, request: StandardRequest, execution: RuntimeExecutionResult, translator) -> AnthropicStreamCompletionResult:
    from backend.services.auth_quota import add_used_tokens

    stream_success = finalize_anthropic_stream_success(
        request=request,
        prompt=prompt,
        execution=execution,
        translator=translator,
    )
    await add_used_tokens(users_db, token, stream_success.usage_delta)
    await cleanup_runtime_resources(client, execution.acc, execution.chat_id)
    return AnthropicStreamCompletionResult(chunks=stream_success.chunks)


def inject_assistant_message(prompt: str, message: str) -> str:
    next_prompt = prompt.rstrip()
    if next_prompt.endswith("Assistant:"):
        return next_prompt[:-len("Assistant:")] + message + "\nAssistant:"
    return next_prompt + "\n\n" + message + "\nAssistant:"


def retryable_usage_delta(prompt: str):
    return lambda execution, current_prompt=None: len(execution.state.answer_text) + len(current_prompt or prompt)


def build_usage_delta_factory(prompt: str) -> Callable[[RuntimeExecutionResult, Any | None], int]:
    return lambda execution, current_prompt=None: len(execution.state.answer_text) + len(current_prompt or prompt)


def request_max_attempts(request: StandardRequest) -> int:
    # 工具模式下给模型更多重试机会（毒性幻觉/重复调用场景常见），
    # 原值 2 在多轮 retry 里太容易用完，升到 4
    return 4 if request.tools else settings.MAX_RETRIES


def plan_runtime_attempts(request: StandardRequest, *, initial_prompt: str) -> RuntimeAttemptPlan:
    loop = build_retry_loop(request, initial_prompt=initial_prompt)
    return RuntimeAttemptPlan(loop=loop, prompt=loop.prompt)


def build_retry_loop(request: StandardRequest, *, initial_prompt: str) -> RuntimeRetryLoop:
    return RuntimeRetryLoop(
        prompt=initial_prompt,
        max_attempts=request_max_attempts(request),
    )


def evaluate_retry_directive(
    *,
    request: StandardRequest,
    current_prompt: str,
    history_messages: list[dict[str, Any]] | None,
    attempt_index: int,
    max_attempts: int,
    state: RuntimeAttemptState,
    allow_after_visible_output: bool = False,
) -> RuntimeRetryDirective:
    if attempt_index >= max_attempts - 1:
        return RuntimeRetryDirective(retry=False, next_prompt=current_prompt, reason=None)

    can_retry_after_output = allow_after_visible_output or not state.emitted_visible_output

    def _retry(reason: str, next_prompt: str) -> RuntimeRetryDirective:
        log.info(
            "[重试] 原因=%s 第%s/%s次 客户端=%s 屏蔽=%s 结束原因=%s 已流出=%s",
            reason,
            attempt_index + 1,
            max_attempts,
            getattr(request, "client_profile", "-"),
            state.blocked_tool_names[:3],
            state.finish_reason,
            state.emitted_visible_output,
        )
        return RuntimeRetryDirective(retry=True, next_prompt=next_prompt, reason=reason)

    if state.blocked_tool_names and request.tools:
        if not can_retry_after_output:
            return RuntimeRetryDirective(retry=False, next_prompt=current_prompt, reason=None)
        blocked_name = normalize_tool_name(state.blocked_tool_names[0], request.tool_names)
        return _retry(
            f"blocked_tool_name:{blocked_name}",
            tool_parser.inject_format_reminder(
                current_prompt,
                blocked_name,
                client_profile=getattr(request, "client_profile", CLAUDE_CODE_OPENAI_PROFILE),
            ),
        )

    if request.tools:
        directive: RuntimeToolDirective | None = None
        if state.answer_text:
            saw_contract_markup = should_retry_textual_tool_contract(state.answer_text)
            if saw_contract_markup and can_retry_after_output:
                if has_invalid_textual_tool_contract(state.answer_text):
                    fallback_tool_name = request.tool_names[0] if request.tool_names else "tool"
                    return _retry(
                        f"invalid_textual_tool_contract:{fallback_tool_name}",
                        tool_parser.inject_format_reminder(
                            current_prompt,
                            fallback_tool_name,
                            client_profile=getattr(request, "client_profile", CLAUDE_CODE_OPENAI_PROFILE),
                        ),
                    )
                directive = parse_tool_directive_once(request, state)
                if directive.stop_reason != "tool_use":
                    fallback_tool_name = request.tool_names[0] if request.tool_names else "tool"
                    return _retry(
                        f"unparsed_textual_tool_contract:{fallback_tool_name}",
                        tool_parser.inject_format_reminder(
                            current_prompt,
                            fallback_tool_name,
                            client_profile=getattr(request, "client_profile", CLAUDE_CODE_OPENAI_PROFILE),
                        ),
                    )
        if directive is None:
            directive = parse_tool_directive_once(request, state)
        if directive.stop_reason == "tool_use":
            first_tool = next((b for b in directive.tool_blocks if b.get("type") == "tool_use"), None)
            if first_tool:
                repeated_same_tool = False
                if getattr(request, "client_profile", CLAUDE_CODE_OPENAI_PROFILE) == "openclaw_openai":
                    repeated_same_tool = has_recent_openai_same_tool_call(
                        history_messages,
                        first_tool.get("name", ""),
                        first_tool.get("input", {}),
                    )
                else:
                    repeated_same_tool = recent_same_tool_identity_count(
                        history_messages,
                        first_tool.get("name", ""),
                        first_tool.get("input", {}),
                    ) >= 1
                if repeated_same_tool and can_retry_after_output:
                    force_text = (
                        f"[强制要求]: 你已经用相同参数调用了 {first_tool.get('name')}。"
                        "不要重复相同的工具调用。"
                        "使用已有的工具结果，选择下一个相关工具或完成任务。"
                        "如果是配置文件任务，读取一次后直接编辑/写入文件，不要重复读取。"
                        f"\n[MANDATORY]: You already called {first_tool.get('name')} with the same input. "
                        "Do NOT repeat the same tool call. "
                        "Use the tool result you already have and either choose the next relevant tool or finish the task. "
                        "If this is a config-file task, read once and then edit/write the file instead of rereading it."
                    )
                    return _retry(
                        f"repeated_same_tool:{first_tool.get('name', '')}",
                        inject_assistant_message(current_prompt, force_text),
                    )
            if (
                first_tool
                and first_tool.get("name") == "Read"
                and has_recent_unchanged_read_result(history_messages)
            ):
                if can_retry_after_output:
                    force_text = (
                        "[强制要求]: 你刚收到'Unchanged since last read'（文件未改变）。"
                        "不要再次读取同一个文件。"
                        "现在选择其他工具或完成任务。"
                        "\n[MANDATORY]: You just received 'Unchanged since last read'. "
                        "Do NOT call Read again. Choose another tool or finish the task."
                    )
                    return _retry(
                        "unchanged_read_result",
                        inject_assistant_message(current_prompt, force_text),
                    )
                else:
                    log.warning(f"[Runtime] Blocked repeated Read after 'Unchanged since last read', but cannot retry")

            # 防止自动调用Agent工具
            if (
                first_tool
                and first_tool.get("name") == "Agent"
                and can_retry_after_output
            ):
                # 检查用户消息中是否明确提到agent相关词汇
                user_mentioned_agent = False
                for msg in reversed(history_messages or []):
                    if msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            text = content.lower()
                        elif isinstance(content, list):
                            text = " ".join(
                                part.get("text", "").lower()
                                for part in content
                                if isinstance(part, dict) and part.get("type") == "text"
                            )
                        else:
                            text = ""
                        if any(keyword in text for keyword in ["agent", "代理", "子任务", "subtask", "background"]):
                            user_mentioned_agent = True
                        break

                if not user_mentioned_agent:
                    force_text = (
                        "[强制要求]: 不要自动调用Agent工具。用户没有要求使用代理或子任务。"
                        "请直接完成用户的请求，使用Read/Write/Edit等工具。"
                        "\n[MANDATORY]: Do NOT call Agent tool automatically. User did not request agent or subtask. "
                        "Complete the user's request directly using Read/Write/Edit tools."
                    )
                    return _retry(
                        "auto_agent_blocked",
                        inject_assistant_message(current_prompt, force_text),
                    )

            if (
                first_tool
                and first_tool.get("name") == "WebSearch"
                and has_recent_search_no_results(history_messages)
                and can_retry_after_output
            ):
                force_text = (
                    "[强制要求]: 上次WebSearch没有返回结果。"
                    "不要用类似的词再次调用WebSearch。"
                    "使用其他工具或用现有信息完成回答。"
                    "\n[MANDATORY]: The last WebSearch returned no results. "
                    "Do NOT call WebSearch again with similar wording. "
                    "Use another tool or finish with the best available answer."
                )
                return _retry(
                    "search_no_results",
                    inject_assistant_message(current_prompt, force_text),
                )

    # 空响应重试：上游返回 answer_chars=0 tool_calls=0 finish_reason=stop
    # 典型场景是 Qwen 后端对某个 chat_id 返回空（常见于池化 chat_id 刚建好就被用、
    # 或 Qwen 服务抖动时）。换账号 + 换新 chat_id 再试一次。
    if (
        not state.answer_text
        and not state.tool_calls
        and state.finish_reason == "stop"
        and not state.emitted_visible_output
    ):
        return _retry(
            "empty_upstream_response",
            current_prompt,  # prompt 不变，让上游重新处理
        )

    return RuntimeRetryDirective(retry=False, next_prompt=current_prompt, reason=None)


async def continue_after_retry_directive(*, client, execution, retry: RuntimeRetryDirective, preserve_chat: bool = False) -> RuntimeRetryContinuation:
    if not retry.retry:
        return RuntimeRetryContinuation(should_continue=False, next_prompt=retry.next_prompt)
    await cleanup_runtime_resources(client, execution.acc, execution.chat_id, preserve_chat=preserve_chat)
    if not preserve_chat:
        await asyncio.sleep(0.15)
    return RuntimeRetryContinuation(should_continue=True, next_prompt=retry.next_prompt)


async def cleanup_runtime_resources(client, acc, chat_id: str | None, *, preserve_chat: bool = False) -> None:
    if acc is None:
        return
    token = getattr(acc, "token", None)
    client.account_pool.release(acc)
    if preserve_chat:
        return
    if chat_id and token:
        async def _delete_chat_later() -> None:
            try:
                await client.delete_chat(token, chat_id)
            except Exception as exc:
                log.debug("[Cleanup] delete_chat failed chat_id=%s error=%s", chat_id, exc)
        asyncio.create_task(_delete_chat_later())
