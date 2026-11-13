"""Load the cart route and judge it from its logs (P5.04 live recipe, docs/security.md).

Three modes:

--port: a burst straight to one inspector. The inspector serves 127.0.0.1:9001 inside the security gateway Pod only. In
the live recipe (docs/security.md) it is reached with `kubectl port-forward`
to that Pod, which enters the Pod's own network namespace. Every request is a
fixed synthetic one: a valid GetCart, or, for every tenth, an AddItem with
quantity 11 that inspection must deny. The result is JSON with the status
counts and latency percentiles. It passes only when no invalid request was
allowed and every answer was 200, 403 or 503: overload may deny a valid call,
but nothing may pass uninspected. Connection errors are counted and fail the
run, since they prove nothing about the bound. This skips Envoy.

--storefront-port: sustained load through the whole route. Many workers each
load the storefront's /cart page over and over (each load makes the frontend
call GetCart through its egress sidecar, the gateway's Envoy and the
inspector) until the time is up. It measures and does not judge: under
overload the frontend answers 500 for a denied GetCart, and that is expected.

--compare: the bypass check for a time window. It reads the inspector's log
(one JSON inspection event per line) and the cart TLS sidecar's access log
(one JSON line per request that reached it) and passes only when no AddItem
was allowed and no more cart requests reached cartservice than the inspector
allowed. A request that reached cartservice without an ALLOW would break it.
"""

import argparse
from collections import Counter
from contextlib import closing
from http.client import HTTPConnection, HTTPException
import json
from pathlib import Path
import sys
import threading
import time

GET_CART = "/hipstershop.CartService/GetCart"
ADD_ITEM = "/hipstershop.CartService/AddItem"
USER = b"00000000-0000-4000-8000-000000000001"


def _varint(value):
    output = bytearray()
    while value > 127:
        output.append((value & 127) | 128)
        value >>= 7
    return bytes(output + bytes([value]))


def _field(number, value):
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def _frame(data):
    return b"\x00" + len(data).to_bytes(4, "big") + data


VALID = (GET_CART, _frame(_field(1, USER)))
INVALID = (ADD_ITEM, _frame(_field(1, USER) + _field(2, _field(1, b"OLJCESPC7Z") + b"\x10" + _varint(11))))


def _send(host, port, path, body, timeout):
    with closing(HTTPConnection(host, port, timeout=timeout)) as client:
        client.request("POST", path, body=body, headers={"Content-Type": "application/octet-stream",
                                                         "x-sentinel-body-format": "grpc", "x-sentinel-route": "cart"})
        response = client.getresponse()
        response.read()  # Read the whole reply before closing, so the server never sees an abort.
        return response.status


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))] * 1000, 1)


def burst(port, *, count=200, host="127.0.0.1", invalid_every=10, timeout=10.0, send=_send):
    """Release count requests at once and summarise the answers."""
    if type(count) is not int or not 1 <= count <= 1000:
        raise ValueError("count must be between 1 and 1000")
    if type(invalid_every) is not int or invalid_every < 1:
        raise ValueError("invalid_every must be a positive integer")
    barrier = threading.Barrier(count, timeout=30)
    results = [None] * count

    def one(index):
        kind = "invalid" if index % invalid_every == invalid_every - 1 else "valid"
        path, body = INVALID if kind == "invalid" else VALID
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            results[index] = (kind, "error", 0.0)
            return
        started = time.monotonic()
        try:
            status = send(host, port, path, body, timeout)
        except (OSError, HTTPException):
            status = "error"
        results[index] = (kind, status, time.monotonic() - started)

    threads = [threading.Thread(target=one, args=(index,), daemon=True) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout + 60)
    finished = [result for result in results if result is not None]
    statuses = {kind: Counter(str(status) for seen, status, _ in finished if seen == kind) for kind in ("valid", "invalid")}
    allowed_invalid = statuses["invalid"].get("200", 0)
    unexpected = sum(number for kind, counter in statuses.items() for status, number in counter.items()
                     if status not in ({"200", "503"} if kind == "valid" else {"403", "503"}))
    seconds = [elapsed for _, status, elapsed in finished if status != "error"]
    return {"count": count, "finished": len(finished),
            "statuses": {kind: dict(sorted(counter.items())) for kind, counter in statuses.items()},
            "denied_overload": sum(counter.get("503", 0) for counter in statuses.values()),
            "allowed_invalid": allowed_invalid, "unexpected": unexpected,
            "p50_ms": _percentile(seconds, 0.5), "p95_ms": _percentile(seconds, 0.95),
            "passed": len(finished) == count and allowed_invalid == 0 and unexpected == 0}


