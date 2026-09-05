#!/usr/bin/env python3
"""自动破解阿里云滑块验证码（YOLO 视觉识别 + 拟人拖动）。

移植自 captcha111 工具包（JS + Chrome CDP）到 Python + Playwright/camoufox：
  1. 抓取验证码背景图（bg）
  2. YOLO(best.pt) 识别缺口位置 -> 缺口左边界 x1
  3. 二次曲线公式：缺口位置 -> 滑块需拖动距离
  4. 拟人化拖动（ease-out 曲线 + 抖动 + 随机微停顿）
  5. 判断结果，失败可重试

依赖：ultralytics + torch（已装）。模型：captcha_solver/best.pt。

对外主函数：
  await solve_slider(page, max_attempts=4) -> bool
    在当前 page（滑块已弹出）上自动破解，返回是否成功。
"""
import asyncio
import base64
import os
import math
import random
import time

# 模型路径：优先 D:\captcha_solver\best.pt，回退到 qwen_reg 的 captcha111/best.pt
# （qwen_reg 是兄弟目录的注册工具，模型随包分发，比硬编码 D 盘可靠）
CAPTCHA_DIR = r"D:\captcha_solver"
_MODEL_CANDIDATES = [
    os.path.join(CAPTCHA_DIR, "best.pt"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "qwen_reg", "qwen_reg", "captcha111", "best.pt"),
]
MODEL_PATH = next((p for p in _MODEL_CANDIDATES if os.path.exists(p)), _MODEL_CANDIDATES[0])

_MAX_TRAVEL = 260  # 阿里云滑块最大行程

_yolo_model = None


def _load_model():
    global _yolo_model
    if _yolo_model is None:
        from ultralytics import YOLO
        _yolo_model = YOLO(MODEL_PATH)
    return _yolo_model


def _detect_gap(image_path: str, conf: float = 0.25):
    """YOLO 检测缺口，返回 dict(x1,x2,center_x,...) 或 None。"""
    model = _load_model()
    results = model(image_path, imgsz=640, conf=conf, verbose=False)
    boxes = results[0].boxes
    if len(boxes) == 0:
        return None
    best_idx = boxes.conf.argmax().item()
    box = boxes[best_idx]
    x1, y1, x2, y2 = box.xyxy[0].tolist()
    return {
        "x1": round(x1, 2), "y1": round(y1, 2),
        "x2": round(x2, 2), "y2": round(y2, 2),
        "center_x": round((x1 + x2) / 2, 2),
        "confidence": round(box.conf[0].item(), 4),
    }


def _puzzle_to_slider(puzzle_px: float, max_travel: int = _MAX_TRAVEL) -> float:
    """缺口位置 -> 需要拖动的滑块距离（阿里云二次曲线逆解）。
    12*s^2 + max_travel*s - 13*max_travel*puzzle = 0
    """
    a = 12.0
    b = float(max_travel)
    c = -13.0 * max_travel * puzzle_px
    disc = b * b - 4 * a * c
    if disc < 0:
        return 0.0
    s = (-b + math.sqrt(disc)) / (2 * a)
    return max(0.0, min(float(max_travel), s))


async def _find_captcha_frame(page):
    """滑块可能在 iframe 内，返回包含 aliyunCaptcha 元素的 frame（找不到返回 page）。"""
    for fr in page.frames:
        try:
            el = await fr.query_selector("#aliyunCaptcha-img, #aliyunCaptcha-sliding-slider")
            if el:
                return fr
        except Exception:
            continue
    return page


async def _grab_bg_image(frame) -> str:
    """抓取验证码背景图，返回本地临时 png 路径；失败返回空串。"""
    src = await frame.evaluate(
        """() => {
            const b = document.getElementById('aliyunCaptcha-img');
            if (b && b.src && (b.src.startsWith('data:image') || b.src.startsWith('http')))
                return b.src;
            return null;
        }"""
    )
    if not src:
        return ""
    # 把图片在页面上下文转成 dataURL（http 图也能取到 base64）
    if src.startswith("http"):
        try:
            data_url = await frame.evaluate(
                """async (u) => {
                    const r = await fetch(u); const bl = await r.blob();
                    return await new Promise((res,rej)=>{const fr=new FileReader();fr.onload=()=>res(fr.result);fr.onerror=rej;fr.readAsDataURL(bl);});
                }""",
                src,
            )
        except Exception:
            data_url = ""
    else:
        data_url = src
    if not data_url or "," not in data_url:
        return ""
    b64 = data_url.split(",", 1)[1]
    tmp = os.path.join(os.environ.get("TEMP", CAPTCHA_DIR), f"cap_bg_{int(time.time()*1000)}.png")
    with open(tmp, "wb") as f:
        f.write(base64.b64decode(b64))
    return tmp


