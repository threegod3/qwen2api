"""话题隔离检测：判断新 user 消息是否开启了与历史无关的新任务。

目标：Claude Code 客户端会在同一 session 里把所有历史都发过来。
但用户意图可能完全切换（如从"读文件"转到"浏览器注册"）。
若不做切分，旧任务的工具调用历史会误导模型（表现：重新 Read 文件而非打开浏览器）。

判定逻辑（启发式，不用模型）：
    1. 抽取每条 user 消息的"关键实体"：
       - 文件路径（E:/xxx, /home/xxx, C:\\xxx 等）
       - URL（http://... https://...）
       - 专名 / 引号字符串 / 驼峰标识符
    2. 比较最新 user 消息实体集 vs 历史 first user 实体集的 Jaccard 相似度
    3. 若：最新 user 有自己的非空实体集 AND 与历史 first user 实体集 Jaccard < 0.1
       → 判定为新任务 → 调用方丢弃历史
"""

from __future__ import annotations

import re

_URL_RE = re.compile(r"https?://[^\s)'\"<>]+", re.IGNORECASE)
_WIN_PATH_RE = re.compile(r"[A-Z]:[\\/](?:[^\s<>'\"|:?*]+[\\/])*[^\s<>'\"|:?*\\/]+", re.IGNORECASE)
_NIX_PATH_RE = re.compile(r"/(?:[\w.-]+/)+[\w.-]+")
_CAMEL_RE = re.compile(r"[a-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+")
_ID_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]{3,}(?:\.[a-zA-Z0-9_]+)?\b")


_STOPWORDS = {
    # 中英文功能词，不作实体
    "http", "https", "the", "and", "for", "with", "from", "into", "this", "that", "then",
    "have", "been", "will", "should", "would", "then", "next", "also",
    "给我", "这个", "那个", "就是", "然后", "一下", "请", "帮我", "需要", "进行", "操作",
    "read", "write", "read_file", "read_dir",
}


def _extract_entities(text: str) -> set[str]:
    if not text:
        return set()
    entities: set[str] = set()
    for m in _URL_RE.findall(text):
        entities.add(m.rstrip(".,;"))
        # 再拆域名主体
        dom = re.search(r"//([^/]+)", m)
        if dom:
            entities.add(dom.group(1).lower())
    for m in _WIN_PATH_RE.findall(text):
        entities.add(m.replace("\\", "/"))
        last = m.replace("\\", "/").rsplit("/", 1)[-1]
        if last:
            entities.add(last)
    for m in _NIX_PATH_RE.findall(text):
        entities.add(m)
        last = m.rsplit("/", 1)[-1]
        if last:
            entities.add(last)
    for m in _CAMEL_RE.findall(text):
        if m.lower() not in _STOPWORDS:
            entities.add(m)
    # 含 .py .md .json 等扩展名的标识
    for m in re.findall(r"\b[\w-]+\.[a-zA-Z0-9]{1,5}\b", text):
        if "." in m and m.lower() not in _STOPWORDS:
            entities.add(m)
    return {e for e in entities if len(e) >= 4}


def detect_topic_change(
    first_user_text: str,
    last_user_text: str,
    *,
    jaccard_threshold: float = 0.1,
) -> bool:
    """若 last user 有自己的实体集 AND 与 first user 实体集的 Jaccard < 阈值 → 新任务。

    空文本或首条==最后 一条 → 非新任务（保守不切）。
    """
    if not first_user_text or not last_user_text:
        return False
    if first_user_text.strip() == last_user_text.strip():
        return False
    last_entities = _extract_entities(last_user_text)
    if not last_entities:
        return False  # 最新 user 没有识别实体，不敢妄判
    first_entities = _extract_entities(first_user_text)
    if not first_entities:
        return False
    intersect = last_entities & first_entities
    union = last_entities | first_entities
    if not union:
        return False
    jaccard = len(intersect) / len(union)
    return jaccard < jaccard_threshold
