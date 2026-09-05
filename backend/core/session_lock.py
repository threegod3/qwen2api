from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class SessionLockRegistry:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}
        self._guard = asyncio.Lock()

    async def _get_lock(self, session_key: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(session_key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_key] = lock
                self._refs[session_key] = 0
            self._refs[session_key] = self._refs.get(session_key, 0) + 1
            return lock

    async def _release_ref(self, session_key: str) -> None:
        async with self._guard:
            if session_key not in self._refs:
                return
            self._refs[session_key] = max(0, self._refs[session_key] - 1)
            if self._refs[session_key] == 0:
                self._refs.pop(session_key, None)
                self._locks.pop(session_key, None)

    @asynccontextmanager
    async def hold(self, session_key: str):
        lock = await self._get_lock(session_key)
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
            await self._release_ref(session_key)
