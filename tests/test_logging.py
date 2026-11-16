"""Structured logging: format, allowlist, redaction, context and commit safety."""
from contextlib import redirect_stderr
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from services.coordinator.core import LocalEventBus, SQLiteStore, handle
from shared import log

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data/sentinel/samples/messages"


def sample(name):
    return json.loads((SAMPLES / name).read_text())


def lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


class LoggingTestCase(unittest.TestCase):
    def capture(self, level="DEBUG", fmt="json"):
        stream = io.StringIO()
        scope = log.configured("test", level=level, fmt=fmt, stream=stream)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        return stream


class FormatTests(LoggingTestCase):
    def test_each_record_is_exactly_one_json_line_with_base_fields(self):
        stream = self.capture()
        log.emit("demo", "demo.started", reason="line one\nline two\x1b[31m", attempt=2)
        log.emit("demo", "demo.finished", level=log.WARNING, duration_ms=1.23456)
        text = stream.getvalue()
        self.assertEqual(text.count("\n"), 2)
        first, second = lines(stream)
        self.assertEqual(first["event"], "demo.started")
        self.assertEqual(first["reason"], "line one\nline two\x1b[31m")  # Escaped on the wire.
        self.assertNotIn("\x1b", text)
        self.assertEqual(first["attempt"], 2)
        for key in ("ts", "mono", "level", "component", "logger", "event"):
            self.assertIn(key, first)
        self.assertTrue(first["ts"].endswith("Z"))
        self.assertEqual((first["component"], first["logger"], first["level"]), ("test", "sentinel.demo", "info"))
        self.assertEqual((second["level"], second["duration_ms"]), ("warning", 1.235))

    def test_allowlist_drops_unknown_keys_structured_values_and_reserved_names(self):
        stream = self.capture()
        log.emit("demo", "demo.event", action_id="ACT-1", unknown="x", nested={"action_id": "inner"},
                 items=["a"], level_name="x", component_override="x", attempt=float("nan"))
        (record,) = lines(stream)
        self.assertEqual(set(record) - {"ts", "mono", "level", "component", "logger", "event"}, {"action_id"})

    def test_allowlist_contains_no_secret_like_names(self):
        self.assertFalse([name for name in log.FIELDS if log.SECRET_KEY.search(name)])

    def test_tokens_authorization_and_payloads_are_never_printed(self):
        stream = self.capture()
        secret = "S3CRET-" + "x" * 40
        log.emit("demo", "demo.event", token=secret, lease_token=secret, authorization="Bearer " + secret,
                 Authorization="Bearer " + secret, headers={"Authorization": secret}, payload={"v": secret},
                 body=secret, approval_body=secret, password=secret, message={"text": secret})
        logger = log.get_logger("demo")
        logger.warning("Authorization: Bearer %s", secret)  # Not made by emit(): message is dropped.
        try:
            raise RuntimeError("leaked " + secret)
        except RuntimeError:
            logger.exception("failed with %s", secret)
        text = stream.getvalue()
        self.assertNotIn(secret, text)
        self.assertNotIn("Bearer", text)
        records = lines(stream)
        self.assertEqual([r["event"] for r in records], ["demo.event", "log.unstructured", "log.unstructured"])
        self.assertEqual(records[2]["error_type"], "RuntimeError")
        self.assertNotIn("Traceback", text)

    def test_long_values_are_truncated(self):
        stream = self.capture()
        log.emit("demo", "demo.event", reason="r" * 1000)
        self.assertEqual(len(lines(stream)[0]["reason"]), log.MAX_VALUE + 3)

    def test_text_format_is_one_escaped_line(self):
        stream = self.capture(fmt="text")
        log.emit("demo", "demo.event", action_id="ACT-1", reason="two\nlines", token="secret-value")
        text = stream.getvalue()
        self.assertEqual(text.count("\n"), 1)
        self.assertIn(" info test demo.event action_id=ACT-1 reason=\"two\\nlines\"", text)
        self.assertNotIn("secret-value", text)

    def test_event_names_are_validated(self):
        for name in ("", "Bad.Name", "with space", "trailing.", "a..b"):
            with self.assertRaises(ValueError):
                log.emit("demo", name)

    def test_level_filters_records(self):
        stream = self.capture(level="WARNING")
        log.emit("demo", "demo.info")
        log.emit("demo", "demo.warning", level=log.WARNING)
        self.assertEqual([r["event"] for r in lines(stream)], ["demo.warning"])


