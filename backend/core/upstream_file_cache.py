from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from backend.core.database import AsyncJsonDB


@dataclass(slots=True)
class UpstreamFileCacheEntry:
    session_key: str
    account_email: str
    sha256: str
    ext: str
    filename: str
    remote_file_meta: dict[str, Any]
    created_at: float
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "account_email": self.account_email,
            "sha256": self.sha256,
            "ext": self.ext,
            "filename": self.filename,
            "remote_file_meta": self.remote_file_meta,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class UpstreamFileCache:
    def __init__(self, db: AsyncJsonDB):
        self.db = db
        self.entries: list[UpstreamFileCacheEntry] = []

    async def load(self):
        data = await self.db.load()
        self.entries = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self.entries.append(UpstreamFileCacheEntry(**item))

    async def save(self):
        await self.db.save([entry.to_dict() for entry in self.entries])

    async def get(self, session_key: str, account_email: str, sha256: str, ext: str) -> UpstreamFileCacheEntry | None:
        if not self.entries:
            await self.load()
        now = time.time()
        for entry in self.entries:
            if entry.expires_at and entry.expires_at < now:
                continue
            if entry.session_key == session_key and entry.account_email == account_email and entry.sha256 == sha256 and entry.ext == ext:
                return entry
        return None

    async def set(self, entry: UpstreamFileCacheEntry):
        self.entries = [e for e in self.entries if not (e.session_key == entry.session_key and e.account_email == entry.account_email and e.sha256 == entry.sha256 and e.ext == entry.ext)]
        self.entries.append(entry)
        await self.save()

    async def cleanup_expired(self):
        now = time.time()
        before = len(self.entries)
        self.entries = [entry for entry in self.entries if not entry.expires_at or entry.expires_at >= now]
        if len(self.entries) != before:
            await self.save()
