from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from backend.core.database import AsyncJsonDB


@dataclass(slots=True)
class SessionAffinityRecord:
    session_key: str
    surface: str
    account_email: str
    uploaded_files: list[dict[str, Any]] = field(default_factory=list)
    chat_id: str | None = None
    message_hashes: list[str] = field(default_factory=list)
    updated_at: float = 0.0
    expires_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "surface": self.surface,
            "account_email": self.account_email,
            "uploaded_files": self.uploaded_files,
            "chat_id": self.chat_id,
            "message_hashes": self.message_hashes,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }


class SessionAffinityStore:
    def __init__(self, db: AsyncJsonDB):
        self.db = db
        self.records: dict[str, SessionAffinityRecord] = {}

    @staticmethod
    def _from_item(item: dict[str, Any]) -> SessionAffinityRecord | None:
        if not isinstance(item, dict) or not item.get("session_key"):
            return None
        return SessionAffinityRecord(
            session_key=str(item.get("session_key") or ""),
            surface=str(item.get("surface") or ""),
            account_email=str(item.get("account_email") or ""),
            uploaded_files=list(item.get("uploaded_files") or []),
            chat_id=(str(item.get("chat_id")) if item.get("chat_id") else None),
            message_hashes=[str(v) for v in (item.get("message_hashes") or []) if str(v)],
            updated_at=float(item.get("updated_at") or 0.0),
            expires_at=float(item.get("expires_at") or 0.0),
        )

    async def load(self):
        data = await self.db.load()
        self.records = {}
        if isinstance(data, list):
            for item in data:
                rec = self._from_item(item)
                if rec is None:
                    continue
                self.records[rec.session_key] = rec

    async def save(self):
        await self.db.save([record.to_dict() for record in self.records.values()])

    async def get(self, session_key: str) -> SessionAffinityRecord | None:
        if not self.records:
            await self.load()
        record = self.records.get(session_key)
        if record and record.expires_at and record.expires_at < time.time():
            self.records.pop(session_key, None)
            await self.save()
            return None
        return record

    async def bind_account(self, session_key: str, surface: str, account_email: str, ttl_seconds: int) -> SessionAffinityRecord:
        now = time.time()
        record = self.records.get(session_key)
        if record is None:
            record = SessionAffinityRecord(session_key=session_key, surface=surface, account_email=account_email)
        record.surface = surface
        record.account_email = account_email
        record.updated_at = now
        record.expires_at = now + max(60, ttl_seconds)
        self.records[session_key] = record
        await self.save()
        return record

    async def bind_chat(
        self,
        session_key: str,
        *,
        surface: str,
        account_email: str,
        chat_id: str,
        message_hashes: list[str],
        ttl_seconds: int,
    ) -> SessionAffinityRecord:
        now = time.time()
        record = self.records.get(session_key)
        if record is None:
            record = SessionAffinityRecord(session_key=session_key, surface=surface, account_email=account_email)
        record.surface = surface
        record.account_email = account_email
        record.chat_id = chat_id
        record.message_hashes = list(message_hashes)
        record.updated_at = now
        record.expires_at = now + max(60, ttl_seconds)
        self.records[session_key] = record
        await self.save()
        return record

    async def clear_chat(self, session_key: str) -> None:
        record = await self.get(session_key)
        if record is None:
            return
        record.chat_id = None
        record.message_hashes = []
        record.updated_at = time.time()
        self.records[session_key] = record
        await self.save()

    async def add_uploaded_file(self, session_key: str, file_meta: dict[str, Any]) -> None:
        record = await self.get(session_key)
        if record is None:
            return
        record.uploaded_files.append(file_meta)
        record.updated_at = time.time()
        self.records[session_key] = record
        await self.save()

    async def clear(self, session_key: str) -> None:
        self.records.pop(session_key, None)
        await self.save()

    def active_chat_ids(self) -> set[str]:
        now = time.time()
        return {
            record.chat_id
            for record in self.records.values()
            if record.chat_id and (not record.expires_at or record.expires_at >= now)
        }

    async def cleanup_expired(self) -> list[SessionAffinityRecord]:
        now = time.time()
        expired_keys = [key for key, record in self.records.items() if record.expires_at and record.expires_at < now]
        expired_records = [self.records[key] for key in expired_keys]
        for key in expired_keys:
            self.records.pop(key, None)
        if expired_keys:
            await self.save()
        return expired_records
