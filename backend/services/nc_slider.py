"""阿里云 noCaptcha「滑块拖到最右」自动破解（Playwright 异步版，无重型依赖）。

背景
----
`chat.qwen.ai` 的 `_____tmd_____/punish` 挑战页内嵌的是 noCaptcha 滑块
（`#nocaptcha / #nc_1_n1z.btn_slide`），**不是**拼图缺口型滑块——YOLO
缺口识别在这里无用武之地（页面里根本没有 `#aliyunCaptcha-img` 背景图）。
旧版 `auto_captcha.solve_slider` 在这里永远走「未检测到滑块元素」分支，
这就是 YOLO 移植「没有效果」的直接原因。

破解要点（2026-09-04 实测通过 headless Playwright + 系统 Chrome 验证）
----
1. 滑块模块是懒加载的，`goto` 后需轮询等待 `#nc_1_n1z / .btn_slide` 出现
   （通常 10~20s，`nc_scale` 文案从「加载中」变为「请按住滑块」）。
2. 行为检测严格：0.5s 内跑完 250px 会被重置；必须 ~2s S 形曲线
   （慢-快-慢）+ 3~7px 过冲 + 回调修正 + 末端 0.25~0.4s 停留。
3. 成功信号：页面导航离开 punish（通常去 `taobao.com`）并写下 `x5sec`
   cookie；调用方保存 cookie 后重试直连即可通过。

对外主函数：`await solve_nocaptcha_slider(page, timeout=120.0) -> bool`
"""

import asyncio
import logging
import math
import random

log = logging.getLogger("qwen2api.nc_slider")

_HANDLE_SELECTORS = "#nc_1_n1z, .btn_slide"
_TRACK_SELECTORS = "#nc_1_wrapper, .nc_scale"


async def _human_drag_to_end(page, handle_box: dict, track_box: dict) -> float:
    """拟人拖拽手柄到轨道最右端。返回拖动距离（px）。"""
    start_x = handle_box["x"] + handle_box["width"] / 2
    start_y = handle_box["y"] + handle_box["height"] / 2
    target_x = track_box["x"] + track_box["width"] - handle_box["width"] / 2 - 1

    # Phase 1: 从远处自然接近（ease-out）
    approach_from_x = start_x - 150 - random.random() * 100
    approach_from_y = start_y + 80 + random.random() * 60
    steps = 18 + random.randint(0, 7)
    for i in range(1, steps + 1):
        t = i / steps
        eased = 1 - (1 - t) ** 2
        await page.mouse.move(
            approach_from_x + (start_x - approach_from_x) * eased,
            approach_from_y + (start_y - approach_from_y) * eased,
        )
        await asyncio.sleep((3 + random.random() * 5) / 1000)
    await page.mouse.move(start_x, start_y)
    await asyncio.sleep((100 + random.random() * 100) / 1000)

    # Phase 2: 按下（带微小随机偏移）
    off_x = (random.random() - 0.5) * 4
    off_y = (random.random() - 0.5) * 4
    await page.mouse.move(start_x + off_x, start_y + off_y)
    await page.mouse.down()
    await asyncio.sleep((120 + random.random() * 80) / 1000)

    # Phase 3: 主拖拽 —— S 形（慢-快-慢）到「过冲点」，总耗时约 2s
    overshoot_px = random.uniform(3.0, 7.0)
    overshoot_x = target_x + overshoot_px
    px_start = start_x + off_x
    distance = overshoot_x - px_start
    drag_steps = random.randint(60, 80)
    s_0 = 1 / (1 + math.exp(4.5))
    s_1 = 1 / (1 + math.exp(-5.5))
    for i in range(1, drag_steps + 1):
        t = i / drag_steps
        s_curve = 1 / (1 + math.exp(-10 * (t - 0.45)))
        eased = (s_curve - s_0) / (s_1 - s_0)
        x = px_start + distance * eased
        y = start_y + off_y * (1 - t) + random.uniform(-1.2, 1.2)
        await page.mouse.move(x, y)
        if t < 0.2:
            step_delay = random.uniform(0.025, 0.040)
        elif t < 0.7:
            step_delay = random.uniform(0.014, 0.024)
        else:
            step_delay = random.uniform(0.028, 0.055)
        await asyncio.sleep(step_delay)
        if 0.3 < t < 0.8 and random.random() < 0.08:
            await asyncio.sleep(random.uniform(0.04, 0.09))

    # Phase 4: 过冲后停顿（真人反应时间）
    await asyncio.sleep(random.uniform(0.12, 0.25))

    # Phase 5: 回调修正 —— 慢速拉回精确位置
    correction_steps = random.randint(12, 20)
    for i in range(1, correction_steps + 1):
        t = i / correction_steps
        eased = 1 - (1 - t) ** 2
        x = overshoot_x + (target_x - overshoot_x) * eased
        await page.mouse.move(x, start_y + random.uniform(-0.8, 0.8))
        await asyncio.sleep(random.uniform(0.025, 0.045))

    # Phase 6: 末端定格确认后释放
    await page.mouse.move(target_x, start_y)
    await asyncio.sleep(random.uniform(0.25, 0.40))
    await page.mouse.up()
    await asyncio.sleep(1.0)
    return round(target_x - start_x, 1)


async def solve_nocaptcha_slider(page, timeout: float = 120.0) -> bool:
    """在已打开 punish 挑战页的 `page` 上求解 noCaptcha 滑块。

    成功（页面跳离 punish / 出现 `x5sec` cookie）返回 True；
    无滑块、超时、异常返回 False（调用方走人工/降级路径）。
    """
    # 1) 等滑块手柄出现（nc 模块懒加载，轮询）
    handle = None
    deadline = asyncio.get_event_loop().time() + min(timeout, 60.0)
    while asyncio.get_event_loop().time() < deadline:
        try:
            handle = await page.query_selector(_HANDLE_SELECTORS)
            if handle:
                box = await handle.bounding_box()
                if box and box["width"] > 0:
                    break
        except Exception:
            pass
        handle = None
        await asyncio.sleep(2.0)
    if handle is None:
        log.info("[nc_slider] 未发现滑块手柄（可能已通过或非滑块挑战）")
        return False

    try:
        handle_box = await handle.bounding_box()
        track_el = await page.query_selector(_TRACK_SELECTORS)
        if track_el is None:
            log.warning("[nc_slider] 找不到滑块轨道")
            return False
        track_box = await track_el.bounding_box()
        if not handle_box or not track_box:
            return False
    except Exception as e:
        log.warning("[nc_slider] 读取滑块几何失败: %s", e)
        return False

    # 2) 拟人拖拽
    try:
        dist = await _human_drag_to_end(page, handle_box, track_box)
        log.info("[nc_slider] 拖拽完成 travel=%.1fpx，等待校验...", dist)
    except Exception as e:
        log.warning("[nc_slider] 拖拽异常: %s", e)
        return False

    # 3) 等待结果：跳离 punish 即成功（noCaptcha 通过后一般 3~10s 内导航）
    for _ in range(10):
        await asyncio.sleep(3.0)
        try:
            url = page.url
        except Exception:
            return True  # 页面已销毁（导航中），视为成功
        if "punish" not in url:
            log.info("[nc_slider] 验证通过，已跳离 punish: %s", url[:100])
            return True
        try:
            cookies = await page.context.cookies()
            if any(c.get("name") == "x5sec" for c in cookies):
                log.info("[nc_slider] 已签发 x5sec，视为通过")
                return True
        except Exception:
            pass
    log.warning("[nc_slider] 拖拽后仍停留在 punish 页")
    return False