async def _get_calibration(frame):
    """背景图自然宽 / 显示宽，用于把 YOLO(自然像素) 换算成显示像素。"""
    return await frame.evaluate(
        """() => {
            const bg = document.getElementById('aliyunCaptcha-img');
            if (!bg) return null;
            return { naturalW: bg.naturalWidth || 296,
                     displayW: bg.getBoundingClientRect().width };
        }"""
    )


async def _measure_padding(frame):
    """像素扫描拼图块，找到不透明像素起始的左内边距 padding。"""
    return await frame.evaluate(
        """async () => {
            try {
                const p = document.getElementById('aliyunCaptcha-puzzle');
                if (!p || !p.src) return 0;
                const r = await fetch(p.src); const bl = await r.blob();
                const durl = await new Promise((res,rej)=>{const fr=new FileReader();fr.onload=()=>res(fr.result);fr.onerror=rej;fr.readAsDataURL(bl);});
                const img = new Image();
                await new Promise((res,rej)=>{img.onload=res;img.onerror=rej;img.src=durl;});
                const cv = document.createElement('canvas');
                cv.width = img.naturalWidth; cv.height = img.naturalHeight;
                const ctx = cv.getContext('2d'); ctx.drawImage(img,0,0);
                const d = ctx.getImageData(0,0,cv.width,cv.height).data;
                for (let x=0;x<cv.width;x++)
                    for (let y=0;y<cv.height;y++)
                        if (d[(y*cv.width+x)*4+3] > 20) return x;
                return 0;
            } catch(e) { return 0; }
        }"""
    )


async def _human_drag(page, frame, target_slider_px: float):
    """拟人化拖动滑块。target_slider_px = 需要拖动的距离（显示像素）。"""
    info = await frame.evaluate(
        """() => {
            const s = document.getElementById('aliyunCaptcha-sliding-slider');
            if (!s) return null;
            const r = s.getBoundingClientRect();
            return { x: r.left + r.width/2, y: r.top + r.height/2 };
        }"""
    )
    if not info:
        return {"error": "slider not found"}

    start_x = info["x"]
    start_y = info["y"]
    target_x = start_x + target_slider_px

    # Phase 1: 自然接近
    approach_from_x = start_x - 150 - random.random() * 100
    approach_from_y = start_y + 80 + random.random() * 60
    steps = 18 + random.randint(0, 7)
    for i in range(1, steps + 1):
        t = i / steps
        ease = 1 - (1 - t) ** 2
        x = approach_from_x + (start_x - approach_from_x) * ease
        y = approach_from_y + (start_y - approach_from_y) * ease
        await page.mouse.move(x, y)
        await asyncio.sleep((3 + random.random() * 5) / 1000)
    await page.mouse.move(start_x, start_y)
    await asyncio.sleep((60 + random.random() * 80) / 1000)

    # Phase 2: 按下
    off_x = (random.random() - 0.5) * 4
    off_y = (random.random() - 0.5) * 4
    await page.mouse.move(start_x + off_x, start_y + off_y)
    await page.mouse.down()
    await asyncio.sleep((80 + random.random() * 60) / 1000)

    # Phase 3: 拟人拖动（cubic ease-out，单调递增不回退）
    px_start = start_x + off_x
    distance = target_x - px_start
    drag_steps = 35 + random.randint(0, 14)
    for i in range(1, drag_steps + 1):
        t = i / drag_steps
        eased = 1 - (1 - t) ** 3
        x = px_start + distance * eased
        y = start_y + off_y * (1 - t) + (random.random() - 0.5) * 1.5
        await page.mouse.move(x, y)
        base_delay = 3 + t * t * 16
        jitter = (random.random() - 0.5) * 3
        await asyncio.sleep(max(2, base_delay + jitter) / 1000)
        if 4 < i < drag_steps - 5 and random.random() < 0.02:
            await asyncio.sleep((15 + random.random() * 30) / 1000)

    # Phase 4: 精确到位
    await page.mouse.move(target_x, start_y)
    await asyncio.sleep((40 + random.random() * 40) / 1000)
    # Phase 5: 释放
    await page.mouse.up()
    await asyncio.sleep(0.2)
    return {"ok": True, "travel": round(target_slider_px, 1)}


