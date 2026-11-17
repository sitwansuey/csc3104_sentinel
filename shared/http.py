"""Bounded cleanup for HTTP requests refused before their body is read."""
from contextlib import suppress
from http.server import BaseHTTPRequestHandler


def drain_request_body(handler: BaseHTTPRequestHandler, limit: int) -> None:
    """Discard only an unambiguous, bounded body, using the socket's existing timeout."""
    lengths = handler.headers.get_all("Content-Length", [])
    if len(lengths) != 1 or handler.headers.get("Transfer-Encoding"):
        return
    length = lengths[0].strip(" \t")
    if not length.isascii() or not length.isdecimal():
        return
    with suppress(ValueError, OSError):
        size = int(length)
        if 0 < size <= limit:
            # Unread received data can reset the socket on close, losing the refusal on Windows.
            handler.rfile.read(size)
