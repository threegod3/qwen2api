from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from backend.core.config import settings
from backend.services.auth_quota import resolve_auth_context

router = APIRouter()


def _allowed_exts() -> set[str]:
    return {item.strip().lower() for item in settings.CONTEXT_ALLOWED_USER_EXTS.split(",") if item.strip()}


def _validate_upload(filename: str) -> None:
    ext = Path(filename).suffix.lower().lstrip(".")
    if not ext or ext not in _allowed_exts():
        raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext or 'none'}")


@router.post("/api/files/upload")
@router.post("/v1/files")
async def upload_file(request: Request, file: UploadFile = File(...)):
    app = request.app
    users_db = app.state.users_db
    auth = await resolve_auth_context(request, users_db)
    token = auth.token

    _validate_upload(file.filename or "")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")

    file_store = app.state.file_store
    meta = await file_store.save_bytes(
        file.filename or "upload.bin",
        file.content_type or "application/octet-stream",
        raw,
        "upload",
        owner_token=token,
    )
    return JSONResponse({
        "id": meta["id"],
        "object": "file",
        "filename": meta["filename"],
        "bytes": meta["size"],
        "content_type": meta["content_type"],
        "created_at": meta["created_at"],
        "content_block": {
            "type": "input_file",
            "file_id": meta["id"],
            "filename": meta["filename"],
            "mime_type": meta["content_type"],
        },
    })


@router.delete("/api/files/{file_id}")
@router.delete("/v1/files/{file_id}")
async def delete_file(request: Request, file_id: str):
    app = request.app
    users_db = app.state.users_db
    auth = await resolve_auth_context(request, users_db)
    token = auth.token

    file_store = app.state.file_store
    meta = await file_store.get(file_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="File not found")
    if meta.get("owner_token") and meta.get("owner_token") != token:
        raise HTTPException(status_code=403, detail="Forbidden")
    await file_store.delete(file_id)
    return {"deleted": True, "id": file_id}
