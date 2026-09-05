from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from backend.adapter.standard_request import CLAUDE_CODE_OPENAI_PROFILE, StandardRequest
from backend.services.prompt_builder import _extract_text, _extract_user_text_only, _render_history_tool_call

log = logging.getLogger("qwen2api.task_session")


@dataclass(slots=True)
class SessionHistoryEntry:
    digest: str
    rendered: str


@dataclass(slots=True)
class PersistentSessionPlan:
    enabled: bool
    reuse_chat: bool
    prompt: str
    full_prompt: str
    current_hashes: list[str]
    existing_chat_id: str | None
    account_email: str | None
    reason: str | None = None
    existing_hash_count: int = 0
    new_entries_count: int = 0


def should_use_persistent_tool_session(request: StandardRequest) -> bool:  # noqa: ARG001
    # 禁用会话复用，对齐 ds2api 架构，避免续接 prompt 导致模型返回空输出
    # 每次请求都创建新的 chat，让上游自动管理会话历史
    return False


def persistent_session_disabled_reason(request: StandardRequest) -> str:  # noqa: ARG001
    # 会话复用已全局禁用
    return "session_reuse_disabled_globally"


def _preview_identifier(value: str | None, *, head: int = 8, tail: int = 6) -> str:
    if not value:
        return "-"
    text = str(value)
    if len(text) <= head + tail + 3:
        return text
    return f"{text[:head]}...{text[-tail:]}"


def _log_session_plan(*, request: StandardRequest, surface: str, plan: PersistentSessionPlan) -> None:
    log.info(
        "[SessionPlan] surface=%s enabled=%s reuse_chat=%s reason=%s session=%s existing_chat=%s current_hashes=%s existing_hashes=%s new_entries=%s account=%s profile=%s tools=%s",
        surface,
        plan.enabled,
        plan.reuse_chat,
        plan.reason or '-',
        _preview_identifier(getattr(request, 'session_key', None)),
        _preview_identifier(plan.existing_chat_id),
        len(plan.current_hashes),
        plan.existing_hash_count,
        plan.new_entries_count,
        plan.account_email or getattr(request, 'bound_account_email', None) or '-',
        getattr(request, 'client_profile', '-'),
        list(getattr(request, 'tool_names', []) or [])[:6],
    )


def log_session_plan_reuse_cancelled(*, request: StandardRequest, planned_chat_id: str | None, reason: str) -> None:
    log.warning(
        "[SessionPlan] surface=%s reuse_chat_cancelled reason=%s session=%s planned_chat=%s account=%s",
        getattr(request, 'surface', '-'),
        reason,
        _preview_identifier(getattr(request, 'session_key', None)),
        _preview_identifier(planned_chat_id),
        getattr(request, 'bound_account_email', None) or '-',
    )


def _assistant_tool_call_markup(message: dict[str, Any], client_profile: str) -> str:
    tc_parts: list[str] = []
    for tc in message.get("tool_calls", []) or []:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        name = fn.get("name", "")
        args_str = fn.get("arguments", "{}")
        try:
            args = json.loads(args_str)
        except Exception:
            args = {"raw": args_str}
        tc_parts.append(_render_history_tool_call(name, args, client_profile))
    return "\n".join(part for part in tc_parts if part)


