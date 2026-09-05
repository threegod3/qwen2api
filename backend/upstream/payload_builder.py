import os
import time
import uuid


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# 思考过程显示开关。
# 上游生成的思考内容会经 openai_stream_translator 以 DeepSeek R1 风格的
# `reasoning_content` 发出，并经 response_formatters 输出 Anthropic 的 thinking block。
# 之前这里硬编码 False（只有 qwen3.8* 被强制开启），所以客户端从来看不到思考过程。
THINKING_ENABLED = _env_bool("QWEN_THINKING_ENABLED", False)
# summary = 思考摘要（实测上游只发空的 thinking_summary 事件，等于看不到内容）；
# raw = 完整思考链（实测上游发 phase=think 且带真实内容）——所以默认用 raw。
THINKING_FORMAT = os.getenv("QWEN_THINKING_FORMAT", "raw")
# 带自定义工具时是否也开思考（默认不开：工具链路更看重延迟与指令遵循）
THINKING_WITH_TOOLS = _env_bool("QWEN_THINKING_WITH_TOOLS", False)


CUSTOM_TOOL_COMPAT_FEATURE_CONFIG = {
    "thinking_enabled": THINKING_ENABLED,
    "output_schema": "phase",
    "research_mode": "normal",
    "auto_thinking": False,
    "thinking_mode": "Auto",
    "thinking_format": THINKING_FORMAT,
    "auto_search": False,
    "code_interpreter": False,
    "plugins_enabled": False,
}

CUSTOM_TOOL_LOW_LATENCY_OVERRIDES = {
    "thinking_enabled": THINKING_ENABLED and THINKING_WITH_TOOLS,
    "auto_thinking": False,
}


def _extract_img_url_from_files(files: list[dict] | None) -> str | None:
    """Best-effort extract a public/OSS image URL from uploaded file refs for i2v."""
    if not files:
        return None
    for f in files:
        if not isinstance(f, dict):
            continue
        for key in ("url", "img_url", "image_url", "src"):
            val = f.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
        file_obj = f.get("file")
        if isinstance(file_obj, dict):
            for key in ("url", "img_url", "image_url"):
                val = file_obj.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    return val
            meta = file_obj.get("meta") if isinstance(file_obj.get("meta"), dict) else {}
            for key in ("url", "img_url", "image_url"):
                val = meta.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    return val
    return None


def build_chat_payload(
    chat_id: str,
    model: str,
    content: str,
    has_custom_tools: bool = False,
    files: list[dict] | None = None,
    chat_type: str = "t2t",
    size: str | None = None,
    img_url: str | None = None,
) -> dict:
    ts = int(time.time())
    # Qwen3.8 preview currently rejects requests with thinking_enabled=false
    # ("invalid_input"). Force thinking for 3.8* models only.
    is_qwen38 = isinstance(model, str) and (
        "qwen3.8" in model.lower() or model.lower().startswith("qwen-3.8")
    )
    if chat_type == "t2t":
        feature_config = {
            **CUSTOM_TOOL_COMPAT_FEATURE_CONFIG,
            **(CUSTOM_TOOL_LOW_LATENCY_OVERRIDES if has_custom_tools else {}),
            # Our Anthropic/OpenAI bridge relies on textual JSON/XML tool directives
            # that are parsed locally. Enabling Qwen native function_calling here causes
            # upstream interception such as `Tool Read/Bash does not exists.` for custom
            # local tools that only exist in the bridge layer.
            "function_calling": False,
            # Additional safeguards to prevent tool call interception
            "enable_tools": False,
            "enable_function_call": False,
            "tool_choice": "none",
        }
        if is_qwen38:
            feature_config["thinking_enabled"] = True
            feature_config["auto_thinking"] = False
            feature_config["thinking_mode"] = "Auto"
    else:
        # Qwen feature modes such as text-to-video use upstream-native async tasks.
        # Do not disable native tools here, or the task handoff can be suppressed.
        feature_config = {
            "thinking_enabled": False,
            "output_schema": "phase",
            "research_mode": "normal",
        }

    # Frontend behavior:
# - UI feature is VideoGeneration (t2v)
# - if files contain type=="image", it auto-switches chat_type/sub_chat_type to i2v
# - file object shape: type=image, file_class=vision, showType=image, url=...
    resolved_img_url = img_url or _extract_img_url_from_files(files)
    effective_chat_type = chat_type
    has_image_files = bool(
        files and any(isinstance(f, dict) and f.get("type") == "image" and f.get("url") for f in files)
    )
    # 仅视频场景（t2v/i2v）在有参考图时自动切 i2v。
    # 图生图（t2i + 参考图）必须保持 t2i，否则会被当成视频任务并空响应。
    if has_image_files and chat_type in {"t2v", "i2v"}:
        effective_chat_type = "i2v"

    meta = {"subChatType": effective_chat_type}
    # Qwen t2v/t2i size 偏好 1280*720 这种星号格式
    norm_size = size
    if isinstance(size, str) and "x" in size.lower() and "*" not in size:
        norm_size = size.lower().replace("x", "*")
    if norm_size:
        meta["size"] = norm_size
    # Some task workers read input.img_url from extra.meta
    if effective_chat_type == "i2v" and resolved_img_url:
        meta["img_url"] = resolved_img_url
        meta["input"] = {"img_url": resolved_img_url}
    if effective_chat_type == "t2i" and resolved_img_url:
        meta["img_url"] = resolved_img_url
        meta["input"] = {"img_url": resolved_img_url}

    message = {
        "fid": str(uuid.uuid4()),
        "parentId": None,
        "childrenIds": [str(uuid.uuid4())],
        "role": "user",
        "content": content,
        "user_action": "chat",
        "files": files or [],
        "timestamp": ts,
        "models": [model],
        "chat_type": effective_chat_type,
        "feature_config": feature_config,
        "extra": {"meta": meta},
        "sub_chat_type": effective_chat_type,
        "parent_id": None,
    }

    payload = {
        "stream": True,
        "version": "2.1",
        "incremental_output": True,
        "chat_id": chat_id,
        "chat_mode": "normal",
        "model": model,
        "parent_id": None,
        "messages": [message],
        "timestamp": ts,
    }
    if norm_size:
        payload["size"] = norm_size
    return payload
