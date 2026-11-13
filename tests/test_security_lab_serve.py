"""The protected-lab dashboard keeps serving while the security gateway has no Pod, and the
inspector the gateway serves through stays bounded: overload and an unwritable journal deny
requests (503, never a pass), and /readyz reports both and recovers (P5.04)."""

from contextlib import closing
from http.client import HTTPConnection
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from runtime import security_inspector
from runtime.security_inspector import Capacity, make_server
from runtime.security_store import probe_writable
from scripts.security import boutique_security as lab
from tests.test_security_inspection import ADD, GET_CART, cart_add, cart_user, frame


class InspectionCollectionTests(unittest.TestCase):
    def test_gateway_without_pods_is_skipped_until_the_next_poll(self):
        # kubectl logs waits for a Pod while the gateway is scaled to zero; its timeout
        # used to end the serve loop and take the dashboard down.
        timeout = subprocess.TimeoutExpired(["kubectl", "logs"], 8)
        with tempfile.TemporaryDirectory() as temp, patch.object(lab, "kube", side_effect=timeout), \
                patch.object(lab, "import_inspections") as store:
            self.assertIsNone(lab.collect_inspections(Path(temp)))
        store.assert_not_called()

    def test_inspection_events_are_still_imported(self):
        output = '{"kind": "inspection", "decision": "ALLOW"}\nnot json\n{"kind": "other"}\n'
        completed = subprocess.CompletedProcess(["kubectl"], 0, output, "")
        with tempfile.TemporaryDirectory() as temp, patch.object(lab, "kube", return_value=completed), \
                patch.object(lab, "import_inspections") as store:
            lab.collect_inspections(Path(temp))
        store.assert_called_once()
        self.assertEqual(store.call_args.args[1], [{"kind": "inspection", "decision": "ALLOW"}])


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class InspectorCapacityTests(unittest.TestCase):
    """A real inspector on loopback; every request reads its whole reply before closing (WinError 10053)."""

    def start(self, **options):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal = Path(self.directory.name) / "data" / "events.sqlite3"
        self.server = make_server(self.journal, port=0, **options)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(2)
        self.addCleanup(stop)

    def post(self, path=GET_CART, data=None):
        data = frame(cart_user()) if data is None else data
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)) as client:
            client.request("POST", path, body=data, headers={"Content-Type": "application/octet-stream",
                                                             "x-sentinel-body-format": "grpc", "x-sentinel-route": "cart"})
            response = client.getresponse()
            response.read()
            return response.status

    def readyz(self):
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)) as client:
            client.request("GET", "/readyz")
            response = client.getresponse()
            return response.status, json.loads(response.read())

    def rows(self):
        if not self.journal.exists():
            return []
        with closing(sqlite3.connect(self.journal)) as db:
            # /readyz's test write is rolled back, table and all, so the table may not exist yet.
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='security_events'").fetchone():
                return []
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM security_events")]

    def hold_inspections(self):
        """Patch inspection so admitted requests wait until the returned event is set."""
        entered, release = threading.Semaphore(0), threading.Event()
        real = security_inspector.inspect_request

        def slow(*args, **kwargs):
            entered.release()
            release.wait(10)
            return real(*args, **kwargs)
        patcher = patch.object(security_inspector, "inspect_request", side_effect=slow)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(release.set)
        return entered, release

    def test_exhausted_inflight_denies_with_503_and_readyz_flips_and_recovers(self):
        self.start(max_inflight=2)
        self.assertEqual(self.readyz(), (200, {"ready": True, "reasons": [], "inflight": 0, "max_inflight": 2,
                                               "max_rps": 200.0, "shed": {"inflight": 0, "rate": 0}}))
        entered, release = self.hold_inspections()
        results = []
        held = [threading.Thread(target=lambda: results.append(self.post())) for _ in range(2)]
        for thread in held:
            thread.start()
        for _ in held:
            self.assertTrue(entered.acquire(timeout=5))
        # Both slots are taken: the next request is denied at once and never inspected or journaled.
        self.assertEqual(self.post(ADD, frame(cart_add())), 503)
        status, body = self.readyz()
        self.assertEqual((status, body["reasons"], body["inflight"], body["shed"]),
                         (503, ["inflight_exhausted"], 2, {"inflight": 1, "rate": 0}))
        self.assertEqual(self.rows(), [])
        release.set()
        for thread in held:
            thread.join(5)
        self.assertEqual(results, [200, 200])
        self.assertEqual(self.readyz()[0], 200)
        self.assertEqual(self.post(ADD, frame(cart_add())), 200)
        self.assertEqual([row["method"] for row in self.rows()], [GET_CART, GET_CART, ADD])
        self.assertEqual(self.server.capacity.state()["inflight"], 0)
        self.assert_every_slot_is_free()

    def test_overload_never_passes_a_request_that_inspection_would_deny(self):
        self.start(max_inflight=1)
        entered, release = self.hold_inspections()
        holder = threading.Thread(target=self.post)
        holder.start()
        self.assertTrue(entered.acquire(timeout=5))
        # Denied (not 200) both for a bad request and a good one while saturated.
        self.assertEqual(self.post(ADD, frame(cart_add(quantity=11))), 503)
        self.assertEqual(self.post(), 503)
        release.set()
        holder.join(5)
        self.assertEqual(self.post(ADD, frame(cart_add(quantity=11))), 403)

    def assert_every_slot_is_free(self):
        """Hold max_inflight requests at once: each is admitted only if no slot leaked."""
        slots = self.server.capacity.max_inflight
        entered, release = self.hold_inspections()
        results = []
        held = [threading.Thread(target=lambda: results.append(self.post())) for _ in range(slots)]
        for thread in held:
            thread.start()
        for _ in held:
            self.assertTrue(entered.acquire(timeout=5), "an inflight slot leaked")
        release.set()
        for thread in held:
            thread.join(5)
        self.assertEqual(results, [200] * slots)

    def test_rate_limit_denies_with_503_and_refills_with_time(self):
        clock = Clock()
        # One slot: a rate refusal that kept its semaphore slot would deny every later request.
        self.start(max_inflight=1, max_rps=2, clock=clock)
        self.assertEqual([self.post() for _ in range(3)], [200, 200, 503])
        self.assertEqual(self.server.capacity.state()["shed"], {"inflight": 0, "rate": 1})
        clock.now += 0.5
        self.assertEqual([self.post(), self.post()], [200, 503])
        clock.now += 10  # The bucket refills to its burst (one second of traffic), never beyond.
        self.assertEqual([self.post() for _ in range(3)], [200, 200, 503])
        self.assertEqual(len(self.rows()), 5)
        # A rate-limited request frees its inflight slot (the counter and the semaphore).
        self.assertEqual(self.server.capacity.state()["inflight"], 0)
        clock.now += 10
        self.assert_every_slot_is_free()

    def test_unwritable_journal_denies_with_503_and_readyz_flips_and_recovers(self):
        # One slot: a journal error that kept its slot would deny every later request.
        self.start(max_inflight=1)
        self.journal.parent.mkdir(parents=True)
        self.journal.mkdir()  # The journal path is a directory: sqlite cannot open it for writing.
        status, body = self.readyz()
        self.assertEqual((status, body["ready"], body["reasons"]), (503, False, ["journal_unwritable"]))
        with self.assertLogs("sentinel.security_inspector", level="ERROR"):
            self.assertEqual(self.post(), 503)
        self.journal.rmdir()
        self.assertEqual(self.readyz()[0], 200)
        self.assertEqual(self.post(), 200)
        self.assertEqual([row["rule"] for row in self.rows()], ["CART_SCHEMA"])
        self.assert_every_slot_is_free()

    def test_readyz_test_write_leaves_no_record(self):
        self.start()
        for _ in range(3):
            self.assertEqual(self.readyz()[0], 200)
        self.assertEqual(self.rows(), [])
        probe_writable(self.journal)
        self.assertEqual(self.rows(), [])

    def test_live_recipe_burst_sees_bounded_denials_and_no_bypass(self):
        from scripts.security.inspector_burst import burst
        self.start(max_inflight=3)
        real = security_inspector.inspect_request

        def slow(*args, **kwargs):
            threading.Event().wait(0.05)  # hold each slot briefly so the burst overruns the bound
            return real(*args, **kwargs)
        with patch.object(security_inspector, "inspect_request", side_effect=slow):
            summary = burst(self.server.server_port, count=40)
        self.assertTrue(summary["passed"], summary)
        self.assertEqual(summary["finished"], 40)
        self.assertGreater(summary["denied_overload"], 0)
        self.assertEqual(summary["allowed_invalid"], 0)
        self.assertEqual(set(summary["statuses"]["valid"]) | set(summary["statuses"]["invalid"]) <= {"200", "403", "503"}, True)
        self.assertEqual(self.server.capacity.state()["inflight"], 0)
        self.assertIsNotNone(summary["p95_ms"])
        # The default backlog of 5 refused connections in this burst before the capacity check ran.
        from scripts.security.security_lab import INSPECTOR_CONNECTIONS
        self.assertGreaterEqual(self.server.request_queue_size, INSPECTOR_CONNECTIONS)

    def test_live_recipe_burst_fails_when_an_invalid_call_is_allowed_or_a_call_errors(self):
        from scripts.security.inspector_burst import burst
        self.assertFalse(burst(1, count=10, send=lambda *args: 200)["passed"])  # a fail-open inspector
        summary = burst(1, count=10, send=lambda *args: 200 if args[2].endswith("GetCart") else 403)
        self.assertTrue(summary["passed"], summary)

        def refused(*args):
            raise ConnectionRefusedError()
        summary = burst(1, count=10, send=refused)
        self.assertEqual((summary["passed"], summary["statuses"]["valid"]), (False, {"error": 9}))
        for count in (0, 1001, 1.5):
            with self.subTest(count=count), self.assertRaises(ValueError):
                burst(1, count=count)

    def test_capacity_bounds_are_validated(self):
        for options in ({"max_inflight": 0}, {"max_inflight": 1.5}, {"max_rps": 0}, {"max_rps": -1},
                        {"max_rps": True}, {"max_rps": float("nan")}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                Capacity(**options)


class GatewayLoadRecipeTests(unittest.TestCase):
    """The live recipe's load through the whole cart route and its bypass check, run offline."""

    def storefront(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        served = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                served.append(self.path)
                # A denied GetCart makes the real frontend answer 500; both are answers.
                self.send_response(500 if len(served) % 3 == 0 else 200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(stop)
        return server.server_port, served

    def test_storefront_load_repeats_cart_loads_from_every_worker_until_time_is_up(self):
        from scripts.security.inspector_burst import storefront_load
        port, served = self.storefront()
        summary = storefront_load(port, workers=4, seconds=0.3)
        self.assertTrue(summary["completed"], summary)
        self.assertEqual((summary["workers_finished"], summary["errors"]), (4, 0))
        self.assertGreater(summary["requests"], 4)
        self.assertEqual(set(summary["statuses"]), {"200", "500"})
        self.assertEqual(set(served), {"/cart"})
        self.assertEqual(len(served), summary["requests"])

    def test_storefront_load_counts_errors_and_is_not_complete_without_answers(self):
        from scripts.security.inspector_burst import storefront_load

        def refused(*args):
            raise ConnectionRefusedError()
        summary = storefront_load(1, workers=2, seconds=0.05, fetch=refused)
        self.assertFalse(summary["completed"])
        self.assertEqual(summary["errors"], summary["requests"])
        for options in ({"workers": 0}, {"workers": 1001}, {"workers": 1.5}, {"seconds": 0}, {"seconds": 301},
                        {"seconds": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                storefront_load(1, **options)

    def inspection(self, method, verdict):
        return json.dumps({"kind": "inspection", "method": "/hipstershop." + method, "verdict": verdict})

    def access(self, status, route="cart-rpc"):
        return json.dumps({"route": route, "status": status, "grpc_status": "-", "flags": "-", "duration_ms": 1})

    def test_bypass_check_passes_when_cartservice_saw_no_more_than_the_inspector_allowed(self):
        from scripts.security.inspector_burst import compare_logs
        inspector = "\n".join([self.inspection("CartService/GetCart", "ALLOW")] * 3
                              + [self.inspection("CartService/AddItem", "DENY"), "warning: not json",
                                 self.inspection("ShippingService/GetQuote", "ALLOW")])
        # Two reached cartservice; a third was refused at the sidecar; a shipping line is ignored.
        upstream = "\n".join([self.access(200), self.access(200), self.access(503), self.access(200, "shipping-rpc")])
        summary = compare_logs(inspector, upstream)
        self.assertTrue(summary["passed"], summary)
        self.assertEqual(summary["inspector_allowed"], {"/hipstershop.CartService/GetCart": 3})
        self.assertEqual(summary["upstream_statuses"], {"200": 2, "503": 1})

    def test_bypass_check_fails_on_an_uninspected_request_or_an_allowed_add_item(self):
        from scripts.security.inspector_burst import compare_logs
        allowed = self.inspection("CartService/GetCart", "ALLOW")
        # More requests reached cartservice than were allowed: one bypassed the inspector.
        self.assertFalse(compare_logs(allowed, "\n".join([self.access(200)] * 2))["passed"])
        # Only cart ALLOWs count; shipping ALLOWs cannot cover a cart request.
        self.assertFalse(compare_logs(self.inspection("ShippingService/GetQuote", "ALLOW"), self.access(200))["passed"])
        summary = compare_logs(self.inspection("CartService/AddItem", "ALLOW"), "")
        self.assertEqual((summary["passed"], summary["allowed_add_item"]), (False, 1))

    def test_burst_command_judges_saved_logs_and_needs_exactly_one_mode(self):
        from scripts.security import inspector_burst
        with tempfile.TemporaryDirectory() as folder:
            inspector, upstream = Path(folder) / "inspector.log", Path(folder) / "upstream.log"
            # Written as PowerShell 5.1 Out-File -Encoding utf8 writes it: a byte order mark, CRLF.
            inspector.write_text(self.inspection("CartService/GetCart", "ALLOW") + "\r\n", encoding="utf-8-sig")
            upstream.write_text(self.access(200) + "\n", encoding="utf-8")
            with patch("builtins.print"):
                self.assertEqual(inspector_burst.main(["--compare", str(inspector), str(upstream)]), 0)
                upstream.write_text(self.access(200) + "\n" + self.access(200) + "\n", encoding="utf-8")
                self.assertEqual(inspector_burst.main(["--compare", str(inspector), str(upstream)]), 1)
        with patch("sys.stderr"):
            for argv in ([], ["--port", "1", "--storefront-port", "2"]):
                with self.subTest(argv=argv), self.assertRaises(SystemExit):
                    inspector_burst.main(argv)


if __name__ == "__main__":
    unittest.main()
