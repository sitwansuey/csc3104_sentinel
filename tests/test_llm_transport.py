"""Transport failures use a bounded retry and release response resources.

No Ollama server or model is used; model accuracy is outside these checks.
"""
import io
import unittest
from urllib.error import HTTPError
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
        self.assertEqual(model.calls, [])
