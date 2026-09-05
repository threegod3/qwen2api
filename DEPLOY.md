# qwen2API 部署指南（2026-08-17 修复版）

本版本修复了 Qwen 前端改版 + WAF 升级导致的流式对话 / 图片 / 视频接口失效问题。
核心修复依赖 **Playwright 浏览器内核**（用于绕过 BaXia WAF 挑战），部署时**必须安装**。

---

## 一、环境要求

| 项目 | 要求 |
|---|---|
| Python | 3.10+（推荐 3.11/3.12） |
| 网络 | 可访问 `chat.qwen.ai`（海外直连或代理均可） |
| 磁盘 | 约 600MB（Python 依赖 + Chromium 内核） |
| 系统 | Windows / Linux / macOS 均可；Docker 需 20.10+ |

---

## 二、安装步骤

### 1. 解压

```bash
# Windows (PowerShell)
Expand-Archive qwen2api-python-2026-08-17-fixed.zip -DestinationPath .\qwen2api
cd qwen2api

# Linux / macOS
unzip qwen2api-python-2026-08-17-fixed.zip -d qwen2api
cd qwen2api
```

### 2. 安装 Python 依赖

```bash
# 建议使用虚拟环境
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r backend\requirements.txt        # Windows
# 或
pip install -r backend/requirements.txt        # Linux/macOS
```

> 依赖清单已包含 `playwright`、`curl_cffi`、`fastapi`、`camoufox` 等。

### 3. 安装浏览器内核（**关键步骤，不可跳过**）

WAF 绕过（流式对话 / 图片 / 视频）依赖 Playwright 控制的 Chromium：

```bash
# 安装 Chromium 内核
python -m playwright install chromium

# Linux 若缺系统库，追加 --with-deps（需要 root/sudo）
# sudo python -m playwright install chromium --with-deps

# 可选：Camoufox 内核（仅账号注册/自动登录功能需要，纯网关可跳过）
python -m camoufox fetch
```

验证：

```bash
python -m playwright --version        # 应有输出，如 Version 1.60.0
python -c "from playwright.async_api import async_playwright; print('playwright OK')"
```

---

## 三、配置

### 1. `.env`（端口 / 管理密钥）

已随包提供，按需修改：

```ini
ADMIN_KEY=test123456          # 管理密钥（请求鉴权用）
PORT=8760                     # 服务端口（Docker 内为 7860）
WORKERS=1
LOG_LEVEL=INFO
QWEN_THINKING_ENABLED=true    # 是否透传思考过程
```

### 2. `data/accounts.json`（上游账号）

已预置 **3 个可用测试账号**（邮箱 + 密码 + 登录 token，token 有效期至 2026-08-31 前后）。
如需换账号：

```json
[
  {
    "email": "你的账号",
    "password": "你的密码",
    "token": "eyJhbGciOi...（Qwen 登录 token）"
  }
]
```

> token 获取方式：登录 chat.qwen.ai 后从浏览器 DevTools → Application → Local Storage → `token` 字段复制。
> token 过期后网关会自动提示，可手动更新。

### 3. `data/api_keys.json`（下游 API Key）

下游客户端调用网关时使用的 API Key 列表。默认有一个测试 key，可自行增删。

### 4. `bx_pool.json`（浏览器指纹池）

已随包提供（web_version=0.2.86）。正常情况下无需改动；若上游大版本升级导致失效，
可用根目录 `auto_bx.py` 重新抓取。

---

## 四、启动服务

### 方式 A：后台服务（推荐，纯 API 网关）

```bash
# Windows (PowerShell)
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8760

# Linux/macOS
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8760
```

看到如下日志即为就绪（约 10~30 秒，期间会预热账号会话）：

```
INFO:     Application startup complete.
[ChatIdPool] started (target=1, ttl=1800s)
```

### 方式 B：一行脚本（前端开发版，含 WebUI 热更新）

```bash
python start.py
# 前端 WebUI: http://127.0.0.1:5174
# 后端 API:   http://127.0.0.1:7860（默认 PORT=7860）
```

> 注意：`start.py` 会安装依赖并启动前端 dev server，适合开发调试；生产建议用方式 A。

### 方式 C：Docker

```bash
docker compose up -d --build
# 服务: http://127.0.0.1:7860
```

> Dockerfile 已内置 Playwright + Chromium。首次构建较慢（需下载浏览器内核）。
> `./data` 与 `./logs` 已挂载为卷，账号数据持久化在宿主机。

