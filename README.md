# qwen2api 网关（分发版）

把通义千问网页版（chat.qwen.ai）转换为 OpenAI / Anthropic / Gemini 兼容接口的本地网关。
后端 FastAPI，前端管理台由后端统一托管（单端口 `7860`）。

> 出处：本项目基于开源项目 qwen2API 二次修改，感谢原作者与社区贡献。

---

## ⚠️ 免责声明（使用前必读）

1. **非官方项目**：本项目与阿里巴巴、通义千问及任何官方服务**没有任何从属、代理、合作或认可关系**，也不是官方产品，不构成任何官方服务承诺。
2. **自行承担风险**：使用本项目可能导致上游账号被限制/封禁、请求被拦截、数据丢失、服务中断。由此产生的一切直接或间接损失（包括账号损失与法律纠纷），**由使用者自行承担**，分发者与维护者不承担任何责任。
3. **遵守规则**：请自行确认你的使用方式符合所在地区法律法规，以及通义千问/阿里云的服务条款与 robots/风控要求。**不要**将本项目用于违反服务条款、违反法律或高并发滥用上游资源的场景。
4. **账号安全**：请使用**专用小号**，不要填入重要账号；`ADMIN_KEY`、API Key、token 属于敏感信息，不要截图外发、不要提交到公开仓库。
5. **无担保**：本项目按"现状"提供，不做任何明示或暗示担保（可用性、稳定性、安全性均不保证）。
6. **权利人联系**：如果你是相关权利人并认为本项目侵犯你的合法权益，请通过分发者提供的联系方式说明，核实后会配合处理（包括下架相关内容）。

**继续使用即视为你已阅读、理解并接受以上全部条款。**

---

## 功能

- `POST /v1/chat/completions`（流式/非流式，OpenAI 兼容）
- `POST /anthropic/v1/messages`（Claude 兼容）、Gemini 兼容接口
- `POST /v1/images/generations` 图片生成、`POST /v1/videos/generations` 视频生成
- 多账号轮询、限流冷却、失败重试；token 过期自动重登；上游风控（滑块）自动求解+自愈
- Web 管理台：`http://127.0.0.1:7860/`（账号管理、Key 管理、接口测试）

---

## 快速开始（Docker，推荐）

### 1. 准备配置

```bash
mkdir qwen2api && cd qwen2api
mkdir -p data logs
```

把本项目所有文件放到该目录，然后编辑 `.env`，**必须**改掉管理密钥：

```env
ADMIN_KEY=换成你自己的强密码
```

### 2. 构建并启动（注意：必须先 build）

```bash
docker compose build
docker compose up -d
docker compose ps            # healthy 即正常
curl http://127.0.0.1:7860/healthz
```

> 改过任何源码后都要重新 `docker compose build`，光 `up` 不会更新镜像。

### 3. 添加上游账号

打开管理台 `http://127.0.0.1:7860/`，用 `ADMIN_KEY` 登录 → 账号管理 → 添加，
填入你的千问账号 `token`（登录 chat.qwen.ai 后从浏览器 localStorage 取 `token`），
可附带 `email` / `password`（用于 token 过期后自动重登）。

或调管理接口（`Authorization: Bearer <ADMIN_KEY>`）：

```bash
curl -X POST http://127.0.0.1:7860/api/admin/accounts \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <ADMIN_KEY>" \
  -d '{"email":"你的邮箱","password":"你的密码","token":"千问网页token"}'
```

### 4. 创建调用 Key 并测试

管理台 → API Key → 新建，把得到的 Key 填到客户端：

```bash
curl -N http://127.0.0.1:7860/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <你的API_KEY>" \
  -d '{"model":"qwen3.7-plus","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

OpenAI Python SDK：`base_url="http://127.0.0.1:7860/v1"` 即可。

---

## 本地源码运行（开发调试用）

```bash
python start.py
```

要求 Python 3.10+；首次会自动装依赖并下载浏览器内核，耗时较长属正常。

---

## 常见问题

| 现象 | 说明 |
|---|---|
| 首次请求很慢（几十秒~几分钟） | 首次需过上游验证/预热会话，后续请求恢复正常速度 |
| `token 过期` 后第一次调用失败 | 自愈是后台任务，过期后第一次失败、重试即好（建议账号附带密码以便自动重登） |
| 容器 `healthy` 但返回空/失败 | 确认镜像是本地 `build` 出来的；确认账号 token 有效（管理台可测） |
| 改了代码没生效 | 必须 `docker compose build` 再 `up` |
| `WORKERS` | 必须保持 `1`，多 worker 会导致数据文件写冲突 |

---

## 数据文件

- `data/accounts.json`：上游账号（**敏感，不要外发**）
- `data/browser_cookies.json`：浏览器会话（敏感，自动维护）
- `data/api_keys.json`：下游调用 Key
- `bx_pool.json`：上游签名池（自动维护，可删除后自动重建）
- `logs/`：运行日志（排查问题先看这里）

---

## 目录结构

```text
qwen2api/
├── backend/            # FastAPI 后端
│   ├── api/            # 协议入口（OpenAI/Claude/Gemini/图片/视频/管理）
│   ├── core/           # 配置、账号池、浏览器引擎
│   ├── runtime/        # 流式执行与重试
│   ├── services/       # 上游客户端、风控自愈、工具链
│   └── upstream/       # 会话管理与流式分发
├── frontend/dist/      # 管理台预构建产物
├── data/               # 运行数据（需持久化挂载）
├── Dockerfile / docker-compose.yml / start.py / .env.example
└── README.md
```
