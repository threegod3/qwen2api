from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.core.config import MODEL_MAP, resolve_model
from backend.services.auth_quota import resolve_auth_context
from backend.services.qwen_client import QwenClient

router = APIRouter()


def _build_model_list_payload() -> dict:
    seen: set[str] = set()
    data: list[dict] = []
    for model_id in MODEL_MAP:
        if model_id in seen:
            continue
        seen.add(model_id)
        data.append({"id": model_id, "object": "model", "owned_by": "qwen2api"})
    return {"object": "list", "data": data}


@router.get("/v1/models")
async def list_models(request: Request):
    app = request.app
    users_db = app.state.users_db
    client: QwenClient = app.state.qwen_client

    # 鉴权（只校验客户端 API KEY，不把它当 Qwen token 用）
    await resolve_auth_context(request, users_db)

    # 从账号池拿合法 Qwen token 调上游 /api/models，带 5min 缓存
    upstream_models = await client.list_models_from_pool()

    if upstream_models:
        data = []
        for item in upstream_models:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id") or item.get("model") or item.get("name")
            if not model_id:
                continue
            data.append({
                "id": model_id,
                "object": "model",
                "owned_by": item.get("owned_by", "qwen"),
                "created": item.get("created_at") or 0,
            })
        return JSONResponse({"object": "list", "data": data})

    # 上游不可用时才回退到静态 MODEL_MAP（包含 gpt-4o/claude 等别名）
    return JSONResponse(_build_model_list_payload())


@router.get("/v1/models/{model_id}")
async def get_model(model_id: str):
    resolved = resolve_model(model_id)
    if resolved == model_id and model_id not in MODEL_MAP:
        raise HTTPException(status_code=404, detail={"error": {"message": f"Model '{model_id}' not found", "type": "invalid_request_error"}})
    return JSONResponse({"id": model_id, "object": "model", "owned_by": "qwen2api", "resolved_model": resolved})
