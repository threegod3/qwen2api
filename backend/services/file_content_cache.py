"""文件内容缓存：修复 Claude Code 客户端"File unchanged since last read"
提示语导致下游 Qwen 拿不到真实文件内容的问题。

Claude Code 的客户端在重复读同一文件时，不重发完整内容，只发一句提示。
但 qwen2API 每次请求都新建 Qwen chat，Qwen 完全没历史，提示语毫无意义。
本缓存在代理侧保留每个 (api_key, file_path) 最近一次真实 Read 结果，
prompt_builder 检测到提示语时用缓存回填。

- 内存 LRU，最多 200 条，每条 TTL 15min
- 按 API KEY 做 session 隔离，不同用户互不影响
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from threading import Lock

_MAX_ENTRIES = 200
_TTL_SECONDS = 900  # 15 min

_lock = Lock()
# key: (api_key, normalized_file_path) -> (content, timestamp)
_store: "OrderedDict[tuple[str, str], tuple[str, float]]" = OrderedDict()


# Claude Code 发出的缓存提示语——命中任何一个就认为是"提示语，需要用真实内容替换"。
_CACHE_HINT_PATTERNS = (
    re.compile(r"File\s+unchanged\s+since\s+last\s+read", re.IGNORECASE),
    re.compile(r"unchanged\s+since\s+last\s+read", re.IGNORECASE),
    re.compile(r"refer\s+to\s+that\s+instead\s+of\s+re-?reading", re.IGNORECASE),
    re.compile(r"still\s+current\s+[—-]\s+refer\s+to", re.IGNORECASE),
)


def is_cache_hint(text: str) -> bool:
    if not text:
        return False
    # 短文本 + 命中模式才判定为提示语，避免误杀文件里恰好含这些词的情况
    if len(text) > 500:
        return False
    return any(p.search(text) for p in _CACHE_HINT_PATTERNS)


def _normalize_path(path: str) -> str:
    if not isinstance(path, str):
        return ""
    return path.strip().replace("\\", "/").lower()


def _prune_expired(now: float) -> None:
    stale = [k for k, (_, ts) in _store.items() if now - ts > _TTL_SECONDS]
    for k in stale:
        _store.pop(k, None)


def put(api_key: str, file_path: str, content: str) -> None:
    """Record the real content of a Read tool_result. Skip obviously-empty or hint-like values."""
    if not file_path or not isinstance(content, str):
        return
    if is_cache_hint(content):
        return
    key = (api_key or "", _normalize_path(file_path))
    now = time.time()
    with _lock:
        _prune_expired(now)
        _store[key] = (content, now)
        _store.move_to_end(key)
        while len(_store) > _MAX_ENTRIES:
            _store.popitem(last=False)


def get(api_key: str, file_path: str) -> str | None:
    """Return the cached real content or None when no fresh entry exists."""
    if not file_path:
        return None
    key = (api_key or "", _normalize_path(file_path))
    now = time.time()
    with _lock:
        _prune_expired(now)
        entry = _store.get(key)
        if not entry:
            return None
        content, ts = entry
        if now - ts > _TTL_SECONDS:
            _store.pop(key, None)
            return None
        _store.move_to_end(key)
        return content
