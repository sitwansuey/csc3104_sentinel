"""Transport failures use a bounded retry and release response resources.

No Ollama server or model is used; model accuracy is outside these checks.
"""
import io
import json
import threading
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from agent.llm import Ollama


class ModelTransportTests(unittest.TestCase):
    def model(self):
        return Ollama({"base_url": "http://127.0.0.1:11434", "model": "fixture-model", "timeout": 3})

    def test_http_errors_close_each_response_before_retry_or_failure(self):
        streams = [io.BytesIO(b"unavailable"), io.BytesIO(b"unavailable")]
        errors = [HTTPError("http://127.0.0.1:11434/api/chat", 503, "Unavailable", {}, stream)
                  for stream in streams]
        with patch("agent.llm.urllib.request.urlopen", side_effect=errors) as request, \
                patch("agent.llm.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                self.model().ask("Recovery Planner", "fixture", {"type": "object"})
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(stream.closed for stream in streams))

    def test_timeouts_exhaust_the_bound_without_inventing_a_decision(self):
        model = self.model()
        with patch("agent.llm.urllib.request.urlopen", side_effect=TimeoutError("fixture timeout")) as request, \
                patch("agent.llm.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "failed"):
                model.ask("Recovery Planner", "fixture", {"type": "object"})
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.kwargs["timeout"], 3)
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
        class SlowResponse(io.BytesIO):
            def read(self, *args):
                if not heartbeat.wait(2):
                    raise AssertionError("No progress while waiting for the model")
                return super().read(*args)
        response = SlowResponse(json.dumps({"message": {"content": '{"action":"ESCALATE"}'},
            "prompt_eval_count": 321, "eval_count": 12, "load_duration": 2000000000,
            "prompt_eval_duration": 1000000000, "eval_duration": 3000000000}).encode())
        model = Ollama({"base_url": "http://fixture", "model": "test-model",
                        "progress_interval_seconds": .01}, progress=progress)
        with patch("agent.llm.urllib.request.urlopen", return_value=response):
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
        model = Ollama({"base_url": "http://fixture", "model": "test-model"}, progress=broken)
        with patch("agent.llm.urllib.request.urlopen", return_value=io.BytesIO(
                b'{"message":{"content":"{}"}}')):
            self.assertEqual(model.ask("Analyzer", "fixture", {}), {})

    def test_truncated_deterministic_output_fails_without_another_full_inference(self):
        model = self.model()
        with patch("agent.llm.urllib.request.urlopen", return_value=io.BytesIO(
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
        with patch("agent.llm.urllib.request.urlopen", side_effect=[error]) as request, \
                patch("agent.llm.time.sleep") as sleep:
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
            with patch("agent.llm.urllib.request.urlopen", side_effect=errors) as request, \
                    patch("agent.llm.time.sleep"):
                with self.assertRaisesRegex(RuntimeError, f"HTTP {code} Busy: busy"):
                    self.model().ask("Planner", "fixture", {"type": "object"})
            self.assertEqual(request.call_count, 2, code)

    def test_unreachable_endpoint_is_named_without_credentials(self):
        model = Ollama({"base_url": "http://user:secret@127.0.0.1:9", "model": "fixture-model", "timeout": 3})
        refused = URLError(ConnectionRefusedError("connection refused"))
        with patch("agent.llm.urllib.request.urlopen", side_effect=[refused, refused]), \
                patch("agent.llm.time.sleep"):
            with self.assertRaises(RuntimeError) as failure:
                model.ask("Planner", "fixture", {"type": "object"})
        self.assertIn("at 127.0.0.1:9:", str(failure.exception))
        self.assertNotIn("secret", str(failure.exception))
