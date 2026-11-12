"""W3C trace context for following one incident across Sentinel processes.

A trace is one incident. Each span is a timed step in one process: a worker
handling a delivery, a Coordinator request, recording a message. Spans are
written as span.start and span.end log lines through shared/log.py, so the core
needs no extra package. While a span is open, every log line carries its
trace_id and span_id.

A traceparent arrives from other processes (HTTP headers, outbox rows, saved
deliveries), so it is untrusted. parse_traceparent() is strict and returns None
for anything malformed; the caller then starts a new trace. Trace data is for
observability only: it never authenticates, authorizes or selects work.
"""
from collections.abc import Callable, Iterator
from contextlib import contextmanager
import contextvars
from dataclasses import dataclass
import re
import secrets
import time

from shared import log

# Version 00 only, lower-case hex, sampled flag 00 or 01. Exactly 55 characters.
TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-(0[01])")
MAX_TRACEPARENT = 55
NAME = re.compile(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)*")
LOG = log.get_logger("trace")


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    flags: str = "01"

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.flags}"


_current: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "sentinel_trace_context", default=None)


def parse_traceparent(value: object) -> TraceContext | None:
    """Return the context in a well-formed traceparent, or None. Never raises."""
    if not isinstance(value, str) or len(value) > MAX_TRACEPARENT:
        return None
    match = TRACEPARENT.fullmatch(value)
    if match is None:
        return None
    trace_id, span_id, flags = match.groups()
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return TraceContext(trace_id, span_id, flags)


def format_traceparent(context: TraceContext) -> str:
    return context.traceparent


def _random_hex(length: int) -> str:
    while True:
        value = secrets.token_hex(length // 2)
        if value != "0" * length:
            return value


def new_trace_id() -> str:
    return _random_hex(32)


def new_span_id() -> str:
    return _random_hex(16)


def current() -> TraceContext | None:
    return _current.get()


def current_traceparent() -> str | None:
    context = _current.get()
    return context.traceparent if context is not None else None


class Span:
    """An open span. set() adds allowlisted log fields to its span.end line."""

    def __init__(self, name: str, context: TraceContext, parent_span_id: str | None,
                 fields: dict[str, object]) -> None:
        self.name, self.context, self.parent_span_id = name, context, parent_span_id
        self.fields = fields
        self.status = "ok"
        self.error_type: str | None = None
        self.level = log.INFO

    def set(self, **fields: object) -> None:
        self.fields.update(fields)

    def fail(self, error_type: str | None = None) -> None:
        """Mark the span failed without an exception, e.g. a refused request."""
        self.status = "error"
        if error_type is not None:
            self.error_type = error_type

    def quiet(self) -> None:
        """Write this span's lines at DEBUG, e.g. an idle poll."""
        self.level = log.DEBUG


Defer = Callable[[Callable[[], None]], None]


def _emit(event: str, level: int, fields: dict[str, object], defer: Defer | None) -> None:
    if defer is None:
        log.emit(LOG, event, level=level, **fields)
    else:
        defer(lambda: log.emit(LOG, event, level=level, **fields))


@contextmanager
def start_span(name: str, parent: TraceContext | None = None, *, defer: Defer | None = None,
               **fields: object) -> Iterator[Span]:
    """Open a child of parent, else of the current span, else a new trace.

    span.start and span.end are logged with trace_id, span_id, parent_span_id
    and span_name; span.end adds duration_ms, status and error_type. Other
    fields pass the shared/log.py allowlist, so no secret or body is written.
    With defer (a store's after_commit), both lines wait for the commit and a
    rolled-back span is never logged.
    """
    if not NAME.fullmatch(name):
        raise ValueError("Span names are lower-case dotted identifiers")
    parent = parent or _current.get()
    context = TraceContext(parent.trace_id if parent else new_trace_id(), new_span_id(),
                           parent.flags if parent else "01")
    span = Span(name, context, parent.span_id if parent else None, dict(fields))
    ids = {"span_name": name, "trace_id": context.trace_id, "span_id": context.span_id,
           "parent_span_id": span.parent_span_id}
    token = _current.set(context)
    started = time.monotonic()
    try:
        with log.bind(trace_id=context.trace_id, span_id=context.span_id):
            _emit("span.start", log.DEBUG, {**span.fields, **ids}, defer)
            try:
                yield span
            except BaseException as error:
                span.fail(type(error).__name__)
                raise
            finally:
                _emit("span.end", span.level, {
                    **span.fields, **ids, "status": span.status, "error_type": span.error_type,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3)}, defer)
    finally:
        _current.reset(token)
