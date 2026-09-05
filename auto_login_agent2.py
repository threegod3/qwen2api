"""
自动登录 agent v2：用本地账号自动完成 Qwen WAF 滑块挑战（YOLO 破解）+ 登录。
用法: python auto_login_agent2.py [--email xxx]
依赖: 系统 Python311 (有 ultralytics/torch) + D:\qwen2api-python\.venv (有 playwright)
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

# 优先使用 venv 的 playwright；auto_captcha 用系统 python 的 torch/ultralytics
VENV_PY = r"D:\qwen2api-python\.venv\Scripts\python.exe"
SYS_PY = r"C:\Users\yelih\AppData\Local\Programs\Python\Python311\python.exe"


def is_token_valid(token: str, now: float) -> bool:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        import base64
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("exp", 0) > now
    except Exception:
        return False


async def wait_waf_clear(page, timeout=180, use_captcha=True):
    """等待 WAF 挑战通过。检测到滑块时调用 auto_captcha.solve_slider 自动破解。"""
    import auto_captcha
    start = time.time()
    while time.time() - start < timeout:
        try:
            html_preview = await page.evaluate("document.body?.innerText?.substring(0, 200) || ''")
            api_ok = await page.evaluate("typeof window.ApiRequest !== 'undefined' && typeof window.ApiRequest.post === 'function'")
            title = await page.evaluate("document.title || ''")
            if api_ok and "aliyun_waf" not in html_preview.lower() and "captcha" not in html_preview.lower():
                print(f"[OK] WAF 已通过! title={title} ApiRequest 可用")
                return True

            # 检测滑块验证码（可能在 iframe 内，遍历所有 frame）
            has_slider = False
            try:
                for fr in page.frames:
                    try:
                        has_slider = await fr.evaluate(
                            """() => !!document.getElementById('aliyunCaptcha-sliding-slider')
                                    || !!document.querySelector('[class*="captcha"]')
                                    || !!document.querySelector('[id*="captcha"]')
                                    || !!document.querySelector('#aliyunCaptcha-img')"""
                        )
                        if has_slider:
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            if has_slider and use_captcha:
                print(f"[captcha] 检测到滑块验证码，调用 YOLO 自动破解 (已等待{int(time.time()-start)}s)...")
                try:
                    ok = await auto_captcha.solve_slider(page, max_attempts=3)
                    if ok:
                        print("[captcha] ✓ 滑块破解成功！")
                        await page.wait_for_timeout(3000)
                        continue
                except Exception as e:
                    print(f"[captcha] 破解异常: {e}")
            elif has_slider:
                print(f"[*] 检测到滑块验证码 (已等待{int(time.time()-start)}s)")
            else:
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
    args = parser.parse_args()

    now = time.time()
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    valid = [a for a in accounts if is_token_valid(a.get("token", ""), now)]
    print(f"[*] 有效 token 账号: {len(valid)} / {len(accounts)}")

    target = None
    if args.email:
        target = next((a for a in valid if a["email"] == args.email), None)
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

        # 阶段1: 等待 WAF 挑战（滑块自动破解）
        print("[*] 阶段1: 等待 WAF 挑战（自动破解滑块）...")
        await wait_waf_clear(page, timeout=180, use_captcha=True)

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
        await wait_waf_clear(page, timeout=120, use_captcha=True)

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
                await wait_waf_clear(page, timeout=90, use_captcha=True)
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
        print("[*] 浏览器保持打开 30 秒供检查，然后自动关闭")
        await asyncio.sleep(30)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
