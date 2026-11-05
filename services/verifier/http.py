"""Bounded read-only HTTP transport for operator-configured evidence endpoints."""

from contextlib import closing
import json
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class EvidenceUnavailable(RuntimeError):
    """A source did not supply usable evidence; never assume it is healthy."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise EvidenceUnavailable("Evidence endpoint redirected")


def _invalid_constant(value):
    raise ValueError(f"Nonfinite JSON number: {value}")


def validate_url(url):
    if not isinstance(url, str):
        raise ValueError("Expected an operator-configured HTTP(S) endpoint")
    try:
        parsed = urlsplit(url)
        parsed.port  # Validate a supplied port before making a request.
    except (TypeError, ValueError) as exc:
        raise ValueError("Expected an operator-configured HTTP(S) endpoint") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.fragment
            or any(character.isspace() for character in url)):
        raise ValueError("Expected an HTTP(S) endpoint without embedded credentials or fragments")
    return url


class EvidenceHTTP:
    def __init__(self, *, timeout_seconds=3, max_bytes=65536, opener=None):
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 10):
            raise ValueError("HTTP timeout must be positive and at most ten seconds")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 2_000_000:
            raise ValueError("HTTP response limit must be between 1 and 2000000 bytes")
        self.timeout = timeout_seconds
        self.max_bytes = max_bytes
        self.open = opener or build_opener(ProxyHandler({}), _NoRedirect()).open

    def _get(self, url):
        validate_url(url)
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            response = self.open(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
        except (URLError, OSError, TimeoutError) as exc:
            raise EvidenceUnavailable("Evidence endpoint unavailable") from exc
        try:
            with closing(response):
                status = response.code
                body = response.read(self.max_bytes + 1)
            if type(status) is not int or not 100 <= status <= 599:
                raise EvidenceUnavailable("Invalid HTTP response status")
            if 300 <= status <= 399:
                raise EvidenceUnavailable("Evidence endpoint redirected")
            if len(body) > self.max_bytes:
                raise EvidenceUnavailable("Evidence response exceeds configured limit")
            return status, body
        except (OSError, TimeoutError) as exc:
            raise EvidenceUnavailable("Evidence response could not be read") from exc

    def health(self, url):
        try:
            status, _ = self._get(url)
            return {"http_status": status, "error": None}
        except EvidenceUnavailable as error:
            return {"http_status": None, "error": str(error)}

    def json(self, url):
        status, body = self._get(url)
        if status != 200:
            raise EvidenceUnavailable("Trace export endpoint did not return HTTP 200")
        try:
            return json.loads(body.decode("utf-8"), parse_constant=_invalid_constant)
        except (ValueError, UnicodeError) as exc:
            raise EvidenceUnavailable("Trace export response is not valid JSON") from exc
