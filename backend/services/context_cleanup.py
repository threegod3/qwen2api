from __future__ import annotations

import asyncio
import logging

from backend.core.request_logging import request_context

log = logging.getLogger("qwen2api.context_cleanup")


async def context_cleanup_loop(app, interval_seconds: int = 300):
    while True:
        try:
            with request_context(surface="context-cleanup"):
                ttl = app.state.context_offloader.settings.CONTEXT_ATTACHMENT_TTL_SECONDS
                await app.state.file_store.cleanup_expired(ttl)
                expired_records = await app.state.session_affinity.cleanup_expired()
                await app.state.upstream_file_cache.cleanup_expired()
                for record in expired_records:
                    acc = app.state.account_pool.get_by_email(record.account_email)
                    if not acc:
                        continue
                    if record.chat_id:
                        try:
                            await app.state.qwen_client.delete_chat(acc.token, record.chat_id)
                        except Exception as exc:
                            log.debug("[ContextCleanup] chat delete failed session=%s chat_id=%s error=%s", record.session_key, record.chat_id, exc)
                    for remote_meta in record.uploaded_files:
                        try:
                            await app.state.upstream_file_uploader.delete_remote_file(acc, remote_meta)
                        except Exception as exc:
                            log.debug("[ContextCleanup] remote delete failed session=%s error=%s", record.session_key, exc)
                log.info("[ContextCleanup] ttl=%s expired_sessions=%s completed", ttl, len(expired_records))
        except Exception as exc:
            log.warning("[ContextCleanup] failed: %s", exc)
        await asyncio.sleep(max(60, interval_seconds))
