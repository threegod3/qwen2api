# syntax=docker/dockerfile:1.7

# 说明：工作区 frontend/ 仅保留预构建产物 dist/（无源码），
# 因此不再做 node 构建阶段，直接把 dist 拷进镜像。
# Stage 1: Runtime image.
FROM python:3.12-slim-bookworm
WORKDIR /workspace

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860 \
    WORKERS=1 \
    LOG_LEVEL=INFO \
    PYTHONPATH=/workspace

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    wget \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdbus-glib-1-2 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpangocairo-1.0-0 \
    libpulse0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    libxshmfence1 \
    fonts-liberation \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Download Camoufox browser at build time so runtime hosts do not need to fetch it again.
RUN python -m camoufox fetch

# Playwright + Chromium：WAF 绕过（流式对话/图片/视频）的硬依赖。
# --with-deps 自动安装 Chromium 所需的系统库（多数已在上方 apt 层覆盖）。
RUN python -m playwright install chromium --with-deps

COPY backend/ ./backend/
COPY start.py ./
# 根目录运行时文件：auto_captcha(YOLO 拼图回退路径 import 用)与 bx_pool(初始签名池，
# 运行时会被 ./bx_pool.json 单文件挂载覆盖)。之前没 COPY 进镜像，容器里
# `from auto_captcha import ...` 报 ModuleNotFoundError、bx 池为空。
COPY auto_captcha.py ./
COPY bx_pool.json ./
# 前端预构建产物（工作区无前端源码，不做 npm 构建）
COPY frontend/dist ./frontend/dist
RUN mkdir -p /workspace/data /workspace/logs /workspace/frontend

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-7860}/healthz" || exit 1

CMD ["sh", "-c", "python -m uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-7860} --workers ${WORKERS:-1}"]
