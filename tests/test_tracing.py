"""W3C trace context: strict parsing, spans as log lines, and propagation through the outbox and HTTP."""
from contextlib import ExitStack
import http.client
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from runtime import worker_http
from runtime.worker_http import WorkerClient, WorkerHTTPServer, handler, serve_until_stopped
from runtime.worker_transport import Delivery, ProtocolError
from scripts import trace_view
from services.coordinator.core import LocalEventBus, handle
from services.coordinator.store import MIGRATIONS, SCHEMA_VERSION, SQLiteStore, migrate, schema_version
from shared import log, tracing

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data/sentinel/samples/messages"
VALID = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
SECRET = "S3CRET-" + "q" * 40


def sample(name):
    return json.loads((SAMPLES / name).read_text())


def lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def spans(stream, name=None):
    return [r for r in lines(stream) if r["event"] == "span.end" and (name is None or r["span_name"] == name)]


class TracingTestCase(unittest.TestCase):
    def capture(self):
        stream = io.StringIO()
        scope = log.configured("test", level="DEBUG", stream=stream)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        return stream


class ParseTests(unittest.TestCase):
    def test_valid_traceparent_round_trips(self):
        for value in (VALID, VALID[:-2] + "00"):
            with self.subTest(value=value):
                context = tracing.parse_traceparent(value)
                self.assertEqual(tracing.format_traceparent(context), value)
        context = tracing.parse_traceparent(VALID)
        self.assertEqual((context.trace_id, context.span_id, context.flags),
                         ("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7", "01"))

    def test_malformed_values_are_ignored_without_raising(self):
        trace, span = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"
        cases = {
            "uppercase": VALID.upper(),
            "uppercase hex only": f"00-{trace.upper()}-{span}-01",
            "short trace": f"00-{trace[:-1]}-{span}-01",
            "long trace": f"00-{trace}a-{span}-01",
            "short span": f"00-{trace}-{span[:-1]}-01",
            "long span": f"00-{trace}-{span}a-01",
            "version ff": "ff" + VALID[2:],
            "future version": "01" + VALID[2:],
            "zero trace": f"00-{'0' * 32}-{span}-01",
            "zero span": f"00-{trace}-{'0' * 16}-01",
            "unknown flags": VALID[:-2] + "02",
            "extra field": VALID + "-00",
            "over 55 characters": VALID + "0",
            "padded": " " + VALID,
            "trailing newline": VALID + "\n",
            "not hex": f"00-{'g' * 32}-{span}-01",
            "empty": "",
        }
        for label, value in cases.items():
            with self.subTest(label):
                self.assertIsNone(tracing.parse_traceparent(value))
        for value in (None, 1, b"00", VALID.encode(), {"traceparent": VALID}, ["x"]):
            with self.subTest(value=value):
                self.assertIsNone(tracing.parse_traceparent(value))

    def test_a_malformed_parent_starts_a_new_trace(self):
        with tracing.start_span("demo.root", parent=tracing.parse_traceparent(VALID.upper())) as span:
            self.assertNotEqual(span.context.trace_id, VALID.split("-")[1])
            self.assertIsNone(span.parent_span_id)

    def test_generated_ids_are_well_formed(self):
        for _ in range(50):
            with tracing.start_span("demo.root") as span:
                self.assertEqual(tracing.parse_traceparent(span.context.traceparent), span.context)