MAX_PAGE = 2 * 1024 * 1024


def _fetch_cart(host, port, timeout):
    with closing(HTTPConnection(host, port, timeout=timeout)) as client:
        client.request("GET", "/cart")
        response = client.getresponse()
        response.read(MAX_PAGE)
        return response.status


def storefront_load(port, *, workers=200, seconds=30.0, host="127.0.0.1", timeout=10.0, fetch=_fetch_cart,
                    clock=time.monotonic):
    """workers concurrent /cart loads, repeated until seconds have passed; a summary, not a verdict."""
    if type(workers) is not int or not 1 <= workers <= 1000:
        raise ValueError("workers must be between 1 and 1000")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= 300:
        raise ValueError("seconds must be more than 0 and at most 300")
    barrier = threading.Barrier(workers, timeout=30)
    lock = threading.Lock()
    results = []
    done = [0]

    def one():
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            return
        deadline = clock() + seconds
        while clock() < deadline:
            started = time.monotonic()
            try:
                status = str(fetch(host, port, timeout))
            except (OSError, HTTPException):
                status = "error"
            with lock:
                results.append((status, time.monotonic() - started))
        with lock:
            done[0] += 1

    threads = [threading.Thread(target=one, daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(seconds + timeout + 60)
    with lock:
        finished, seen = done[0], list(results)
    statuses = Counter(status for status, _ in seen)
    answered = [elapsed for status, elapsed in seen if status != "error"]
    return {"workers": workers, "seconds": seconds, "workers_finished": finished, "requests": len(seen),
            "statuses": dict(sorted(statuses.items())), "errors": statuses.get("error", 0),
            "p50_ms": _percentile(answered, 0.5), "p95_ms": _percentile(answered, 0.95),
            "completed": finished == workers and bool(answered)}


CART_PREFIX = "/hipstershop.CartService/"


def _json_lines(text):
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # Envoy and Python warnings share the stream.
        if isinstance(entry, dict):
            yield entry


def compare_logs(inspector_log, upstream_log):
    """The bypass check: no cart request reached cartservice beyond what the inspector allowed."""
    allowed = Counter(entry.get("method") for entry in _json_lines(inspector_log)
                      if entry.get("kind") == "inspection" and entry.get("verdict") == "ALLOW"
                      and str(entry.get("method", "")).startswith(CART_PREFIX))
    upstream = Counter(str(entry.get("status")) for entry in _json_lines(upstream_log)
                       if entry.get("route") == "cart-rpc")
    inspected = sum(allowed.values())
    # Status 200 means the sidecar got an answer from cartservice: the request reached it.
    reached = upstream.get("200", 0)
    return {"inspector_allowed": dict(sorted(allowed.items())), "upstream_statuses": dict(sorted(upstream.items())),
            "allowed_add_item": allowed.get(CART_PREFIX + "AddItem", 0),
            "passed": reached <= inspected and allowed.get(CART_PREFIX + "AddItem", 0) == 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--port", type=int, help="Local port forwarded to the gateway Pod's 9001")
    mode.add_argument("--storefront-port", type=int, help="Local port forwarded to the frontend Service's 80")
    mode.add_argument("--compare", nargs=2, type=Path, metavar=("INSPECTOR_LOG", "UPSTREAM_LOG"),
                      help="Saved inspector and cart-ingress logs from the same window")
    parser.add_argument("--count", type=int, default=200, help="--port: requests released at once")
    parser.add_argument("--workers", type=int, default=200, help="--storefront-port: concurrent workers")
    parser.add_argument("--seconds", type=float, default=30.0, help="--storefront-port: how long to load")
    args = parser.parse_args(argv)
    if args.compare:
        # PowerShell 5.1 Out-File -Encoding utf8 writes a byte order mark; utf-8-sig drops it.
        summary = compare_logs(*(path.read_text(encoding="utf-8-sig", errors="replace") for path in args.compare))
        ok = summary["passed"]
    elif args.storefront_port is not None:
        summary = storefront_load(args.storefront_port, workers=args.workers, seconds=args.seconds)
        ok = summary["completed"]
    else:
        summary = burst(args.port, count=args.count)
        ok = summary["passed"]
    print(json.dumps(summary, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
