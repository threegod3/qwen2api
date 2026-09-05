"""手动过滑块 + 登录，产出干净的 browser_cookies.json 和新 token。

用法：
    python manual_login.py                 # 用 accounts.json 里 token 最新的账号
    python manual_login.py <email>         # 指定账号

流程：弹出可见浏览器 -> 自动填邮箱密码 -> 你手动拖滑块 ->
      脚本检测到登录成功后，保存 cookie 到 data/browser_cookies.json，
      并把新 token 写回 data/accounts.json 对应账号。
"""
import asyncio
import base64
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
ACC_FILE = os.path.join(ROOT, "data", "accounts.json")
COOKIE_FILE = os.path.join(ROOT, "data", "browser_cookies.json")
BASE_URL = "https://chat.qwen.ai"


def _exp(tok: str) -> int:
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get("exp", 0)
    except Exception:
        return 0


def load_accounts():
    with open(ACC_FILE, "r", encoding="utf-8") as f:
        d = json.load(f)
    return d if isinstance(d, list) else d.get("accounts", [])


def save_accounts(lst):
    with open(ACC_FILE, "r", encoding="utf-8") as f:
        d = json.load(f)
    if isinstance(d, list):
        out = lst
    else:
        d["accounts"] = lst
        out = d
    with open(ACC_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def pick(email=None):
    lst = load_accounts()
    if email:
        for i, x in enumerate(lst):
            if x.get("email") == email:
                return i, x, lst
        raise SystemExit(f"未找到账号 {email}")
    best, bi, bexp = None, -1, -1
    for i, x in enumerate(lst):
        if not x.get("password") or x.get("activation_pending"):
            continue
        e = _exp(x.get("token") or "")
        if e > bexp:
            bexp, bi, best = e, i, x
    if best is None:
        raise SystemExit("accounts.json 里没有带密码的可用账号")
    return bi, best, lst


async def main():
    from playwright.async_api import async_playwright

    email_arg = sys.argv[1] if len(sys.argv) > 1 else None
    idx, acc, lst = pick(email_arg)
    email, password = acc["email"], acc["password"]

    print("=" * 62)
    print("账号 :", email)
    print("密码 :", password)
    print("=" * 62)
    print("浏览器窗口即将弹出。脚本会自动填好邮箱和密码；")
    print("如果出现滑块/人机验证，请你手动拖动完成。")
    print("登录成功后脚本会自动保存 cookie 和新 token，然后自己退出。")
    print("=" * 62)

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=False,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
              "--window-size=1280,900"],
    )
    ctx = await browser.new_context(
        viewport={"width": 1280, "height": 860},
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
        locale="zh-CN",
    )
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    page = await ctx.new_page()

    print("\n[1/4] 打开登录页 ...")
    try:
        await page.goto(f"{BASE_URL}/auth?action=signin", wait_until="domcontentloaded",
                        timeout=90000)
    except Exception as e:
        print("  首次导航异常（继续尝试）:", e)
    await asyncio.sleep(4)

    print("[2/4] 自动填写邮箱和密码 ...")
    filled = 0
    for sel in ['input[type="email"]', 'input[name="email"]',
                'input[placeholder*="mail" i]', 'input[autocomplete="username"]']:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await el.fill(email)
                filled += 1
                break
        except Exception:
            pass
    for sel in ['input[type="password"]', 'input[name="password"]']:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await el.fill(password)
                filled += 1
                break
        except Exception:
            pass
    print(f"  已填写 {filled} 个字段" + ("" if filled == 2 else "（不足 2 个，请在窗口里手动补全）"))

    if filled == 2:
        for sel in ['button[type="submit"]', 'button:has-text("登录")',
                    'button:has-text("Sign in")', 'button:has-text("Log in")']:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    print("  已点击登录按钮")
                    break
            except Exception:
                pass

    print("\n[3/4] 等待登录完成（最多 10 分钟）——请在窗口里完成滑块验证 ...")
    token = None
    deadline = time.time() + 600
    last_note = 0
    while time.time() < deadline:
        await asyncio.sleep(3)
        try:
            token = await page.evaluate("localStorage.getItem('token')")
        except Exception:
            token = None
        if token and len(token) > 80:
            print("  ✓ 检测到登录 token")
            break
        if time.time() - last_note > 30:
            last_note = time.time()
            try:
                url = page.url
            except Exception:
                url = "?"
            print(f"  ...等待中 ({int(deadline - time.time())}s 剩余) url={url[:80]}")

    if not token:
        print("\n[X] 超时未检测到 token。窗口保留 60 秒，你可继续操作后重跑本脚本。")
        await asyncio.sleep(60)
        await browser.close()
        await pw.stop()
        return 1

    print("[4/4] 让会话通过 completions 端点风控（打开一次对话页）...")
    try:
        await page.goto(f"{BASE_URL}/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(6)
    except Exception as e:
        print("  对话页导航异常（忽略）:", e)

    cookies = await ctx.cookies()
    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    if os.path.exists(COOKIE_FILE):
        os.replace(COOKIE_FILE, COOKIE_FILE + ".bak-manual")
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    print(f"  ✓ 已保存 {len(cookies)} 个 cookie -> {COOKIE_FILE}")

    lst[idx]["token"] = token
    lst[idx]["cookies"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    lst[idx]["consecutive_failures"] = 0
    lst[idx]["last_error"] = ""
    save_accounts(lst)
    exp = _exp(token)
    left = (exp - time.time()) / 86400 if exp else 0
    print(f"  ✓ 已更新 accounts.json 中 {email} 的 token（剩余 {left:.1f} 天）")

    print("\n完成。窗口 10 秒后关闭，随后请重启网关加载新 cookie。")
    await asyncio.sleep(10)
    await browser.close()
    await pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
