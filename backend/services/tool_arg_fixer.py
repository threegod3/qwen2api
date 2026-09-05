"""智能引号修复 + Edit/StrReplace 模糊匹配。

动机：
    - AI 模型经常把普通 ASCII 引号写成中文/智能引号（" " ' '），
      导致 Edit 工具的 old_string 在文件中 exact match 不中直接失败。
    - old_string 中有微小格式差异（多空格、tab 变 space、反斜杠层级不同）也会 exact fail。

策略：
    - replace_smart_quotes：把所有智能引号变成 ASCII 引号
    - repair_exact_match：old_string 不 exact match 时，构造 fuzzy 正则（容忍引号/空白/反斜杠）
      在文件里搜，如果唯一命中则替换 args 里的 old_string 为真实匹配文本
"""

from __future__ import annotations

import os
import re
from typing import Any


_SMART_DOUBLE_QUOTES = {"\u00ab", "\u201c", "\u201d", "\u275e", "\u201f", "\u201e", "\u275d", "\u00bb"}
_SMART_SINGLE_QUOTES = {"\u2018", "\u2019", "\u201a", "\u201b"}

_DOUBLE_QUOTE_CLASS = '["\u00ab\u201c\u201d\u275e\u201f\u201e\u275d\u00bb]'
_SINGLE_QUOTE_CLASS = "['\u2018\u2019\u201a\u201b]"


def replace_smart_quotes(text: str) -> str:
    if not isinstance(text, str):
        return text
    out = []
    for ch in text:
        if ch in _SMART_DOUBLE_QUOTES:
            out.append('"')
        elif ch in _SMART_SINGLE_QUOTES:
            out.append("'")
        else:
            out.append(ch)
    return "".join(out)


def _build_fuzzy_pattern(text: str) -> str:
    parts = []
    for ch in text:
        if ch in _SMART_DOUBLE_QUOTES or ch == '"':
            parts.append(_DOUBLE_QUOTE_CLASS)
        elif ch in _SMART_SINGLE_QUOTES or ch == "'":
            parts.append(_SINGLE_QUOTE_CLASS)
        elif ch in (" ", "\t"):
            parts.append(r"\s+")
        elif ch == "\\":
            parts.append(r"\\{1,2}")
        else:
            parts.append(re.escape(ch))
    return "".join(parts)


def repair_exact_match(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """对 Edit / StrReplace / search_replace 类工具：若 old_string 在文件里 exact
    不中，用 fuzzy pattern 搜；唯一命中则替换 args 里的 old_string 为精确匹配文本。"""
    if not isinstance(args, dict):
        return args
    lower = (tool_name or "").lower()
    if not any(key in lower for key in ("edit", "str_replace", "strreplace", "search_replace")):
        return args

    old_string = args.get("old_string") or args.get("old_str")
    if not isinstance(old_string, str) or not old_string:
        return args

    file_path = args.get("file_path") or args.get("path")
    if not isinstance(file_path, str) or not file_path:
        return args

    try:
        if not os.path.exists(file_path):
            return args
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return args

    if old_string in content:
        # 即使 exact match，仍规范化 new_string 里的智能引号
        _normalize_new_string(args)
        return args

    # fuzzy 搜
    try:
        pattern = _build_fuzzy_pattern(old_string)
        matches = list(re.finditer(pattern, content))
    except re.error:
        return args

    if len(matches) != 1:
        return args

    matched_text = matches[0].group(0)
    if "old_string" in args:
        args["old_string"] = matched_text
    elif "old_str" in args:
        args["old_str"] = matched_text
    _normalize_new_string(args)
    return args


def _normalize_new_string(args: dict[str, Any]) -> None:
    for key in ("new_string", "new_str"):
        if key in args and isinstance(args[key], str):
            args[key] = replace_smart_quotes(args[key])


def fix_tool_call_arguments(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """对所有工具调用应用全部修复。幂等。"""
    if not isinstance(args, dict):
        return args
    return repair_exact_match(tool_name, args)
