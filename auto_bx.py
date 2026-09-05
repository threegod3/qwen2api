"""自动抓取 Qwen 反爬签名（bx-ua / bx-umidtoken / bx-v）并写入 bx_pool.json。

无需手动抓 HAR。原理：用 Playwright 无头浏览器打开 https://chat.qwen.ai ，
等页面里的 BaXia 反爬 SDK 初始化后，在页面内发一个真实的 fetch 请求，
BaXia 会自动往请求头里注入 bx-* 签名，脚本拦截这些请求头并写入 bx_pool.json。

用法:
    python auto_bx.py            # 抓一条并追加进池子
    python auto_bx.py --show     # 只查看当前池子
    python auto_bx.py --headed   # 显示浏览器窗口（调试用）

依赖:
    pip install playwright
    python -m playwright install chromium

网关会在 60 秒内自动重读 bx_pool.json（无需重启）。
建议：多跑几次本脚本，池子里多几条签名可降低单条过期/被风控的概率。
"""
import asyncio
import json
import os
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
POOL_PATH = os.path.join(PROJECT_ROOT, "bx_pool.json")
ACCOUNTS_PATH = os.path.join(PROJECT_ROOT, "data", "accounts.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def load_pool() -> list:
    if not os.path.exists(POOL_PATH):
        return []
    try:
        with open(POOL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_pool(pool: list):
    with open(POOL_PATH, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)


def show_pool():
    pool = load_pool()
    print(f"当前池子: {POOL_PATH}")
    print(f"已有 {len(pool)} 条 bx 记录:")
    for i, e in enumerate(pool):
        print(
            f"  [{i}] {e.get('captured_at','?')}  "
            f"bx_ua_len={len(e.get('bx_ua',''))}  "
            f"bx_v={e.get('bx_v','?')}  ver={e.get('web_version','?')}  "
            f"note={e.get('note','')}"
        )


def _pick_token() -> str:
    """从 accounts.json 里挑一个可用 token（可选，用于让页面处于登录态）。"""
    if not os.path.exists(ACCOUNTS_PATH):
        return ""
    try:
        with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        accs = data if isinstance(data, list) else list(data.values()) if isinstance(data, dict) else []
        for a in accs:
            if isinstance(a, dict) and a.get("token"):
                return a["token"]
    except Exception:
        pass
    return ""


async def capture_bx(headed: bool = False, timeout_ms: int = 60000) -> dict | None:
    """打开 chat.qwen.ai，触发带 bx-* 的真实请求并拦截其请求头。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[!] 未安装 playwright。请先执行:")
        print("    pip install playwright")
        print("    python -m playwright install chromium")
        return None

    token = _pick_token()
    captured: list[dict] = []

    p = await async_playwright().start()
    browser = await p.chromium.launch(
        headless=not headed,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    ctx = await browser.new_context(
        user_agent=USER_AGENT,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport={"width": 1600, "height": 900},
    )
    if token:
        try:
            await ctx.add_cookies(
                [{"name": "token", "value": token, "domain": "chat.qwen.ai", "path": "/"}]
            )
        except Exception:
            pass

    page = await ctx.new_page()

    def on_request(req):
        h = req.headers
        if "bx-ua" in h and "bx-umidtoken" in h:
            captured.append(
                {
                    "bx_ua": h.get("bx-ua", ""),
                    "bx_umidtoken": h.get("bx-umidtoken", ""),
                    "bx_v": h.get("bx-v", ""),
                    "web_version": h.get("version", ""),
                    "timezone": h.get("timezone", ""),
                    "user_agent": req.headers.get("user-agent", USER_AGENT),
                }
            )

    page.on("request", on_request)

    if token:
        try:
            await page.add_init_script(
                f"try{{localStorage.setItem('token',{json.dumps(token)});}}catch(e){{}}"
            )
        except Exception:
            pass

    try:
        try:
            await page.goto("https://chat.qwen.ai/", timeout=45000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[i] 页面加载提示（可忽略）: {str(e)[:80]}")

        if token:
            try:
                await page.evaluate(
                    f"try{{localStorage.setItem('token',{json.dumps(token)});}}catch(e){{}}"
                )
            except Exception:
                pass

        # 等 BaXia SDK 初始化（window.baxiaCommon 就绪）
        for _ in range(20):
            ready = await page.evaluate("typeof window.baxiaCommon === 'function'")
            if ready:
                break
            await page.wait_for_timeout(500)
        await page.wait_for_timeout(1500)

        # 在页面内发真实请求，BaXia 会自动注入 bx-* 头；轮询直到拦截到
        deadline_polls = max(1, timeout_ms // 3000)
        for attempt in range(deadline_polls):
            try:
                await page.evaluate(
                    """async (tk) => {
                        const headers = tk ? {'authorization': 'Bearer ' + tk} : {};
                        try { await fetch('https://chat.qwen.ai/api/models', {headers, credentials:'include'}); } catch(e){}
                    }""",
                    token,
                )
            except Exception:
                pass
            await page.wait_for_timeout(3000)
            if captured:
                break
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await p.stop()
        except Exception:
            pass

    if not captured:
        return None
    entry = captured[0]
    if not entry.get("bx_ua") or not entry.get("bx_umidtoken"):
        return None
    entry["captured_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry["note"] = "auto_bx.py"
    return entry


async def main_async():
    headed = "--headed" in sys.argv
    entry = await capture_bx(headed=headed)
    if not entry:
        print("[!] 没抓到 bx 签名。可能原因：网络不通、被 WAF 拦截、或 BaXia 未初始化。")
        print("    可重试，或改用 --headed 观察，或退回 HAR 方式（add_bx_from_har.py）。")
        sys.exit(1)

    pool = load_pool()
    if any(e.get("bx_ua") == entry["bx_ua"] for e in pool):
        print("[=] 抓到的 bx-ua 已在池子里，跳过（可稍后重试获取新签名）。")
    else:
        pool.append(entry)
        save_pool(pool)
        print(f"[+] 已写入新签名，池子现共 {len(pool)} 条。")
        print(
            f"    bx_ua_len={len(entry['bx_ua'])}  bx_v={entry['bx_v']}  "
            f"web_version={entry['web_version']}  captured_at={entry['captured_at']}"
        )
    print("\n网关会在 60 秒内自动重读 bx_pool.json（无需重启）。")


def main():
    if "--show" in sys.argv:
        show_pool()
        return
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
