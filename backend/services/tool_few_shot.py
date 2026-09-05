"""Namespace-based few-shot injection：让模型学会调用所有类别的工具。

动机：
    当用户有 90+ 工具（含 MCP、Skills、Plugin 等多个命名空间），Qwen/上游只看
    prompt 里的指令列表时，经常只会调用几个它"熟悉"的核心工具（Read/Write/Bash），
    MCP/Skills 类的第三方工具几乎不会被主动调用。
    原因：模型倾向于**模仿 few-shot 里见过的调用**。如果历史里没有 MCP 工具的
    调用示例，它就不会尝试 MCP 工具。

做法：
    - 按命名空间把工具分组（mcp__playwright / mcp__memory / mcp__context7 / ...）
    - 每个命名空间选一个代表工具（描述最长的那个）
    - 构造一条合成的 [user→assistant] 对话，assistant 展示**多个不同类别**的工具
      同时被调用（1 个核心工具 + 最多 4 个第三方代表）
    - 这条合成对话插在真实用户请求之前

效果：模型看到"assistant 在第一步就用了 5 种不同类型的工具"，会复现这种多样性。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 核心工具名（Claude Code / Cursor / Codex 通用）——视为"基础能力"
_CORE_TOOL_PATTERNS = [
    re.compile(r"^(Read|read_file|ReadFile)$", re.IGNORECASE),
    re.compile(r"^(Write|write_to_file|WriteFile|write_file)$", re.IGNORECASE),
    re.compile(r"^(Bash|execute_command|RunCommand|run_command)$", re.IGNORECASE),
    re.compile(r"^(ListDir|list_dir|list_directory|ListDirectory|list_files)$", re.IGNORECASE),
    re.compile(r"^(Search|search_files|SearchFiles|grep_search|codebase_search|Grep|Glob)$", re.IGNORECASE),
    re.compile(r"^(Edit|edit_file|EditFile|replace_in_file)$", re.IGNORECASE),
    re.compile(r"^(attempt_completion|ask_followup_question|AskFollowupQuestion)$", re.IGNORECASE),
]


def _is_core_tool(name: str) -> bool:
    return any(p.match(name) for p in _CORE_TOOL_PATTERNS)


def _tool_namespace(name: str) -> str:
    """推断工具的命名空间，用于分组。"""
    if not name:
        return ""
    # mcp__playwright__click → mcp__playwright
    m = re.match(r"^(mcp__[^_]+)", name)
    if m:
        return m.group(1)
    # foo__bar__baz → foo
    m = re.match(r"^([^_]+)__", name)
    if m:
        return m.group(1)
    # snake_case：以首段为 namespace（前提至少 3 段，避免 "read_file" 被归为 "read"）
    parts = name.split("_")
    if len(parts) >= 3:
        return parts[0]
    # camelCase：取首段驼峰
    m = re.match(r"^([A-Z][a-z]+(?:[A-Z][a-z]+)?)", name)
    if m and m.group(1) != name:
        return m.group(1)
    return name


def _example_params_for_core(name: str) -> dict[str, Any]:
    low = name.lower()
    if any(p.match(name) for p in [_CORE_TOOL_PATTERNS[0]]):  # Read
        return {"file_path": "src/index.ts"}
    if any(p.match(name) for p in [_CORE_TOOL_PATTERNS[1]]):  # Write
        return {"file_path": "output.txt", "content": "..."}
    if any(p.match(name) for p in [_CORE_TOOL_PATTERNS[2]]):  # Bash
        return {"command": "ls -la"}
    if any(p.match(name) for p in [_CORE_TOOL_PATTERNS[3]]):  # ListDir
        return {"path": "."}
    if any(p.match(name) for p in [_CORE_TOOL_PATTERNS[4]]):  # Search / Grep / Glob
        if "glob" in low:
            return {"pattern": "**/*.py"}
        return {"pattern": "TODO"}
    if any(p.match(name) for p in [_CORE_TOOL_PATTERNS[5]]):  # Edit
        return {"file_path": "src/main.ts", "old_string": "old", "new_string": "new"}
    return {"input": "value"}


def _example_params_from_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """从工具 schema 提取前 2 个参数作为示例。"""
    schema = tool.get("parameters") or tool.get("input_schema") or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return {"input": "value"}
    out: dict[str, Any] = {}
    for key, spec in list(props.items())[:2]:
        if not isinstance(spec, dict):
            out[key] = "value"
            continue
        t = spec.get("type", "string")
        if t == "boolean":
            out[key] = True
        elif t in ("number", "integer"):
            out[key] = 1
        elif t == "array":
            out[key] = []
        elif t == "object":
            out[key] = {}
        else:
            out[key] = "value"
    return out or {"input": "value"}


def pick_few_shot_tools(tools: list[dict[str, Any]], max_third_party: int = 4) -> list[dict[str, Any]]:
    """从工具列表选出 few-shot 代表集：
    - 1 个核心工具（优先 Read，次之 Bash，再次之任意核心工具）
    - 最多 max_third_party 个第三方工具代表（按命名空间分组，每组选描述最长）
    """
    if not tools:
        return []

    core_tools = [t for t in tools if _is_core_tool(t.get("name", ""))]
    third_party = [t for t in tools if not _is_core_tool(t.get("name", ""))]

    chosen: list[dict[str, Any]] = []

    # 1) 核心代表
    def _find(pattern: re.Pattern) -> dict[str, Any] | None:
        return next((t for t in tools if pattern.match(t.get("name", ""))), None)

    core_pick = _find(_CORE_TOOL_PATTERNS[0]) or _find(_CORE_TOOL_PATTERNS[2])
    if core_pick is None and core_tools:
        core_pick = core_tools[0]
    if core_pick is not None:
        chosen.append(core_pick)

    # 2) 第三方按 namespace 分组
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in third_party:
        ns = _tool_namespace(t.get("name", ""))
        groups.setdefault(ns, []).append(t)

    # 命名空间按工具数量降序；每个空间取描述最长的一个作代表
    for ns, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(chosen) >= 1 + max_third_party:
            break
        rep = max(items, key=lambda x: len(x.get("description", "") or ""))
        chosen.append(rep)

    # 还是空就退化为第一个工具
    if not chosen and tools:
        chosen.append(tools[0])

    return chosen


def build_example_params(tool: dict[str, Any]) -> dict[str, Any]:
    """对任意工具构造一个合理的 input 示例。"""
    name = tool.get("name", "")
    if _is_core_tool(name):
        return _example_params_for_core(name)
    return _example_params_from_schema(tool)


def render_few_shot_turn(
    few_shot_tools: list[dict[str, Any]],
    render_tool_call,
    thinking_enabled: bool = False,
) -> tuple[str, str]:
    """渲染合成的 [user, assistant] 对话。返回 (user_text, assistant_text)。

    render_tool_call(name, input) -> str: 使用调用方的工具调用渲染函数
      （通常是 ##TOOL_CALL##\n{json}\n##END_CALL##）。
    """
    actions = [render_tool_call(t.get("name", ""), build_example_params(t)) for t in few_shot_tools]
    user_text = (
        "[FEW-SHOT WARM-UP] Now show me how you would emit multiple action markers in a single "
        "response. Use representatives from different action categories — including MCP/plugin "
        "actions if available — to demonstrate you can invoke any of them, not just core ones."
    )
    body = "\n\n".join(actions)
    if thinking_enabled:
        thinking = (
            "<thinking>\n"
            "The user wants me to demonstrate multi-action capability. I should emit markers "
            "for representatives from each action category (core + MCP/plugin) in one turn.\n"
            "</thinking>\n\n"
        )
        assistant_text = f"{thinking}Here are examples across action categories:\n\n{body}"
    else:
        assistant_text = f"Understood. Here are example markers across action categories:\n\n{body}"
    return user_text, assistant_text


def tool_summary_for_log(few_shot_tools: list[dict[str, Any]]) -> str:
    return ", ".join(t.get("name", "?") for t in few_shot_tools)
