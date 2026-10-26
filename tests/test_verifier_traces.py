from copy import deepcopy
from datetime import datetime, timezone
import unittest

from services.verifier_traces import trace_edges

NOW = datetime(2026, 10, 26, 0, 0, 0, tzinfo=timezone.utc)


def export():
    resources = []
    for alias, span_id, parent, kind in (("caller-otel", "1" * 16, None, 3),
                                         ("callee-otel", "2" * 16, "1" * 16, 2)):
        span = {"traceId": "a" * 32, "spanId": span_id, "kind": kind,
                "endTimeUnixNano": str(int(NOW.timestamp()) * 1_000_000_000)}
        if parent:
            span["parentSpanId"] = parent
        resources.append({"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": alias}}]},
                          "scopeSpans": [{"spans": [span]}]})
    return {"resourceSpans": resources}


class TraceEvidenceTests(unittest.TestCase):
    def parse(self, payload):
        return trace_edges(payload, {"caller-otel": "checkout", "callee-otel": "payment"}, now=NOW)

    def test_parent_span_produces_edge_and_provenance(self):
        result = self.parse(export())
        self.assertEqual(result["edges"], [{"from": "checkout", "to": "payment"}])
        self.assertEqual(result["unknowns"], [])
        self.assertEqual(result["evidence"][0]["source"], "otlp_parent_span")

    def test_missing_parent_is_incomplete_not_a_proven_empty_graph(self):
        payload = export()
        payload["resourceSpans"] = payload["resourceSpans"][1:]
        result = self.parse(payload)
        self.assertEqual(result["edges"], [])
        self.assertIn("Missing parent span for server/consumer", result["unknowns"])

    def test_stale_future_and_malformed_spans_are_unknown(self):
        for time in (str(int(NOW.timestamp() - 301) * 1_000_000_000),
                     str(int(NOW.timestamp() + 1) * 1_000_000_000), None):
            payload = export()
            payload["resourceSpans"][1]["scopeSpans"][0]["spans"][0]["endTimeUnixNano"] = time
            self.assertTrue(self.parse(payload)["unknowns"])
            self.assertEqual(self.parse(payload)["edges"], [])

    def test_trace_identity_prevents_joining_unrelated_requests(self):
        payload = export()
        payload["resourceSpans"][1]["scopeSpans"][0]["spans"][0]["traceId"] = "b" * 32
        self.assertEqual(self.parse(payload)["edges"], [])

    def test_empty_export_is_unknown(self):
        self.assertTrue(self.parse({"resourceSpans": []})["unknowns"])

    def test_unmapped_service_and_namespace_mismatch_are_unknown(self):
        payload = export()
        payload["resourceSpans"][0]["resource"]["attributes"][0]["value"]["stringValue"] = "unmapped"
        self.assertTrue(self.parse(payload)["unknowns"])
        payload = export()
        payload["resourceSpans"][0]["resource"]["attributes"].append(
            {"key": "k8s.namespace.name", "value": {"stringValue": "wrong"}})
        result = trace_edges(payload, {"caller-otel": "checkout", "callee-otel": "payment"},
                             now=NOW, namespaces={"caller-otel": "expected"})
        self.assertTrue(result["unknowns"])

    def test_conflicting_span_identity_is_not_used(self):
        payload = export()
        duplicate = deepcopy(payload["resourceSpans"][0])
        duplicate["scopeSpans"][0]["spans"][0]["kind"] = 1
        payload["resourceSpans"].append(duplicate)
        self.assertTrue(self.parse(payload)["unknowns"])
        self.assertEqual(self.parse(payload)["edges"], [])

    def test_invalid_top_level_and_clock_raise(self):
        with self.assertRaises(ValueError):
            self.parse({})
        with self.assertRaises(ValueError):
            trace_edges(export(), {}, now=datetime(2026, 10, 26))
