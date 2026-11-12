"""Transport failures use a bounded retry and release response resources.

No real Ollama server or model is used; model accuracy is outside these checks.
The first class stubs the opener. The second uses a loopback fake server from
tests/support/fake_ollama.py to check sizes, deadlines, redirects, proxies,
schema checks, the call budget, preflight, warm-up and runtime_info.
"""
import io
import json
import os
from pathlib import Path
import threading
import time
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from agent.agent import ANALYSIS, PLAN
from agent.jsonschema_lite import check_schema, validate
from agent.llm import Ollama
from runtime.remote_adapters import OllamaProvider
from tests.support.fake_ollama import DIGEST, FakeOllama, Reply, chat_reply

OPEN = "agent.llm.urllib.request.OpenerDirector.open"


def incident_context(number):
    """A Planner context for one numbered copy of the sample incident."""
    incident = json.loads((Path(__file__).resolve().parents[1] / "data/sentinel/samples/messages/incident.json")
                          .read_text(encoding="utf-8"))
    return {"incident": dict(incident, incident_id=f"INC-DEMO-{number:03d}"), "baselines": [], "runbooks": []}


RECOVERY = {"type": "object", "properties": {
    "action": {"type": "string", "enum": ["ROLLBACK", "ESCALATE", "NO_ACTION"]},
    "parameters": {"type": "object", "properties": {
        "baseline_id": {"type": "string"}, "template_sha256": {"type": "string"}},
        "additionalProperties": False},
    "rationale": {"type": "string", "maxLength": 2000}},
    "required": ["action", "parameters", "rationale"], "additionalProperties": False}


class Response(io.BytesIO):
    """A stand-in for an http.client response with headers."""

    def __init__(self, body, content_type="application/json"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}