---

## 五、验证服务

### 健康检查

```bash
curl http://127.0.0.1:8760/api
# {"status":"qwen2API Enterprise Gateway is running","docs":"/docs","version":"2.0.0"}
```

### 1. 文本对话（流式）

```bash
curl -N http://127.0.0.1:8760/v1/chat/completions \
  -H "Authorization: Bearer 你的APIKey" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.8-max","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

### 2. 文生图

```bash
curl http://127.0.0.1:8760/v1/images/generations \
  -H "Authorization: Bearer 你的APIKey" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"一只柴犬在草地上奔跑","model":"qwen-image-max","response_format":"url"}'
```

### 3. 文生视频（异步协议）

```bash
# 提交任务（立即返回 task_id）
curl http://127.0.0.1:8760/v1/videos/generations \
  -H "Authorization: Bearer 你的APIKey" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"一只猫在沙滩上散步","model":"qwen-video"}'
# → {"task_id":"qv_xxx","status":"queued"}

# 轮询结果（约 2~4 分钟生成完成）
curl http://127.0.0.1:8760/v1/videos/generations/qv_xxx \
  -H "Authorization: Bearer 你的APIKey"
# → {"task_id":"...","status":"completed","url":"https://cdn.qwenlm.ai/..."}
```

### 4. 图生视频（带参考图）

```bash
curl http://127.0.0.1:8760/v1/videos/generations \
  -H "Authorization: Bearer 你的APIKey" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"让图中的女子向镜头走来","model":"qwen-video","image":"/绝对路径/参考图.png"}'
```

> `image` 支持：本地绝对路径 / http(s) URL / data:base64 三种格式。
> 生成的视频 URL 路径含 `/i2v/` 即为参考图生效。

### 5. API 文档

浏览器打开 **http://127.0.0.1:8760/docs**（Swagger UI，可在线调试所有接口）。

---

## 六、接口速查

| 端点 | 功能 | 备注 |
|---|---|---|
| `POST /v1/chat/completions` | 文本对话 | OpenAI 兼容，SSE 流式 |
| `GET /v1/models` | 模型列表 | |
| `POST /v1/images/generations` | 文生图 | |
| `POST /v1/images/edits` | 图生图 | multipart 或 JSON |
| `POST /v1/videos/generations` | 文生视频 / 图生视频 | 异步，返回 task_id |
| `GET /v1/videos/generations/{task_id}` | 视频任务查询 | 轮询直到 completed |
| `POST /v1/chat/completions` (Anthropic/Gemini 兼容路由) | 多协议 | |

---

## 七、常见问题

### Q1：启动报 `ModuleNotFoundError: No module named 'playwright'`
→ 未安装 Playwright。执行：
```bash
pip install playwright && python -m playwright install chromium
```

### Q2：报错 `BrowserType.launch: Executable doesn't exist ... chromium`
→ Chromium 内核未下载。执行 `python -m playwright install chromium`。

### Q3：日志出现 `curl_cffi 真流式未取得数据 status=403 waf=punish`，最终仍报 WAF
→ 这是预期内的降级路径日志（直连被 WAF 拦 → 自动切浏览器 UI 驱动）。
若最终仍失败，检查：Playwright/Chromium 是否装好、账号 token 是否过期、
`data/browser_cookies.json` 是否损坏（可删除后重启让服务重建）。

### Q4：视频任务一直 processing / 返回 504
→ 上游视频生成本身需 2~5 分钟，属正常。若超 10 分钟仍 running，
通常是账号风控（quota），服务会自动换账号重试；多个账号都失败时请更换账号。

### Q5：端口被占用
```bash
# Windows
netstat -ano | findstr :8760
taskkill /F /PID <pid>
```

### Q6：Docker 部署后接口 500 / 浏览器相关报错
→ 确认镜像构建时 Playwright 内核装好（构建日志应有 `playwright install chromium`）。
若使用旧镜像，需重新 `docker compose build`。

### Q7：上游 Qwen 前端又升级（version 变化）
→ 观察 `bx_pool.json` 里的 `web_version`，若与上游不一致，运行 `auto_bx.py` 重新采集。

---

## 八、安全提醒

- 本包 `data/accounts.json` 含 **3 个真实 Qwen 测试账号** 凭据，请勿公开分享。
- 对外提供服务前，务必修改 `.env` 中的 `ADMIN_KEY` 和 `data/api_keys.json`。
- 生产环境建议置于反向代理（HTTPS）之后。
