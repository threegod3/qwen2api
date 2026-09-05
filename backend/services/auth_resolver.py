import asyncio
import html as html_lib
import json
import logging
import random
import re
import string
import time
from typing import Optional

from backend.core.account_pool import Account, AccountPool
from backend.core.browser_engine import _new_browser
from backend.core.config import settings
log = logging.getLogger(__name__)

BASE_URL = "https://chat.qwen.ai"

async def _verify_qwen_token(token: str) -> bool:
    if not token:
        return False
    try:
        import httpx
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://chat.qwen.ai/",
            "Origin": "https://chat.qwen.ai",
            "Connection": "keep-alive"
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{BASE_URL}/api/v1/auths/", headers=headers)
        if resp.status_code != 200:
            return False
        try:
            data = resp.json()
            return data.get("role") == "user"
        except Exception:
            txt = resp.text.lower()
            return 'aliyun_waf' in txt or '<!doctype' in txt
    except Exception:
        return False


async def get_fresh_token(email: str, password: str) -> str:
    """如果提供了此功能，用 playwright 重新登录获取 Token，这里提供一个 mock 或抛错以防未实现"""
    raise NotImplementedError("Auto-login not fully implemented yet in the separated architecture")

def _gen_password(length=14):
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pwd = "".join(random.choices(chars, k=length))
        if (any(c.isupper() for c in pwd) and any(c.islower() for c in pwd)
                and any(c.isdigit() for c in pwd) and any(c in "!@#$%^&*" for c in pwd)):
            return pwd

def _gen_username():
    first = random.choice(["Alex", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie",
                            "Drew", "Avery", "Quinn", "Blake", "Sage", "Reese", "Dakota", "Emery"])
    last = random.choice(["Smith", "Brown", "Wilson", "Lee", "Chen", "Wang", "Kim", "Park",
                           "Davis", "Miller", "Garcia", "Martinez", "Anderson", "Taylor", "Thomas"])
    return f"{first} {last}"

MAIL_BASE = "https://mail.chatgpt.org.uk"
MAIL_LINK_KEYWORDS = ("qwen", "verify", "activate", "confirm", "aliyun", "alibaba", "qwenlm")

async def _extract_verify_link_from_page(page) -> str:
    js_find_link = """() => {
        const keywords = ['qwen', 'verify', 'activate', 'confirm', 'aliyun', 'alibaba', 'qwenlm'];
        const links = Array.from(document.querySelectorAll('a[href]'));
        for (const link of links) {
            const href = link.href || '';
            const text = (link.textContent || '').toLowerCase();
            if (keywords.some((keyword) => href.toLowerCase().includes(keyword))) return href;
            if (keywords.some((keyword) => text.includes(keyword)) && href.startsWith('http')) return href;
        }
        const html = document.body ? document.body.innerHTML : '';
        const matches = html.match(/https?:\\/\\/[^"'\\s<>\\\\]+/g) || [];
        for (const match of matches) {
            if (keywords.some((keyword) => match.toLowerCase().includes(keyword))) return match;
        }
        return null;
    }"""

    try:
        iframe_el = await page.query_selector('#emailFrame')
        if iframe_el:
            await asyncio.sleep(3)
            frame = await iframe_el.content_frame()
            if frame:
                verify_link = await frame.evaluate(js_find_link)
                if verify_link:
                    return verify_link
    except Exception as e:
        log.debug(f"[Activate] iframe read failed: {e}")

    try:
        return await page.evaluate(js_find_link) or ""
    except Exception:
        return ""

