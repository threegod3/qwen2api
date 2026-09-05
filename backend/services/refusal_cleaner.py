"""历史拒绝清洗：扫描过往 assistant 消息里的拒绝/自我限制文本，
命中后把整条消息内容替换为占位工具调用，防止模型看到自己的拒绝模式并级联复现。

典型拒绝场景：
    - "I'm sorry, I cannot help with that"
    - "I only answer questions about Cursor"
    - "我只能回答编程相关问题"
    - "Tool X does not exist"  （Qwen 自制错误）
    - "I cannot execute this tool"
"""

from __future__ import annotations

import re

_REFUSAL_PATTERNS: tuple[re.Pattern, ...] = (
    # ── 英文：道歉/拒绝 ──
    re.compile(r"I[''\u2019]\s*m\s+sorry", re.IGNORECASE),
    re.compile(r"I\s+am\s+sorry", re.IGNORECASE),
    re.compile(r"I\s+cannot\s+help\s+with", re.IGNORECASE),
    re.compile(r"I\s+can\s+only\s+answer", re.IGNORECASE),
    re.compile(r"I\s+only\s+answer", re.IGNORECASE),
    re.compile(r"not\s+able\s+to\s+fulfill", re.IGNORECASE),
    re.compile(r"cannot\s+perform", re.IGNORECASE),
    re.compile(r"cannot\s+write\s+files", re.IGNORECASE),
    re.compile(r"not\s+able\s+to\s+search", re.IGNORECASE),
    re.compile(r"I\s+cannot\s+search", re.IGNORECASE),
    re.compile(r"outside\s+(?:my|the)\s+capabilities", re.IGNORECASE),
    re.compile(r"beyond\s+(?:my|the)\s+scope", re.IGNORECASE),
    re.compile(r"I[''\u2019]?m\s+not\s+(?:able|designed)\s+to", re.IGNORECASE),
    re.compile(r"I\s+don[''\u2019]t\s+have\s+(?:the\s+)?(?:ability|capability)", re.IGNORECASE),
    re.compile(r"(?:can[.']?t|cannot|unable\s+to)\s+help\s+with\s+(?:this|that)", re.IGNORECASE),
    re.compile(r"scoped\s+to\s+(?:answering|helping)", re.IGNORECASE),
    re.compile(r"falls\s+outside\s+(?:the\s+scope|what\s+I)", re.IGNORECASE),

    # ── 英文：Qwen 特有的工具错误幻觉 ──
    re.compile(r"Tool\s+[\w.:-]+\s+does\s+not\s+exists?", re.IGNORECASE),
    re.compile(r"I\s+cannot\s+execute\s+this\s+tool", re.IGNORECASE),
    re.compile(r"tool\s+.+\s+is\s+not\s+available", re.IGNORECASE),
    re.compile(r"the\s+tool\s+\S+\s+is\s+(?:not\s+)?registered", re.IGNORECASE),

    # ── 中文：身份/话题拒绝 ──
    re.compile(r"我是\s*Cursor\s*的?\s*支持助手"),
    re.compile(r"我的职责是帮助你解答"),
    re.compile(r"我无法透露"),
    re.compile(r"我只能回答"),
    re.compile(r"无法提供.*信息"),
    re.compile(r"我没有.*也不会提供"),
    re.compile(r"(?:与|和)\s*(?:编程|代码|开发)\s*无关"),
    re.compile(r"请提问.*(?:编程|代码|开发|技术).*问题"),
    re.compile(r"只能帮助.*(?:编程|代码|开发)"),

    # ── 中文：工具调用相关 ──
    re.compile(r"无法调用.*?工具"),
    re.compile(r"工具.*?不存在"),
    re.compile(r"我无法执行.*?工具"),
    re.compile(r"我不能(?:运行|执行).*?函数"),
)


def is_refusal_text(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _REFUSAL_PATTERNS)


def clean_refusal_messages(
    messages: list,
    *,
    replacement_text: str = "",
) -> tuple[list, int]:
    """扫 messages 里 assistant 角色的消息，若其纯文本内容命中拒绝模式则替换。

    - 如果消息的 content 是字符串：整条替换为空或 replacement_text
    - 如果 content 是 list of blocks：只替换其中的 text block；保留 tool_use / tool_result
      （因为拒绝通常只出现在解释文本里，保留工具调用让 pair 完整）
    - 如果消息已经包含 tool_use block，倾向于保留（那是正常的多步调用）

    返回 (cleaned_messages, replacements_count)
    """
    if not messages:
        return messages, 0

    placeholder = replacement_text or "[earlier assistant turn omitted by proxy]"
    out: list = []
    replaced = 0
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            out.append(msg)
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if is_refusal_text(content):
                new_msg = dict(msg)
                new_msg["content"] = placeholder
                out.append(new_msg)
                replaced += 1
            else:
                out.append(msg)
        elif isinstance(content, list):
            has_tool_use = any(isinstance(p, dict) and p.get("type") == "tool_use" for p in content)
            new_content = []
            mutated = False
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    txt = part.get("text", "")
                    if is_refusal_text(txt):
                        if has_tool_use:
                            # 保留 tool_use block，只删掉道歉文本
                            mutated = True
                            continue
                        new_content.append({"type": "text", "text": placeholder})
                        mutated = True
                        continue
                new_content.append(part)
            if mutated:
                new_msg = dict(msg)
                new_msg["content"] = new_content or [{"type": "text", "text": placeholder}]
                out.append(new_msg)
                replaced += 1
            else:
                out.append(msg)
        else:
            out.append(msg)
    return out, replaced
