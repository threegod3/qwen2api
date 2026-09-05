"""用干净浏览器会话获取新 cookie，保存到 browser_cookies.json，并更新 bx_pool 的 version。

原理：punish 是 cookie 被风控标记。诊断已证明干净的浏览器会话（不带旧 cookie）
能正常访问 chat.qwen.ai（fetch /api/models 返回 200）。本脚本用干净会话导航，
等页面加载完，保存新 cookie；同时把 bx_pool.json 里过期的 web_version 更新为
当前前端版本 0.2.86（诊断脚本从页面真实请求头里抓到的）。
"""
import asyncio
import json
import os
import time

BASE_URL = "https://chat.qwen.ai"
PROJECT = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(PROJECT, "data", "browser_cookies.json")
BX_POOL_FILE = os.path.join(PROJECT, "bx_pool.json")
ACCOUNTS_FILE = os.path.join(PROJECT, "data", "accounts.json")
NEW_VERSION = "0.2.86"


def pick_valid_token() -> str:
    import base64
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            accounts = json.load(f)
    except Exception:
        return ""
    now = time.time()
    for a in accounts:
        tok = a.get("token", "")
        if not tok:
            continue
        try:
            payload = tok.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            if data.get("exp", 0) > now:
                return tok
        except Exception:
            continue
    return ""


async def main():
    from playwright.async_api import async_playwright
    token = pick_valid_token()
    print(f"[*] token: {'有(' + str(len(token)) + '字符)' if token else '无'}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        # 干净会话：不带任何旧 cookie
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)
        if token:
            try:
                await ctx.add_init_script(f"try{{localStorage.setItem('token',{json.dumps(token)});}}catch(e){{}}")
            except Exception:
                pass
        page = await ctx.new_page()

        print("[*] 导航到 chat.qwen.ai ...")
        try:
            await page.goto(f"{BASE_URL}/", timeout=45000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[*] 导航提示: {str(e)[:80]}")
        # 等待页面 JS 执行（WAF JS 挑战 + BaXia 初始化）
        await page.wait_for_timeout(12000)

        # 验证页面状态
        title = await page.title()
        body = await page.evaluate("document.body?.innerText?.substring(0,100) || ''")
        print(f"[*] 标题: {title}")
        print(f"[*] 正文(前100): {body}")

        # 探测 version（从页面真实请求或全局变量）
        version = await page.evaluate("""() => {
            // 尝试从全局配置读取版本
            try {
                const v = localStorage.getItem('version') || '';
                if (v) return v;
            } catch(e) {}
            return '';
        }""")
        print(f"[*] localStorage version: {version or '(空)'}")

        # 保存 cookie
        cookies = await ctx.cookies()
        os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        print(f"[OK] 已保存 {len(cookies)} 个新 cookie 到 {COOKIE_FILE}")
        for c in cookies:
            print(f"    {c['name']} @ {c['domain']}")

        # 更新 bx_pool.json 的 web_version
        if os.path.exists(BX_POOL_FILE):
            try:
                with open(BX_POOL_FILE, "r", encoding="utf-8") as f:
                    pool = json.load(f)
                updated = 0
                for e in pool:
                    if isinstance(e, dict) and e.get("web_version") != NEW_VERSION:
                        e["web_version"] = NEW_VERSION
                        updated += 1
                with open(BX_POOL_FILE, "w", encoding="utf-8") as f:
                    json.dump(pool, f, ensure_ascii=False, indent=2)
                print(f"[OK] 已更新 bx_pool.json 中 {updated} 条的 web_version -> {NEW_VERSION}")
            except Exception as e:
                print(f"[!] bx_pool 更新失败: {e}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