class SpanTests(TracingTestCase):
    def test_nested_spans_share_a_trace_and_log_lines_carry_ids(self):
        stream = self.capture()
        self.assertIsNone(tracing.current())
        with tracing.start_span("demo.outer", incident_id="INC-1") as outer:
            log.emit("demo", "demo.inside")
            with tracing.start_span("demo.inner") as inner:
                self.assertEqual(tracing.current(), inner.context)
                log.emit("demo", "demo.deeper")
            self.assertEqual(tracing.current(), outer.context)
        self.assertIsNone(tracing.current())
        log.emit("demo", "demo.after")
        records = {r["event"] + ":" + r.get("span_name", ""): r for r in lines(stream)}
        self.assertEqual(records["demo.inside:"]["span_id"], outer.context.span_id)
        self.assertEqual(records["demo.deeper:"]["span_id"], inner.context.span_id)
        self.assertEqual(records["demo.deeper:"]["trace_id"], outer.context.trace_id)
        self.assertNotIn("trace_id", records["demo.after:"])
        end_inner, end_outer = records["span.end:demo.inner"], records["span.end:demo.outer"]
        self.assertEqual(end_inner["parent_span_id"], outer.context.span_id)
        self.assertNotIn("parent_span_id", end_outer)
        self.assertEqual((end_outer["status"], end_outer["incident_id"]), ("ok", "INC-1"))
        self.assertIsInstance(end_outer["duration_ms"], (int, float))
        self.assertEqual(records["span.start:demo.outer"]["level"], "debug")
        self.assertEqual(end_outer["level"], "info")

    def test_a_failing_span_records_the_class_only_and_reraises(self):
        stream = self.capture()
        with self.assertRaises(RuntimeError), tracing.start_span("demo.failing"):
            raise RuntimeError("leaked " + SECRET)
        (end,) = spans(stream)
        self.assertEqual((end["status"], end["error_type"]), ("error", "RuntimeError"))
        self.assertNotIn(SECRET, stream.getvalue())

    def test_no_secret_reaches_a_span_line(self):
        stream = self.capture()
        with tracing.start_span("demo.request", token=SECRET, lease_token=SECRET, authorization="Bearer " + SECRET,
                                payload={"v": SECRET}, body=SECRET, message={"m": SECRET}) as span:
            span.set(password=SECRET, headers={"Authorization": SECRET}, topic="planner.request")
        text = stream.getvalue()
        self.assertNotIn(SECRET, text)
        self.assertNotIn("Bearer", text)
        self.assertEqual(spans(stream)[0]["topic"], "planner.request")

    def test_span_names_must_be_fixed_identifiers(self):
        for name in ("coordinator /v1/claim", "Coordinator.claim", "", "a\nb", "x." + SECRET):
            with self.subTest(name=name), self.assertRaises(ValueError), tracing.start_span(name):
                pass

    def test_threads_started_with_context_thread_inherit_the_span(self):
        seen = []
        with tracing.start_span("demo.outer") as span:
            thread = log.context_thread(lambda: seen.append(tracing.current()))
            thread.start()
            thread.join()
            plain = threading.Thread(target=lambda: seen.append(tracing.current()))
            plain.start()
            plain.join()
        self.assertEqual(seen, [span.context, None])

    def test_quiet_spans_log_at_debug_and_deferred_spans_wait_for_commit(self):
        stream = self.capture()
        with tracing.start_span("demo.idle") as span:
            span.quiet()
        self.assertEqual(spans(stream)[0]["level"], "debug")
        pending = []
        with tracing.start_span("demo.deferred", defer=pending.append):
            pass
        self.assertEqual(len(spans(stream)), 1)
        for callback in pending:
            callback()
        self.assertEqual([r["span_name"] for r in spans(stream)], ["demo.idle", "demo.deferred"])

    def test_imports_and_runs_without_opentelemetry(self):
        code = ("import sys; sys.modules['opentelemetry'] = None\n"
                "from shared import tracing; import runtime.worker_http, runtime.remote_worker\n"
                "with tracing.start_span('demo.root') as span: pass\n"
                "assert not [m for m, v in sys.modules.items() if m.startswith('opentelemetry') and v is not None]\n")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


