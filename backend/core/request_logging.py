import contextvars
import logging
import uuid
from contextlib import contextmanager
from typing import Any

_REQUEST_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "request_context", default={}
)

_REQUEST_DEFAULTS: dict[str, Any] = {
    "req_id": "-",
    "surface": "-",
    "requested_model": "-",
    "resolved_model": "-",
    "chat_id": "-",
    "stream_attempt": "-",
    "upstream_attempt": "-",
}

_LOG_FORMAT = (
    "%(asctime)s [%(levelname)s] %(message)s"
)


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = get_request_context()
        for key, default in _REQUEST_DEFAULTS.items():
            setattr(record, key, ctx.get(key, default))
        return True


request_context_filter = RequestContextFilter()


class SafeRequestFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        for key, default in _REQUEST_DEFAULTS.items():
            if not hasattr(record, key):
                setattr(record, key, default)
        return super().format(record)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)

    # 应用精简日志过滤器
    try:
        from backend.core.log_filter import SimplifiedLogFilter
        if not any(isinstance(f, SimplifiedLogFilter) for f in root.filters):
            root.addFilter(SimplifiedLogFilter())
    except ImportError:
        pass

    formatter = SafeRequestFormatter(_LOG_FORMAT)

    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(level)
        handler.setFormatter(formatter)
        root.addHandler(handler)
        return

    for handler in root.handlers:
        handler.setLevel(level)
        handler.setFormatter(formatter)


def new_request_id() -> str:
    return uuid.uuid4().hex[:8]


def get_request_context() -> dict[str, Any]:
    ctx = dict(_REQUEST_DEFAULTS)
    ctx.update(_REQUEST_CONTEXT.get({}))
    return ctx


def update_request_context(**kwargs: Any) -> dict[str, Any]:
    ctx = get_request_context()
    for key, value in kwargs.items():
        if value is not None:
            ctx[key] = value
    _REQUEST_CONTEXT.set(ctx)
    return ctx


@contextmanager
def request_context(**kwargs: Any):
    merged = get_request_context()
    merged.update({k: v for k, v in kwargs.items() if v is not None})
    token = _REQUEST_CONTEXT.set(merged)
    try:
        yield _REQUEST_CONTEXT.get({})
    finally:
        _REQUEST_CONTEXT.reset(token)
