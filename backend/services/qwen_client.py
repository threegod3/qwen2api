import asyncio
import json
import logging
import os
import time
from typing import AsyncIterator

import httpx
from playwright.async_api import async_playwright

from backend.core.account_pool import AccountPool
from backend.services.auth_resolver import BASE_URL, AuthResolver
from backend.upstream.payload_builder import build_chat_payload
from backend.upstream.qwen_executor import QwenExecutor
from backend.upstream.sse_consumer import parse_sse_chunk

log = logging.getLogger("qwen2api.client")

# Cookie 持久化文件
_COOKIE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "browser_cookies.json")

# 流式请求超时（真流式：只约束"连接"和"两次数据块之间的最大空闲"，
# 不再对整个响应设硬上限，避免长任务/agent 工作跑到一半被整体超时杀掉）。
_CHAT_STREAM_CONNECT_TIMEOUT = float(os.getenv("QWEN_STREAM_CONNECT_TIMEOUT", "30"))
_CHAT_STREAM_IDLE_TIMEOUT = float(os.getenv("QWEN_STREAM_IDLE_TIMEOUT", "180"))


class QwenClient:
    def __init__(self, account_pool: AccountPool):
        self.account_pool = account_pool
        self.auth_resolver = AuthResolver(account_pool) if account_pool is not None else None
        self.executor = QwenExecutor(self, account_pool)

        # httpx 用于非流式 API 调用（不会被 WAF 拦截）
        limits = httpx.Limits(max_connections=50, max_keepalive_connections=10, keepalive_expiry=30.0)
        timeout = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)
        self._http_client = httpx.AsyncClient(
            limits=limits, timeout=timeout, http2=False, follow_redirects=True,
        )

        # Playwright 用于流式聊天请求（httpx 会被 WAF 拦截）
        self._pw_playwright = None
        self._pw_browser = None
        self._pw_context = None
        self._pw_main_page = None  # 长期有效的已通过 WAF 挑战的主页面
        self._pw_init_lock = asyncio.Lock()
        self._waf_cleared = False  # 标记 WAF 是否已通过
        self._ui_stream_lock = asyncio.Lock()  # UI 驱动流式必须串行（页面状态冲突保护）

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # 关闭 HTTP 连接池前保存 cookie
        await self._save_cookies()
        if self._http_client:
            await self._http_client.aclose()
        if self._pw_main_page:
            await self._pw_main_page.close()
        if self._pw_context:
            await self._pw_context.close()
        if self._pw_browser:
            await self._pw_browser.close()
        if self._pw_playwright:
            await self._pw_playwright.stop()
        return False

    async def _ensure_browser(self):
        """延迟初始化 Playwright 浏览器（仅流式请求时使用）。
        
        环境变量 BROWSER_HEADED=1 时打开可见窗口，用于手动 captcha 求解。
        Cookie 自动持久化到 data/browser_cookies.json，重启后保留登录态。
        
        使用 asyncio.Lock 防止并发请求同时初始化浏览器。
        """
        if self._pw_context is not None:
            return self._pw_context
        
        async with self._pw_init_lock:
            # 双重检查：获取锁后再次检查是否已被其他协程初始化
            if self._pw_context is not None:
                return self._pw_context
            
            headed = os.getenv("BROWSER_HEADED", "").lower() in ("1", "true", "yes")
            self._pw_playwright = await async_playwright().start()
            # 本地优先系统 Chrome(channel="chrome")，容器里没有则回退自带
            # Chromium --headless=new（见 backend/services/browser_launch.py）。
            # ponytail: 之前写死 channel="chrome"，容器内直接抛错导致浏览器
            # 整条路径全灭，上游表现为"空响应"。
            from backend.services.browser_launch import launch_browser
            self._pw_browser = await launch_browser(self._pw_playwright, headed=headed)
            import os as _os
            _proxy = _os.getenv("PLAYWRIGHT_PROXY") or _os.getenv("HTTP_PROXY") or _os.getenv("HTTPS_PROXY")
            _ctx_kwargs = {
                "viewport": {"width": 1920, "height": 1080},
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
            }
            if _proxy:
                _ctx_kwargs["proxy"] = {"server": _proxy}
                log.info("[QwenClient] Playwright 代理已配置: %s", _proxy)
            self._pw_context = await self._pw_browser.new_context(**_ctx_kwargs)
            await self._pw_context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """)
            
            # 恢复持久化的 cookie
            await self._load_cookies()
            
            log.info(f"[QwenClient] Playwright 浏览器已初始化 (headed={headed})")
            if headed:
                log.info(f"[QwenClient] 浏览器窗口已打开，请在浏览器中手动登录 Qwen 并完成验证码")
                log.info(f"[QwenClient] 访问 {BASE_URL}/ 并设置 token 到 localStorage")
            return self._pw_context
    
    async def _load_cookies(self):
        """从磁盘加载持久化的 cookie。"""
        try:
            if os.path.exists(_COOKIE_FILE):
                with open(_COOKIE_FILE, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                if cookies:
                    await self._pw_context.add_cookies(cookies)
                    log.info(f"[QwenClient] 已恢复 {len(cookies)} 个持久化 cookie")
        except Exception as e:
            log.warning(f"[QwenClient] 加载 cookie 失败: {e}")
    
    async def _save_cookies(self):
        """将当前 context 的 cookie 持久化到磁盘。"""
        try:
            if self._pw_context is None:
                return
            cookies = await self._pw_context.cookies()
            os.makedirs(os.path.dirname(_COOKIE_FILE), exist_ok=True)
            with open(_COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            log.info(f"[QwenClient] 已持久化 {len(cookies)} 个 cookie 到 {_COOKIE_FILE}")
        except Exception as e:
            log.warning(f"[QwenClient] 保存 cookie 失败: {e}")

    @staticmethod
    def _prepend_runtime_bx_entry(headers: dict[str, str], note: str = "ui-runtime") -> None:
        """把 UI 路径拦截到的运行时 bx 头插到 bx_pool.json 队首。

        运行时签名的"新鲜度 + 与当前 cookie 同会话"是降低 punish 概率的关键,
        unshift 到队首后 _build_headers random.choice 命中率最高。旧离线条目
        保留作 fallback,避免 bx_pool 空时 httpx 路径无 bx 头可发。

        ponytail: 上限 32 是凭直觉定的,如果发现频繁重试导致条目迅速轮转,
        丢掉了"昨天抓的、今天还能用"的会话相关条目,再上调。
        """
        import os, json as _json, time as _time
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        pool_path = os.path.join(project_root, "bx_pool.json")
        entry = {
            "bx_ua": headers.get("bx-ua", ""),
            "bx_umidtoken": headers.get("bx-umidtoken", ""),
            "bx_v": headers.get("bx-v", ""),
            "user_agent": headers.get("user-agent", ""),
            "web_version": "",  # 前端 common-lib 不带 Version 头,留空 → _build_headers 用 env 兜底
            "timezone": "",
            "captured_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": note,
        }
        # 任何关键字段缺失就跳过,避免污染 pool
        if not entry["bx_ua"] or not entry["bx_umidtoken"] or not entry["user_agent"]:
            return
        # 占位签名（BaXia 传感器未就绪时的 defaultFY2_*）必须挡掉：
        # 它代表"没算出来"，入库后 curl_cffi 拿去用必 punish。
        if "default" in entry["bx_ua"] or "default" in entry["bx_umidtoken"]:
            return
        if len(entry["bx_ua"]) < 200 or len(entry["bx_umidtoken"]) < 64:
            return
        # 与已有最新条目去重(网络重试会触发,避免膨胀)
        pool: list = []
        try:
            if os.path.exists(pool_path):
                with open(pool_path, "r", encoding="utf-8") as f:
                    pool = _json.load(f) or []
        except Exception:
            pool = []
        if pool and pool[0].get("bx_ua") == entry["bx_ua"] and pool[0].get("bx_umidtoken") == entry["bx_umidtoken"]:
            return
        pool.insert(0, entry)
        pool = pool[:32]  # 上限,防止历史污染失控
        try:
            with open(pool_path, "w", encoding="utf-8") as f:
                _json.dump(pool, f, ensure_ascii=False, indent=2)
            # 失效 60s 缓存,下次 _load_bx_pool 立刻读到新条目
            QwenClient._bx_pool_cache = None
            log.info("[QwenClient] bx_pool 头部已插入运行时 entry (bx_ua len=%d)", len(entry["bx_ua"]))
        except Exception as e:
            log.warning(f"[QwenClient] 写 bx_pool.json 失败: {e}")

    async def _persist_after_ui_round(self, captured_bx_headers: dict[str, str]) -> None:
        """UI 路径(文本流式 / 媒体生成)结束时统一落盘:bx_pool 队首 + cookie。

        异常分支也调用,网络抖动也算"前端已过 WAF",不留残留状态。

        ponytail: bx_pool 写入 gate 在 _waf_cleared——若当前 cookie 仍被
        服务端标记(没破 WAF 就被 punish),抓到的 bx-ua 也是"被惩罚会话的
        bx-ua",落盘会污染 bx_pool 队首、让后续 curl_cffi 命中并复现 punish。
        """
        if not self._waf_cleared:
            log.warning("[QwenClient] WAF 未通过,跳过 bx_pool 落盘(避免把被惩罚会话的 bx-ua 写入)")
            try:
                await self._save_cookies()
            except Exception as e:
                log.warning(f"[QwenClient] 落 cookie 失败: {e}")
            return
        if captured_bx_headers:
            try:
                self._prepend_runtime_bx_entry(captured_bx_headers, note="ui-runtime")
            except Exception as e:
                log.warning(f"[QwenClient] 落 bx_pool 失败: {e}")
        try:
            await self._save_cookies()
        except Exception as e:
            log.warning(f"[QwenClient] 落 cookie 失败: {e}")

    async def invalidate_browser_session(self, reason: str = "") -> None:
        """丢弃当前浏览器会话并立即重建一套干净的 cookie。

        BaXia punish 挑战一旦出现，说明当前会话 cookie 已被风控标记，
        换账号 / 换 bx 签名都救不回来——实测唯一有效的是换一套 cookie。

        注意：只删不建是无效的。curl_cffi 直连路径靠 _COOKIE_FILE 注入
        Cookie 头，删空之后它会变成裸请求，反而更容易被 punish。所以这里
        清理完必须重新导航 chat.qwen.ai 走一遍 WAF 挑战，把新 cookie 落盘。
        """
        log.warning("[QwenClient] 丢弃浏览器会话 原因=%s", reason or "punish")
        try:
            if self._pw_context is not None:
                await self._pw_context.clear_cookies()
        except Exception as e:
            log.warning("[QwenClient] 清理内存 cookie 失败: %s", e)
        try:
            if os.path.exists(_COOKIE_FILE):
                os.remove(_COOKIE_FILE)
                log.info("[QwenClient] 已删除被标记的 cookie 文件 %s", _COOKIE_FILE)
        except Exception as e:
            log.warning("[QwenClient] 删除 cookie 文件失败: %s", e)
        try:
            if self._pw_main_page is not None:
                await self._pw_main_page.close()
        except Exception:
            pass
        self._pw_main_page = None
        self._waf_cleared = False

        # 重新导航主页拿新会话并落盘，供 curl_cffi 路径使用
        try:
            await self._ensure_main_page()
            await self._save_cookies()
            log.info("[QwenClient] 已重建浏览器会话 waf_cleared=%s", self._waf_cleared)
        except Exception as e:
            log.warning("[QwenClient] 重建浏览器会话失败: %s", e)

    @staticmethod
    def _load_bx_pool() -> list[dict]:
        """加载 bx_pool.json（多 bx-ua 轮换池，降低风控概率）。

        加载时过滤掉降级值(default_not_value)和缺字段条目,避免 random.choice
        命中半残条目导致 bx-ua 与 bx-umidtoken 拆错配 → 服务端判为不完整签名
        → punish。缓存 60s,_prepend_runtime_bx_entry 写完后会置 None 失效。
        """
        import os, json as _json, time as _time
        try:
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            pool_path = os.path.join(project_root, "bx_pool.json")
            now = _time.time()
            cache = getattr(QwenClient, "_bx_pool_cache", None)
            if cache is not None and now - cache["loaded_at"] < 60:
                return cache["pool"]
            if not os.path.exists(pool_path):
                QwenClient._bx_pool_cache = {"pool": [], "loaded_at": now}
                return []
            with open(pool_path, "r", encoding="utf-8") as f:
                pool = _json.load(f) or []
            # 过滤掉降级值(default_not_value)和缺字段条目
            pool = [
                e for e in pool
                if isinstance(e, dict)
                and e.get("bx_ua")
                and e.get("bx_umidtoken")
                and e.get("bx_umidtoken") != "default_not_value"
            ]
            QwenClient._bx_pool_cache = {"pool": pool, "loaded_at": now}
            return pool
        except Exception:
            return []

    @staticmethod
    def _build_headers(token: str) -> dict[str, str]:
        """构建 httpx 请求头（含 bx 反爬头）。"""
        import os, uuid, random
        pool = QwenClient._load_bx_pool()
        # 双保险:即便 _load_bx_pool 因别的原因失效,这里也再过滤一次降级值
        valid = [
            e for e in pool
            if e.get("bx_ua") and e.get("bx_umidtoken")
            and e.get("bx_umidtoken") != "default_not_value"
        ]
        entry = random.choice(valid) if valid else {}
        ua = entry.get("user_agent") or os.getenv(
            "QWEN_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        )
        bx_ua = entry.get("bx_ua") or os.getenv("QWEN_BX_UA", "")
        bx_umid = entry.get("bx_umidtoken") or os.getenv("QWEN_BX_UMIDTOKEN", "")
        bx_v = entry.get("bx_v") or os.getenv("QWEN_BX_V", "")
        version = entry.get("web_version") or os.getenv("QWEN_WEB_VERSION", "")
        timezone = entry.get("timezone") or os.getenv("QWEN_TIMEZONE", "")

        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{BASE_URL}/",
            "Origin": BASE_URL,
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "source": "web",
            "X-Request-Id": str(uuid.uuid4()),
        }
        if bx_ua:
            headers["bx-ua"] = bx_ua
        if bx_umid:
            headers["bx-umidtoken"] = bx_umid
        if bx_v:
            headers["bx-v"] = bx_v
        if version:
            headers["Version"] = version
        if timezone:
            headers["Timezone"] = timezone
        # 注入浏览器 cookie，降低 WAF 拦截概率（与网页会话对齐）
        try:
            if os.path.exists(_COOKIE_FILE):
                with open(_COOKIE_FILE, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                if isinstance(cookies, list) and cookies:
                    cookie_str = "; ".join(
                        f"{c.get('name')}={c.get('value')}"
                        for c in cookies
                        if c.get("name") and c.get("value")
                    )
                    if cookie_str:
                        headers["Cookie"] = cookie_str
        except Exception:
            pass
        return headers

    # ========================================================================
    # 非流式 API 请求：使用 httpx（WAF 通常不拦截 GET/DELETE，POST 尝试后降级）
    # ========================================================================
    async def _request_json(self, method: str, path: str, token: str, body: dict | None = None, timeout: float = 30.0) -> dict:
        """发送非流式 API 请求。

        使用 httpx 发送所有非流式请求（create_chat, list_chats, delete_chat 等），
        旧的 page.evaluate(fetch) 方式已被 Qwen BaXia 拦截导致挂起。
        流式请求（chat/completions）走独立的 _browser_request_json。

        如果 httpx 返回 WAF 挑战页面，尝试浏览器备用路径。
        """
        try:
            resp = await self._http_client.request(
                method,
                f"{BASE_URL}{path}",
                headers=self._build_headers(token),
                json=body,
                timeout=timeout,
            )
            body_text = resp.text
            status = resp.status_code

            # 检测 WAF 拦截
            is_waf, waf_type = self._is_waf_challenge(body_text)
            if is_waf:
                log.warning("[QwenClient] httpx 请求被 WAF 拦截 (type=%s, status=%d, path=%s)", waf_type, status, path)
                # 如果浏览器已初始化且通过了 WAF，尝试用浏览器 API 重试
                if self._pw_context is not None and self._waf_cleared:
                    log.info("[QwenClient] 尝试通过浏览器 ApiRequest 重试...")
                    browser_result = await self._browser_request_json(
                        method, f"{BASE_URL}{path}", token, body=body, timeout=timeout
                    )
                    return browser_result
                # 否则返回 WAF 错误
                return {"status": status, "body": body_text, "waf": waf_type}

            return {"status": status, "body": body_text}
        except httpx.TimeoutException:
            log.warning("[QwenClient] httpx 请求超时 (path=%s, timeout=%s)", path, timeout)
            # 超时后尝试浏览器路径
            if self._pw_context is not None and self._waf_cleared:
                log.info("[QwenClient] httpx 超时，尝试通过浏览器 ApiRequest 重试...")
                browser_result = await self._browser_request_json(
                    method, f"{BASE_URL}{path}", token, body=body, timeout=timeout
                )
                return browser_result
            return {"status": 0, "body": f"httpx timeout ({timeout}s)"}
        except Exception as e:
            log.warning(f"[QwenClient] _request_json 异常: {e}")
            return {"status": 0, "body": str(e)}

    async def create_chat(self, token: str, model: str, chat_type: str = "t2t") -> str:
        return await self.executor.create_chat(token, model, chat_type=chat_type)

    async def delete_chat(self, token: str, chat_id: str):
        await self._request_json("DELETE", f"/api/v2/chats/{chat_id}", token, timeout=20.0)

    async def list_chats(self, token: str, limit: int = 50) -> list[dict]:
        res = await self._request_json("GET", f"/api/v2/chats?limit={limit}", token, timeout=20.0)
        if res["status"] != 200:
            return []
        try:
            data = json.loads(res.get("body", "{}"))
        except Exception:
            return []
        chats = data.get("data", [])
        return chats if isinstance(chats, list) else []

    async def get_chat(self, token: str, chat_id: str) -> dict:
        res = await self._request_json("GET", f"/api/v2/chats/{chat_id}", token, timeout=30.0)
        if res["status"] != 200:
            raise Exception(f"get_chat HTTP {res['status']}: {res.get('body', '')[:200]}")
        try:
            return json.loads(res.get("body", "{}"))
        except Exception as e:
            raise Exception(f"get_chat parse error: {e}, body={res.get('body', '')[:200]}")

    async def get_task_status(self, token: str, task_id: str) -> dict:
        # 1) httpx
        res = await self._request_json("GET", f"/api/v1/tasks/status/{task_id}", token, timeout=30.0)
        body = res.get("body", "") or ""
        status = res.get("status", 0)
        is_waf, _ = self._is_waf_challenge(body)
        if status == 200 and not is_waf:
            try:
                return json.loads(body)
            except Exception as e:
                raise Exception(f"task status parse error: {e}, body={body[:200]}")

        # 2) curl_cffi + bx
        try:
            from curl_cffi.requests import AsyncSession
            headers = self._build_headers(token)
            async with AsyncSession(impersonate="chrome131", timeout=30.0) as session:
                resp = await session.get(
                    f"{BASE_URL}/api/v1/tasks/status/{task_id}",
                    headers=headers,
                )
                if resp.status_code == 200 and not self._is_waf_challenge(resp.text or "")[0]:
                    return resp.json()
                status = resp.status_code
                body = resp.text or body
        except Exception as e:
            log.warning("[QwenClient] get_task_status curl_cffi failed: %s", e)

        # 3) browser fallback
        br = await self._browser_request_json(
            "GET",
            f"{BASE_URL}/api/v1/tasks/status/{task_id}",
            token,
            timeout=30.0,
        )
        if br.get("status") == 200:
            try:
                return json.loads(br.get("body", "{}"))
            except Exception as e:
                raise Exception(f"task status parse error: {e}, body={br.get('body', '')[:200]}")

        raise Exception(f"task status HTTP {status or br.get('status', 0)}: {body[:200]}")

    async def complete_once(
        self,
        token: str,
        chat_id: str,
        model: str,
        content: str,
        has_custom_tools: bool = False,
        files: list[dict] | None = None,
        chat_type: str = "t2t",
        size: str | None = None,
        img_url: str | None = None,
        local_files: list[str] | None = None,
    ) -> dict:
        """完成一次生成（t2t/t2i/t2v）。

        上游 t2i/t2v 对 stream=false 常返回 Bad_Request；
        这里强制 stream=true，再把 SSE 聚合成 dict 供 images/videos 解析 URL/task_id。
        local_files：本地参考图路径列表，仅 i2v/i2i 的浏览器 UI 降级路径需要
        （作为前端"上传附件"的输入）。
        """
        payload = build_chat_payload(
            chat_id,
            model,
            content,
            has_custom_tools,
            files=files,
            chat_type=chat_type,
            size=size,
            img_url=img_url,
        )
        # t2i: stream=true 直接出图 URL
        # t2v/i2v: stream=false 才能拿到 task_id（stream=true 常空答）
        # t2t: stream=true
        prefer_stream = chat_type not in {"t2v", "i2v"}
        attempts = [True] if prefer_stream else [False, True]
        if prefer_stream:
            attempts = [True, False]

        body_text = ""
        status = 0
        last_err = ""
        for use_stream in attempts:
            payload["stream"] = use_stream
            payload["incremental_output"] = True
            # 1) curl_cffi
            curl_res = await self._stream_chat_via_curl_cffi(token, chat_id, payload, timeout=300.0)
            if curl_res and curl_res.get("status") == 200 and not self._is_waf_challenge(curl_res.get("body") or "")[0]:
                status = 200
                body_text = curl_res.get("body") or ""
            else:
                # 2) httpx
                res = await self._request_json(
                    "POST",
                    f"/api/v2/chat/completions?chat_id={chat_id}",
                    token,
                    payload,
                    timeout=300.0,
                )
                status = res.get("status", 0)
                body_text = res.get("body", "") or ""
                is_waf, _ = self._is_waf_challenge(body_text)
                if status != 200 or is_waf:
                    # 3) browser UI 驱动（window.ApiRequest 已移除，原生 fetch/XHR 被 BaXia 拦截）
                    if chat_type in {"t2i", "t2v", "i2v"}:
                        # 媒体生成必须走前端"生成图像/创建视频"模式：
                        # 文本 UI 驱动只会得到普通文字回复（提取的 URL 无签名 → 404）
                        br = await self._generate_media_via_browser_ui(
                            token, chat_id, model, payload.get("messages", [{}])[0].get("content", ""),
                            media_type=chat_type, timeout=300.0, files=local_files,
                        )
                        if br.get("status") == 200:
                            body_text = br.get("body") or ""
                            status = 200
                        else:
                            status = br.get("status", 0)
                            body_text = br.get("body", "") or ""
                    else:
                        br = await self._stream_chat_via_browser_ui(
                            token, chat_id, payload, timeout=300.0,
                        )
                        status = br.get("status", 0)
                        body_text = br.get("body", "") or ""

            if status != 200:
                last_err = f"HTTP {status}: {body_text[:200]}"
                continue

            # 判断是否有实质内容：task_id / 媒体 URL / 非空 answer
            has_task = ("task_id" in body_text) or ("taskId" in body_text)
            has_media = any(x in body_text for x in (".mp4", ".png", ".jpg", "cdn.qwenlm.ai", "/t2i/", "/t2v/"))
            empty_answer = (
                '"content": ""' in body_text
                and "task_id" not in body_text
                and "cdn.qwenlm.ai" not in body_text
            )
            # 对 t2v：空答则换下一种 stream 模式
            if chat_type in {"t2v", "i2v"} and not has_task and not has_media and empty_answer:
                last_err = "empty t2v answer without task_id"
                log.warning("[complete_once] t2v empty with stream=%s, try next mode", use_stream)
                continue
            break
        else:
            raise Exception(f"complete_once failed: {last_err or 'unknown'}")

        if status != 200:
            raise Exception(f"complete_once HTTP {status}: {body_text[:300]}")

        # SSE 文本：聚合成可解析结构
        events: list[dict] = []
        for line in body_text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                events.append(json.loads(data_str))
            except Exception:
                events.append({"raw": data_str})

        if not events:
            try:
                return json.loads(body_text)
            except Exception:
                return {"raw_sse": body_text, "events": [], "success": True}

        return {"raw_sse": body_text, "events": events, "success": True}

    async def verify_token(self, token: str) -> bool:
        if not token:
            return False
        try:
            resp = await self._http_client.get(
                f"{BASE_URL}/api/v1/auths/",
                headers=self._build_headers(token),
                timeout=15.0,
            )
            if resp.status_code != 200:
                return False
            try:
                data = resp.json()
                return data.get("role") == "user"
            except Exception as e:
                log.warning(f"[verify_token] JSON 解析失败: {e}, status={resp.status_code}, text={resp.text[:100]}")
                if "aliyun_waf" in resp.text.lower() or "<!doctype" in resp.text.lower():
                    log.info("[verify_token] WAF 拦截页面，放行")
                    return True
                return False
        except Exception as e:
            log.warning(f"[verify_token] HTTP 异常: {e}")
            return False

    async def list_models(self, token: str) -> list:
        try:
            resp = await self._http_client.get(
                f"{BASE_URL}/api/models",
                headers=self._build_headers(token),
                timeout=10.0,
            )
            if resp.status_code != 200:
                return []
            try:
                return resp.json().get("data", [])
            except Exception as e:
                log.warning(f"[list_models] JSON 解析失败: {e}")
                return []
        except Exception:
            return []

    # ---- cached upstream-pool model list ----
    _UPSTREAM_MODELS_TTL = 300
    _upstream_models_cache: list[dict] = []
    _upstream_models_fetched_at: float = 0.0

    async def list_models_from_pool(self) -> list[dict]:
        now = time.time()
        if self._upstream_models_cache and (now - self._upstream_models_fetched_at) < self._UPSTREAM_MODELS_TTL:
            return self._upstream_models_cache
        if self.account_pool is None:
            return []
        acc = None
        try:
            acc = await self.account_pool.acquire_wait(timeout=5)
            if not acc:
                return []
            models = await self.list_models(acc.token)
            if models:
                QwenClient._upstream_models_cache = models
                QwenClient._upstream_models_fetched_at = now
            return models
        except Exception as e:
            log.warning(f"[list_models_from_pool] failed: {e}")
            return []
        finally:
            if acc is not None:
                self.account_pool.release(acc)

    def _build_payload(
        self,
        chat_id: str,
        model: str,
        content: str,
        has_custom_tools: bool = False,
        files: list[dict] | None = None,
        chat_type: str = "t2t",
        size: str | None = None,
    ) -> dict:
        return build_chat_payload(
            chat_id,
            model,
            content,
            has_custom_tools,
            files=files,
            chat_type=chat_type,
            size=size,
        )

    def parse_sse_chunk(self, chunk: str) -> list[dict]:
        return parse_sse_chunk(chunk)

    async def stream(self, token: str, chat_id: str, model: str, content: str, has_custom_tools: bool = False, files: list[dict] | None = None):
        async for event in self.executor.stream(token, chat_id, model, content, has_custom_tools, files=files):
            yield event

    async def chat_stream_events_with_retry(
        self,
        model: str,
        content: str,
        has_custom_tools: bool = False,
        files: list[dict] | None = None,
        fixed_account=None,
        existing_chat_id: str | None = None,
    ):
        """转发到 executor.chat_stream_events_with_retry（带重试的流式聊天）。"""
        async for item in self.executor.chat_stream_events_with_retry(
            model, content,
            has_custom_tools=has_custom_tools,
            files=files,
            fixed_account=fixed_account,
            existing_chat_id=existing_chat_id,
        ):
            yield item

    # ========================================================================
    # WAF 检测
    # ========================================================================
    def _is_waf_challenge(self, text: str) -> tuple[bool, str]:
        """检测响应是否为 WAF 挑战页面。返回 (is_waf, challenge_type)。"""
        if not text:
            return False, ""
        if "FAIL_SYS_USER_VALIDATE" in text:
            return True, "punish"
        # BaXia punish 跳转页：一段裸 <script>，把浏览器重定向到
        # /_____tmd_____/punish?x5secdata=...&x5step=1。它既不含
        # FAIL_SYS_USER_VALIDATE 也不含 <!doctype，所以必须单独识别——
        # 否则会被当成普通 SSE 解析出 0 个事件，最终误报 empty_upstream_response，
        # 触发"换账号重试"这种对风控完全无效的策略。
        if "_____tmd_____/punish" in text or ("x5secdata=" in text and "x5step=" in text):
            return True, "punish"
        if "aliyun_waf_aa" in text or "aliyun_waf_bb" in text:
            return True, "aa_bb"
        if "captcha" in text.lower() and ("sliding" in text.lower() or "滑块" in text):
            return True, "captcha"
        if "<!doctype" in text[:50].lower() and "waf" in text.lower():
            return True, "unknown_html"
        return False, ""

    @staticmethod
    def _extract_punish_url(text: str) -> str:
        """从 punish 响应体中提取浏览器可访问的挑战 URL。

        punish 有三种形态：
        1. 裸 <script> 跳转页：window.location.replace(".../punish?x5secdata=...&x5step=1")
        2. punish JSON：{"data": {"url": "https://.../punish?..."}}
        3. UI 拦截 SSE 里的同 1/2（调用方先取 body 再调这里）
        取不到返回空串。
        """
        if not text:
            return ""
        import re
        m = re.search(r'window\.location\.replace\("([^"]*?punish[^"]*)"', text)
        if m:
            return m.group(1)
        try:
            data = json.loads(text)
            url = (data.get("data") or {}).get("url", "") if isinstance(data, dict) else ""
            if url and "punish" in url:
                return url if url.startswith("http") else f"{BASE_URL}{url}"
        except Exception:
            pass
        m = re.search(r'(https?://[^\s"\'<>]*?punish[^\s"\'<>]*)', text)
        return m.group(1) if m else ""

    async def _clear_punish_via_browser(self, punish_url: str) -> bool:
        """在**同一浏览器会话**内打开 punish 挑战页并自动求解 noCaptcha 滑块。

        必须用同一 context（cookie 隔离 per-context）：解出的 `x5sec` 才会
        落到会话 cookie 里，后续 curl_cffi 直连才能复用。实测解出 x5sec 后
        直连由 punish 变为应用层响应（不再被 WAF 拦截）。

        用临时 page 而不是 _pw_main_page，避免把长生命周期主页导航走。
        成功返回 True（已落盘 cookie），失败/无滑块返回 False。
        """
        if not punish_url:
            return False
        try:
            ctx = await self._ensure_browser()
        except Exception as e:
            log.warning("[QwenClient] punish 自愈：浏览器不可用: %s", e)
            return False
        page = None
        try:
            page = await ctx.new_page()
            try:
                await page.goto(punish_url, timeout=60000, wait_until="domcontentloaded")
            except Exception as e:
                log.warning("[QwenClient] punish 自愈：挑战页导航异常: %s", e)
            await page.wait_for_timeout(3000)
            from backend.services.nc_slider import solve_nocaptcha_slider
            ok = await solve_nocaptcha_slider(page)
            if ok:
                # 回到主站让会话水合，再落盘（含 x5sec + 新 acw_tc）
                try:
                    await page.goto(f"{BASE_URL}/", timeout=45000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(5000)
                except Exception:
                    pass
                await self._save_cookies()
                log.info("[QwenClient] punish 自愈成功，会话已更新")
                return True
            log.warning("[QwenClient] punish 自愈：滑块未通过")
            return False
        except Exception as e:
            log.warning("[QwenClient] punish 自愈异常: %s", e)
            return False
        finally:
            try:
                if page is not None:
                    await page.close()
            except Exception:
                pass

    # ========================================================================
    # 浏览器主页面管理（WAF 挑战 + 会话保持）
    # ========================================================================
    async def _ensure_main_page(self):
        """确保有一个已加载 Qwen 主页面并完成 WAF 挑战的页面。
        
        这个方法保留一个长期有效的页面，用于：
        1. 完成 Aliyun WAF 的 JavaScript 挑战（通过页面导航）
        2. 维护浏览器 session/cookies 的有效性
        3. 在 headed 模式下等待用户手动完成验证码
        4. 作为流式 API 请求的浏览器上下文
        """
        if self._pw_main_page is not None:
            # 检查页面是否还活着
            try:
                await self._pw_main_page.evaluate("1")
                return self._pw_main_page
            except Exception:
                log.warning("[QwenClient] 主页面已失效，重新创建")
                self._pw_main_page = None
                self._waf_cleared = False
        
        ctx = await self._ensure_browser()
        self._pw_main_page = await ctx.new_page()
        
        headed = os.getenv("BROWSER_HEADED", "").lower() in ("1", "true", "yes")
        
        # 导航到 Qwen 主页，等待 DOM 加载
        log.info("[QwenClient] 正在导航到 %s 以完成 WAF 挑战...", BASE_URL)
        try:
            await self._pw_main_page.goto(
                f"{BASE_URL}/",
                timeout=60000,
                wait_until="domcontentloaded",
            )
            log.info("[QwenClient] 主页面 DOM 已加载")
        except Exception as e:
            log.warning("[QwenClient] 主页面导航异常: %s", e)
        
        # 等待页面 JS 执行（包括 WAF 挑战和 BaXia 初始化）
        await self._pw_main_page.wait_for_timeout(8000)

        # WAF 清除等待(headless 模式下:acw_tc 有效 → 跳过;否则自动调 solve_slider 破滑块;
        # 失败 → 返回 False 但保留旧 cookie,避免把裸 cookie 喂给 curl_cffi)
        await self._ensure_waf_clearance(self._pw_main_page)

        # 保存更新后的 cookie
        # headless 模式下若 WAF 未确认通过(常见于 token 已失效/会话刚失效),
        # 写盘会把未过 WAF 的 cookie 喂给 curl_cffi 路径 → 必 punish。
        # 只在 waf_cleared=True 时落盘,否则保留旧的 browser_cookies.json。
        if self._waf_cleared:
            await self._save_cookies()
        else:
            log.warning("[QwenClient] WAF 未确认通过,本次 cookie 仅保留在浏览器内存,跳过落盘")
        return self._pw_main_page

    async def _ensure_waf_clearance(self, page):
        """确保 WAF 挑战已被清除。

        在 headed 模式下，等待用户手动完成 WAF 验证码/登录。
        在 headless 模式下，检测当前状态并尝试重试。
        """
        headed = os.getenv("BROWSER_HEADED", "").lower() in ("1", "true", "yes")
        
        if headed:
            # === Headed 模式：交互等待用户完成 WAF 验证 ===
            log.info("=" * 60)
            log.info("[QwenClient] ===== Headed 模式：等待 WAF 验证完成 =====")
            log.info("[QwenClient] 请在打开的浏览器窗口中完成以下步骤：")
            log.info("[QwenClient] 1. 如果出现 WAF 滑块验证码 → 手动滑动完成")
            log.info("[QwenClient] 2. 如果出现 Aliyun WAF 页面 → 等待自动跳转")
            log.info("[QwenClient] 3. 页面加载完成后 → 登录 Qwen 账号（如需）")
            log.info("[QwenClient] 4. 确认可以看到 Qwen 聊天界面")
            log.info("[QwenClient] 完成后我会自动检测到并继续执行。")
            log.info("[QwenClient] 超时时间：5 分钟")
            log.info("=" * 60)
            
            start_time = time.time()
            max_wait = 300  # 5 分钟超时
            poll_interval = 3  # 每 3 秒检测一次
            
            while time.time() - start_time < max_wait:
                await asyncio.sleep(poll_interval)
                
                try:
                    # 检测 1：页面内容是否包含 WAF 关键词
                    html_preview = await page.evaluate("document.body?.innerText?.substring(0, 200) || ''")
                    
                    # 检测 2：检查是否已通过 WAF（AjaxRequest 是否存在）
                    api_request_available = await page.evaluate("typeof window.ApiRequest !== 'undefined' && typeof window.ApiRequest.post === 'function'")
                    
                    # 检测 3：检查页面标题
                    title = await page.evaluate("document.title || ''")
                    
                    waf_detected_in_html = (
                        "aliyun_waf" in html_preview.lower() or
                        "滑块" in html_preview or
                        "captcha" in html_preview.lower() or
                        "验证" in html_preview
                    )
                    
                    if not waf_detected_in_html and api_request_available:
                        log.info("[QwenClient] WAF 验证已通过！ApiRequest 可用，页面加载完成。")
                        log.info(f"[QwenClient] 页面标题: {title}")
                        self._waf_cleared = True
                        return True
                    
                    elapsed = int(time.time() - start_time)
                    if elapsed % 15 == 0:  # 每 15 秒提醒一次
                        log.info(f"[QwenClient] 等待 WAF 验证完成... (已等待 {elapsed}s)")
                        if waf_detected_in_html:
                            log.info(f"[QwenClient] 仍检测到验证码/安全挑战，请完成浏览器中的验证")
                
                except Exception as e:
                    log.debug(f"[QwenClient] WAF 检测异常: {e}")
                    continue
            
            log.warning(f"[QwenClient] WAF 验证等待超时 ({max_wait}s)")
            # 超时后仍然继续，尝试使用当前状态
            return False
        
        else:
            # === Headless 模式：自动检测 + 自动破滑块 ===
            # ponytail: 用 cookie 中的 acw_tc 替代"页面文字含 'aliyun_waf'"作为
            # 唯一可信的 WAF 通过信号——前者是服务端写入的会话凭据,后者
            # 可能因前端懒加载/水合时机问题误判。
            def _has_valid_acw(cookies) -> bool:
                now_ts = time.time()
                return any(
                    c.get("name") == "acw_tc"
                    and c.get("domain", "").endswith("qwen.ai")
                    and (c.get("expires", -1) < 0 or c.get("expires", 0) > now_ts)
                    for c in cookies
                )

            try:
                cookies = await page.context.cookies()
                acw_ok = _has_valid_acw(cookies)
            except Exception:
                acw_ok = False

            if acw_ok:
                # 仅"页面活着 + acw_tc 有效"就认为已过 WAF,跳过 ApiRequest 探针
                # (ApiRequest 由前端 common-lib 懒加载,首次进 /c/{id} 才出现,
                #  在主页 goto 后立刻探大概率 False,造成误判)
                self._waf_cleared = True
                log.info("[QwenClient] Headless 模式：acw_tc cookie 有效,视为已过 WAF")
                return True

            # acw_tc 缺失或过期 → 主动清掉旧 cookie,重新导航拿全新 WAF 挑战。
            # 关键:必须在清 cookie 后 reload——不重新加载,页面 HTML/挑战状态
            # 还是旧会话的,后续检测全是空中楼阁。
            try:
                await page.context.clear_cookies()
            except Exception:
                pass
            log.info("[QwenClient] Headless 模式：acw_tc 无效,清 cookie 后重新导航获取新 WAF 挑战...")
            try:
                await page.goto(f"{BASE_URL}/", timeout=60000, wait_until="domcontentloaded")
                await page.wait_for_timeout(5000)
            except Exception as e:
                log.warning("[QwenClient] Headless 重新导航异常: %s", e)

            # 重新导航后服务端可能直接发了新 acw_tc(透明 JS 挑战自动过) → 再查一次
            try:
                cookies2 = await page.context.cookies()
                if _has_valid_acw(cookies2):
                    self._waf_cleared = True
                    log.info("[QwenClient] Headless 模式：重新导航后取得新 acw_tc,已过 WAF")
                    await self._save_cookies()
                    return True
            except Exception:
                pass

            # 还没过 → 按页面实际出现的挑战类型分诊：
            #   * noCaptcha「拖到最右」(#nc_1_n1z)：punish 页的真正形态，无拼图，
            #     YOLO 在此无用，用 backend/services/nc_slider.py 的拟人拖拽。
            #   * aliyunCaptcha 拼图(#aliyunCaptcha-img)：仅注册/登录等页出现，
            #     才用 auto_captcha.solve_slider(YOLO)。
            # 主页 GOTO 后两种一般都不出现（直接透明过），此时直接返回 False，
            # 不再空等 10s+（旧逻辑无条件调 solve_slider，白白阻塞）。
            try:
                has_nc = await page.query_selector("#nocaptcha, #nc_1_n1z, .btn_slide") is not None
            except Exception:
                has_nc = False
            try:
                has_puzzle = await page.query_selector("#aliyunCaptcha-img") is not None
            except Exception:
                has_puzzle = False
            if not has_nc and not has_puzzle:
                log.info("[QwenClient] Headless 模式：无滑块元素，仅 acw_tc 缺失，跳过破解等待")
                return False
            try:
                ok = False
                if has_nc:
                    from backend.services.nc_slider import solve_nocaptcha_slider
                    log.warning("[QwenClient] Headless 模式：检测到 noCaptcha 滑块，尝试拟人拖拽...")
                    ok = await solve_nocaptcha_slider(page)
                elif has_puzzle:
                    from auto_captcha import solve_slider
                    log.warning("[QwenClient] Headless 模式：检测到拼图滑块，尝试 YOLO 破解...")
                    ok = await solve_slider(page, max_attempts=3)
                if ok:
                    # 等前端页面状态刷新 + 落 acw_tc 等新 cookie
                    await page.wait_for_timeout(2000)
                    cookies3 = await page.context.cookies()
                    if _has_valid_acw(cookies3):
                        self._waf_cleared = True
                        log.info("[QwenClient] Headless 模式：滑块破解成功,acw_tc 已写入,落 cookie")
                        await self._save_cookies()
                        return True
                    log.warning("[QwenClient] Headless 模式：滑块破解后仍未出现有效 acw_tc,视作 WAF 未通过")
                else:
                    log.warning("[QwenClient] Headless 模式：滑块破解失败,建议设置 BROWSER_HEADED=1")
            except Exception as e:
                log.warning("[QwenClient] Headless WAF 检测/破解异常: %s", e)
            return False

    # ========================================================================
    # UI 驱动流式：通过前端对话框发送消息并拦截完整 SSE（绕过 BaXia/WAF）
    # ========================================================================
    async def _stream_chat_via_browser_ui(self, token: str, chat_id: str, payload: dict, timeout: float = 120.0) -> dict:
        """驱动前端 UI 发送消息并拦截完整 SSE 响应。

        Qwen 前端改版后 window.ApiRequest 已移除；原生 fetch/XHR 也被 BaXia
        拦截挂起。实测唯一可靠路径是驱动前端 UI 输入消息 + 回车 —— 前端
        common-lib 会在请求层注入运行时 bx 签名（bx-ua/bx-umidtoken），
        stream 端点才肯放行。我们只负责在对话页 `/c/{chat_id}` 里输入内容、
        回车发送，并用 expect_response 拦截返回的完整 SSE。

        注意：
        - UI 发送使用前端页面当前配置（模型由 create_chat 决定；thinking /
          tools 等 feature_config 走前端默认），与 payload 中的 feature_config
          不完全一致 —— 复杂自定义工具链仍以 curl_cffi/httpx 直连为优先。
        - 必须串行执行（_ui_stream_lock），避免多请求争抢同一页面状态。
        """
        async with self._ui_stream_lock:
            main_page = await self._ensure_main_page()

            # 1) 注入登录态：token cookie + localStorage（新版前端读 cookie 判断登录）
            escaped_token = token.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
            try:
                await main_page.context.add_cookies([
                    {"name": "token", "value": token, "domain": "chat.qwen.ai", "path": "/"},
                ])
                log.info("[QwenClient] UI 驱动：已注入 token cookie (len=%d)", len(token))
            except Exception as e:
                log.warning("[QwenClient] UI 驱动：注入 token cookie 失败: %s", e)
            try:
                await main_page.evaluate(f"localStorage.setItem('token', '{escaped_token}')")
            except Exception:
                pass

            # 2) 提取最后一条 user 消息文本
            messages = payload.get("messages") or []
            user_msg = ""
            for m in reversed(messages):
                if isinstance(m, dict) and m.get("role") == "user":
                    content = m.get("content") or ""
                    if isinstance(content, str):
                        user_msg = content
                    elif isinstance(content, list):
                        user_msg = " ".join(
                            p.get("text", "") for p in content
                            if isinstance(p, dict) and p.get("type") == "text"
                        )
                    break
            user_msg = (user_msg or "").strip()
            if not user_msg:
                log.warning("[QwenClient] UI 驱动：payload 中未找到 user 消息文本")
                return {"status": 0, "body": "UI stream: no user message text in payload"}

            # 3) 导航到对话页（前端 stream 请求的 referer 是 /c/{chat_id}）
            try:
                await main_page.goto(f"{BASE_URL}/c/{chat_id}", timeout=45000, wait_until="domcontentloaded")
            except Exception as e:
                log.warning("[QwenClient] UI 驱动：导航对话页异常: %s", e)
            await main_page.wait_for_timeout(3500)

            # 4) 等待聊天输入框就绪
            try:
                await main_page.click('textarea, [contenteditable="true"]', timeout=10000)
            except Exception as e:
                log.warning("[QwenClient] UI 驱动：找不到聊天输入框: %s", e)
                return {"status": 0, "body": f"UI stream: chat input not found: {e}"}

            # 5) 输入消息 + 发送，同时拦截 stream 响应
            #    注意：不能用 keyboard.type —— 对长文本/含换行的内容会丢失字符
            #    （T2/T3 实测：176/1184 字符 prompt 送达模型时只剩开头）。
            #    fill 一次性写入完整值；发送优先点「发送」按钮（fill+Enter 在
            #    前端 0.2.91 下偶发不触发提交，请求根本发不出去）。
            #
            #    顺带：拦截"实际发出去"的 chat/completions 请求头，
            #    把运行时 bx-ua/bx-umidtoken/bx-v/user-agent 落到 bx_pool.json 队首，
            #    让后续 curl_cffi/httpx 复用同一对签名(与当前 cookie 同会话)。
            #    注意只收**真实签名**：BaXia 传感器未就绪时请求头里是
            #    `defaultFY2_load_failed with timeout` 占位值（73 字符），这种
            #    必须跳过，否则会污染 bx_pool 队首（旧 once() 只抓第一条，
            #    首条恰为占位时 100% 污染）。用 on() 常驻监听 + 只保留真实值。
            captured_bx_headers: dict[str, str] = {}

            def _on_request(req):
                if "chat/completions" in req.url and "punish" not in req.url:
                    h = req.headers or {}
                    bx_ua = h.get("bx-ua", "")
                    if not bx_ua or "default" in bx_ua or len(bx_ua) < 200:
                        return  # 占位签名，跳过
                    for k in ("bx-ua", "bx-umidtoken", "bx-v", "user-agent"):
                        v = h.get(k)
                        if v:
                            captured_bx_headers[k] = v

            main_page.on("request", _on_request)
            try:
                url_pattern = lambda r: "chat/completions" in r.url and "punish" not in r.url
                async with main_page.expect_response(url_pattern, timeout=timeout * 1000) as resp_info:
                    await main_page.fill('textarea, [contenteditable="true"]', user_msg)
                    await main_page.wait_for_timeout(400)
                    sent = await main_page.evaluate("""() => {
                        const btns = Array.from(document.querySelectorAll('button'));
                        for (const b of btns) {
                            const t = (b.getAttribute('aria-label') || '') + (b.innerText || '');
                            if (/send|发送/i.test(t) && !b.disabled) { b.click(); return true; }
                        }
                        return false;
                    }""")
                    if not sent:
                        await main_page.keyboard.press("Enter")
                resp = await resp_info.value
                status = resp.status
                sse_text = await resp.text()
                log.info("[QwenClient] UI 驱动 stream 完成 status=%d len=%d", status, len(sse_text))
            except Exception as e:
                log.warning("[QwenClient] UI 驱动 stream 异常: %s", e)
                # 异常也算"前端已经过 WAF",落 cookie + 尝试落 bx_pool
                await self._persist_after_ui_round(captured_bx_headers)
                return {"status": 0, "body": f"UI stream failed: {e}"}
            finally:
                try:
                    main_page.remove_listener("request", _on_request)
                except Exception:
                    pass

            # 6) 检测 punish / WAF
            is_waf, waf_type = self._is_waf_challenge(sse_text[:2000])
            # 落运行时 bx 头 + 落 cookie,让后续 curl_cffi/httpx 走同一份会话
            await self._persist_after_ui_round(captured_bx_headers)
            return {"status": status, "body": sse_text, "waf": waf_type if is_waf else None}

    # ========================================================================
    # 媒体生成（图片/视频）：驱动前端 UI 生成模式并拦截响应（绕过 punish）
    # ========================================================================
    async def _generate_media_via_browser_ui(self, token: str, chat_id: str, model: str, prompt: str, media_type: str = "t2i", timeout: float = 300.0, files: list[str] | None = None) -> dict:
        """驱动前端 UI 的"生成图像/创建视频"模式生成媒体并拦截上游响应。

        前端"选择模式"(.mode-select-open) 菜单含 生成图像(t2i) / 创建视频(t2v)
        两个入口。切到对应模式后在对话框输入 prompt 并回车，前端 common-lib
        会发出一条**带运行时 bx 签名**的 chat/completions 请求（非文本模式，
        不会被 BaXia 拦截），我们只需拦截该响应：

        - t2i: 响应为 SSE，`delta.content` 即带签名 key 的图片 URL
              （cdn.qwenlm.ai/output/.../t2i/....png?key=jwt）
        - t2v: 响应为 JSON，`data.messages[0].extra.wanx.task_id` 是视频任务 ID，
              调用方需继续轮询 /api/v2/task/status/{task_id} 取最终视频 URL

        与 _stream_chat_via_browser_ui 不同：这里**必须先切换生成模式**，
        否则前端只当普通文本对话回复（提取到的 URL 无签名 → 404）。
        """
        async with self._ui_stream_lock:
            main_page = await self._ensure_main_page()

            # 1) 登录态注入：token cookie + localStorage
            escaped_token = token.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
            try:
                await main_page.context.add_cookies([
                    {"name": "token", "value": token, "domain": "chat.qwen.ai", "path": "/"},
                ])
                log.info("[QwenClient] 媒体生成：已注入 token cookie (len=%d)", len(token))
            except Exception as e:
                log.warning("[QwenClient] 媒体生成：注入 token cookie 失败: %s", e)
            try:
                await main_page.evaluate(f"localStorage.setItem('token', '{escaped_token}')")
            except Exception:
                pass

            # 2) 导航到对话页
            try:
                await main_page.goto(f"{BASE_URL}/c/{chat_id}", timeout=45000, wait_until="domcontentloaded")
            except Exception as e:
                log.warning("[QwenClient] 媒体生成：导航对话页异常: %s", e)
            await main_page.wait_for_timeout(3500)

            # 2.5) i2v（图生视频）：先通过"上传附件"上传参考图，再切创建视频模式
            if media_type == "i2v" and files:
                try:
                    await main_page.click(".mode-select-open", timeout=8000)
                    await main_page.wait_for_timeout(1000)
                    up_clicked = await main_page.evaluate("""async () => {
                        const items = document.querySelectorAll('[class*="mode-select-dropdown-item"]');
                        for (const it of items) {
                            if ((it.innerText || '').includes('上传附件')) { it.click(); return true; }
                        }
                        return false;
                    }""")
                    if not up_clicked:
                        return {"status": 0, "body": "UI media: upload attachment menu not found"}
                    await main_page.wait_for_timeout(1500)
                    # #filesUpload 是隐藏的 file input；set_input_files 内部会等待元素 attach
                    local_files = [f for f in files if os.path.exists(f)]
                    if not local_files:
                        return {"status": 0, "body": "UI media: no local reference image files exist"}
                    await main_page.set_input_files('#filesUpload', local_files, timeout=15000)
                    # 等待前端上传到 OSS（附件卡片出现 + 上传完成）
                    await main_page.wait_for_timeout(8000)
                    log.info("[QwenClient] 媒体生成：已上传 %d 个参考图到对话", len(local_files))
                except Exception as e:
                    log.warning("[QwenClient] 媒体生成：上传参考图失败: %s", e)
                    return {"status": 0, "body": f"UI media: upload refs failed: {e}"}
                await main_page.wait_for_timeout(1000)

            # 3) 打开"选择模式"菜单，点击生成图像/创建视频
            label = "创建视频" if media_type in ("t2v", "i2v") else "生成图像"
            try:
                await main_page.click(".mode-select-open", timeout=8000)
                await main_page.wait_for_timeout(1200)
            except Exception as e:
                log.warning("[QwenClient] 媒体生成：找不到选择模式按钮: %s", e)
                return {"status": 0, "body": f"UI media: mode-select button not found: {e}"}
            try:
                clicked = await main_page.evaluate(f"""async () => {{
                    const items = document.querySelectorAll('[class*="mode-select-dropdown-item"]');
                    for (const it of items) {{
                        if ((it.innerText || '').includes('{label}')) {{ it.click(); return true; }}
                    }}
                    return false;
                }}""")
                if not clicked:
                    return {"status": 0, "body": f"UI media: menu item '{label}' not found"}
            except Exception as e:
                return {"status": 0, "body": f"UI media: click '{label}' failed: {e}"}
            await main_page.wait_for_timeout(1500)

            # 4) 输入 prompt + 发送，拦截 chat/completions 上游响应
            #    顺带:拦截 request 头拿运行时 bx 签名,落 bx_pool 队首 + 落 cookie
            #    （只收真实签名，跳过 BaXia 占位值 defaultFY2_*，见文本路径注释）
            captured_bx_headers: dict[str, str] = {}

            def _on_media_request(req):
                if "chat/completions" in req.url and "punish" not in req.url:
                    h = req.headers or {}
                    bx_ua = h.get("bx-ua", "")
                    if not bx_ua or "default" in bx_ua or len(bx_ua) < 200:
                        return
                    for k in ("bx-ua", "bx-umidtoken", "bx-v", "user-agent"):
                        v = h.get(k)
                        if v:
                            captured_bx_headers[k] = v

            main_page.on("request", _on_media_request)
            try:
                await main_page.click('textarea, [contenteditable="true"]', timeout=8000)
            except Exception as e:
                try:
                    main_page.remove_listener("request", _on_media_request)
                except Exception:
                    pass
                return {"status": 0, "body": f"UI media: chat input not found: {e}"}
            try:
                url_pat = lambda r: "chat/completions" in r.url and "punish" not in r.url
                async with main_page.expect_response(url_pat, timeout=timeout * 1000) as resp_info:
                    # fill 一次性写入完整 prompt（keyboard.type 对长文本会丢字）
                    await main_page.fill('textarea, [contenteditable="true"]', prompt)
                    await main_page.keyboard.press("Enter")
                resp = await resp_info.value
                body_text = await resp.text()
                log.info("[QwenClient] 媒体生成 %s 完成 status=%d len=%d", media_type, resp.status, len(body_text))
                # 落运行时 bx 头 + 落 cookie
                await self._persist_after_ui_round(captured_bx_headers)
                return {"status": resp.status, "body": body_text}
            except Exception as e:
                log.warning("[QwenClient] 媒体生成 %s 异常: %s", media_type, e)
                await self._persist_after_ui_round(captured_bx_headers)
                return {"status": 0, "body": f"UI media {media_type} failed: {e}"}
            finally:
                try:
                    main_page.remove_listener("request", _on_media_request)
                except Exception:
                    pass

    # ========================================================================
    # 浏览器内 API 请求（通过 window.ApiRequest 绕过 BaXia）
    # ========================================================================
    async def _browser_request_json(self, method: str, url: str, token: str, body: dict | None = None, timeout: float = 120.0) -> dict:
        """通过浏览器 page.evaluate 使用 window.ApiRequest 发送请求。

        Qwen 前端的 window.ApiRequest 是官方 API 客户端，自动处理：
        - bx-* 安全头的注入（通过 BaXia 集成）
        - 请求签名和时间戳
        - 会话身份验证

        相比直接使用 window.fetch（被 BaXia 包裹导致挂起），ApiRequest 是受支持的途径。
        如果 ApiRequest 不可用，降级到 window.fetch。
        """
        main_page = await self._ensure_main_page()
        
        # 设置 token 到 localStorage + cookie（新版前端读 cookie 判断登录态）
        escaped_token = token.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        try:
            await main_page.context.add_cookies([
                {"name": "token", "value": token, "domain": "chat.qwen.ai", "path": "/"},
            ])
        except Exception:
            pass
        try:
            await main_page.evaluate(f"localStorage.setItem('token', '{escaped_token}')")
        except Exception:
            pass
        
        # 安全的字符串转义
        def _js_str(s):
            return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        
        safe_url = _js_str(url)
        safe_method = _js_str(method)
        safe_token = escaped_token
        
        body_json = ""
        if body is not None:
            body_json = json.dumps(body, ensure_ascii=False)
        
        timeout_ms = int(timeout * 1000)
        
        # 构建 JS：优先使用 window.ApiRequest，降级到 window.fetch
        # ApiRequest API（从运行时检测得出）：
        #   ApiRequest.post(url, bodyObj, opts)  → POST with object body
        #   ApiRequest.postBody(url, bodyStr, opts) → POST with string body
        #   ApiRequest.request(method, url, opts) → generic (no body)
        #   ApiRequest.requestBody(method, url, bodyStr, opts) → generic with string body
        has_body = body is not None
        if has_body:
            # 使用 JSON.parse/stringify 确保 body_json 在 JS 中可用
            request_js = f"""
