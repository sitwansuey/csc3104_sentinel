"""Structured, allowlisted log lines for Sentinel processes.

Library code calls emit(); only process entrypoints call configure(). Until
then the "sentinel" logger has a NullHandler and does not propagate, so imports
and the test suite stay silent. Each record is one line on stderr: JSON by
default, or key=value text. Only the identifier and status fields in FIELDS are
written. Anything else is dropped, including tokens, Authorization headers,
request bodies and payloads. Correlation IDs bound with bind() live in a
contextvar; start threads with context_thread() so they inherit them.
shared/tracing.py binds trace_id and span_id here while a span is open.
A handler that catches a broad exception and carries on reports it with
failure(), which records the class name only, never the message.
"""
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
import contextvars
from datetime import datetime, timezone
import functools
import json
import logging
import math
import os
import re
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, ParamSpec, TextIO, TypeVar

Scalar = str | int | float | bool | None
_P = ParamSpec("_P")
_R = TypeVar("_R")

ROOT = "sentinel"
DEBUG, INFO, WARNING, ERROR = logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR
LEVELS = {"DEBUG": DEBUG, "INFO": INFO, "WARNING": WARNING, "ERROR": ERROR}
FORMATS = {"json", "text"}

# Identifiers, codes and measurements only. Free text, bodies and payloads are
# never fields, so a value can never be a request body or a credential.
FIELDS = frozenset({
    "incident_id", "action_id", "event_id", "delivery_id", "topic", "attempt",
    "identity", "role", "operation", "outcome", "reason", "status", "http_status",
    "method", "path", "error_type", "error_ref", "duration_ms", "fence_epoch", "basis",
    "trace_id", "span_id", "parent_span_id", "span_name", "from_state", "to_state", "cause", "seq", "outbox_high_water",
    "replay", "message_kind", "port", "schema_version",
    "store_generation", "commit_counter", "previous_generation", "previous_commit",
})
SECRET_KEY = re.compile(r"(?i)token|authori[sz]ation|secret|password|passwd|credential|cookie|payload|body")
EVENT = re.compile(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)*")
MAX_VALUE = 256

_context: contextvars.ContextVar[dict[str, Scalar] | None] = contextvars.ContextVar(
    "sentinel_log_context", default=None)
_installed: logging.Handler | None = None

_root = logging.getLogger(ROOT)
_root.addHandler(logging.NullHandler())
_root.propagate = False


def _value(value: object) -> Scalar:
    if value is None or isinstance(value, bool) or type(value) is int:
        return value
    if isinstance(value, float):
        return round(value, 3) if math.isfinite(value) else None
    if isinstance(value, str):
        return value if len(value) <= MAX_VALUE else value[:MAX_VALUE] + "..."
    return None  # Structured values could carry a body; never log them.


def clean(fields: Mapping[str, object]) -> dict[str, Scalar]:
    """Keep allowlisted scalar fields only; drop None and everything else."""
    result: dict[str, Scalar] = {}
    for key, raw in fields.items():
        if key not in FIELDS or SECRET_KEY.search(key):
            continue
        value = _value(raw)
        if value is not None:
            result[key] = value
    return result


def context() -> dict[str, Scalar]:
    return dict(_context.get() or {})


@contextmanager
def bind(**fields: object) -> Iterator[None]:
    """Attach correlation IDs to every record emitted in this context."""
    token = _context.set({**(_context.get() or {}), **clean(fields)})
    try:
        yield
    finally:
        _context.reset(token)


def context_thread(target: Callable[..., object], *, args: tuple[Any, ...] = (),
                   kwargs: Mapping[str, Any] | None = None, **options: Any) -> threading.Thread:
    """A Thread that runs target inside a copy of the caller's log context."""
    copied = contextvars.copy_context()
    return threading.Thread(target=copied.run, args=(target, *args), kwargs=dict(kwargs or {}), **options)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(ROOT + "." + name)


def emit(logger: logging.Logger | str, event: str, *, level: int = INFO, **fields: object) -> None:
    """Log one structured event. Fields outside FIELDS are dropped."""
    if not EVENT.fullmatch(event):
        raise ValueError("Log event names are lower-case dotted identifiers")
    if isinstance(logger, str):
        logger = get_logger(logger)
    if not logger.isEnabledFor(level):
        return
    merged = clean({**(_context.get() or {}), **fields})
    logger.log(level, event, extra={"sentinel_event": event, "sentinel_fields": merged,
                                    "sentinel_mono": time.monotonic()})