def render_session_message(message: dict[str, Any], *, client_profile: str, tools_enabled: bool) -> str:
    role = message.get("role", "")
    if role not in ("user", "assistant", "system", "tool"):
        return ""

    if role == "tool":
        tool_content = message.get("content", "") or ""
        tool_call_id = str(message.get("tool_call_id", "") or "")
        if isinstance(tool_content, list):
            tool_content = "\n".join(
                p.get("text", "")
                for p in tool_content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        elif not isinstance(tool_content, str):
            tool_content = str(tool_content)
        if not tool_content.strip():
            return ""
        return f"[Tool Result]{(' id=' + tool_call_id) if tool_call_id else ''}\n{tool_content}\n[/Tool Result]"

    user_text_only = _extract_user_text_only(message.get("content", ""), client_profile=client_profile) if role == "user" else ""
    text = _extract_text(
        message.get("content", ""),
        user_tool_mode=(tools_enabled and role == "user" and client_profile == CLAUDE_CODE_OPENAI_PROFILE),
        client_profile=client_profile,
    )

    if role == "assistant" and not text and message.get("tool_calls"):
        text = _assistant_tool_call_markup(message, client_profile)

    if not str(text or "").strip():
        return ""

    is_tool_result_only_user_msg = role == "user" and not user_text_only.strip() and bool(str(text).strip())
    prefix = "" if is_tool_result_only_user_msg else {
        "user": "Human: ",
        "assistant": "Assistant: ",
        "system": "System: ",
    }.get(role, "")
    return text if is_tool_result_only_user_msg else f"{prefix}{text}"


def extract_session_history_entries(messages: list[dict[str, Any]], *, client_profile: str, tools_enabled: bool) -> list[SessionHistoryEntry]:
    entries: list[SessionHistoryEntry] = []
    for message in messages or []:
        rendered = render_session_message(message, client_profile=client_profile, tools_enabled=tools_enabled).strip()
        if not rendered:
            continue
        digest = hashlib.sha256(rendered.encode("utf-8", errors="ignore")).hexdigest()
        entries.append(SessionHistoryEntry(digest=digest, rendered=rendered))
    return entries


def build_continuation_prompt(
    new_entries: list[SessionHistoryEntry],
    *,
    tool_names: list[str],
    tools: list[dict] | None = None
) -> str:
    """构建会话续接 prompt，包含精简工具定义以避免 Qwen 拦截"""

    # 构建精简工具定义
    if tools:
        tool_lines = []
        for tool in tools:
            name = tool.get("name", "")
            if not name:
                continue

            # 提取参数列表（最多显示 5 个）
            params = tool.get("input_schema", {}).get("properties", {})
            param_keys = list(params.keys())[:5]

            if param_keys:
                tool_lines.append(f"- {name}: {', '.join(param_keys)}")
            else:
                tool_lines.append(f"- {name}")

        tool_definitions = '\n'.join(tool_lines) if tool_lines else f"Available tools: {', '.join(tool_names[:12])}"
    else:
        # 降级：只显示工具名称（保持向后兼容）
        tool_definitions = f"Available tools: {', '.join(tool_names[:12]) if tool_names else 'the available tools'}"

    lines = [
        '=== SAME TASK SESSION CONTINUATION ===',
        'This is the same ongoing task in the same upstream chat.',
        'Earlier context already exists in the conversation history of this chat.',
        'Process ONLY the new items below and continue the task from there.',
        '',
        'CRITICAL TOOL CALL FORMAT - MUST USE ##TOOL_CALL## MARKERS:',
        '##TOOL_CALL##',
        '{"name": "EXACT_TOOL_NAME", "input": {"param": "value"}}',
        '##END_CALL##',
        '',
        '=== AVAILABLE TOOLS ===',
        tool_definitions,
        '',
        'EXECUTION RULES:',
        '- When you need to call a tool, output EXACTLY the ##TOOL_CALL##...##END_CALL## format above',
        '- NO other format will work - pure JSON without markers will be INTERCEPTED',
        '- If you just received a tool result, use it to continue the task immediately',
        '- Do NOT call Agent, AskUserQuestion, EnterPlanMode, ExitPlanMode, EnterWorktree, ExitWorktree',
        '- Do NOT ask questions or wait - execute the task directly',
        '- Continue from where you left off based on the tool results',
        '',
        'Do not repeat the same Read/Bash/WebSearch call if the result is already available in this chat.',
        '',
        'NEW ITEMS:',
    ]

    if new_entries:
        lines.extend(entry.rendered for entry in new_entries)
    else:
        lines.append('No new client messages were appended. Continue from the current chat state and produce the next best response for the current task.')

    lines.extend(['', 'Assistant:'])
    return '\n'.join(lines)


def build_retry_rebase_prompt(request: StandardRequest, *, reason: str | None = None) -> str:
    base = (request.full_prompt or request.prompt or '').rstrip()
    guidance = (
        '[MANDATORY NEXT STEP]: Continue this task from scratch using the tool contract exactly. '
        'Do NOT repeat the previous malformed, redundant, or already-resolved step. '
        'If the needed tool result is already in the provided history, use it and choose the next action or finish.'
    )
    if reason:
        if reason.startswith('repeated_same_tool:'):
            tool_name = reason.split(':', 1)[1] or 'the same tool'
            guidance = (
                f'[MANDATORY NEXT STEP]: You already called {tool_name} with the same input. '
                'Do NOT repeat it. Use the existing result and move to the next relevant step or finish the task.'
            )
        elif reason == 'unchanged_read_result':
            guidance = (
                "[MANDATORY NEXT STEP]: You already received 'Unchanged since last read'. "
                'Do NOT call Read again on the same target. Use the current result and choose a different next action.'
            )
        elif reason == 'search_no_results':
            guidance = (
                '[MANDATORY NEXT STEP]: The last WebSearch returned no results. '
                'Do NOT repeat the same search. Use another tool or answer with the best available information.'
            )
    if base.endswith('Assistant:'):
        return base[:-len('Assistant:')] + guidance + '\nAssistant:'
    return base + '\n\n' + guidance + '\nAssistant:'


async def plan_persistent_session_turn(*, app, request: StandardRequest, payload: dict[str, Any], surface: str) -> PersistentSessionPlan:
    full_prompt = request.prompt
    entries = extract_session_history_entries(
        payload.get('messages', []) or [],
        client_profile=request.client_profile,
        tools_enabled=bool(request.tools),
    )
    current_hashes = [entry.digest for entry in entries]

    if not should_use_persistent_tool_session(request):
        plan = PersistentSessionPlan(
            enabled=False,
            reuse_chat=False,
            prompt=full_prompt,
            full_prompt=full_prompt,
            current_hashes=current_hashes,
            existing_chat_id=None,
            account_email=request.bound_account_email,
            reason=persistent_session_disabled_reason(request),
            existing_hash_count=0,
            new_entries_count=len(entries),
        )
        _log_session_plan(request=request, surface=surface, plan=plan)
        return plan

    record = await app.state.session_affinity.get(request.session_key or '')
    existing_hashes = list(record.message_hashes) if record else []
    existing_chat_id = record.chat_id if record else None
    account_email = request.bound_account_email or (record.account_email if record else None)

    if not record or not existing_chat_id or not existing_hashes:
        plan = PersistentSessionPlan(
            enabled=True,
            reuse_chat=False,
            prompt=full_prompt,
            full_prompt=full_prompt,
            current_hashes=current_hashes,
            existing_chat_id=None,
            account_email=account_email,
            reason='new_session',
            existing_hash_count=len(existing_hashes),
            new_entries_count=len(entries),
        )
        _log_session_plan(request=request, surface=surface, plan=plan)
        return plan

    if len(current_hashes) < len(existing_hashes) or current_hashes[:len(existing_hashes)] != existing_hashes:
        plan = PersistentSessionPlan(
            enabled=True,
            reuse_chat=False,
            prompt=full_prompt,
            full_prompt=full_prompt,
            current_hashes=current_hashes,
            existing_chat_id=None,
            account_email=account_email,
            reason='history_desync',
            existing_hash_count=len(existing_hashes),
            new_entries_count=len(entries),
        )
        _log_session_plan(request=request, surface=surface, plan=plan)
        return plan

    new_entries = entries[len(existing_hashes):]
    plan = PersistentSessionPlan(
        enabled=True,
        reuse_chat=True,
        prompt=build_continuation_prompt(
            new_entries,
            tool_names=request.tool_names,
            tools=request.tools
        ),
        full_prompt=full_prompt,
        current_hashes=current_hashes,
        existing_chat_id=existing_chat_id,
        account_email=account_email,
        reason='reuse_chat',
        existing_hash_count=len(existing_hashes),
        new_entries_count=len(new_entries),
    )
    _log_session_plan(request=request, surface=surface, plan=plan)
    return plan


def build_anthropic_assistant_history_message(*, execution, request: StandardRequest, directive) -> dict[str, Any]:
    del request
    content_blocks: list[dict[str, Any]] = []
    for block in directive.tool_blocks:
        if block.get('type') == 'thinking':
            continue
        content_blocks.append(block)
    if directive.stop_reason != 'tool_use' and execution.state.answer_text and not content_blocks:
        content_blocks.append({"type": "text", "text": execution.state.answer_text})
    return {"role": "assistant", "content": content_blocks}


def build_openai_assistant_history_message(*, execution, request: StandardRequest, directive) -> dict[str, Any]:
    del request
    if directive.stop_reason == 'tool_use':
        tool_calls = [
            {
                'id': block['id'],
                'type': 'function',
                'function': {
                    'name': block['name'],
                    'arguments': json.dumps(block.get('input', {}), ensure_ascii=False),
                },
            }
            for block in directive.tool_blocks
            if block.get('type') == 'tool_use'
        ]
        return {'role': 'assistant', 'content': None, 'tool_calls': tool_calls}
    return {'role': 'assistant', 'content': execution.state.answer_text}


def extend_hashes_with_assistant(*, current_hashes: list[str], assistant_message: dict[str, Any], request: StandardRequest) -> list[str]:
    entries = extract_session_history_entries([assistant_message], client_profile=request.client_profile, tools_enabled=bool(request.tools))
    if not entries:
        return list(current_hashes)
    return list(current_hashes) + [entry.digest for entry in entries]


async def persist_session_turn(*, app, request: StandardRequest, surface: str, execution, assistant_message: dict[str, Any]) -> None:
    if not getattr(request, 'persistent_session', False):
        return
    if not getattr(request, 'session_key', None):
        return
    if execution is None or not getattr(execution, 'chat_id', None) or getattr(execution, 'acc', None) is None:
        return
    ttl_seconds = app.state.context_offloader.settings.CONTEXT_ATTACHMENT_TTL_SECONDS
    synced_hashes = extend_hashes_with_assistant(
        current_hashes=getattr(request, 'session_message_hashes', []),
        assistant_message=assistant_message,
        request=request,
    )
    bound_email = getattr(execution.acc, 'email', '') or getattr(request, 'bound_account_email', '') or ''
    await app.state.session_affinity.bind_chat(
        request.session_key,
        surface=surface,
        account_email=bound_email,
        chat_id=execution.chat_id,
        message_hashes=synced_hashes,
        ttl_seconds=ttl_seconds,
    )
    log.info(
        "[SessionPlan] persisted surface=%s session=%s chat=%s hashes=%s account=%s",
        surface,
        _preview_identifier(request.session_key),
        _preview_identifier(getattr(execution, 'chat_id', None)),
        len(synced_hashes),
        bound_email or '-',
    )
    request.session_chat_invalidated = False


async def clear_invalidated_session_chat(*, app, request: StandardRequest) -> None:
    if not getattr(request, 'session_chat_invalidated', False):
        return
    if not getattr(request, 'session_key', None):
        return
    await app.state.session_affinity.clear_chat(request.session_key)
    log.warning(
        "[SessionPlan] cleared_invalidated_chat surface=%s session=%s",
        getattr(request, 'surface', '-'),
        _preview_identifier(request.session_key),
    )
    request.session_chat_invalidated = False
