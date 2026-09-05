from fastapi import APIRouter, Request, Depends, HTTPException
from backend.api.admin import verify_admin
from backend.core.database import AsyncJsonDB

router = APIRouter()


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request):
    required_state = (
        "accounts_db",
        "users_db",
        "captures_db",
        "account_pool",
        "qwen_client",
        "file_store",
        "session_affinity",
        "upstream_file_cache",
    )
    missing = [name for name in required_state if not hasattr(request.app.state, name)]
    if missing:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "missing": missing})
    return {"status": "ready"}


@router.get("/admin/dev/captures", dependencies=[Depends(verify_admin)])
async def get_captures(request: Request):
    db: AsyncJsonDB = request.app.state.captures_db
    return {"captures": await db.get()}


@router.delete("/admin/dev/captures", dependencies=[Depends(verify_admin)])
async def clear_captures(request: Request):
    db: AsyncJsonDB = request.app.state.captures_db
    await db.save([])
    return {"status": "cleared"}