(async () => {{
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), {timeout_ms});
    try {{
        const useApiRequest = typeof window.ApiRequest !== 'undefined' && typeof window.ApiRequest.postBody === 'function';
        
        if (useApiRequest) {{
            // 使用 Qwen 官方 ApiRequest（自动处理 bx-* 安全头）
            // postBody 接受 (url, bodyString, opts) —— body 以字符串传递
            const resp = await window.ApiRequest.postBody(
                '{safe_url}',
                '{_js_str(body_json)}',
                {{
                    headers: {{
                        'Authorization': 'Bearer {safe_token}',
                        'Content-Type': 'application/json',
                    }},
                    signal: controller.signal,
                }}
            );
            clearTimeout(id);
            // ApiRequest 返回的响应可能有不同格式
            if (typeof resp.json === 'function') {{
                const data = await resp.json();
                return {{ status: resp.status || 200, body: JSON.stringify(data) }};
            }} else if (typeof resp.text === 'function') {{
                const text = await resp.text();
                return {{ status: resp.status || 200, body: text }};
            }} else {{
                return {{ status: 200, body: JSON.stringify(resp) }};
            }}
        }} else {{
            // 降级到 window.fetch（可能被 BaXia 拦截或挂起）
            const resp = await fetch('{safe_url}', {{
                method: '{safe_method}',
                headers: {{
                    'Authorization': 'Bearer {safe_token}',
                    'Content-Type': 'application/json',
                    'Accept': 'application/json, text/plain, */*',
                    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    'Origin': '{_js_str(BASE_URL)}',
                    'Referer': '{_js_str(BASE_URL)}/',
                    'source': 'web',
                }},
                body: '{_js_str(body_json)}',
                signal: controller.signal,
                credentials: 'include',
            }});
            clearTimeout(id);
            const text = await resp.text();
            return {{ status: resp.status, body: text }};
        }}
    }} catch (e) {{
        clearTimeout(id);
        return {{ status: 0, body: e.toString() }};
    }}
}})()
"""
        else:
            # 没有 body 的请求（GET/DELETE）
            request_js = f"""
