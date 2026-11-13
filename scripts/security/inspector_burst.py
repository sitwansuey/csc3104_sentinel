"""Send a burst of concurrent cart authorization checks to one inspector (P5.04 live recipe).

The inspector serves 127.0.0.1:9001 inside the security gateway Pod only. In
the live recipe (docs/security.md) it is reached with `kubectl port-forward`
to that Pod, which enters the Pod's own network namespace. Every request is a
fixed synthetic one: a valid GetCart, or, for every tenth, an AddItem with
quantity 11 that inspection must deny. The result is JSON with the status
counts and latency percentiles. It passes only when no invalid request was
allowed and every answer was 200, 403 or 503: overload may deny a valid call,
but nothing may pass uninspected. Connection errors are counted and fail the
run, since they prove nothing about the bound.
"""

import argparse
from collections import Counter
from contextlib import closing
from http.client import HTTPConnection, HTTPException
import json
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, required=True, help="Local port forwarded to the gateway Pod's 9001")
    parser.add_argument("--count", type=int, default=200)
    args = parser.parse_args(argv)
    summary = burst(args.port, count=args.count)
    print(json.dumps(summary, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
