import os, json
from pathlib import Path
bx_env = Path(os.environ.get("LOCALAPPDATA", "")) / "Temp" / "bx_env.json"
loaded = 0
if bx_env.exists():
    for k, v in json.loads(bx_env.read_text(encoding="utf-8")).items():
        os.environ[k] = v; loaded += 1
    print(f"[startup] loaded {loaded} bx vars from bx_env.json")
else:
    fb = Path(__file__).parent / ".env.bxheaders"
    if fb.exists():
        for line in fb.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1); os.environ[k.strip()] = v.strip(); loaded += 1
        print(f"[startup] loaded {loaded} bx vars from .env.bxheaders")
os.environ.setdefault("ADMIN_KEY", "test123456")
# 加载项目 .env（本项目代码里没有 load_dotenv，需在启动时手动注入，
# 否则 QWEN_CTX_* / QWEN_THINKING_* 等配置不会生效）。
# 注意：已存在的环境变量优先，不覆盖（bx 变量等由上面先注入）。
_dotenv = Path(__file__).parent / ".env"
if _dotenv.exists():
    _n = 0
    for line in _dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v
            _n += 1
    print(f"[startup] loaded {_n} vars from .env")
os.environ["PYTHONPATH"] = str(Path(__file__).parent)
import uvicorn
uvicorn.run("backend.main:app", host="0.0.0.0", port=8760, workers=1)