class StoreTests(TracingTestCase):
    def setUp(self):
        self.store = SQLiteStore()
        self.addCleanup(self.store.close)

    def rows(self):
        return {row["topic"]: tracing.parse_traceparent(row["trace_context"])
                for row in self.store.db.execute("SELECT topic,trace_context FROM outbox ORDER BY id")}

    def test_incident_and_its_outbox_rows_inherit_the_callers_span(self):
        stream = self.capture()
        incident = sample("incident.json")
        with tracing.start_span("observer.ingest") as root:
            handle(incident, self.store, LocalEventBus())
        (record,) = spans(stream, "coordinator.record")
        self.assertEqual((record["trace_id"], record["parent_span_id"]), (root.context.trace_id, root.context.span_id))
        stored = self.store.trace_context(incident_id=incident["incident_id"])
        self.assertEqual((stored.trace_id, stored.span_id), (root.context.trace_id, record["span_id"]))
        self.assertEqual(self.rows()["planner.request"], stored)
        enqueued = [r for r in lines(stream) if r["event"] == "coordinator.outbox.enqueued"]
        transition = [r for r in lines(stream) if r["event"] == "coordinator.state.transition"]
        self.assertEqual({r["trace_id"] for r in enqueued + transition}, {root.context.trace_id})

    def test_messages_and_rows_without_a_span_join_the_incidents_trace(self):
        incident, proposal = sample("incident.json"), sample("proposal.json")
        handle(incident, self.store, LocalEventBus())  # No caller span: a new trace starts here.
        stored = self.store.trace_context(incident_id=incident["incident_id"])
        self.assertIsNotNone(stored)
        handle(proposal, self.store, LocalEventBus())
        guard = self.rows()["guard.request"]
        self.assertEqual(guard.trace_id, stored.trace_id)
        self.assertNotEqual(guard.span_id, stored.span_id)  # The proposal's own record span.
        # A row written outside every span (an approval) uses the incident's stored context.
        with self.store.transaction():
            self.store.enqueue("incident.review", {"note": "x"}, proposal["action_id"])
        self.assertEqual(self.rows()["incident.review"], stored)

    def test_an_unreadable_stored_context_is_ignored(self):
        incident, proposal = sample("incident.json"), sample("proposal.json")
        handle(incident, self.store, LocalEventBus())
        self.store.db.execute("UPDATE incidents SET trace_context='not-a-traceparent'")
        self.assertIsNone(self.store.trace_context(incident_id=incident["incident_id"]))
        self.assertTrue(handle(proposal, self.store, LocalEventBus()))  # A new trace, same decision.
        self.assertEqual(self.store.incident(incident["incident_id"])["state"], "VALIDATING")
        self.assertIsNotNone(self.rows()["guard.request"])

    def test_a_rolled_back_record_writes_no_span(self):
        stream = self.capture()
        handle(sample("incident.json"), self.store, LocalEventBus())
        before = stream.getvalue()
        with patch.object(self.store, "enqueue", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                handle(sample("proposal.json"), self.store, LocalEventBus())
        self.assertEqual(stream.getvalue(), before)

    def test_the_traceparent_never_changes_a_decision(self):
        incident, proposal = sample("incident.json"), sample("proposal.json")
        forged = tracing.parse_traceparent(VALID)
        with tracing.start_span("forged.parent", parent=forged):
            handle(incident, self.store, LocalEventBus())
        other = SQLiteStore()
        self.addCleanup(other.close)
        handle(incident, other, LocalEventBus())
        for store in (self.store, other):
            handle(proposal, store, LocalEventBus())
        first, second = (s.incident(incident["incident_id"]) for s in (self.store, other))
        self.assertEqual({k: v for k, v in first.items() if k != "trace_context"},
                         {k: v for k, v in second.items() if k != "trace_context"})
        self.assertEqual(self.store.trace_context(incident_id=incident["incident_id"]).trace_id, forged.trace_id)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "coordinator.db"

    def test_a_version_2_database_gains_nullable_trace_columns_and_keeps_working(self):
        previous = MIGRATIONS[:2]
        db = sqlite3.connect(self.path, isolation_level=None)
        migrate(db, previous)
        incident = sample("incident.json")
        db.execute("INSERT INTO incidents VALUES(?,?,?,NULL)", (incident["incident_id"],
                   json.dumps(incident["target"]), "OPEN"))
        db.execute("INSERT INTO outbox(topic,payload,action_id) VALUES(?,?,NULL)",
                   ("planner.request", json.dumps({"incident": incident})))
        self.assertEqual(schema_version(db), 2)
        db.close()
        with SQLiteStore(self.path) as store:
            self.assertEqual(schema_version(store.db), SCHEMA_VERSION)
            self.assertIsNone(store.incident(incident["incident_id"])["trace_context"])
            self.assertIsNone(store.trace_context(incident_id=incident["incident_id"]))
            delivery = Delivery(store).claim("planner", "planner")["delivery"]
            self.assertEqual(delivery["topic"], "planner.request")
            self.assertNotIn("traceparent", delivery)  # Old rows carry no context.
            proposal = sample("proposal.json")
            self.assertTrue(handle(proposal, store, LocalEventBus()))
            row = store.db.execute("SELECT trace_context FROM outbox WHERE topic='guard.request'").fetchone()
            self.assertIsNotNone(tracing.parse_traceparent(row[0]))


class TransportTests(TracingTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "coordinator.db"
        self.services = {"observer": ("observer", "o" * 40), "planner": ("planner", "p" * 40)}

    def serve(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        with SQLiteStore(self.path) as store:
            Delivery(store)
        stopping = threading.Event()
        server = stack.enter_context(WorkerHTTPServer(("127.0.0.1", 0), handler(self.path, self.services,
                                     stopping=stopping), request_timeout=5))
        thread = threading.Thread(target=serve_until_stopped, args=(server, stopping))
        thread.start()
        stack.callback(thread.join, 5)
        stack.callback(stopping.set)
        self.port = server.server_port
        return f"http://127.0.0.1:{self.port}"

    def client(self, role, url):
        return WorkerClient(url, role, self.services[role][1], timeout=5)

    def test_claim_hands_out_the_rows_traceparent_and_only_a_valid_one(self):
        with SQLiteStore(self.path) as store:
            with tracing.start_span("observer.ingest"):
                handle(sample("incident.json"), store, LocalEventBus())
            row = store.db.execute("SELECT trace_context FROM outbox").fetchone()[0]
            delivery = Delivery(store).claim("planner", "planner")["delivery"]
            self.assertEqual(delivery["traceparent"], row)
            store.db.execute("UPDATE outbox SET trace_context='00-zz'")
            store.db.execute("DELETE FROM transport_receipts")
            self.assertNotIn("traceparent", Delivery(store).claim("planner", "planner")["delivery"])

    def test_client_sends_the_current_span_as_a_header(self):
        sent = []

        class Connection:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, method, path, body, headers):
                sent.append(dict(headers))

            def getresponse(self):
                response = io.BytesIO(b'{"delivery": null}')
                response.status = 200
                return response

            def close(self):
                pass

        client = WorkerClient("http://127.0.0.1:1", "planner", "p" * 40)
        with patch.object(worker_http, "HTTPConnection", Connection):
            client.post("claim", {})
            with tracing.start_span("worker.process") as span:
                client.post("claim", {})
        self.assertNotIn("traceparent", sent[0])
        self.assertEqual(sent[1]["traceparent"], span.context.traceparent)

    def test_server_span_continues_the_callers_span(self):
        stream = self.capture()
        url = self.serve()
        with tracing.start_span("worker.process") as span:
            self.assertIsNone(self.client("planner", url).post("claim", {})["delivery"])
        (server,) = spans(stream, "coordinator.claim")
        self.assertEqual((server["trace_id"], server["parent_span_id"]), (span.context.trace_id, span.context.span_id))
        self.assertEqual((server["identity"], server["http_status"], server["level"]), ("planner", 200, "debug"))
        (access,) = [r for r in lines(stream) if r["event"] == "http.access"]
        self.assertEqual(access["span_id"], server["span_id"])

    def raw_post(self, headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("POST", "/v1/claim", b"{}", {"Content-Type": "application/json", **headers})
            return connection.getresponse().status
        finally:
            connection.close()

    def test_a_forged_header_from_an_unauthenticated_request_is_never_logged(self):
        stream = self.capture()
        self.serve()
        forged = "00-" + "f" * 31 + "e-" + "a" * 15 + "b-01"
        for headers in ({"X-Service-Identity": "planner", "Authorization": "Bearer " + "w" * 40},
                        {"X-Service-Identity": "nobody", "Authorization": "Bearer " + "p" * 40},
                        {}):
            with self.subTest(headers=headers):
                self.assertEqual(self.raw_post({**headers, "traceparent": forged}), 401)
        text = stream.getvalue()
        self.assertNotIn("f" * 31 + "e", text)
        self.assertNotIn("a" * 15 + "b", text)
        self.assertEqual(spans(stream), [])
        self.assertEqual([r["event"] for r in lines(stream)], ["auth.denied"] * 3)

    def test_malformed_or_repeated_headers_open_no_span_and_grant_nothing(self):
        stream = self.capture()
        self.serve()
        auth = {"X-Service-Identity": "observer", "Authorization": "Bearer " + "o" * 40}
        self.assertEqual(self.raw_post({**auth, "traceparent": VALID}), 403)  # A valid header is not a role.
        for value in (VALID.upper(), "00-" + "0" * 32 + "-00f067aa0ba902b7-01", VALID + "-x"):
            self.assertEqual(self.raw_post({**auth, "traceparent": value}), 403)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.putrequest("POST", "/v1/claim")
            for key, value in {**auth, "Content-Type": "application/json", "Content-Length": "2"}.items():
                connection.putheader(key, value)
            connection.putheader("traceparent", VALID)
            connection.putheader("traceparent", VALID)
            connection.endheaders(b"{}")
            self.assertEqual(connection.getresponse().status, 403)
        finally:
            connection.close()
        claims = spans(stream, "coordinator.claim")
        self.assertEqual(len(claims), 1)  # Only the single well-formed header.
        self.assertEqual((claims[0]["status"], claims[0]["http_status"]), ("error", 403))
        self.assertEqual(claims[0]["trace_id"], VALID.split("-")[1])

    def test_worker_ingest_starts_one_trace_for_a_new_incident(self):
        from runtime.remote_worker import RemoteIngress
        stream = self.capture()
        url = self.serve()
        incident = sample("incident.json")
        RemoteIngress(self.client("observer", url))(incident)
        names = {r["span_name"]: r for r in spans(stream)}
        with self.assertRaises(ProtocolError):
            RemoteIngress(self.client("planner", url))(incident)  # Role check is unchanged.
        root, server, record = names["observer.ingest"], names["coordinator.ingest"], names["coordinator.record"]
        self.assertNotIn("parent_span_id", root)
        self.assertEqual(server["parent_span_id"], root["span_id"])
        self.assertEqual(record["parent_span_id"], server["span_id"])
        self.assertEqual({root["trace_id"], server["trace_id"], record["trace_id"]}, {root["trace_id"]})
        with SQLiteStore(self.path) as store:
            stored = store.trace_context(incident_id=incident["incident_id"])
        self.assertEqual((stored.trace_id, stored.span_id), (root["trace_id"], record["span_id"]))


class ViewerTests(unittest.TestCase):
    def records(self):
        stream = io.StringIO()
        with log.configured("test", level="DEBUG", stream=stream):
            with tracing.start_span("observer.ingest", incident_id="INC-1"):
                store = SQLiteStore()
                self.addCleanup(store.close)
                handle(sample("incident.json"), store, LocalEventBus())
                with tracing.start_span("worker.process", topic="planner.request"):
                    pass
            with tracing.start_span("other.root"):
                pass
        return stream.getvalue()

    def test_tree_and_waterfall_from_logs_alone(self):
        text = "not json\n" + self.records() + '{"event": 1}\n'
        traces = trace_view.build(trace_view.read_lines(text.splitlines()))
        self.assertEqual(len(traces), 2)
        main = next(t for t in traces.values() if len(t["spans"]) == 3)
        (root,) = main["roots"]
        self.assertEqual(root["span_name"], "observer.ingest")
        self.assertEqual(sorted(c["span_name"] for c in root["children"]), ["coordinator.record", "worker.process"])
        self.assertEqual([t["to_state"] for t in main["transitions"]], ["OPEN"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.log"
            path.write_text(text, encoding="utf-8")
            out = io.StringIO()
            self.assertEqual(trace_view.main([str(path), "--width", "10"], stdout=out), 0)
        output = out.getvalue()
        self.assertIn("observer.ingest", output)
        self.assertIn("  coordinator.record", output)
        self.assertIn("> - -> OPEN (seq 1)", output)
        self.assertIn("topic=planner.request", output)

    def test_missing_parents_are_roots_and_empty_input_fails(self):
        record = {"ts": "2026-11-12T00:00:00.000Z", "event": "span.end", "trace_id": "a" * 32,
                  "span_id": "b" * 16, "parent_span_id": "c" * 16, "span_name": "worker.result",
                  "duration_ms": 5, "status": "error"}
        (trace,) = trace_view.build([record]).values()
        self.assertTrue(trace["roots"][0]["missing_parent"])
        lines_out = trace_view.render("a" * 32, trace, width=0)
        self.assertIn("parent not in these logs", lines_out[1])
        self.assertIn("status=error", lines_out[1])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.log"
            path.write_text("plain text\n", encoding="utf-8")
            out = io.StringIO()
            self.assertEqual(trace_view.main([str(path)], stdout=out), 1)

    def test_journal_rows_are_listed_per_trace_without_migrating(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coordinator.db"
            with SQLiteStore(path) as store:
                with tracing.start_span("observer.ingest") as root:
                    handle(sample("incident.json"), store, LocalEventBus())
                handle(sample("proposal.json"), store, LocalEventBus())
            rows = trace_view.journal(path)
            (entry,) = rows[root.context.trace_id]
            self.assertEqual([t["to_state"] for t in entry["transitions"]], ["OPEN", "VALIDATING"])
            old = Path(directory) / "old.db"
            db = sqlite3.connect(old, isolation_level=None)
            migrate(db, MIGRATIONS[:2])
            db.close()
            self.assertEqual(trace_view.journal(old), {})
            db = sqlite3.connect(old)
            self.assertEqual(schema_version(db), 2)  # Read only: never migrated.
            db.close()


if __name__ == "__main__":
    unittest.main()
