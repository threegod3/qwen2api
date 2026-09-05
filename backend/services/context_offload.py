from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from backend.adapter.standard_request import CLAUDE_CODE_OPENAI_PROFILE

SYSTEM_CONTEXT_FILE_PREFIX = "qwen2api_context"
SYSTEM_CONTEXT_PROMPT_NOTE = (
    "System context files named qwen2api_context*.txt/.md/.json/.log may be attached. "
    "Use them as supporting context. User-uploaded files are separate user inputs and should also be respected."
)


@dataclass(slots=True)
class LocalContextFile:
    filename: str
    ext: str
    content_type: str
    text: str
    sha256: str
    purpose: str = "context"
    local_path: str = ""


@dataclass(slots=True)
class ContextOffloadPlan:
    mode: str
    inline_messages: list[dict[str, Any]]
    generated_files: list[LocalContextFile] = field(default_factory=list)
    summary_text: str = ""
    estimated_prompt_len: int = 0
    note: str = ""


class ContextOffloader:
    def __init__(self, settings):
        self.settings = settings

    def estimate_prompt_len(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, client_profile: str = "") -> int:
        total = 0
        for msg in messages or []:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        total += len(str(part.get("text", "")))
                        total += len(str(part.get("content", "")))
            total += 24
        total += sum(len(str(tool.get("name", ""))) + len(str(tool.get("description", ""))) for tool in (tools or []))
        if client_profile == CLAUDE_CODE_OPENAI_PROFILE:
            total += 512
        return total

    def _extract_text(self, msg: dict[str, Any]) -> str:
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        chunks.append(str(part.get("text", "")))
                    elif part.get("type") == "tool_result":
                        chunks.append(str(part.get("content", "")))
            return "\n".join(chunk for chunk in chunks if chunk)
        return str(content)

    def _make_file(self, base_name: str, ext: str, text: str, content_type: str) -> LocalContextFile:
        data = text.encode("utf-8")
        return LocalContextFile(
            filename=f"{base_name}.{ext}",
            ext=ext,
            content_type=content_type,
            text=text,
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def plan(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, client_profile: str = "") -> ContextOffloadPlan:
        estimated = self.estimate_prompt_len(messages, tools=tools, client_profile=client_profile)
        if estimated <= self.settings.CONTEXT_INLINE_MAX_CHARS:
            return ContextOffloadPlan(mode="inline", inline_messages=messages, estimated_prompt_len=estimated)

        user_messages = [m for m in messages if m.get("role") == "user"]
        latest_user = user_messages[-1] if user_messages else {"role": "user", "content": ""}
        latest_user_text = self._extract_text(latest_user)

        older_messages = messages[:-1] if messages else []
        serialized_parts: list[str] = []
        for idx, msg in enumerate(older_messages, 1):
            role = msg.get("role", "unknown")
            text = self._extract_text(msg)
            if not text.strip():
                continue
            serialized_parts.append(f"## Message {idx} [{role}]\n{text.strip()}\n")
        attachment_text = "\n".join(serialized_parts).strip()
        summary_text = attachment_text[:1200] if attachment_text else ""

        if estimated <= self.settings.CONTEXT_FORCE_FILE_MAX_CHARS:
            mode = "hybrid"
        else:
            mode = "file"

        generated_files: list[LocalContextFile] = []
        if attachment_text:
            generated_files.append(
                self._make_file(
                    f"{SYSTEM_CONTEXT_FILE_PREFIX}_history",
                    "txt",
                    attachment_text,
                    "text/plain",
                )
            )

        rewritten_messages = [latest_user]
        if latest_user_text.strip():
            latest_user_rewrite = f"{latest_user_text.strip()}\n\n{SYSTEM_CONTEXT_PROMPT_NOTE}"
        else:
            latest_user_rewrite = SYSTEM_CONTEXT_PROMPT_NOTE
        rewritten_messages = [{"role": "user", "content": latest_user_rewrite}]

        return ContextOffloadPlan(
            mode=mode,
            inline_messages=rewritten_messages,
            generated_files=generated_files,
            summary_text=summary_text,
            estimated_prompt_len=estimated,
            note=SYSTEM_CONTEXT_PROMPT_NOTE,
        )
