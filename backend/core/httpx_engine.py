"""
httpx_engine.py — 用 curl_cffi 直连 Qwen API（Chrome TLS 指纹）
优点：TLS 指纹与真实 Chrome 一致，无编码问题，支持流式早期中止
优化：使用全局连接池，避免频繁 TLS 握手
"""

import asyncio
import json
import logging
from curl_cffi.requests import AsyncSession

log = logging.getLogger("qwen2api.httpx_engine")

BASE_URL = "https://chat.qwen.ai"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://chat.qwen.ai/",
    "Origin": "https://chat.qwen.ai",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

_IMPERSONATE = "chrome124"

# ✅ 全局连接池（避免频繁 TLS 握手）
_global_session: AsyncSession = None
_session_lock: asyncio.Lock = None


async def _get_global_session() -> AsyncSession:
    """获取全局 AsyncSession"""
    global _global_session, _session_lock

    if _session_lock is None:
        _session_lock = asyncio.Lock()

    if _global_session is not None:
        return _global_session

    async with _session_lock:
        if _global_session is not None:
            return _global_session

        _global_session = AsyncSession(impersonate=_IMPERSONATE, timeout=30)
        log.info("[HttpxEngine] ✅ 全局连接池已初始化")
        return _global_session


async def _close_global_session():
    """关闭全局 session"""
    global _global_session
    if _global_session:
        try:
            await _global_session.close()
            log.info("[HttpxEngine] ✅ 全局连接池已关闭")
        except Exception as e:
            log.error(f"[HttpxEngine] 关闭连接池失败: {e}")
        finally:
            _global_session = None


class HttpxEngine:
    """Direct curl_cffi engine — Chrome TLS fingerprint, same interface as BrowserEngine."""

    def __init__(self, pool_size: int = 3, base_url: str = BASE_URL):
        self.base_url = base_url
        self._started = False
        self._ready = asyncio.Event()

    async def start(self):
        # ✅ 初始化全局连接池
        await _get_global_session()
        self._started = True
        self._ready.set()
        log.info("[HttpxEngine] 已启动（curl_cffi Chrome指纹直连模式 + 全局连接池）")

    async def stop(self):
        self._started = False
        # ✅ 关闭全局连接池
        await _close_global_session()
        log.info("[HttpxEngine] 已停止")

    def _auth_headers(self, token: str) -> dict:
        return {**_HEADERS, "Authorization": f"Bearer {token}"}

    async def api_call(self, method: str, path: str, token: str, body: dict = None) -> dict:
        # ✅ 改进：使用全局 session 而不是创建新的
        url = self.base_url + path
        headers = {**self._auth_headers(token), "Content-Type": "application/json"}
        data = json.dumps(body, ensure_ascii=False).encode() if body else None
        try:
            session = await _get_global_session()
            resp = await session.request(method, url, headers=headers, data=data)
            return {"status": resp.status_code, "body": resp.text}
        except Exception as e:
            log.error(f"[HttpxEngine] api_call error: {e}")
            return {"status": 0, "body": str(e)}

    async def fetch_chat(self, token: str, chat_id: str, payload: dict, buffered: bool = False):
        """Stream Qwen SSE via curl_cffi with Chrome TLS fingerprint (with global connection pool)."""
        # ✅ 改进：使用全局 session 而不是创建新的
        url = self.base_url + f"/api/v2/chat/completions?chat_id={chat_id}"
        headers = {
            **self._auth_headers(token),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        body_bytes = json.dumps(payload, ensure_ascii=False).encode()

        try:
            session = await _get_global_session()
            async with session.stream("POST", url, headers=headers, data=body_bytes) as resp:
                if resp.status_code != 200:
                    body_chunks = []
                    async for chunk in resp.aiter_content():
                        body_chunks.append(chunk)
                    body_text = b"".join(body_chunks).decode(errors="replace")[:2000]
                    yield {"status": resp.status_code, "body": body_text}
                    return

                async for chunk in resp.aiter_content():
                    decoded = chunk.decode("utf-8", errors="replace")
                    yield {"status": "streamed", "chunk": decoded}

        except Exception as e:
            log.error(f"[HttpxEngine] fetch_chat error: {e}")
            yield {"status": 0, "body": str(e)}