def failure(logger: logging.Logger | str, event: str, error: BaseException, *, level: int = WARNING,
            **fields: object) -> None:
    """Log a caught exception that is handled, by its class name only.

    The message and traceback are never written: they can hold a token, a
    request body or raw output from a cluster or a model.
    """
    emit(logger, event, level=level, **{**fields, "error_type": type(error).__name__})


def _record(record: logging.LogRecord, component: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds")
              .replace("+00:00", "Z"),
        "mono": round(getattr(record, "sentinel_mono", time.monotonic()), 3),
        "level": record.levelname.lower(), "component": component, "logger": record.name,
        # A record not made by emit() keeps no message text: it may hold a secret.
        "event": getattr(record, "sentinel_event", "log.unstructured"),
    }
    item.update(clean(getattr(record, "sentinel_fields", {})))
    if record.exc_info and record.exc_info[0] is not None:
        item.setdefault("error_type", record.exc_info[0].__name__)
    return item


class JsonFormatter(logging.Formatter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        # ensure_ascii escapes newlines and control characters: one line per record.
        return json.dumps(_record(record, self.component), ensure_ascii=True, allow_nan=False,
                          separators=(",", ":"))


class TextFormatter(JsonFormatter):
    PLAIN = re.compile(r"[A-Za-z0-9_.:/@+-]*")

    def format(self, record: logging.LogRecord) -> str:
        item = _record(record, self.component)
        head = " ".join(str(item.pop(key)) for key in ("ts", "level", "component", "event"))
        item.pop("mono")
        item.pop("logger")
        parts = [f"{key}={value}" if isinstance(value, str) and self.PLAIN.fullmatch(value)
                 else f"{key}={json.dumps(value, ensure_ascii=True)}" for key, value in item.items()]
        return " ".join([head, *parts])


if TYPE_CHECKING:
    _StreamHandler = logging.StreamHandler[TextIO]
else:
    _StreamHandler = logging.StreamHandler


class _StderrHandler(_StreamHandler):
    """Resolve sys.stderr on every write, so a redirected stderr is honoured."""
    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, _value: TextIO) -> None:
        pass


def configure(component: str, *, level: str | None = None, fmt: str | None = None,
              stream: TextIO | None = None) -> logging.Handler:
    """Install one handler for this process; replaces an earlier configure()."""
    global _installed
    if not isinstance(component, str) or not EVENT.fullmatch(component):
        raise ValueError("Component must be a lower-case dotted identifier")
    level_name = (level or os.environ.get("SENTINEL_LOG_LEVEL") or "INFO").upper()
    fmt = (fmt or os.environ.get("SENTINEL_LOG_FORMAT") or "json").lower()
    if level_name not in LEVELS:
        raise ValueError("SENTINEL_LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
    if fmt not in FORMATS:
        raise ValueError("SENTINEL_LOG_FORMAT must be json or text")
    handler: logging.Handler = _StderrHandler() if stream is None else logging.StreamHandler(stream)
    handler.setFormatter((JsonFormatter if fmt == "json" else TextFormatter)(component))
    if _installed is not None:
        _root.removeHandler(_installed)
    _root.addHandler(handler)
    _root.setLevel(LEVELS[level_name])
    _installed = handler
    return handler


def reset() -> None:
    """Remove the configured handler; the logger is silent again."""
    global _installed
    if _installed is not None:
        _root.removeHandler(_installed)
        _installed = None
    _root.setLevel(logging.NOTSET)


@contextmanager
def configured(component: str, **options: Any) -> Iterator[logging.Handler]:
    """configure() for the lifetime of an entrypoint call, then restore."""
    global _installed
    previous, previous_level = _installed, _root.level
    handler = configure(component, **options)
    try:
        yield handler
    finally:
        _root.removeHandler(handler)
        if previous is not None:
            _root.addHandler(previous)
        _installed = previous
        _root.setLevel(previous_level)


def entrypoint(component: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R | int]]:
    """Decorate a main(): configure logging from the environment around the call.

    An invalid SENTINEL_LOG_FORMAT or SENTINEL_LOG_LEVEL stops with exit code 2.
    """
    def decorate(main: Callable[_P, _R]) -> Callable[_P, _R | int]:
        @functools.wraps(main)
        def run(*args: _P.args, **kwargs: _P.kwargs) -> _R | int:
            with ExitStack() as stack:
                try:
                    stack.enter_context(configured(component))
                except ValueError as error:
                    print(f"error: {error}", file=sys.stderr)
                    return 2
                return main(*args, **kwargs)
        return run
    return decorate
