"""Native toolkit integration and bounded model investigation behavior."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from agent.datasets.casetoolkit import gather_evidence, investigate, prepare_view


@unittest.skipUnless(importlib.util.find_spec("RCAEval") and importlib.util.find_spec("pyarrow"),
                     "optional CaseToolkit dependencies are not installed")
class ToolkitTests(unittest.TestCase):
    def test_native_view_tools_and_validated_model_submission(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        from RCAEval.agent.toolkit import CaseToolkit
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pq.write_table(pa.table({"time": [998, 999, 1000, 1001],
                                    "cartservice_cpu": [1., 2., 50., 60.]}), root / "metrics.parquet")
            task = {"case_id": "case-blind", "dataset": "RE2-OB", "system": "Online Boutique",
                    "inject_time": 1000, "standard_window": {"start": 990, "end": 1010},
                    "candidate_services": ["cartservice"], "candidate_indicators": ["cartservice_cpu"],
                    "files": {"metrics": "metrics.parquet"}}
            view = prepare_view(root, task, root / "view")
            toolkit = CaseToolkit(str(view))
            self.assertIn("error", toolkit.call("search_logs", {}))
            result = toolkit.call("get_metric", {"name": "cartservice_cpu"})
            self.assertEqual(result["after_inject"]["mean"], 55.)
            llm = Mock(calls=[])
            llm.ask.side_effect = [
                {"tool": "submit", "arguments": json.dumps({"ranks": ["invented"]})},
                {"tool": "submit", "arguments": json.dumps({"ranks": ["cartservice_cpu"]})}]
            self.assertEqual(investigate(toolkit, llm, root, 2), ["cartservice_cpu"])
            self.assertFalse(toolkit.calls[-2]["ok"])
            self.assertTrue(toolkit.calls[-1]["ok"])
            self.assertTrue((root / "evidence-review.json").exists())
            calls = [c["tool"] for c in toolkit.calls]
            self.assertIn("get_metric", calls[calls.index("rank_anomalies"):calls.index("submit")])
            self.assertIn("compare at least two plausible hypotheses", llm.ask.call_args.args[1])
            self.assertEqual(json.loads((view / "task.json").read_text())["dataset"], "re2-ob")

    def test_budget_exhaustion_abstains_and_preserves_usage(self):
        toolkit = Mock(submission=None, calls=[])
        toolkit.call.return_value = {"error": "no logs"}
        llm = Mock(calls=[{"prompt_tokens": 42}])
        llm.ask.return_value = {"tool": "search_logs", "arguments": "{}"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(investigate(toolkit, llm, root, 1), [])
            self.assertEqual(llm.ask.call_count, 1)
            self.assertEqual(json.loads((root / "llm-calls.json").read_text()), llm.calls)

    def test_review_checks_alternative_indicators_and_available_modalities(self):
        def call(name, args):
            if name == "rank_anomalies":
                return {"ranked": [{"name": "a_cpu"}, {"name": "a_mem"}, {"name": "b_latency"}]}
            if name == "list_metrics":
                return {"metrics": [args["service"] + "_" + i for i in ["cpu", "mem", "socket", "latency"]]}
            return {"observed": True}
        record = Mock(side_effect=call)
        evidence = gather_evidence(record, {"telemetry": {"metrics": "", "logs": "", "traces": ""}})
        self.assertEqual([s["service"] for s in evidence["services"]], ["a", "b"])
        self.assertEqual(sum(c.args[0] == "get_metric" for c in record.call_args_list), 8)
        self.assertTrue({"logs", "traces", "call_graph"} <= evidence.keys())
        record.reset_mock()
        evidence = gather_evidence(record, {"telemetry": {"metrics": ""}})
        self.assertFalse({"logs", "traces", "call_graph"} & evidence.keys())


if __name__ == "__main__":
    unittest.main()