class ConfigureTests(LoggingTestCase):
    def test_nothing_is_emitted_before_configure_in_a_fresh_process(self):
        code = ("import logging; from shared import log; import runtime.worker_http, runtime.coordinator_server, "
                "runtime.worker_service; log.emit('demo', 'demo.error', level=log.ERROR, action_id='A'); "
                "log.get_logger('demo').error('plain')")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                                timeout=60, env={**os.environ, "SENTINEL_LOG_LEVEL": "DEBUG"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((result.stdout, result.stderr), ("", ""))

    def test_reset_makes_logging_silent_again(self):
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            log.configure("test")
            log.reset()
            log.emit("demo", "demo.error", level=log.ERROR)
        self.assertEqual(buffer.getvalue(), "")

    def test_default_handler_follows_redirected_stderr(self):
        buffer = io.StringIO()
        with log.configured("test"), redirect_stderr(buffer):
            log.emit("demo", "demo.event")
        self.assertEqual(lines(buffer)[0]["event"], "demo.event")

    def test_environment_selects_format_and_level(self):
        stream = io.StringIO()
        with patch.dict(os.environ, {"SENTINEL_LOG_FORMAT": "text", "SENTINEL_LOG_LEVEL": "warning"}):
            with log.configured("test", stream=stream):
                log.emit("demo", "demo.info")
                log.emit("demo", "demo.warning", level=log.WARNING)
        self.assertEqual(len(stream.getvalue().splitlines()), 1)
        self.assertIn("warning test demo.warning", stream.getvalue())

    def test_invalid_settings_are_refused(self):
        for env in ({"SENTINEL_LOG_FORMAT": "xml"}, {"SENTINEL_LOG_LEVEL": "LOUD"}):
            with patch.dict(os.environ, env), self.assertRaises(ValueError):
                log.configure("test")
        with self.assertRaises(ValueError):
            log.configure("Bad Component")

    def test_entrypoint_exits_2_on_invalid_settings_and_restores_after(self):
        calls = []
        main = log.entrypoint("test")(lambda: calls.append(logging.getLogger("sentinel").level) or 0)
        buffer = io.StringIO()
        with patch.dict(os.environ, {"SENTINEL_LOG_FORMAT": "xml"}), redirect_stderr(buffer):
            self.assertEqual(main(), 2)
        self.assertEqual(calls, [])
        self.assertIn("SENTINEL_LOG_FORMAT", buffer.getvalue())
        with patch.dict(os.environ, {"SENTINEL_LOG_LEVEL": "DEBUG"}):
            self.assertEqual(main(), 0)
        self.assertEqual(calls, [logging.DEBUG])
        self.assertEqual(logging.getLogger("sentinel").level, logging.NOTSET)

    def test_configured_restores_an_outer_configuration(self):
        outer, inner = io.StringIO(), io.StringIO()
        with log.configured("outer", stream=outer):
            with log.configured("inner", stream=inner):
                log.emit("demo", "demo.inner")
            log.emit("demo", "demo.outer")
        self.assertEqual([r["event"] for r in lines(inner)], ["demo.inner"])
        self.assertEqual([r["event"] for r in lines(outer)], ["demo.outer"])


class ContextTests(LoggingTestCase):
    def test_bound_ids_reach_nested_calls_and_context_threads_only(self):
        stream = self.capture()

        def work(name):
            log.emit("demo", "demo." + name)

        with log.bind(incident_id="INC-1", action_id="ACT-1", token="never"):
            work("nested")
            with log.bind(delivery_id="d1"):
                work("inner")
            inherited = log.context_thread(work, args=("thread",))
            plain = threading.Thread(target=work, args=("plain",))
            for thread in (inherited, plain):
                thread.start()
                thread.join()
        work("after")
        records = {r["event"]: r for r in lines(stream)}
        self.assertEqual(records["demo.nested"]["action_id"], "ACT-1")
        self.assertEqual(records["demo.inner"]["delivery_id"], "d1")
        self.assertEqual(records["demo.thread"]["incident_id"], "INC-1")
        self.assertNotIn("incident_id", records["demo.plain"])  # Plain threads need context_thread.
        self.assertNotIn("incident_id", records["demo.after"])
        self.assertNotIn("delivery_id", records["demo.nested"])
        self.assertNotIn("never", stream.getvalue())

    def test_explicit_fields_override_bound_ids(self):
        stream = self.capture()
        with log.bind(action_id="ACT-1"):
            log.emit("demo", "demo.event", action_id="ACT-2")
        self.assertEqual(lines(stream)[0]["action_id"], "ACT-2")


class CommitSafetyTests(LoggingTestCase):
    def transitions(self, stream):
        return [r for r in lines(stream) if r["event"] == "coordinator.state.transition"]

    def test_committed_change_logs_exactly_one_transition_after_commit(self):
        stream = self.capture()
        with SQLiteStore() as store:
            with store.transaction():
                store.add_incident("INC-1", sample("incident.json")["target"], "OPEN")
                self.assertEqual(self.transitions(stream), [])  # Not before COMMIT.
            (record,) = self.transitions(stream)
        self.assertEqual((record["incident_id"], record["to_state"], record["seq"]), ("INC-1", "OPEN", 1))
        self.assertNotIn("from_state", record)

    def test_rolled_back_transaction_emits_no_transition(self):
        stream = self.capture()
        with SQLiteStore() as store:
            handle(sample("incident.json"), store, LocalEventBus())
            before = stream.getvalue()
            with patch.object(store, "enqueue", side_effect=RuntimeError("injected")):
                with self.assertRaises(RuntimeError):
                    handle(sample("proposal.json"), store, LocalEventBus())
            self.assertEqual(stream.getvalue(), before)
            self.assertEqual(store.incident(sample("incident.json")["incident_id"])["state"], "OPEN")
            self.assertEqual([t["to_state"] for t in store.transitions()], ["OPEN"])
            handle(sample("proposal.json"), store, LocalEventBus())
        moves = [(r.get("from_state"), r["to_state"], r["cause"]) for r in self.transitions(stream)]
        self.assertEqual(moves, [(None, "OPEN", "incident"), ("OPEN", "VALIDATING", "proposal")])

    def test_replay_emits_only_a_debug_duplicate(self):
        stream = self.capture()
        with SQLiteStore() as store:
            handle(sample("incident.json"), store, LocalEventBus())
            start = len(lines(stream))
            self.assertFalse(handle(sample("incident.json"), store, LocalEventBus()))
        replay = lines(stream)[start:]
        self.assertEqual([(r["event"], r["level"]) for r in replay], [("coordinator.message.duplicate", "debug")])


if __name__ == "__main__":
    unittest.main()
