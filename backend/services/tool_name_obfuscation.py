"""工具名混淆：让 Claude Code 的 **全部** 工具名都与 Qwen 内置函数命名空间隔离，
避免 Qwen 上游把客户端工具名当内置函数名校验并返回 "Tool X does not exists." 拦截。

策略（两层）：
1. **显式别名**（高价值、常被拦截的短名）：Read→fs_open_file 等
2. **通用兜底**：其余所有工具自动加 `u_` 前缀，如 TaskCreate→u_TaskCreate
   mcp__playwright__click→u_mcp__playwright__click 等。
   `u_` 前缀保证名字空间不会与 Qwen 内置函数碰撞。

出站（发往 Qwen）：客户端名 → 别名 / u_前缀名
入站（Qwen 返回）：别名 / u_前缀名 → 客户端名
"""

from __future__ import annotations

import re

# 高价值显式别名：这些短名最容易被 Qwen 内置函数命中，用完全独立的 snake_case。
TOOL_NAME_ALIASES: dict[str, str] = {
    "Read": "fs_open_file",
    "Write": "fs_put_file",
    "Edit": "fs_patch_file",
    "Bash": "shell_run",
    "Grep": "text_search",
    "Glob": "path_find",
    "NotebookEdit": "notebook_patch",
    "WebFetch": "http_get_url",
    "WebSearch": "web_query",
}

REVERSE_ALIASES: dict[str, str] = {v: k for k, v in TOOL_NAME_ALIASES.items()}

# 所有未显式别名的工具自动加这个前缀，保证别名空间与 Qwen 内置隔离。
# 选 "u_" (user tool) 短、没语义、绝不可能和任何 Qwen 内置函数名冲突。
_AUTO_PREFIX = "u_"


def to_qwen_name(name: str) -> str:
    """出站：客户端工具名 → Qwen-safe 别名。
    - 有显式别名：用别名（如 Read → fs_open_file）
    - 无显式别名：加 u_ 前缀（如 TaskCreate → u_TaskCreate）
    - 空值：原样返回
    """
    if not isinstance(name, str) or not name:
        return name
    if name in TOOL_NAME_ALIASES:
        return TOOL_NAME_ALIASES[name]
    # 已经是 Qwen-safe 别名（回调场景避免双重前缀）
    if name in REVERSE_ALIASES or name.startswith(_AUTO_PREFIX):
        return name
    return _AUTO_PREFIX + name


def from_qwen_name(name: str) -> str:
    """入站：Qwen 返回的别名 → 客户端原名。
    - 命中显式反向表：映射回原名（fs_open_file → Read）
    - 命中 u_ 前缀：剥掉前缀（u_TaskCreate → TaskCreate）
    - 未识别：原样返回（兼容 Qwen 偶尔直接返回原名的情况）
    """
    if not isinstance(name, str) or not name:
        return name
    if name in REVERSE_ALIASES:
        return REVERSE_ALIASES[name]
    if name.startswith(_AUTO_PREFIX):
        return name[len(_AUTO_PREFIX):]
    return name


# 用于替换 prompt 中的裸工具名引用（如 "Read/Edit/Write"、"call Read"）。
# 只处理有显式别名的那些（其他名字像 TaskCreate 很少在自由文本里裸出现）。
# 按长度降序避免短名被先匹配。
_BARE_NAME_PATTERN = re.compile(
    r"\b(" + "|".join(sorted(TOOL_NAME_ALIASES.keys(), key=len, reverse=True)) + r")\b"
)


def obfuscate_bare_names(text: str) -> str:
    """把 prompt 文本里裸出现的工具名（如指令块里的 "Read/Edit/Write"）替换成别名。"""
    if not text:
        return text
    return _BARE_NAME_PATTERN.sub(lambda m: TOOL_NAME_ALIASES[m.group(1)], text)