async def _check_result(frame, timeout_ms: int = 12000) -> bool:
    """判断验证结果：verified=成功，fail=失败，navigation=可能成功。"""
    end = time.time() + timeout_ms / 1000
    while time.time() < end:
        try:
            st = await frame.evaluate(
                """() => {
                    const e = document.getElementById('aliyunCaptcha-sliding-text');
                    return { cls: e ? e.className : null, res: window.__captchaResult || null };
                }"""
            )
            if st:
                cls = (st.get("cls") or "").split()
                if "verified" in cls:
                    return True
                if "fail" in cls:
                    return False
                res = st.get("res")
                if isinstance(res, dict):
                    if res.get("ok"):
                        return True
                    return False
        except Exception as e:
            msg = str(e)
            if any(k in msg for k in ("context was destroyed", "navigation", "Execution context")):
                return True  # 页面跳转，通常代表成功
        await asyncio.sleep(0.5)
    return False


async def _refresh_challenge(frame):
    """刷新验证码换一张图重试。"""
    try:
        await frame.click("#aliyunCaptcha-btn-close", timeout=3000)
    except Exception:
        pass
    await asyncio.sleep(1)
    try:
        await frame.click("#button", timeout=5000)
    except Exception:
        try:
            await frame.click("#aliyunCaptcha-captcha-text", timeout=2000)
        except Exception:
            pass
    await asyncio.sleep(2)


async def solve_slider(page, max_attempts: int = 4, tolerance: int = 6) -> bool:
    """在当前 page（滑块已弹出）上自动破解阿里云滑块。返回是否成功。"""
    for attempt in range(1, max_attempts + 1):
        print(f"  [captcha] 第 {attempt}/{max_attempts} 次自动破解...")
        frame = await _find_captcha_frame(page)

        # 等滑块与背景图就绪
        ready = False
        for _ in range(20):
            has = await frame.evaluate(
                """() => !!document.getElementById('aliyunCaptcha-sliding-slider')
                        && !!document.getElementById('aliyunCaptcha-img')"""
            )
            if has:
                ready = True
                break
            await asyncio.sleep(0.5)
        if not ready:
            print("  [captcha] 未检测到滑块元素")
            return False

        bg_path = await _grab_bg_image(frame)
        if not bg_path:
            print("  [captcha] 抓取背景图失败，刷新重试")
            await _refresh_challenge(frame)
            continue

        try:
            gap = await asyncio.to_thread(_detect_gap, bg_path)
        except Exception as e:
            print(f"  [captcha] YOLO 检测异常: {e}")
            gap = None
        finally:
            try:
                os.remove(bg_path)
            except Exception:
                pass

        if not gap:
            print("  [captcha] 未检测到缺口，刷新重试")
            await _refresh_challenge(frame)
            continue

        calib = await _get_calibration(frame)
        padding = await _measure_padding(frame)
        gap_left = gap["x1"]
        if calib and calib.get("displayW", 0) > 0 and calib.get("naturalW", 0) > 0:
            scale = calib["displayW"] / calib["naturalW"]
            target_puzzle_px = round((gap_left - padding) * scale)
        else:
            target_puzzle_px = round(gap_left - padding)

        target_slider = _puzzle_to_slider(target_puzzle_px)
        print(f"  [captcha] 缺口x1={gap_left} conf={gap['confidence']} "
              f"padding={padding} -> 目标拖动={target_slider:.1f}px")

        drag = await _human_drag(page, frame, target_slider)
        if drag.get("error"):
            print(f"  [captcha] 拖动失败: {drag['error']}")
            await _refresh_challenge(frame)
            continue

        ok = await _check_result(frame)
        if ok:
            print(f"  [captcha] ✓ 第 {attempt} 次破解成功")
            return True
        print(f"  [captcha] ✗ 第 {attempt} 次未通过，刷新重试")
        await _refresh_challenge(frame)

    print(f"  [captcha] 已尝试 {max_attempts} 次仍未通过")
    return False