async def _find_verify_link_via_mail_page(email: str) -> str:
    mail_url = f"{MAIL_BASE}/{email}"
    try:
        async with _new_browser() as browser:
            page = await browser.new_page()
            try:
                await page.goto(mail_url, wait_until="networkidle", timeout=30000)
            except Exception:
                try:
                    await page.goto(mail_url, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
            await asyncio.sleep(6)

            # Dismiss the site announcement modal if present; it intercepts pointer events.
            try:
                await page.evaluate("""() => {
                    const modal = document.querySelector('#siteAnnouncementModal');
                    if (modal) {
                        modal.classList.remove('active');
                        modal.setAttribute('aria-hidden', 'true');
                        modal.style.display = 'none';
                        modal.style.pointerEvents = 'none';
                    }
                    document.querySelectorAll('.modal-overlay, .announcement-modal-overlay').forEach((el) => {
                        el.classList.remove('active');
                        el.setAttribute('aria-hidden', 'true');
                        el.style.display = 'none';
                        el.style.pointerEvents = 'none';
                    });
                }""")
                await asyncio.sleep(0.5)
            except Exception:
                pass

            clicked_email = False
            for sel in ['#emailList li:first-child', '#emailList li', '[class*="EmailItem"]',
                        '[class*="email-item"]', '[class*="MailItem"]', '[class*="mail-item"]',
                        'table tbody tr:first-child', '[role="row"]:first-child']:
                try:
                    await page.wait_for_selector(sel, timeout=10000)
                    el = await page.query_selector(sel)
                    if el:
                        await el.click(force=True)
                        await asyncio.sleep(4)
                        clicked_email = True
                        break
                except Exception:
                    pass

            if not clicked_email:
                for sel in ['li', 'tr', 'div[class]', '[class*="row"]', '[class*="item"]']:
                    try:
                        els = await page.query_selector_all(sel)
                        for el in (els or [])[:10]:
                            try:
                                text = await el.inner_text()
                                if any(keyword in text.lower() for keyword in MAIL_LINK_KEYWORDS):
                                    await el.click(force=True)
                                    await asyncio.sleep(4)
                                    clicked_email = True
                                    break
                            except Exception:
                                pass
                        if clicked_email:
                            break
                    except Exception:
                        pass

            return await _extract_verify_link_from_page(page)
    except Exception as e:
        log.warning(f"[Activate] mailbox page fallback failed for {email}: {e}")
        return ""

class _EmailSession:
    def __init__(self):
        from curl_cffi import requests as cffi_requests
        self._session = cffi_requests.Session(impersonate="chrome")
        self._session.headers.update({
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self._current_token = ""
        self._token_expires_at = 0
        self._initialized = False

    def _init_session(self) -> bool:
        try:
            resp = self._session.get(f"{MAIL_BASE}/", timeout=15)
            if resp.status_code != 200:
                return False
            match = re.search(r'window\.__BROWSER_AUTH\s*=\s*(\{[^}]+\})', resp.text)
            if match:
                auth_data = json.loads(match.group(1))
                self._current_token = auth_data.get("token", "")
                self._token_expires_at = auth_data.get("expires_at", 0)
                self._initialized = True
                return True
            # 新增获取 Token 的 fallback 逻辑
            resp = self._session.get(f"{MAIL_BASE}/api/auth/token", timeout=15)
            if resp.status_code == 200:
                auth_data = resp.json()
                self._current_token = auth_data.get("token", "")
                self._token_expires_at = auth_data.get("expires_at", 0)
                self._initialized = True
                return True
            return False
        except Exception as e:
            log.warning(f"[MailSession] init error: {e}")
            return False

    def _ensure_token(self) -> bool:
        if not self._initialized or not self._current_token or time.time() > self._token_expires_at - 120:
            return self._init_session()
        return True

    def _set_auth(self, auth_data: dict):
        if not isinstance(auth_data, dict):
            return
        new_tok = str(auth_data.get("token", "") or "").strip()
        if new_tok:
            self._current_token = new_tok
        self._token_expires_at = int(auth_data.get("expires_at", 0) or 0)
        self._initialized = bool(self._current_token)

    def _refresh_mailbox_token(self, email: str) -> bool:
        email = str(email or "").strip().lower()
        if not email:
            return False
        try:
            resp = self._session.post(
                f"{MAIL_BASE}/api/inbox-token",
                json={"email": email},
                headers={"content-type": "application/json", "referer": f"{MAIL_BASE}/{email}"},
                timeout=15,
            )
            if resp.status_code != 200:
                return False
            data = resp.json()
            if not data.get("success") or not data.get("auth"):
                return False
            self._set_auth(data.get("auth", {}))
            return bool(self._current_token)
        except Exception as e:
            log.warning(f"[MailSession] refresh mailbox token error for {email}: {e}")
            return False

    def _extract_verify_link_from_email_record(self, msg: dict) -> str:
        subject = str(msg.get("subject", ""))
        parts = []
        for field in ("html_content", "content", "body", "html", "text", "raw"):
            v = msg.get(field)
            if v:
                parts.append(str(v))
        for field in ("payload", "data", "message"):
            v = msg.get(field)
            if isinstance(v, dict):
                parts.extend(str(x) for x in v.values() if x)
            elif isinstance(v, str) and v:
                parts.append(v)
        combined = " ".join(parts)
        combined = html_lib.unescape(combined)
        combined = (combined.replace("\u003c", "<").replace("\u003e", ">")
                            .replace("\u0026", "&").replace("\\/", "/"))

        href_links = re.findall(r"href=[\"'](https?://[^\"']+)[\"']", combined, flags=re.IGNORECASE)
        text_links = re.findall(r"https?://[^\s\"'<>\,\)]+", combined)
        for link in href_links + text_links:
            link = link.rstrip('.,;)')
            if any(keyword in link.lower() for keyword in MAIL_LINK_KEYWORDS):
                return link
        if any(keyword in subject.lower() for keyword in MAIL_LINK_KEYWORDS):
            for link in href_links + text_links:
                if link.startswith('http'):
                    return link.rstrip('.,;)')
        return ""

    def get_email(self) -> str:
        if not self._ensure_token():
            raise Exception("mail.chatgpt.org.uk: session init failed")
        resp = self._session.get(
            f"{MAIL_BASE}/api/generate-email",
            headers={"accept": "*/*", "referer": f"{MAIL_BASE}/",
                     "x-inbox-token": self._current_token},
            timeout=15,
        )
        if resp.status_code == 401 or resp.status_code == 403:
            self._initialized = False
            self._init_session()
            resp = self._session.get(
                f"{MAIL_BASE}/api/generate-email",
                headers={"accept": "*/*", "referer": f"{MAIL_BASE}/",
                         "x-inbox-token": self._current_token},
                timeout=15,
            )
        data = resp.json()
        if not data.get("success"):
            raise Exception(f"mail.chatgpt.org.uk: generate-email failed: {data}")
        email = str(data.get("data", {}).get("email", "")).strip()
        new_tok = data.get("auth", {}).get("token", "")
        if new_tok:
            self._current_token = new_tok
            self._token_expires_at = data.get("auth", {}).get("expires_at", 0)
        return email

    def poll_verify_link(self, email: str, timeout_sec: int = 300) -> str:
        email = str(email or "").strip().lower()
        log.info(f"[MailSession] Polling inbox for {email} (timeout {timeout_sec}s)...")
        deadline = time.time() + timeout_sec
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                if not self._refresh_mailbox_token(email):
                    if not self._ensure_token():
                        time.sleep(2)
                        continue

                resp = self._session.get(
                    f"{MAIL_BASE}/api/emails",
                    params={"email": email},
                    headers={
                        "accept": "*/*",
                        "referer": f"{MAIL_BASE}/{email}",
                        "x-inbox-token": self._current_token,
                    },
                    timeout=15,
                )

                if resp.status_code in (401, 403):
                    try:
                        data = resp.json()
                    except Exception:
                        data = {}
                    if data.get("auth"):
                        self._set_auth(data.get("auth", {}))
                        resp = self._session.get(
                            f"{MAIL_BASE}/api/emails",
                            params={"email": email},
                            headers={
                                "accept": "*/*",
                                "referer": f"{MAIL_BASE}/{email}",
                                "x-inbox-token": self._current_token,
                            },
                            timeout=15,
                        )
                    elif not self._refresh_mailbox_token(email):
                        time.sleep(2)
                        continue
                    else:
                        resp = self._session.get(
                            f"{MAIL_BASE}/api/emails",
                            params={"email": email},
                            headers={
                                "accept": "*/*",
                                "referer": f"{MAIL_BASE}/{email}",
                                "x-inbox-token": self._current_token,
                            },
                            timeout=15,
                        )

                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("auth"):
                        self._set_auth(data.get("auth", {}))
                    emails_list = data.get("data", {}).get("emails", [])
                    log.info(f"[邮件] 第 {attempt} 次轮询，收到 {len(emails_list)} 封邮件")
                    for msg in emails_list:
                        link = self._extract_verify_link_from_email_record(msg)
                        if link:
                            log.info(f"[邮件] 找到验证链接：{link[:160]}...")
                            return link
                else:
                    log.warning(f"[MailSession] email API HTTP {resp.status_code}: {resp.text[:120]}")
            except Exception as e:
                log.warning(f"[MailSession] poll error: {e}")
            time.sleep(2)
        log.error("[邮件] 轮询超时，未找到验证邮件")
        return ""

class _MailTmSession:
    """mail.tm 临时邮箱客户端。curl_cffi + chrome 指纹直连 REST API，
    不依赖 window.__BROWSER_AUTH，规避 GPTMail 的 Cloudflare 拦截。"""

    API = "https://api.mail.tm"

    def __init__(self):
        from curl_cffi import requests as cffi_requests
        self._session = cffi_requests.Session(impersonate="chrome", timeout=25)
        self._session.headers.update({
            "accept": "application/json",
            "content-type": "application/json",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self._token = ""
        self._email = ""
        self._password = ""

    @staticmethod
    def _members(resp):
        data = resp.json()
        if isinstance(data, dict):
            return data.get("hydra:member", data)
        return data

    def get_email(self) -> str:
        doms = self._members(self._session.get(f"{self.API}/domains"))
        if not doms:
            raise Exception("mail.tm: no domains available")
        domain = doms[0]["domain"]
        user = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        self._email = f"{user}@{domain}"
        self._password = "Pw" + "".join(random.choices(string.ascii_letters + string.digits, k=12))
        r = self._session.post(f"{self.API}/accounts",
                               data=json.dumps({"address": self._email, "password": self._password}))
        if r.status_code not in (200, 201):
            raise Exception(f"mail.tm: create account failed HTTP {r.status_code}: {r.text[:120]}")
        rt = self._session.post(f"{self.API}/token",
                                data=json.dumps({"address": self._email, "password": self._password}))
        if rt.status_code != 200:
            raise Exception(f"mail.tm: token failed HTTP {rt.status_code}: {rt.text[:120]}")
        self._token = rt.json().get("token", "")
        if not self._token:
            raise Exception("mail.tm: empty token")
        return self._email

    def _auth_headers(self):
        return {"accept": "application/json", "Authorization": f"Bearer {self._token}"}

    def poll_verify_link(self, email: str, timeout_sec: int = 300) -> str:
        log.info(f"[MailTm] Polling inbox for {email} (timeout {timeout_sec}s)...")
        deadline = time.time() + timeout_sec
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                r = self._session.get(f"{self.API}/messages", headers=self._auth_headers())
                if r.status_code == 200:
                    msgs = self._members(r)
                    log.info(f"[邮件] 第 {attempt} 次轮询，收到 {len(msgs)} 封邮件")
                    for m in (msgs or []):
                        mid = m.get("id")
                        if not mid:
                            continue
                        full = self._session.get(f"{self.API}/messages/{mid}", headers=self._auth_headers())
                        if full.status_code != 200:
                            continue
                        detail = full.json()
                        parts = []
                        for f in ("html", "text"):
                            v = detail.get(f)
                            if isinstance(v, list):
                                parts.extend(str(x) for x in v)
                            elif v:
                                parts.append(str(v))
                        parts.append(str(m.get("subject", "")))
                        combined = html_lib.unescape(" ".join(parts))
                        combined = (combined.replace("\\u003c", "<").replace("\\u003e", ">")
                                    .replace("\\u0026", "&").replace("\\/", "/"))
                        href_links = re.findall(r"href=[\"'](https?://[^\"']+)[\"']", combined, flags=re.IGNORECASE)
                        text_links = re.findall(r"https?://[^\s\"'<>\,\)]+", combined)
                        for link in href_links + text_links:
                            link = link.rstrip('.,;)')
                            if any(kw in link.lower() for kw in MAIL_LINK_KEYWORDS):
                                log.info(f"[邮件] 找到验证链接：{link[:160]}...")
                                return link
                else:
                    log.warning(f"[MailTm] messages HTTP {r.status_code}: {r.text[:120]}")
            except Exception as e:
                log.warning(f"[MailTm] poll error: {e}")
            time.sleep(3)
        log.error("[邮件] 轮询超时，未找到验证邮件")
        return ""


class _AsyncMailClient:
    def __init__(self):
        self._sess = None
        self._email = ""
        self._provider = ""

    async def __aenter__(self):
        # 优先使用 mail.tm（curl_cffi + chrome 指纹可直连，无 Cloudflare 拦截）；
        # 若创建失败再回退到 GPTMail（mail.chatgpt.org.uk，近期被 CF 保护，常失败）。
        try:
            self._sess = await asyncio.to_thread(_MailTmSession)
            self._provider = "mail.tm"
            log.info("[Register] 邮件服务商: mail.tm")
        except Exception as e:
            log.warning(f"[Register] mail.tm 初始化失败({e})，回退 GPTMail")
            self._sess = await asyncio.to_thread(_EmailSession)
            self._provider = "gptmail"
        return self

    async def __aexit__(self, *args):
        pass

    async def generate_email(self) -> str:
        self._email = await asyncio.to_thread(self._sess.get_email)
        return self._email

    async def get_verify_link(self, timeout_sec: int = 300) -> str:
        return await asyncio.to_thread(self._sess.poll_verify_link, self._email, timeout_sec)

    async def get_verify_link_for_email(self, email: str, timeout_sec: int = 300) -> str:
        return await asyncio.to_thread(self._sess.poll_verify_link, email, timeout_sec)

async def register_qwen_account() -> Optional[Account]:
    log.info("[Register] ── 开始注册流程 ──")
    async with _AsyncMailClient() as mail_client:
        log.info("[Register] [1/7] 生成临时邮箱...")
        email = await mail_client.generate_email()
        password = _gen_password()
        username = _gen_username()
        log.info(f"[Register] [1/7] 邮箱: {email}  用户名: {username}")

        try:
            async with _new_browser() as browser:
                page = await browser.new_page()
                log.info(f"[Register] [2/7] 打开注册页面: {BASE_URL}/auth?action=signup")
                try:
                    await page.goto(f"{BASE_URL}/auth?action=signup", wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    log.warning(f"[Register] [2/7] 页面加载异常: {e}")

                log.info("[Register] [3/7] 填写注册表单...")
                name_input = None
                for sel in ['input[placeholder*="Full Name"]', 'input[placeholder*="Name"]']:
                    try:
                        name_input = await page.wait_for_selector(sel, timeout=15000)
                        if name_input: break
                    except Exception:
                        pass
                if not name_input:
                    inputs = await page.query_selector_all('input')
                    name_input = inputs[0] if len(inputs) >= 4 else None
                if not name_input:
                    log.error("[Register] [3/7] 找不到姓名输入框，注册中止")
                    return None

                await name_input.click(); await name_input.fill(username)
                log.info(f"[Register] [3/7]  ✓ 姓名: {username}")
                email_input = await page.query_selector('input[placeholder*="Email"]')
                if not email_input:
                    inputs = await page.query_selector_all('input')
                    email_input = inputs[1] if len(inputs) >= 2 else None
                if email_input: await email_input.click(); await email_input.fill(email)
                log.info(f"[Register] [3/7]  ✓ 邮箱: {email}")

                pwd_input = await page.query_selector('input[placeholder*="Password"]:not([placeholder*="Again"])')
                if not pwd_input:
                    inputs = await page.query_selector_all('input')
                    pwd_input = inputs[2] if len(inputs) >= 3 else None
                if pwd_input: await pwd_input.click(); await pwd_input.fill(password)

                confirm_input = await page.query_selector('input[placeholder*="Again"]')
                if not confirm_input:
                    inputs = await page.query_selector_all('input')
                    confirm_input = inputs[3] if len(inputs) >= 4 else None
                if confirm_input: await confirm_input.click(); await confirm_input.fill(password)
                log.info("[Register] [3/7]  ✓ 密码已填写")

                checkbox = await page.query_selector('input[type="checkbox"]')
                if checkbox and not await checkbox.is_checked(): await checkbox.click()
                else:
                    agree = await page.query_selector('text=I agree')
                    if agree: await agree.click()
                log.info("[Register] [3/7]  ✓ 同意条款")

                log.info("[Register] [4/7] 提交注册表单...")
                await asyncio.sleep(1)
                submit = await page.query_selector('button:has-text("Create Account")') or await page.query_selector('button[type="submit"]')
                if submit: await submit.click()
                log.info("[Register] [4/7] 已点击提交，等待页面跳转（6s）...")
                await asyncio.sleep(6)

                url_after = page.url
                log.info(f"[Register] [4/7] 提交后URL: {url_after}")

                # Check if already logged in (redirected to main page)
                token = None
                if BASE_URL in url_after and "auth" not in url_after:
                    log.info("[Register] [5/7] 已跳转主页，尝试直接获取token...")
                    await asyncio.sleep(3)
                    token = await page.evaluate("localStorage.getItem('token')")
                    if token:
                        log.info("[Register] [5/7] ✓ 注册后直接获取到token，跳过邮件验证")

                # If no token yet, try explicit login with email+password (faster than email poll)
                if not token:
                    log.info("[Register] [5/7] 尝试用账号密码直接登录...")
                    try:
                        await page.goto(f"{BASE_URL}/auth", wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(3)
                        li_email = await page.query_selector('input[placeholder*="Email"]')
                        if li_email: await li_email.fill(email)
                        li_pwd = await page.query_selector('input[type="password"]')
                        if li_pwd: await li_pwd.fill(password)
                        li_btn = await page.query_selector('button:has-text("Log in")') or await page.query_selector('button[type="submit"]')
                        if li_btn: await li_btn.click()
                        await asyncio.sleep(8)
                        token = await page.evaluate("localStorage.getItem('token')")
                        if token:
                            log.info("[Register] [5/7] ✓ 直接登录成功，获取到token")
                    except Exception as e:
                        log.warning(f"[Register] [5/7] 直接登录失败: {e}")

                # If still no token, use mailbox email API first, then fall back to page lookup.
                if not token:
                    log.info(f"[Register] [6/7] polling mailbox email API for {email}...")
                    verify_link = await mail_client.get_verify_link(timeout_sec=60)
                    if not verify_link:
                        log.info(f"[注册] [6/7] 邮件 API 未返回链接，尝试页面方式 {email}")
                        verify_link = await _find_verify_link_via_mail_page(email)

                    if not verify_link:
                        log.error("[Register] [6/7] verification email not found")
                        return None

                    log.info(f"[Register] [6/7] ✓ 收到验证链接，访问中...")
                    try:
                        await page.goto(verify_link, wait_until="domcontentloaded", timeout=30000)
                    except Exception: pass
                    await asyncio.sleep(6)
                    token = await page.evaluate("localStorage.getItem('token')")
                    log.info(f"[Register] [6/7] 验证后URL: {page.url}")

                    # Login after verification
                    if not token:
                        log.info("[Register] [6/7] 验证链接后尝试登录...")
                        try:
                            await page.goto(f"{BASE_URL}/auth", wait_until="domcontentloaded", timeout=30000)
                            await asyncio.sleep(3)
                            li_email = await page.query_selector('input[placeholder*="Email"]')
                            if li_email: await li_email.fill(email)
                            li_pwd = await page.query_selector('input[type="password"]')
                            if li_pwd: await li_pwd.fill(password)
                            li_btn = await page.query_selector('button:has-text("Log in")') or await page.query_selector('button[type="submit"]')
                            if li_btn: await li_btn.click()
                            await asyncio.sleep(8)
                            token = await page.evaluate("localStorage.getItem('token')")
                            if token:
                                log.info("[Register] [6/7] ✓ 验证后登录成功")
                        except Exception: pass

                if not token:
                    log.error("[Register] 所有方法均无法获取token，注册失败")
                    return None

                log.info("[Register] [7/7] 提取 cookies...")
                all_cookies = await page.context.cookies()
                cookie_str = "; ".join(f"{c.get('name','')}={c.get('value','')}" for c in all_cookies if "qwen" in c.get("domain", ""))
                log.info(f"[Register] ✓ 注册完成: {email}")
                return Account(email=email, password=password, token=token, cookies=cookie_str, username=username, activation_pending=False)
        except Exception as e:
            import traceback
            log.error(f"[Register] 注册异常: {e}\n{traceback.format_exc()}")
            return None

async def _login_and_get_token(page, email: str, password: str, timeout_sec: int = 20) -> str:
    try:
        await page.goto(f"{BASE_URL}/auth", wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    await asyncio.sleep(2)

    email_input = None
    pwd_input = None
    try:
        email_input = await page.query_selector('input[placeholder*="Email"]')
    except Exception:
        email_input = None
    try:
        pwd_input = await page.query_selector('input[type="password"]')
    except Exception:
        pwd_input = None

    if (not email_input) or (not pwd_input):
        try:
            inputs = await page.query_selector_all('input')
        except Exception:
            inputs = []
        text_inputs = []
        for item in inputs:
            try:
                t = await item.get_attribute('type')
            except Exception:
                t = None
            if t in (None, '', 'text', 'email'):
                text_inputs.append(item)
        if not email_input and text_inputs:
            email_input = text_inputs[0]
        if not pwd_input:
            for item in inputs:
                try:
                    t = await item.get_attribute('type')
                except Exception:
                    t = None
                if t == 'password':
                    pwd_input = item
                    break

    if email_input:
        try:
            await email_input.click()
        except Exception:
            pass
        await email_input.fill(email)
    if pwd_input:
        try:
            await pwd_input.click()
        except Exception:
            pass
        await pwd_input.fill(password)

    submit = None
    for sel in [
        'button:has-text("Log in")',
        'button[type="submit"]:not([disabled])',
        'button[type="submit"]',
        'button:has-text("Continue")',
    ]:
        try:
            submit = await page.query_selector(sel)
            if submit:
                disabled = await submit.get_attribute('disabled')
                aria_disabled = await submit.get_attribute('aria-disabled')
                if disabled is None and aria_disabled not in ('true', 'disabled'):
                    break
        except Exception:
            pass

    clicked = False
    if submit:
        try:
            await submit.click(timeout=5000)
            clicked = True
        except Exception:
            try:
                await submit.click(force=True, timeout=3000)
                clicked = True
            except Exception:
                pass

    if not clicked and pwd_input:
        try:
            await pwd_input.press('Enter')
        except Exception:
            pass

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            token = await page.evaluate("localStorage.getItem('token')")
        except Exception:
            token = None
        if token:
            return token
        await asyncio.sleep(1)
    return ""

async def activate_account(acc: Account) -> bool:
    """Use inbox API first, then mailbox-page fallback, to activate an account."""
    started_at = float(getattr(acc, "_activation_started_at", 0) or 0)
    if getattr(acc, "_is_activating", False):
        if started_at and (time.time() - started_at) < 90:
            log.info(f"[激活] {acc.email} 正在激活中，跳过重复调用")
            return acc.valid and not acc.activation_pending
        log.warning(f"[激活] {acc.email} 激活超时，重置激活标志重新尝试")
        setattr(acc, "_is_activating", False)

    log.info(f"[激活] 开始激活账号 {acc.email}")
    setattr(acc, "_is_activating", True)
    setattr(acc, "_activation_started_at", time.time())
    try:
        verify_link = ""
        try:
            async with _AsyncMailClient() as mail_client:
                verify_link = await mail_client.get_verify_link_for_email(acc.email, timeout_sec=30)
        except Exception as e:
            log.warning(f"[激活] {acc.email} 邮件 API 失败: {e}")

        if not verify_link:
            log.info(f"[激活] {acc.email} 邮件 API 未返回链接，使用页面方式")
            verify_link = await _find_verify_link_via_mail_page(acc.email)

        if not verify_link:
            log.warning(f"[Activate] activation email not found for {acc.email}")
            return False

        log.info(f"[激活] {acc.email} 找到验证链接：{verify_link[:120]}")

        async with _new_browser() as browser:
            page = await browser.new_page()
            try:
                await page.goto(verify_link, wait_until="networkidle", timeout=30000)
            except Exception:
                try:
                    await page.goto(verify_link, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass

            await asyncio.sleep(5)
            token = await page.evaluate("localStorage.getItem('token')")
            log.info(f"[激活] {acc.email} 访问验证链接后 URL={page.url}，Token：{'有' if token else '无'}")

            if not token and acc.password:
                try:
                    token = await _login_and_get_token(page, acc.email, acc.password, timeout_sec=20)
                except Exception as e:
                    log.warning(f"[激活] {acc.email} 登录获取 token 失败: {e}")

            if token:
                acc.token = token
                acc.valid = True
                acc.activation_pending = False
                log.info(f"[激活] {acc.email} 激活成功")
                return True

            # Some activation links make the original token usable again without issuing a new one.
            if await _verify_qwen_token(acc.token):
                acc.valid = True
                acc.activation_pending = False
                log.info(f"[激活] {acc.email} 旧 Token 仍然有效，激活完成")
                return True

            log.warning(f"[激活] {acc.email} 激活失败，无法获取 Token")
            return False
    except Exception as e:
        log.error(f"[激活] {acc.email} 激活异常: {e}")
        return False
    finally:
        setattr(acc, "_is_activating", False)
        setattr(acc, "_activation_started_at", 0)

async def refresh_token_via_password_login(email: str, password: str, timeout_sec: int = 60) -> str:
    """用邮箱+密码在真实 Chrome（Playwright channel=chrome）里登录拿新 token。

    实测（2026-09-04）：登录页无滑块、无 punish，直登即拿 token + 会话 cookie。
    返回新 token，拿不到返回空串。调用方负责写回账号池。
    """
    if not email or not password:
        return ""
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        log.warning(f"[Refresh] playwright 不可用，跳过密码登录: {e}")
        return ""
    try:
        async with async_playwright() as pw:
            from backend.services.browser_launch import launch_browser
            browser = await launch_browser(pw)
            try:
                ctx = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                )
                page = await ctx.new_page()
                try:
                    await page.goto(f"{BASE_URL}/auth", wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    log.warning(f"[Refresh] {email} 登录页导航异常: {e}")
                # SPA 水合慢（实测约 10s 才出现输入框），必须等够
                email_input = None
                for _ in range(30):
                    email_input = await page.query_selector('input[name="email"]')
                    if email_input:
                        break
                    await asyncio.sleep(1)
                if not email_input:
                    log.warning(f"[Refresh] {email} 登录页未出现邮箱输入框")
                    return ""
                pwd_input = await page.query_selector('input[name="password"]')
                if not pwd_input:
                    log.warning(f"[Refresh] {email} 登录页未出现密码输入框")
                    return ""
                await email_input.fill(email)
                await pwd_input.fill(password)
                await asyncio.sleep(1)
                submit = await page.query_selector('button[type="submit"]')
                if submit:
                    try:
                        await submit.click(timeout=10000)
                    except Exception:
                        pass
                deadline = time.time() + timeout_sec
                while time.time() < deadline:
                    try:
                        token = await page.evaluate("localStorage.getItem('token')")
                    except Exception:
                        token = None
                    if token:
                        log.info(f"[Refresh] {email} 密码登录成功，拿到新 token")
                        return token
                    # 登录后若冒出 noCaptcha 滑块，尝试自动求解一次
                    try:
                        has_nc = await page.query_selector("#nocaptcha, #nc_1_n1z, .btn_slide")
                        if has_nc is not None:
                            log.warning(f"[Refresh] {email} 登录遇到滑块，尝试自动求解...")
                            from backend.services.nc_slider import solve_nocaptcha_slider
                            await solve_nocaptcha_slider(page, timeout=90.0)
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                log.warning(f"[Refresh] {email} 登录超时未拿到 token")
                return ""
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"[Refresh] {email} 密码登录异常: {e}")
        return ""


class AuthResolver:
    """自动登录并提取 Token，在检测到 401 时自动自愈凭证"""
    def __init__(self, pool: AccountPool):
        self.pool = pool

    async def auto_heal_account(self, acc: Account):
        """Background task to refresh token. If successful, marks account valid.
        If refresh fails or account is pending activation, tries to activate via email."""
        if getattr(acc, "healing", False):
            log.info(f"[BGRefresh] {acc.email} healing already in progress")
            return

        acc.healing = True
        try:
            ok = await self.refresh_token(acc)
            if ok:
                if not getattr(acc, 'activation_pending', False):
                    acc.valid = True
                    await self.pool.save()
                    log.info(f"[自愈] {acc.email} Token 刷新成功，已标记有效")
                    return
                log.info(f"[BGRefresh] {acc.email} token refreshed but account still needs activation")
            else:
                log.warning(f"[自愈] {acc.email} Token 刷新失败，尝试激活")

            activated = await activate_account(acc)
            if activated:
                acc.activation_pending = False
                acc.valid = True
                await self.pool.save()
                log.info(f"[自愈] {acc.email} 激活成功，已保存")
            else:
                log.warning(f"[自愈] {acc.email} 激活失败")
        except Exception as e:
            log.warning(f"[BGRefresh] {acc.email} auto heal failed: {e}")
        finally:
            acc.healing = False

    async def refresh_token(self, acc: Account) -> bool:

        """Re-login with email+password to get a fresh token. Returns True on success."""
        if not acc.email or not acc.password:
            log.warning(f"[Refresh] 账号 {acc.email} 无密码，无法刷新")
            return False

        log.info(f"[Refresh] 正在为 {acc.email} 刷新 token...")
        # 首选：真实 Chrome 密码直登（无滑块，实测有效；失败再走旧 Camoufox 路径）
        try:
            new_token = await refresh_token_via_password_login(acc.email, acc.password)
            if new_token:
                if new_token != acc.token:
                    old_prefix = acc.token[:20] if acc.token else "空"
                    acc.token = new_token
                    acc.valid = True
                    await self.pool.save()
                    log.info(f"[Refresh] {acc.email} token 已更新 ({old_prefix}... → {new_token[:20]}...)")
                else:
                    acc.valid = True
                    log.info(f"[Refresh] {acc.email} token 未变化，重新标记有效")
                return True
            log.warning(f"[Refresh] {acc.email} Chrome 直登未拿到 token，回退 Camoufox 路径")
        except Exception as e:
            log.warning(f"[Refresh] {acc.email} Chrome 直登异常({e})，回退 Camoufox 路径")
        try:
            async with _new_browser() as browser:
                page = await browser.new_page()
                new_token = await _login_and_get_token(page, acc.email, acc.password, timeout_sec=20)
                if new_token and new_token != acc.token:
                    old_prefix = acc.token[:20] if acc.token else "空"
                    acc.token = new_token
                    acc.valid = True
                    await self.pool.save()
                    log.info(f"[Refresh] {acc.email} token 已更新 ({old_prefix}... → {new_token[:20]}...)")
                    return True
                elif new_token == acc.token:
                    # Token same but might still be valid — mark valid again
                    acc.valid = True
                    log.info(f"[Refresh] {acc.email} token 未变化，重新标记有效")
                    return True
                else:
                    log.warning(f"[Refresh] {acc.email} 登录后未获取到token，URL={page.url}")
                    return False
        except Exception as e:
            log.error(f"[Refresh] {acc.email} 刷新异常: {e}")
            return False
