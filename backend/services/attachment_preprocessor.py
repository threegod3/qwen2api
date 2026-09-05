from __future__ import annotations

import base64
import copy
from dataclasses import dataclass, field
from typing import Any

from backend.runtime.attachment_types import NormalizedAttachment


@dataclass(slots=True)
class PreprocessedAttachments:
    payload: dict[str, Any]
    attachments: list[NormalizedAttachment] = field(default_factory=list)
    uploaded_file_ids: list[str] = field(default_factory=list)


def _decode_data_uri(url: str) -> tuple[str, bytes]:
    header, encoded = url.split(",", 1)
    content_type = header.split(";", 1)[0][5:] or "application/octet-stream"
    return content_type, base64.b64decode(encoded)


def _extract_inline_file_payload(block: dict[str, Any]) -> tuple[str, str, bytes] | None:
    filename = str(block.get("filename") or block.get("name") or "attachment.txt")
    content_type = str(block.get("mime_type") or block.get("content_type") or "text/plain")

    if isinstance(block.get("text"), str):
        return filename, content_type, block["text"].encode("utf-8")
    if isinstance(block.get("content"), str) and not str(block.get("content", "")).startswith("data:"):
        return filename, content_type, block["content"].encode("utf-8")
    if isinstance(block.get("data_base64"), str):
        return filename, content_type, base64.b64decode(block["data_base64"])
    if isinstance(block.get("data"), str):
        return filename, content_type, base64.b64decode(block["data"])
    if isinstance(block.get("content"), str) and str(block.get("content", "")).startswith("data:"):
        ct, raw = _decode_data_uri(block["content"])
        return filename, ct, raw
    return None


async def preprocess_attachments(payload: dict[str, Any], file_store, owner_token: str | None = None) -> PreprocessedAttachments:
    messages = payload.get("messages", [])
    needs_rewrite = False
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image_url":
                image_url = block.get("image_url") or {}
                if isinstance(image_url, dict) and str(image_url.get("url") or "").strip().startswith("data:"):
                    needs_rewrite = True
                    break
            if block.get("type") in ("input_file", "file") and _extract_inline_file_payload(block):
                needs_rewrite = True
                break
        if needs_rewrite:
            break

    if not needs_rewrite:
        return PreprocessedAttachments(payload=payload)

    rewritten = copy.deepcopy(payload)
    attachments: list[NormalizedAttachment] = []
    uploaded_file_ids: list[str] = []

    for message in rewritten.get("messages", []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image_url":
                image_url = block.get("image_url") or {}
                if isinstance(image_url, dict) and str(image_url.get("url") or "").strip().startswith("data:"):
                    content_type, raw = _decode_data_uri(str(image_url.get("url") or ""))
                    result = await file_store.save_bytes("inline-image", content_type, raw, "vision", owner_token=owner_token)
                    uploaded_file_ids.append(result["id"])
                    attachments.append(NormalizedAttachment(
                        file_id=result["id"],
                        filename=result["filename"],
                        content_type=content_type,
                        source="inline-image",
                        local_path=result["path"],
                        sha256=result["sha256"],
                        purpose="user-upload",
                    ))
                    content[index] = {"type": "input_image", "file_id": result["id"], "mime_type": content_type}
                    continue

            if block.get("type") in ("input_file", "file"):
                existing_file_id = str(block.get("file_id") or "").strip()
                if existing_file_id:
                    existing = await file_store.get(existing_file_id)
                    if existing and (not existing.get("owner_token") or existing.get("owner_token") == (owner_token or "")):
                        attachments.append(NormalizedAttachment(
                            file_id=existing["id"],
                            filename=existing["filename"],
                            content_type=existing.get("content_type", "application/octet-stream"),
                            source="manual-upload-ref",
                            local_path=existing.get("path", ""),
                            sha256=existing.get("sha256", ""),
                            purpose="user-upload",
                        ))
                        content[index] = {
                            "type": "input_file",
                            "file_id": existing["id"],
                            "filename": existing["filename"],
                            "mime_type": existing.get("content_type", "application/octet-stream"),
                        }
                        continue
                extracted = _extract_inline_file_payload(block)
                if not extracted:
                    continue
                filename, content_type, raw = extracted
                result = await file_store.save_bytes(filename, content_type, raw, "upload", owner_token=owner_token)
                uploaded_file_ids.append(result["id"])
                attachments.append(NormalizedAttachment(
                    file_id=result["id"],
                    filename=result["filename"],
                    content_type=content_type,
                    source="manual-upload",
                    local_path=result["path"],
                    sha256=result["sha256"],
                    purpose="user-upload",
                ))
                content[index] = {
                    "type": "input_file",
                    "file_id": result["id"],
                    "filename": result["filename"],
                    "mime_type": content_type,
                }

    return PreprocessedAttachments(payload=rewritten, attachments=attachments, uploaded_file_ids=uploaded_file_ids)
