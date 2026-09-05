from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any


class LocalFileStore:
    def __init__(self, root_dir: str, metadata_db=None):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_db = metadata_db
        self._metadata: dict[str, dict[str, Any]] = {}

    async def load(self):
        if self.metadata_db is None:
            return
        data = await self.metadata_db.load()
        self._metadata = {}
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("id"):
                    self._metadata[item["id"]] = item

    async def save(self):
        if self.metadata_db is None:
            return
        await self.metadata_db.save(list(self._metadata.values()))

    async def save_bytes(self, filename: str, content_type: str, raw: bytes, purpose: str, owner_token: str | None = None) -> dict:
        file_id = uuid.uuid4().hex
        suffix = Path(filename).suffix or mimetypes.guess_extension(content_type or "") or ""
        safe_name = (Path(filename).stem or "file").replace(" ", "_")
        target = self.root_dir / purpose / f"{file_id}_{safe_name}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, raw)
        meta = {
            "id": file_id,
            "path": str(target),
            "filename": f"{safe_name}{suffix}",
            "content_type": content_type or "application/octet-stream",
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "created_at": time.time(),
            "owner_token": owner_token or "",
            "purpose": purpose,
        }
        self._metadata[file_id] = meta
        await self.save()
        return meta

    async def save_text(self, filename: str, text: str, content_type: str = "text/plain", purpose: str = "context", owner_token: str | None = None) -> dict:
        raw = text.encode("utf-8")
        return await self.save_bytes(filename, content_type, raw, purpose, owner_token=owner_token)

    async def get(self, file_id: str) -> dict[str, Any] | None:
        if not self._metadata and self.metadata_db is not None:
            await self.load()
        return self._metadata.get(file_id)

    async def delete(self, file_id: str) -> None:
        meta = await self.get(file_id)
        if meta and meta.get("path"):
            await self.delete_path(meta["path"])
        self._metadata.pop(file_id, None)
        await self.save()

    async def delete_path(self, path: str) -> None:
        target = Path(path)
        try:
            await asyncio.to_thread(target.unlink)
        except FileNotFoundError:
            pass
        remove_id = None
        for file_id, meta in self._metadata.items():
            if meta.get("path") == str(target):
                remove_id = file_id
                break
        if remove_id:
            self._metadata.pop(remove_id, None)
            await self.save()

    async def cleanup_expired(self, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        cutoff = time.time() - ttl_seconds
        expired_ids: list[str] = []
        for file_id, meta in list(self._metadata.items()):
            if meta.get("created_at", 0) < cutoff:
                expired_ids.append(file_id)
        for file_id in expired_ids:
            meta = self._metadata.get(file_id, {})
            path = meta.get("path")
            if path:
                try:
                    await asyncio.to_thread(Path(path).unlink)
                except FileNotFoundError:
                    pass
            self._metadata.pop(file_id, None)
        if expired_ids:
            await self.save()
