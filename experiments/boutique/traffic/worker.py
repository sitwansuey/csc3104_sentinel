"""In-cluster synthetic shoppers; stdout is JSONL with no fault labels."""

from datetime import datetime, timezone
import json
import os
import signal
import threading
import time

if __package__:
    from .storefront import StorefrontProbe
else:
    from storefront import StorefrontProbe


def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TimedProbe(StorefrontProbe):
    def _request(self, opener, path, fields=None):
        started, clock = utc(), time.monotonic()
        result = super()._request(opener, path, fields)
        self.requests.append({"started_at": started, "completed_at": utc(),
                              "path": path, "method": "POST" if fields else "GET",
                              "duration_ms": round((time.monotonic() - clock) * 1000, 3),
                              "status": result[0], "error": result[2]})
        return result


def main():
    stop, lock = threading.Event(), threading.Lock()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    def shopper(worker):
        probe = TimedProbe(os.environ["STOREFRONT_URL"], timeout=5, allow_cluster_service=True)
        sequence = 0
        while not stop.is_set():
            sequence += 1
            probe.requests = []
            started, clock = utc(), time.monotonic()
            try:
                result = probe.journey()
            except Exception as error:  # noqa: BLE001 - a failed journey is a row with its error class
                result = {"passed": None, "error": type(error).__name__}
            row = {"run_id": os.environ["RUN_ID"], "worker": worker, "sequence": sequence,
                   "started_at": started, "completed_at": utc(),
                   "duration_ms": round((time.monotonic() - clock) * 1000, 3),
                   "requests": probe.requests, "journey": result}
            with lock:
                print(json.dumps(row), flush=True)
            stop.wait(float(os.environ["TRAFFIC_INTERVAL"]))

    threads = [threading.Thread(target=shopper, args=(i,), daemon=True)
               for i in range(int(os.environ["TRAFFIC_WORKERS"]))]
    for thread in threads:
        thread.start()
    while not stop.wait(1):
        if not all(thread.is_alive() for thread in threads):
            raise RuntimeError("A traffic worker exited")


if __name__ == "__main__":
    main()