(async () => {{
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), {timeout_ms});
    try {{
        const useApiRequest = typeof window.ApiRequest !== 'undefined' && typeof window.ApiRequest.request === 'function';
        
        if (useApiRequest) {{
            const resp = await window.ApiRequest.request(
                '{safe_method}',
                '{safe_url}',
                {{
                    headers: {{
                        'Authorization': 'Bearer {safe_token}',
                    }},
                    signal: controller.signal,
                }}
            );
            clearTimeout(id);
            if (typeof resp.json === 'function') {{
                const data = await resp.json();
                return {{ status: resp.status || 200, body: JSON.stringify(data) }};
            }} else if (typeof resp.text === 'function') {{
                const text = await resp.text();
                return {{ status: resp.status || 200, body: text }};
            }} else {{
                return {{ status: 200, body: JSON.stringify(resp) }};
            }}
        }} else {{
            const resp = await fetch('{safe_url}', {{
                method: '{safe_method}',
                headers: {{
                    'Authorization': 'Bearer {safe_token}',
                    'Accept': 'application/json, text/plain, */*',
                    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    'Origin': '{_js_str(BASE_URL)}',
                    'Referer': '{_js_str(BASE_URL)}/',
                    'source': 'web',
                }},
                signal: controller.signal,
                credentials: 'include',
            }});
            clearTimeout(id);
            const text = await resp.text();
            return {{ status: resp.status, body: text }};
        }}
    }} catch (e) {{
        clearTimeout(id);
        return {{ status: 0, body: e.toString() }};
    }}
}})()
"""
        log.info("[QwenClient] browser_fetch: 发送 %s %s ... (useApiRequest=%s)", method, url.split('?')[0], "yes")
        try:
            result = await asyncio.wait_for(
                main_page.evaluate(request_js),
                timeout=timeout + 5.0,  # 额外 5 秒用于 JS 执行开销
            )
            status = result.get("status", 0)
            body_len = len(result.get("body", ""))
            log.info("[QwenClient] browser_fetch 完成: status=%d 长度=%d", status, body_len)
            if body_len > 0 and status != 0:
                log.info("[QwenClient] browser_fetch 响应前100: %s", result.get("body", "")[:100])
            return result
        except asyncio.TimeoutError:
            log.error(f"[QwenClient] browser_fetch 超时 ({timeout}s)")
            return {"status": 0, "body": f"browser fetch timeout ({timeout}s)"}
        except Exception as e:
            log.error(f"[QwenClient] browser_fetch 异常: {e}", exc_info=True)
            return {"status": 0, "body": str(e)}
        finally:
            await self._save_cookies()

    # ========================================================================
    #  流式聊天请求
    # ========================================================================
    async def _stream_chat_via_curl_cffi(self, token: str, chat_id: str, payload: dict, timeout: float = 120.0) -> dict | None:
        """优先走 curl_cffi(chrome131)+bx 头直连上游，避免 Playwright 超时。

        【缓冲模式】用于 t2i/t2v 等需要一次性拿到完整 SSE 再解析 task_id/URL 的场景。
        用 (connect, idle) 元组超时：curl 在 stream 关闭时以 LOW_SPEED 语义判定，
        即"整段响应期间持续无数据"才中断，而不是对总时长设硬上限，
        因此长任务不会被拦腰截断。
        """
        try:
            from curl_cffi.requests import AsyncSession
        except Exception as e:
            log.warning("[QwenClient] curl_cffi 不可用: %s", e)
            return None

        headers = self._build_headers(token)
        headers["Accept"] = "text/event-stream, application/json"
        url = f"{BASE_URL}/api/v2/chat/completions?chat_id={chat_id}"
        # 元组超时：(连接超时, 读取空闲超时)；stream 关闭 → curl 用 LOW_SPEED_TIME=connect+idle
        idle = max(timeout, _CHAT_STREAM_IDLE_TIMEOUT)
        tup_timeout = (_CHAT_STREAM_CONNECT_TIMEOUT, idle)
        try:
            async with AsyncSession(impersonate="chrome131", timeout=tup_timeout) as session:
                chunks: list[str] = []
                async with session.stream("POST", url, headers=headers, json=payload) as resp:
                    async for chunk in resp.aiter_content():
                        if chunk:
                            chunks.append(chunk.decode("utf-8", errors="replace"))
                    status_code = resp.status_code
                return {"status": status_code, "body": "".join(chunks)}
        except Exception as e:
            log.warning("[QwenClient] curl_cffi 流式请求失败: %s", e)
            return None

    async def _stream_chat_via_curl_cffi_iter(self, token: str, chat_id: str, payload: dict):
        """【真流式模式】逐块产出上游 SSE，用于对话流式返回。

        与缓冲版的区别：数据一到就 yield，客户端 SSE 连接持续有数据流动，
        不会因等待完整响应而空闲断连；同时用 (connect, idle) 元组超时，
        只在"长时间完全无新数据"时才中断，长任务/agent 全程不被总时长上限杀掉。

        产出:
          {"status": int, "body": str}                # 首个错误/WAF 状态帧（非 200 时）
          {"chunk": str}                              # 正常 SSE 数据块
        """
        try:
            from curl_cffi.requests import AsyncSession
        except Exception as e:
            log.warning("[QwenClient] curl_cffi 不可用: %s", e)
            yield {"status": 0, "body": f"curl_cffi unavailable: {e}"}
            return

        headers = self._build_headers(token)
        headers["Accept"] = "text/event-stream, application/json"
        url = f"{BASE_URL}/api/v2/chat/completions?chat_id={chat_id}"
        tup_timeout = (_CHAT_STREAM_CONNECT_TIMEOUT, _CHAT_STREAM_IDLE_TIMEOUT)
        try:
            async with AsyncSession(impersonate="chrome131", timeout=tup_timeout) as session:
                async with session.stream("POST", url, headers=headers, json=payload) as resp:
                    status_code = resp.status_code
                    if status_code != 200:
                        body_chunks: list[str] = []
                        async for chunk in resp.aiter_content():
                            if chunk:
                                body_chunks.append(chunk.decode("utf-8", errors="replace"))
                        yield {"status": status_code, "body": "".join(body_chunks)[:2000]}
                        return
                    async for chunk in resp.aiter_content():
                        if chunk:
                            yield {"chunk": chunk.decode("utf-8", errors="replace")}
        except Exception as e:
            log.warning("[QwenClient] curl_cffi 真流式请求失败: %s", e)
            yield {"status": 0, "body": str(e)}

    async def stream_chat_once(self, token: str, chat_id: str, payload: dict, _punish_healed: bool = False) -> AsyncIterator[dict]:
        """流式聊天：curl_cffi 真流式直连 → punish 自愈（仅一次）→ 浏览器 UI 降级。

        `_punish_healed` 为内部递归标记：自愈重试只做一次，避免死循环。
        """
        # 先尝试真流式：逐块透传，遇到首个数据块即视为成功；非 200/WAF 再降级浏览器。
        first_error: dict | None = None
        got_data = False
        yielded_any = False  # 是否已向下游 yield 过 chunk（用于只在首帧检测 WAF）
        buffer = ""
        async for item in self._stream_chat_via_curl_cffi_iter(token, chat_id, payload):
            if "chunk" in item:
                got_data = True
                buffer += item["chunk"]
                # 保持 SSE 事件边界（\n\n）切分后透传
                while "\n\n" in buffer:
                    msg, buffer = buffer.split("\n\n", 1)
                    # 尚未向下游发过任何数据时，若首帧命中 WAF 文本，转错误帧走降级
                    if not yielded_any and self._is_waf_challenge(msg)[0]:
                        # 保留完整首帧（punish 跳转 URL 藏在里面，截断就提不出来）
                        first_error = {"status": 403, "body": msg[:8000]}
                        got_data = False
                        break
                    yield {"chunk": msg + "\n\n"}
                    yielded_any = True
                if first_error is not None:
                    break
                continue
            # 状态帧（非 200 或 curl 失败）
            status0 = item.get("status", 0)
            body0 = item.get("body", "") or ""
            first_error = {"status": status0, "body": body0[:8000]}
            break

        if got_data:
            # flush 残留 buffer
            if buffer.strip():
                # curl_cffi 的 punish JSON 响应（{"ret":["FAIL_SYS_USER_VALIDATE"...],
                # "data":{"url":"...punish?x5secdata=x5step..."}}）没有 \n\n 分隔，
                # 上面的 while 循环拆分不到，会残留在这里被当成普通数据透传。
                # 若尚未向客户端透传任何数据 → 转错误帧，走下方浏览器 UI 降级。
                if not yielded_any and self._is_waf_challenge(buffer)[0]:
                    first_error = {"status": 403, "body": buffer[:8000]}
                else:
                    yield {"chunk": buffer if buffer.endswith("\n\n") else buffer + "\n\n"}
            if first_error is None:
                return

        # 直连命中 punish → 尝试浏览器内自愈（开挑战页拖滑块拿 x5sec），仅一次。
        # 自愈成功则递归重试直连（_punish_healed=True 保证不循环）。
        if first_error is not None and not _punish_healed:
            err_body = first_error.get("body", "") or ""
            is_waf0, waf0 = self._is_waf_challenge(err_body)
            if is_waf0 and waf0 == "punish":
                punish_url = self._extract_punish_url(err_body)
                if punish_url:
                    log.warning("[QwenClient] 直连命中 punish，尝试浏览器内自愈...")
                    try:
                        healed = await self._clear_punish_via_browser(punish_url)
                    except Exception as e:
                        log.warning("[QwenClient] punish 自愈异常: %s", e)
                        healed = False
                    if healed:
                        log.info("[QwenClient] punish 自愈成功，重试直连")
                        async for item in self.stream_chat_once(token, chat_id, payload, _punish_healed=True):
                            yield item
                        return
                    log.warning("[QwenClient] punish 自愈失败，走 UI 降级")

        # 未取得任何有效数据（或仅有 WAF/惩罚帧） → 降级浏览器 UI 驱动
        use_browser = True
        if first_error is not None:
            is_waf0, waf0 = self._is_waf_challenge(first_error.get("body", "") or "")
            log.warning(
                "[QwenClient] curl_cffi 真流式未取得数据 status=%s waf=%s，降级浏览器",
                first_error.get("status"), waf0 or "-",
            )

        result = await self._stream_chat_via_browser_ui(
            token, chat_id, payload, timeout=_CHAT_STREAM_IDLE_TIMEOUT,
        )

        status = result.get("status", 0)
        raw_text = result.get("body", "")

        # 检测 WAF 挑战
        is_waf, waf_type = self._is_waf_challenge(raw_text)
        if is_waf:
            log.warning("[QwenClient] 上游返回 WAF 挑战 (type=%s, status=%d)", waf_type, status)
            if waf_type == "punish" and not _punish_healed:
                # UI 路径也被 punish：说明整个会话被标记，先自愈再让上层重试。
                # 这里只做自愈（拿 x5sec + 落 cookie），把 403 原样返回，
                # executor 的 punish 重试逻辑会作废会话后再次进入本函数，
                # 届时直连大概率已恢复。
                punish_url = self._extract_punish_url(raw_text)
                if punish_url:
                    log.info("[QwenClient] UI 路径命中 punish，尝试自愈会话...")
                    try:
                        if await self._clear_punish_via_browser(punish_url):
                            log.info("[QwenClient] UI 路径 punish 自愈成功")
                    except Exception as e:
                        log.warning("[QwenClient] UI 路径 punish 自愈异常: %s", e)
            yield {"status": 403, "body": f"WAF challenge ({waf_type}): {raw_text[:300]}"}
            return

        if status != 200:
            log.warning("[QwenClient] 上游 HTTP %d: %.200s", status, raw_text)
            yield {"status": status, "body": raw_text[:500]}
            return

        # 浏览器降级路径是缓冲返回：按原始字节流逐块输出（保持 SSE 格式）
        buffer = ""
        for chunk_text in [raw_text[i:i+4096] for i in range(0, len(raw_text), 4096)]:
            buffer += chunk_text
            while "\n\n" in buffer:
                msg, buffer = buffer.split("\n\n", 1)
                yield {"chunk": msg + "\n\n"}
