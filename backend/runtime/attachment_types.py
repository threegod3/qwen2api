from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class NormalizedAttachment:
    file_id: str
    filename: str = ""
    content_type: str = "application/octet-stream"
    source: str = "upload"
    local_path: str = ""
    sha256: str = ""
    purpose: str = "context"
    remote_file_id: str = ""
    remote_object_key: str = ""
    remote_parse_status: str = ""
    remote_ref: dict[str, Any] = field(default_factory=dict)
