"""Offline retrieval boundaries: reject excess context without dropping evidence."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from services.planner.core import _retrieve, mock_provider, propose, retrieve_runbooks
from shared.contracts import ContractError, validate_message

ROOT = Path(__file__).resolve().parents[1]


class RetrievalBoundsTests(unittest.TestCase):
    def setUp(self):
        self.incident = json.loads((ROOT / "data/sentinel/samples/messages/incident.json").read_text())
        self.baseline = json.loads((ROOT / "data/sentinel/samples/baseline.json").read_text())
        self.diagnosis = {"incident_id": self.incident["incident_id"],
                          "prediction": {"1": {"component": "demo-api", "reason": "startup failure"}}}

        def provider(context):
            answer = mock_provider(context)
            if "diagnosis" in context:
                del answer["evidence_refs"]
            return answer

        self.provider = Mock(side_effect=provider)

    def propose(self, runbooks, diagnosis=None):
        return propose(self.incident, [self.baseline], lambda _: runbooks, self.provider, diagnosis=diagnosis)

    def test_count_cap_accepts_ten_and_rejects_eleven_without_calling_provider(self):
        for mode in ("offline", "rca-list", "rca-structured"):
            with self.subTest(mode=mode):
                diagnosis = None if mode == "offline" else self.diagnosis
                passages = [{"id": f"runbook-{number}", "content": "Recovery guidance"} for number in range(10)]
                runbooks = {"kp": passages} if mode == "rca-structured" else passages
                self.assertEqual(self.propose(runbooks, diagnosis)["action"], "ROLLBACK")
                self.assertEqual(self.provider.call_args.args[0]["runbooks"], runbooks)
                self.provider.reset_mock()
                passages.append({"id": "runbook-10", "content": "Recovery guidance"})
                with self.assertRaisesRegex(ContractError, "bounded"):
                    self.propose(runbooks, diagnosis)
                self.provider.assert_not_called()

    def test_passage_size_cap_preserves_content_at_the_limit_and_rejects_excess(self):
        for size in (1, 20000, 0, 20001):
            with self.subTest(size=size):
                runbooks = [{"id": "failed-rollout", "content": "x" * size}]
                self.provider.reset_mock()
                if size in (1, 20000):
                    self.assertEqual(self.propose(runbooks)["action"], "ROLLBACK")
                    self.assertEqual(self.provider.call_args.args[0]["runbooks"], runbooks)
                else:
                    with self.assertRaisesRegex(ContractError, "bounded"):
                        self.propose(runbooks)
                    self.provider.assert_not_called()

    def test_rca_knowledge_cap_counts_the_complete_json_encoding(self):
        for structured in (False, True):
            with self.subTest(structured=structured):
                payload = {"content": ""}
                runbooks = ({"kp": [{"id": "wf-startup"}], "kr": [], "kt": payload} if structured
                            else [{"id": "wf-startup", **payload}])
                if not structured:
                    payload = runbooks[0]
                # Non-ASCII text expands in the default JSON encoding. Rules and
                # tool metadata count too, even when the workflow itself is tiny.
                repeats, remainder = divmod(200000 - len(json.dumps(runbooks)), 6)
                payload["content"] = "\u00e9" * repeats + "x" * remainder
                self.assertEqual(len(json.dumps(runbooks)), 200000)
                self.assertEqual(_retrieve(self.incident, Mock(return_value=runbooks), self.diagnosis), runbooks)
                payload["content"] += "x"
                self.assertEqual(len(json.dumps(runbooks)), 200001)
                self.provider.reset_mock()
                with self.assertRaisesRegex(ContractError, "context limit"):
                    self.propose(runbooks, self.diagnosis)
                self.provider.assert_not_called()

    def test_empty_or_missing_local_corpus_escalates_without_calling_provider(self):
        for directory_exists in (True, False):
            with self.subTest(directory_exists=directory_exists), tempfile.TemporaryDirectory(dir=ROOT) as temporary:
                root = Path(temporary)
                if directory_exists:
                    (root / "runbooks").mkdir()
                with patch("services.planner.core.__file__", str(root / "services/planner/core.py")):
                    self.assertEqual(retrieve_runbooks(self.incident), [])
                    for diagnosis in (None, self.diagnosis):
                        retriever = (retrieve_runbooks if diagnosis is None
                                     else lambda _: {"kp": retrieve_runbooks(self.incident)})
                        result = propose(self.incident, [self.baseline], retriever, self.provider, diagnosis=diagnosis)
                        validate_message(result)
                        self.assertEqual((result["action"], result["parameters"]), ("ESCALATE", {}))
                        self.assertIn("relevant runbook", result["rationale"])
                        self.assertEqual(result["target"], self.incident["target"])
                        self.assertEqual(result["evidence_refs"], [s["evidence_id"] for s in self.incident["signals"]])
                self.provider.assert_not_called()

    def test_missing_selected_file_does_not_leave_partial_guidance(self):
        self.incident["signals"][-1]["reason"] = "OOMKilled"
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            (root / "runbooks").mkdir()
            (root / "runbooks/failed-rollout.md").write_text("Recovery guidance", encoding="utf-8")
            with patch("services.planner.core.__file__", str(root / "services/planner/core.py")):
                result = propose(self.incident, [self.baseline], retrieve_runbooks, self.provider)
            self.assertEqual(result["action"], "ESCALATE")
            self.provider.assert_not_called()

    def test_total_context_limit_preserves_identity_and_evidence_or_rejects(self):
        for diagnosis in (None, self.diagnosis):
            with self.subTest(rca=diagnosis is not None):
                incident = deepcopy(self.incident)
                runbooks = [{"id": "failed-rollout", "content": "Recovery guidance"}]
                context = {"incident": incident, "baselines": [self.baseline], "runbooks": runbooks}
                if diagnosis is not None:
                    context["diagnosis"] = diagnosis
                # Put decisive evidence at the tail, where a prefix cut loses it.
                signal = incident["signals"][-1]
                signal["detail"] = "x" * (200000 - len(json.dumps(context))) + signal["detail"]
                self.assertEqual(len(json.dumps(context)), 200000)
                retriever = Mock(return_value=runbooks)
                result = propose(incident, [self.baseline], retriever, self.provider, diagnosis=diagnosis)
                self.assertEqual(result["action"], "ROLLBACK")
                self.assertEqual(self.provider.call_args.args[0], context)
                self.assertEqual(result["target"], incident["target"])
                self.assertEqual(result["evidence_refs"], [s["evidence_id"] for s in incident["signals"]])
                signal["detail"] += "x"
                self.assertEqual(len(json.dumps(context)), 200001)
                before = deepcopy(context)
                self.provider.reset_mock()
                with self.assertRaisesRegex(ContractError, "context.*200000"):
                    propose(incident, [self.baseline], retriever, self.provider, diagnosis=diagnosis)
                self.provider.assert_not_called()
                self.assertEqual(context, before)

    def test_non_json_context_is_rejected_without_calling_provider(self):
        for invalid in (object(), float("nan")):
            with self.subTest(invalid=invalid):
                diagnosis = {**self.diagnosis, "report": invalid}
                with self.assertRaisesRegex(ContractError, "context must be JSON"):
                    self.propose([{"id": "failed-rollout", "content": "Recovery guidance"}], diagnosis)
                self.provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
