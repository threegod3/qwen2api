"""增量文本流式释放：warmup + guard window。

动机：
    1. Qwen 有时在答复开头先吐一小段"道歉前缀"（"Sorry..."/"我无法..."）然后才开始
       真实内容。如果代理一字一字直接透传，用户会看到拒绝前缀闪烁再被覆盖。
    2. 跨 chunk 的清洗规则（如识别 ##TOOL_CALL## 开始标记）需要一定上下文，直接透传
       会把边界附近的文本过早发出去。

策略：
    - warmup: 累积 96 字符再开始输出，期间可做拒绝检测 / 格式判断
    - guard: 任何时候输出到客户端时都保留末尾 256 字符暂不输出，给跨 chunk 检测留空间
    - finish(): 结束时把剩余全部补齐
"""

from __future__ import annotations

import re
from typing import Callable, Optional


_DEFAULT_WARMUP = 96
_DEFAULT_GUARD = 256
_START_BOUNDARY = re.compile(r"[\n。！？.!?]")
_HTML_TOKEN_RE = re.compile(r"(</?[a-z][a-z0-9]*\s*/?>|&[a-z]+;)", re.IGNORECASE)
_HTML_VALID_RATIO_MIN = 0.2


class IncrementalTextStreamer:
    def __init__(
        self,
        *,
        warmup_chars: int = _DEFAULT_WARMUP,
        guard_chars: int = _DEFAULT_GUARD,
        transform: Optional[Callable[[str], str]] = None,
        is_blocked_prefix: Optional[Callable[[str], bool]] = None,
    ):
        self.warmup_chars = warmup_chars
        self.guard_chars = guard_chars
        self.transform = transform or (lambda s: s)
        self.is_blocked_prefix = is_blocked_prefix or (lambda _s: False)
        self._raw = ""
        self._sent = ""
        self._unlocked = False
        self._sent_any = False

    def _try_unlock(self) -> bool:
        if self._unlocked:
            return True
        preview = self.transform(self._raw)
        if not preview.strip():
            return False
        has_boundary = bool(_START_BOUNDARY.search(preview))
        enough = len(preview) >= self.warmup_chars
        if not has_boundary and not enough:
            return False
        if self.is_blocked_prefix(preview.strip()):
            return False
        # HTML token 比例检测：防止纯 <br> &nbsp; 重复连发时过早放行
        if len(preview) < self.guard_chars:
            no_space = re.sub(r"\s", "", preview)
            stripped = _HTML_TOKEN_RE.sub("", no_space)
            ratio = 0 if not no_space else len(stripped) / len(no_space)
            if ratio < _HTML_VALID_RATIO_MIN:
                return False
        self._unlocked = True
        return True

    def _emit_up_to(self, raw_length: int) -> str:
        transformed = self.transform(self._raw[:raw_length])
        if len(transformed) <= len(self._sent):
            return ""
        delta = transformed[len(self._sent):]
        self._sent = transformed
        if delta:
            self._sent_any = True
        return delta

    def push(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._raw += chunk
        if not self._try_unlock():
            return ""
        safe_len = max(0, len(self._raw) - self.guard_chars)
        if safe_len <= 0:
            return ""
        return self._emit_up_to(safe_len)

    def finish(self) -> str:
        if not self._raw:
            return ""
        return self._emit_up_to(len(self._raw))

    @property
    def unlocked(self) -> bool:
        return self._unlocked

    @property
    def sent_any(self) -> bool:
        return self._sent_any

    @property
    def raw_text(self) -> str:
        return self._raw
