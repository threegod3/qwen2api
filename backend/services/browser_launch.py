"""Playwright 浏览器启动：本地系统 Chrome 优先，容器 Chromium 兜底。

- 本地：`channel="chrome"`（系统 Chrome，指纹最真，WAF 通过率最高）。
- 容器/CI：没有系统 Chrome，`launch(channel="chrome")` 直接抛错，
  之前会导致整个浏览器路径全灭（表现为上游空响应）。此时回退到
  Playwright 自带 Chromium，以 `--headless=new`（完整 Blink，有始有终，
  比 headless shell 隐蔽得多）无头运行，无需 X server。
"""

import logging
import os

log = logging.getLogger("qwen2api.browser_launch")

_LAUNCH_ARGS = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]


async def launch_browser(pw, headed: bool = False):
    """按环境启动 Chromium 内核。headed=True 仅本地人工场景使用。"""
    if headed:
        # 人工值守：必须可见窗口，回退无意义（容器里本来也看不见）
        return await pw.chromium.launch(
            channel="chrome", headless=False, args=list(_LAUNCH_ARGS),
        )
    # 无头：先试系统 Chrome
    try:
        return await pw.chromium.launch(
            channel="chrome", headless=True, args=list(_LAUNCH_ARGS),
        )
    except Exception as e:
        log.warning("[browser_launch] 系统 Chrome 不可用(%s)，回退容器 Chromium --headless=new", str(e)[:120])
    # 容器：自带 Chromium + new headless（不需要 X）
    extra = []
    if os.getenv("PLAYWRIGHT_CHROMIUM_SANDBOX_OFF", "1") == "1":
        extra.append("--disable-dev-shm-usage")
    return await pw.chromium.launch(
        headless=False,
        args=["--headless=new", "--disable-gpu", *extra, *_LAUNCH_ARGS],
    )
