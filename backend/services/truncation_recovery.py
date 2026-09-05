"""截断检测 + 自动续写。

动机：
    上游 Qwen 的 max_output_tokens 较小，长工具调用（Write 大文件、Edit 长补丁）经常
    在 JSON 中途被截断。客户端（Claude Code）看到不完整的 ##TOOL_CALL## 块会解析失败
    直接把它当纯文本返回给用户，导致任务失败。

做法：
    1. isTruncated(text)：检测 ##TOOL_CALL## 开标签数 > ##END_CALL## 闭标签数
       → 说明有一个 action block 尚未闭合 → 需续写
    2. autoContinue(req, initialResponse)：
       - 构造续写请求：丢弃全部工具定义和历史（省 token）
       - 只保留 initialResponse 末尾 2000 字节作为 anchor
       - 在 assistant 角色塞入这个 anchor + user "请从中断点继续，不要重复"
       - 收到续写 → deduplicateContinuation 去掉与 existing 尾部的重叠
       - 拼接，若还是截断继续下一轮，最多 MAX_AUTO_CONTINUE 次
"""

from __future__ import annotations

import re


_TOOL_CALL_OPEN_RE = re.compile(r"##TOOL_CALL##", re.IGNORECASE)
_TOOL_CALL_CLOSE_RE = re.compile(r"##END_CALL##", re.IGNORECASE)


def is_truncated(text: str) -> bool:
    """检测响应是否在 ##TOOL_CALL## 块中间被截断。"""
    if not text or not text.strip():
        return False
    trimmed = text.rstrip()
    opens = len(_TOOL_CALL_OPEN_RE.findall(trimmed))
    closes = len(_TOOL_CALL_CLOSE_RE.findall(trimmed))
    if opens > closes:
        return True
    # 无 action block 的纯文本截断检测
    if opens == 0:
        # 以逗号、冒号、开括号、反斜杠结尾 → 明显未完成
        if re.search(r"[,;:\[{(\\]\s*$", trimmed):
            return True
    return False


def deduplicate_continuation(existing: str, continuation: str) -> str:
    """在 existing 的尾部和 continuation 的头部之间寻找最长重叠，
    返回去除重叠后的 continuation 部分。"""
    if not existing or not continuation:
        return continuation
    max_overlap = min(500, len(existing), len(continuation))
    if max_overlap < 10:
        return continuation

    # 尝试字符级最长匹配
    best_overlap = 0
    for length in range(max_overlap, 9, -1):
        prefix = continuation[:length]
        if existing.endswith(prefix):
            best_overlap = length
            break

    if best_overlap >= 10:
        return continuation[best_overlap:]

    # 行级匹配（对付格式微差）
    tail_lines = existing.splitlines()[-20:]
    cont_lines = continuation.splitlines()
    if tail_lines and cont_lines:
        first_cont = cont_lines[0].strip()
        if first_cont:
            for i in range(len(tail_lines)):
                if tail_lines[i].strip() != first_cont:
                    continue
                matched = 1
                for k in range(1, len(cont_lines)):
                    if i + k >= len(tail_lines):
                        break
                    if cont_lines[k].strip() == tail_lines[i + k].strip():
                        matched += 1
                    else:
                        break
                if matched >= 2:
                    return "\n".join(cont_lines[matched:])

    return continuation


def build_continuation_prompt(partial_response: str, anchor_chars: int = 2000) -> tuple[str, str]:
    """构造续写请求的 (assistant_context, user_followup)。

    - assistant_context：partial_response 的末尾 anchor_chars 字符（让上游知道之前输出过什么）
    - user_followup：要求严格从中断点继续，不要重复已产出
    """
    anchor = partial_response[-anchor_chars:] if len(partial_response) > anchor_chars else partial_response
    assistant_ctx = ("...\n" + anchor) if len(partial_response) > anchor_chars else anchor
    followup = (
        "Your previous response was cut off mid-output. The last part was:\n\n"
        "```\n"
        f"...{anchor[-300:] if len(anchor) > 300 else anchor}\n"
        "```\n\n"
        "Continue EXACTLY from where you stopped. DO NOT repeat any content already generated. "
        "DO NOT restart the response. Output ONLY the remaining content, starting immediately from the cut-off point."
    )
    return assistant_ctx, followup