class ModelTransportTests(unittest.TestCase):
    def model(self):
        return Ollama({"base_url": "http://127.0.0.1:11434", "model": "fixture-model", "timeout": 3})

    def test_http_errors_close_each_response_before_retry_or_failure(self):
        streams = [io.BytesIO(b"unavailable"), io.BytesIO(b"unavailable")]
        errors = [HTTPError("http://127.0.0.1:11434/api/chat", 503, "Unavailable", {}, stream)
                  for stream in streams]
        with patch(OPEN, side_effect=errors) as request, patch("agent.llm.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                self.model().ask("Recovery Planner", "fixture", {"type": "object"})
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(stream.closed for stream in streams))

    def test_timeouts_exhaust_the_bound_without_inventing_a_decision(self):
        model = self.model()
        with patch(OPEN, side_effect=TimeoutError("fixture timeout")) as request, \
                patch("agent.llm.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "failed"):
                model.ask("Recovery Planner", "fixture", {"type": "object"})
        self.assertEqual(request.call_count, 2)
        # The socket timeout never exceeds the configured timeout or the call deadline.
        self.assertAlmostEqual(request.call_args.kwargs["timeout"], 3, delta=.5)
        self.assertLessEqual(request.call_args.kwargs["timeout"], 3)
        sleep.assert_called_once_with(1)
        self.assertEqual([c["status"] for c in model.calls], ["failed", "failed"])
        self.assertTrue(all(c["error_type"] == "TimeoutError" for c in model.calls))

    def test_heartbeat_arrives_while_model_is_blocked_and_usage_includes_timings(self):
        updates = []
        heartbeat = threading.Event()
        def progress(update):
            updates.append(update)
            if update["status"] == "running":
                heartbeat.set()
        class SlowResponse(Response):
            def read1(self, *args):
                if not heartbeat.wait(2):
                    raise AssertionError("No progress while waiting for the model")
                return super().read1(*args)
        response = SlowResponse(json.dumps({"message": {"content": '{"action":"ESCALATE"}'},
            "prompt_eval_count": 321, "eval_count": 12, "load_duration": 2000000000,
            "prompt_eval_duration": 1000000000, "eval_duration": 3000000000}).encode())
        model = Ollama({"base_url": "http://127.0.0.1:11434", "model": "test-model",
                        "progress_interval_seconds": .01}, progress=progress)
        with patch(OPEN, return_value=response):
            result = model.ask("Analyzer", "PRIVATE TELEMETRY", {"type": "object"})
        self.assertEqual(result, {"action": "ESCALATE"})
        self.assertEqual(updates[0]["status"], "started")
        self.assertEqual(updates[-1]["status"], "completed")
        self.assertNotIn("PRIVATE TELEMETRY", json.dumps(updates))
        self.assertEqual(model.calls[0]["prompt_tokens"], 321)
        self.assertEqual(model.calls[0]["load_duration_seconds"], 2)
        self.assertEqual(model.calls[0]["eval_duration_seconds"], 3)
        self.assertTrue(response.closed)

    def test_progress_sink_failure_does_not_change_model_answer(self):
        def broken(update):
            raise OSError("Dashboard is offline")
        model = Ollama({"base_url": "http://127.0.0.1:11434", "model": "test-model"}, progress=broken)
        with patch(OPEN, return_value=Response(b'{"message":{"content":"{}"}}')):
            self.assertEqual(model.ask("Analyzer", "fixture", {}), {})

    def test_truncated_deterministic_output_fails_without_another_full_inference(self):
        model = self.model()
        with patch(OPEN, return_value=Response(
                b'{"done_reason":"length","message":{"content":"{}"}}')) as request, \
                patch("agent.llm.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "truncated"):
                model.ask("Analyzer", "fixture", {})
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(model.calls[0]["status"], "failed")

    def test_missing_model_reports_ollamas_reason_without_retry(self):
        body = io.BytesIO(b'{"error":"model \'fixture-model\' not found"}')
        error = HTTPError("http://127.0.0.1:11434/api/chat", 404, "Not Found", {}, body)
        with patch(OPEN, side_effect=[error]) as request, patch("agent.llm.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "fixture-model at 127.0.0.1:11434: HTTP 404 Not Found: "
                                                      "model 'fixture-model' not found"):
                self.model().ask("Planner", "fixture", {"type": "object"})
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()
        self.assertTrue(body.closed)

    def test_retried_http_errors_report_the_servers_reason(self):
        for code in (408, 429, 500):
            errors = [HTTPError("http://127.0.0.1:11434/api/chat", code, "Busy", {}, io.BytesIO(b"busy"))
                      for _ in range(2)]
            with patch(OPEN, side_effect=errors) as request, patch("agent.llm.time.sleep"):
                with self.assertRaisesRegex(RuntimeError, f"HTTP {code} Busy: busy"):
                    self.model().ask("Planner", "fixture", {"type": "object"})
            self.assertEqual(request.call_count, 2, code)

    def test_unreachable_endpoint_is_named(self):
        model = Ollama({"base_url": "http://127.0.0.1:9", "model": "fixture-model", "timeout": 3})
        refused = URLError(ConnectionRefusedError("connection refused"))
        with patch(OPEN, side_effect=[refused, refused]), patch("agent.llm.time.sleep"):
            with self.assertRaises(RuntimeError) as failure:
                model.ask("Planner", "fixture", {"type": "object"})
        self.assertIn("Ollama Planner failed for fixture-model at 127.0.0.1:9:", str(failure.exception))

    def test_connection_reset_while_reading_is_retried_then_reported(self):
        class Reset(Response):
            def read1(self, *args):
                raise ConnectionResetError("reset by peer")
        with patch(OPEN, side_effect=[Reset(b""), Reset(b"")]) as request, patch("agent.llm.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "reset by peer"):
                self.model().ask("Planner", "fixture", {"type": "object"})
        self.assertEqual(request.call_count, 2)


class FakeServerTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeOllama()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        self.host = self.server.url.split("//")[1]

    def model(self, **config):
        return Ollama({"base_url": self.server.url, "model": "fixture-model", "timeout": 10, **config})

    def chats(self):
        return self.server.paths("/api/chat")

    def assert_refused(self, model, pattern, schema=None, *, secret="SECRET"):
        with patch("agent.llm.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, pattern) as failure:
                model.ask("Recovery Planner", "fixture", RECOVERY if schema is None else schema)
        self.assertNotIn(secret, str(failure.exception))
        self.assertTrue(str(failure.exception).startswith("Ollama Recovery Planner failed for fixture-model at "
                                                          + self.host + ": "))
        return failure.exception

    # Response size

    def test_declared_oversize_body_is_refused_without_retry(self):
        self.server.chat = Reply(chat_reply({}, padding="x" * 131072))
        model = self.model(max_response_bytes=65536)
        self.assert_refused(model, "larger than 65536 bytes", {})
        self.assertEqual(len(self.chats()), 1)
        self.assertEqual(model.calls[0]["error_type"], "ModelResponseTooLarge")

    def test_undeclared_oversize_body_is_aborted_at_the_limit(self):
        self.server.chat = Reply(chat_reply({}, padding="x" * 131072), length=False)
        model = self.model(max_response_bytes=65536)
        self.assert_refused(model, "larger than 65536 bytes", {})
        self.assertEqual(len(self.chats()), 1)

    def test_body_at_the_limit_is_accepted(self):
        body = json.dumps(chat_reply({"action": "ESCALATE", "parameters": {}, "rationale": "r"}))
        self.server.chat = Reply(body.encode(), length=False)
        model = self.model(max_response_bytes=max(1024, len(body)))
        self.assertEqual(model.ask("Recovery Planner", "fixture", RECOVERY)["action"], "ESCALATE")

    # Deadline

    def test_slow_drip_body_hits_the_overall_deadline(self):
        # Each byte arrives well inside the 10 s socket timeout, so only an
        # overall deadline can stop this call.
        self.server.chat = Reply(b'{"message":{"content":"{}"}}', drip=.2)
        model = self.model(request_deadline_seconds=1)
        started = time.monotonic()
        self.assert_refused(model, "deadline", {})
        self.assertLess(time.monotonic() - started, 4.5)
        self.assertEqual(len(self.chats()), 2)  # A deadline is retried once.
        self.assertEqual([c["error_type"] for c in model.calls], ["TimeoutError", "TimeoutError"])

    def test_silent_server_is_bounded_by_the_deadline(self):
        self.server.chat = Reply(b'{"message":{"content":"{}"}}', drip=30)
        model = self.model(request_deadline_seconds=.5, timeout=30)
        started = time.monotonic()
        self.assert_refused(model, "timed out|deadline", {})
        self.assertLess(time.monotonic() - started, 3)

    def test_slow_drip_headers_hit_the_overall_deadline(self):
        # The status line and headers (over 300 bytes) arrive one byte every
        # 0.2 s, inside the 10 s socket timeout. The deadline must cover them too.
        self.server.chat = Reply(chat_reply({}), header_drip=.2)
        model = self.model(request_deadline_seconds=1)
        started = time.monotonic()
        self.assert_refused(model, "deadline", {})
        self.assertLess(time.monotonic() - started, 4.5)
        self.assertEqual(len(self.chats()), 2)
        self.assertEqual([c["error_type"] for c in model.calls], ["TimeoutError", "TimeoutError"])

    def test_socket_timeout_still_bounds_each_read_inside_a_longer_deadline(self):
        self.server.chat = Reply(b'{"message":{"content":"{}"}}', drip=30)
        model = self.model(timeout=.5, request_deadline_seconds=60)
        started = time.monotonic()
        self.assert_refused(model, "timed out", {})
        self.assertLess(time.monotonic() - started, 4.5)

    # Origin: no redirects, no environment proxies

    def test_redirect_is_refused_and_never_followed(self):
        with FakeOllama() as other:
            self.server.chat = Reply(b"", status=302, headers={"Location": other.url + "/api/chat"})
            self.assert_refused(self.model(), "HTTP 302", {})
            self.assertEqual(other.requests, [])
        self.assertEqual(len(self.chats()), 1)

    def test_environment_proxy_is_ignored(self):
        with FakeOllama() as proxy:
            environment = {k: v for k, v in os.environ.items() if k.lower() != "no_proxy"}
            environment.update(HTTP_PROXY=proxy.url, HTTPS_PROXY=proxy.url, ALL_PROXY=proxy.url)
            # urllib caches its default opener, and with it the proxy settings
            # read at first use. Clear it so this test sees the environment.
            with patch.dict(os.environ, environment, clear=True), patch("urllib.request._opener", None):
                self.assertEqual(self.model().ask("Analyzer", "PRIVATE TELEMETRY", {}), {})
            self.assertEqual(proxy.requests, [])
        self.assertEqual(len(self.chats()), 1)

    # Body and content checks

    def test_malformed_replies_are_refused_without_echoing_content(self):
        cases = [
            (Reply(b'{"message":{"content":"{}"},"SECRET":1}', content_type="text/html"), "not application/json"),
            (Reply(b"SECRET is not json"), "Ollama response is not valid JSON"),
            (Reply(b"[1, 2]"), "not a JSON object"),
            (Reply(chat_reply("SECRET is not json")), "Model output is not valid JSON"),
            (Reply(chat_reply("[" * 100000 + "]" * 100000)), "nested too deeply"),
            (Reply({"message": {"role": "assistant"}}), "no message content"),
            (Reply({"message": {"content": 5}}), "no message content"),
            (Reply({"message": "SECRET"}), "no message content"),
            (Reply(chat_reply('{"a":"' + "SECRET" * 50000 + '"}')), "longer than 262144 characters"),
            (Reply(chat_reply('["SECRET"]')), "Expected a JSON object"),
            (Reply(b'{"message":', length=200), "Ollama response is not valid JSON"),
        ]
        for reply, pattern in cases:
            with self.subTest(pattern=pattern):
                before = len(self.chats())
                self.server.chat = reply
                model = self.model(request_deadline_seconds=2)
                self.assert_refused(model, pattern, {})
                self.assertEqual(len(self.chats()) - before, 1)

    # Schema checks

    def test_schema_violations_are_refused_without_echoing_content(self):
        good = {"action": "ESCALATE", "parameters": {}, "rationale": "Evidence is insufficient."}
        cases = [
            ({**good, "SECRET": "x"}, RECOVERY, r"at \$: unexpected property"),
            ({**good, "action": "DELETE_SECRET"}, RECOVERY, r"at \$\.action: value is not one of"),
            ({k: v for k, v in good.items() if k != "rationale"}, RECOVERY,
             r"at \$\.rationale: required property is missing"),
            ({**good, "rationale": "SECRET" * 400}, RECOVERY, r"at \$\.rationale: text is longer than 2000"),
            ({**good, "parameters": {"baseline_id": 7}}, RECOVERY, r"at \$\.parameters\.baseline_id: expected string"),
            ({"plan": {"kind": "sequence", "goal": "g", "children": [{"kind": "SECRET", "goal": "x"}]}}, PLAN,
             r"at \$\.plan\.children\[0\]\.kind: value is not one of"),
            ({"plan": {"kind": "tool", "goal": "g", "tool": "logs", "args": {"SECRET": 5}}}, PLAN,
             r"at \$\.plan\.args\.\*: expected string"),
            ({"summary": "s", "complete": True, "confidence": 1.5, "next_steps": ""}, ANALYSIS,
             r"at \$\.confidence: number is above the maximum"),
            ({"summary": "s", "complete": True, "confidence": True, "next_steps": ""}, ANALYSIS,
             r"at \$\.confidence: expected number"),
        ]
        for result, schema, pattern in cases:
            with self.subTest(pattern=pattern):
                self.server.chat = Reply(chat_reply(result))
                model = self.model()
                self.assert_refused(model, pattern, schema)
                self.assertEqual(len(model.calls), 1)  # Deterministic output is not retried.
                self.assertEqual(model.calls[0]["error_type"], "ValueError")

    def test_valid_outputs_pass_the_schema_check(self):
        cases = [
            ({"action": "ROLLBACK", "parameters": {"baseline_id": "b", "template_sha256": "t"},
              "rationale": "Bad rollout."}, RECOVERY),
            ({"plan": {"kind": "parallel", "goal": "g", "children": [
                {"kind": "tool", "goal": "x", "tool": "logs", "args": {"file": "a.csv"}}]}}, PLAN),
            ({"summary": "s", "complete": False, "confidence": 0, "next_steps": "n"}, ANALYSIS),
        ]
        for result, schema in cases:
            self.server.chat = Reply(chat_reply(result))
            self.assertEqual(self.model().ask("Analyzer", "fixture", schema), result)

    def test_unsupported_schema_keyword_fails_closed_before_any_request(self):
        for schema in ({"type": "object", "pattern": "x"}, {"type": "object", "properties": {"a": {"oneOf": []}}},
                       {"$ref": "http://example.invalid/schema"}, {"type": "tuple"}):
            with self.subTest(schema=schema):
                with self.assertRaisesRegex(ValueError, "Unsupported|reference"):
                    self.model().ask("Planner", "fixture", schema)
        self.assertEqual(self.chats(), [])

    def test_reporter_correction_still_runs_after_a_schema_valid_but_invalid_report(self):
        from agent.agent import validate_report
        report = {"failures": [{"root cause component": "  "}], "explanation": "e", "limitations": "l"}
        schema = {"type": "object", "properties": {
            "failures": {"type": "array", "maxItems": 10, "items": {"type": "object", "properties": {
                "root cause component": {"type": "string"}}, "required": [], "additionalProperties": False}},
            "explanation": {"type": "string"}, "limitations": {"type": "string"}},
            "required": ["failures", "explanation", "limitations"], "additionalProperties": False}
        self.server.chat = Reply(chat_reply(report))
        returned = self.model().ask("Reporter", "fixture", schema)
        with self.assertRaisesRegex(ValueError, "invalid root cause fields"):
            validate_report(returned, None, None, True)

    # Configuration

    def test_bad_base_url_is_rejected_at_construction(self):
        for url in ("http://user:SECRET@127.0.0.1:11434", "http://:SECRET@127.0.0.1:11434",
                    "http://127.0.0.1:11434/?token=SECRET", "http://127.0.0.1:11434/api",
                    "http://127.0.0.1:11434/#SECRET", "ftp://127.0.0.1:11434", "127.0.0.1:11434",
                    "http://", "http://127.0.0.1:99999", "http://ollama.example:11434", None):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as failure:
                    Ollama({"base_url": url, "model": "fixture-model"})
                self.assertNotIn("SECRET", str(failure.exception))

    def test_remote_origins_need_https_or_an_explicit_opt_in(self):
        for config in ({"base_url": "https://ollama.example"}, {"base_url": "http://localhost:11434/"},
                       {"base_url": "http://[::1]:11434"},
                       {"base_url": "http://ollama.example:11434", "allow_insecure_remote": True}):
            with self.subTest(config=config):
                Ollama({"model": "fixture-model", **config})
        with self.assertRaisesRegex(ValueError, "allow_insecure_remote"):
            Ollama({"base_url": "http://ollama.example:11434", "model": "m", "allow_insecure_remote": "yes"})

    def test_invalid_limits_are_rejected(self):
        for key, value in (("max_response_bytes", 9 * 1024 * 1024), ("max_response_bytes", 10),
                           ("max_response_bytes", True), ("max_model_calls", 0), ("max_model_calls", 1.5),
                           ("request_deadline_seconds", 0), ("request_deadline_seconds", float("inf")),
                           ("warm_up_timeout_seconds", -1), ("timeout", "300"),
                           ("keep_alive", "forever"), ("keep_alive", 1.5), ("keep_alive", True)):
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(ValueError, key):
                    self.model(**{key: value})

    # Budget and keep_alive

    def test_max_model_calls_is_enforced(self):
        model = self.model(max_model_calls=2)
        self.assertEqual(model.ask("Planner", "fixture", {}), {})
        self.assertEqual(model.ask("Analyzer", "fixture", {}), {})
        with self.assertRaisesRegex(RuntimeError, "Ollama Reporter failed for fixture-model at " + self.host
                                    + ": max_model_calls budget of 2 is exhausted"):
            model.ask("Reporter", "fixture", {})
        self.assertEqual(len(self.chats()), 2)
        self.assertEqual(len(model.calls), 2)

    def test_retries_count_against_the_budget(self):
        self.server.chat = Reply({"error": "busy"}, status=500)
        model = self.model(max_model_calls=1)
        with patch("agent.llm.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                model.ask("Planner", "fixture", {})
        self.assertEqual(len(self.chats()), 1)
        sleep.assert_not_called()

    def test_reset_budget_starts_a_new_task(self):
        model = self.model(max_model_calls=1)
        self.assertEqual(model.ask("Planner", "fixture", {}), {})
        with self.assertRaisesRegex(RuntimeError, "budget of 1 is exhausted"):
            model.ask("Planner", "fixture", {})
        model.reset_budget()
        self.assertEqual(model.ask("Planner", "fixture", {}), {})
        self.assertEqual(len(self.chats()), 2)

    def test_long_lived_provider_plans_more_incidents_than_the_budget(self):
        # One planner worker keeps one provider for its whole life.
        self.server.chat = Reply(chat_reply({"action": "ESCALATE", "parameters": {},
                                             "rationale": "r", "evidence_refs": []}))
        provider = OllamaProvider({"base_url": self.server.url, "model": "fixture-model", "timeout": 5,
                                   "max_model_calls": 2})
        for incident in range(5):
            self.assertEqual(provider(incident_context(incident))["action"], "ESCALATE")
        self.assertEqual(len(self.chats()), 5)

    def test_long_lived_provider_still_bounds_each_incident(self):
        self.server.chat = Reply({"error": "busy"}, status=500)
        provider = OllamaProvider({"base_url": self.server.url, "model": "fixture-model", "timeout": 5,
                                   "max_model_calls": 1})
        with patch("agent.llm.time.sleep") as sleep:
            for incident in range(3):
                with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                    provider(incident_context(incident))
                self.assertEqual(len(provider.usage["calls"]), 1)
        # One request per incident: the budget of one stops the retry each time.
        self.assertEqual(len(self.chats()), 3)
        sleep.assert_not_called()

    def test_keep_alive_is_sent_only_when_configured(self):
        self.model().ask("Planner", "fixture", {})
        self.model(keep_alive="30m").ask("Planner", "fixture", {})
        self.model(keep_alive=600).ask("Planner", "fixture", {})
        bodies = [r["body"] for r in self.server.requests if r["path"] == "/api/chat"]
        self.assertNotIn("keep_alive", bodies[0])
        self.assertEqual([b.get("keep_alive") for b in bodies[1:]], ["30m", 600])

    # Preflight, warm-up and runtime_info

    def test_preflight_accepts_installed_models_including_the_implied_latest_tag(self):
        self.model().preflight()
        self.model(model="fixture-model:latest").preflight()
        self.assertEqual(self.chats(), [])

    def test_preflight_names_a_missing_model(self):
        model = self.model(role_models={"Recovery Planner": "fixture-other:7b"})
        with self.assertRaisesRegex(RuntimeError, "Ollama preflight failed for fixture-other:7b at "
                                    + self.host + ": model is not installed"):
            model.preflight()
        self.assertEqual(self.chats(), [])

    def test_preflight_names_an_unreachable_host(self):
        with FakeOllama() as closed:
            url = closed.url
        model = Ollama({"base_url": url, "model": "fixture-model", "timeout": 5})
        with self.assertRaisesRegex(RuntimeError, "Ollama preflight failed for fixture-model at "
                                    + url.split("//")[1] + ": "):
            model.preflight()

    def test_preflight_refuses_an_unexpected_tags_reply(self):
        self.server.routes["/api/tags"] = Reply({"models": "SECRET"})
        with self.assertRaisesRegex(RuntimeError, "unexpected /api/tags response"):
            self.model().preflight()

    def test_warm_up_loads_with_planning_settings_and_records_load_time(self):
        model = self.model(keep_alive="30m", num_ctx=8192)
        result = model.warm_up()
        self.assertEqual(result, [{"model": "fixture-model", "status": "completed",
                                   "seconds": result[0]["seconds"], "load_duration_seconds": .25}])
        body = self.server.requests[-1]["body"]
        self.assertEqual((body["keep_alive"], body["options"]["num_ctx"]), ("30m", 8192))
        self.assertEqual(model.calls, [])
        self.assertEqual(model.requests, 0)

    def test_warm_up_failures_are_clear(self):
        for reply, pattern in ((Reply({"error": "out of memory"}, status=500), "HTTP 500 .*out of memory"),
                               (Reply(chat_reply({"ready": "yes"})), r"at \$\.ready: expected boolean"),
                               (Reply(b'{"message":{"content":"{}"}}', drip=.3), "deadline")):
            with self.subTest(pattern=pattern):
                self.server.chat = reply
                with self.assertRaisesRegex(RuntimeError, "Ollama warm-up failed for fixture-model at "
                                            + self.host + ": .*" + pattern):
                    self.model(warm_up_timeout_seconds=1).warm_up()

    def test_runtime_info_records_identity_and_placement_only(self):
        info = self.model(keep_alive="30m").runtime_info()
        self.assertEqual(info["server_version"], "0.0.0-fixture")
        self.assertEqual(info["keep_alive"], "30m")
        entry = info["models"]["fixture-model"]
        self.assertEqual(entry["digest"], DIGEST)
        self.assertEqual((entry["quantization_level"], entry["parameter_size"], entry["family"]),
                         ("Q4_K_M", "1B", "fixture"))
        self.assertEqual(entry["trained_context_length"], 40960)
        self.assertEqual(entry["placement"]["gpu_fraction"], .75)
        self.assertEqual(entry["placement"]["processor"], "CPU/GPU split")
        self.assertEqual(entry["placement"]["context_length"], 16384)
        self.assertNotIn("license", json.dumps(info))
        self.assertNotIn("Prompt", json.dumps(info))

    def test_runtime_info_degrades_to_unavailable_and_never_raises(self):
        self.server.routes["/api/ps"] = Reply({"error": "not found"}, status=404)
        self.server.routes["/api/version"] = Reply(b"<html>", content_type="text/html")
        entry = self.model().runtime_info()
        self.assertEqual(entry["server_version"], "unavailable")
        self.assertEqual(entry["models"]["fixture-model"]["placement"], "unavailable")
        self.assertEqual(entry["models"]["fixture-model"]["digest"], DIGEST)
        self.server.routes["/api/ps"] = Reply({"models": []})
        self.assertEqual(self.model().runtime_info()["models"]["fixture-model"]["placement"], "not loaded")
        with FakeOllama() as closed:
            url = closed.url
        info = Ollama({"base_url": url, "model": "m", "timeout": 2}).runtime_info()
        self.assertEqual(info["models"]["m"], {
            "digest": "unavailable", "family": "unavailable", "parameter_size": "unavailable",
            "quantization_level": "unavailable", "trained_context_length": None, "placement": "unavailable"})


class SchemaCheckerTests(unittest.TestCase):
    def test_project_schemas_are_supported(self):
        from agent.agent import FINAL
        for schema in (PLAN, ANALYSIS, FINAL, RECOVERY):
            check_schema(schema)

    def test_deep_reference_chains_are_bounded(self):
        schema = {"$defs": {"n": {"type": "array", "items": {"$ref": "#/$defs/n"}}}, "$ref": "#/$defs/n"}
        value = []
        for _ in range(40):
            value = [value]
        with self.assertRaisesRegex(ValueError, "nested too deeply"):
            validate(value, schema)

    def test_integers_and_booleans_are_distinct(self):
        validate({"n": 2.0}, {"type": "object", "properties": {"n": {"type": "integer"}}})
        for value in (True, 2.5, "2"):
            with self.assertRaises(ValueError):
                validate({"n": value}, {"type": "object", "properties": {"n": {"type": "integer"}}})


if __name__ == "__main__":
    unittest.main()
