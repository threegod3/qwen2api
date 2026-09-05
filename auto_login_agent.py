"""
自动登录 agent：用本地账号自动完成 Qwen WAF 挑战 + 登录，保存干净 cookie。
用法: python auto_login_agent.py [--email xxx] [--headed]
"""
import asyncio
import json
import os
import sys
import time

BASE_URL = "https://chat.qwen.ai"
PROJECT = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(PROJECT, "data", "accounts.json")
COOKIE_FILE = os.path.join(PROJECT, "data", "browser_cookies.json")


def is_token_valid(token: str, now: float) -> bool:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        import base64
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("exp", 0) > now
    except Exception:
        return False


async def wait_waf_clear(page, timeout=180):
    """等待 WAF 挑战通过（JS 挑战自动过，滑块需检测提示）。"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            # 检测滑块验证码元素
            slider = await page.query_selector('[class*="slider"], [class*="captcha"], [id*="captcha"], [class*="verify"]')
            html_preview = await page.evaluate("document.body?.innerText?.substring(0, 200) || ''")
            api_ok = await page.evaluate("typeof window.ApiRequest !== 'undefined' && typeof window.ApiRequest.post === 'function'")
            title = await page.evaluate("document.title || ''")
            if api_ok and "aliyun_waf" not in html_preview.lower() and "captcha" not in html_preview.lower():
                print(f"[OK] WAF 已通过! title={title} ApiRequest 可用")
                return True
            if slider and ("captcha" in (await page.content()).lower() or "验证" in html_preview):
                print(f"[!] 检测到滑块/验证码元素: {slider}")
                # 尝试点击验证框（很多 WAF 只需点击）
                try:
                    await slider.click(timeout=2000)
                    print("[*] 已点击验证框，等待挑战完成...")
                except Exception:
                    pass
            print(f"[*] 等待 WAF... ({int(time.time()-start)}s) title={title}")
        except Exception as e:
            print(f"[*] 检测异常: {e}")
        await asyncio.sleep(3)
    return False


async def try_login(page, email, password):
    """账号密码登录。"""
    try:
        await page.goto(f"{BASE_URL}/auth", wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    await asyncio.sleep(3)
    inputs = await page.query_selector_all("input")
    email_input, pwd_input = None, None
    for item in inputs:
        t = await item.get_attribute("type")
        if t in (None, "", "text", "email") and email_input is None:
            email_input = item
        if t == "password":
            pwd_input = item
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
    for sel in ["button:has-text('Log in')", "button[type='submit']:not([disabled])", "button[type='submit']", "button:has-text('Continue')"]:
        try:
            submit = await page.query_selector(sel)
            if submit:
                disabled = await submit.get_attribute("disabled")
                aria_disabled = await submit.get_attribute("aria-disabled")
                if disabled is None and aria_disabled not in ("true", "disabled"):
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
            await pwd_input.press("Enter")
        except Exception:
            pass
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            token = await page.evaluate("localStorage.getItem('token')")
        except Exception:
            token = None
        if token:
            return token
        await asyncio.sleep(1)
    return ""


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default="")
    parser.add_argument("--headed", action="store_true", default=True)
    args = parser.parse_args()

    now = time.time()
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    # 选出有效账号
    valid = [a for a in accounts if is_token_valid(a.get("token", ""), now)]
    print(f"[*] 有效 token 账号: {len(valid)} / {len(accounts)}")

    target = None
    if args.email:
        target = next((a for a in valid if a["email"] == args.email), None)
        if not target:
            print(f"[!] 未找到账号 {args.email}，回退到第一个有效账号")
    if not target and valid:
        target = valid[0]
    if not target:
        print("[!] 没有可用账号")
        sys.exit(1)
    print(f"[*] 使用账号: {target['email']}")

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
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
        page = await ctx.new_page()

        print(f"[*] 导航到 {BASE_URL}/ ...")
        try:
            await page.goto(f"{BASE_URL}/", timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[*] 导航异常: {e}")
        await page.wait_for_timeout(8000)

        # 阶段1: 等待 WAF JS 挑战自动完成
        print("[*] 阶段1: 等待 WAF 挑战...")
        await wait_waf_clear(page, timeout=120)

        # 阶段2: 注入 token
        print("[*] 阶段2: 注入 token 到 localStorage...")
        try:
            await page.evaluate(f"localStorage.setItem('token', '{target['token']}')")
            print("[*] token 已注入，刷新页面...")
            await page.goto(f"{BASE_URL}/", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(6000)
        except Exception as e:
            print(f"[!] token 注入失败: {e}")

        # 阶段3: 再次等待 WAF + 检查 ApiRequest
        print("[*] 阶段3: 确认 WAF 通过...")
        await wait_waf_clear(page, timeout=90)

        # 阶段4: 如果 ApiRequest 不可用，尝试账号密码登录
        api_ok = await page.evaluate("typeof window.ApiRequest !== 'undefined' && typeof window.ApiRequest.post === 'function'")
        if not api_ok:
            print("[*] ApiRequest 不可用，尝试账号密码登录...")
            token = await try_login(page, target["email"], target.get("password", ""))
            if token:
                print(f"[OK] 登录成功，新 token 获取 ({token[:25]}...)")
                target["token"] = token
                with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(accounts, f, ensure_ascii=False, indent=2)
                await page.goto(f"{BASE_URL}/", timeout=60000, wait_until="domcontentloaded")
                await page.wait_for_timeout(6000)
                await wait_waf_clear(page, timeout=60)
            else:
                print("[!] 登录失败（可能需要验证码）")

        # 阶段5: 保存 cookies
        cookies = await ctx.cookies()
        os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        print(f"[OK] 已保存 {len(cookies)} 个 cookie 到 {COOKIE_FILE}")

        api_ok = await page.evaluate("typeof window.ApiRequest !== 'undefined' && typeof window.ApiRequest.post === 'function'")
        print(f"[结果] ApiRequest 可用: {api_ok}")
        print("[*] 浏览器保持打开 60 秒供检查，然后自动关闭")
        await asyncio.sleep(60)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
