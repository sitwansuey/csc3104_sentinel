import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shared.contracts import ContractError, validate_message


class ContractBoundaryTests(unittest.TestCase):
    def proposal(self):
        return json.loads((ROOT / "data/sentinel/samples/messages/proposal.json").read_text())

    def test_all_bundled_messages_are_compatible(self):
        for path in (ROOT / "data/sentinel/samples/messages").glob("*.json"):
            with self.subTest(path=path.name):
                validate_message(json.loads(path.read_text()))

    def test_arbitrary_command_action_is_rejected(self):
        message = self.proposal()
        message["action"] = "SHELL"
        message["parameters"] = {"command": "kubectl delete namespace default"}
        with self.assertRaises(ContractError):
            validate_message(message)

    def test_extra_command_cannot_be_hidden_in_valid_action(self):
        message = self.proposal()
        message["parameters"]["command"] = "unexpected executable content"
        with self.assertRaises(ContractError):
            validate_message(message)

    def test_execution_status_vocabulary_includes_superseded_only(self):
        message = json.loads((ROOT / "data/sentinel/samples/messages/execution-result.json").read_text())
        for status in ("APPLIED", "NOOP", "FAILED", "CONFLICT", "SUPERSEDED", "NEEDS_RECONCILIATION"):
            with self.subTest(status=status):
                validate_message(dict(message, status=status))
        for status in ("superseded", "OVERWRITTEN", "", None):
            with self.subTest(status=status), self.assertRaises(ContractError):
                validate_message(dict(message, status=status))

    def test_missing_uid_is_rejected(self):
        message = self.proposal()
        del message["target"]["uid"]
        with self.assertRaises(ContractError):
            validate_message(message)

    def test_resource_versions_remain_opaque_strings(self):
        message = self.proposal()
        message["based_on_resource_version"] = "opaque-version-token"
        validate_message(message)
        message["based_on_resource_version"] = 102
        with self.assertRaises(ContractError):
            validate_message(message)

    def test_scale_names_its_baseline_template_and_a_bounded_replica_count(self):
        # P5.07: SCALE restores a reviewed count, so zero and counts above 10 are refused.
        message = self.proposal()
        message["action"] = "SCALE"
        bound = {"baseline_id": "BASE-1", "template_sha256": "a" * 64}
        for invalid in [-1, 0, 11, True, 1.5, "3"]:
            with self.subTest(value=invalid), self.assertRaises(ContractError):
                bad = copy.deepcopy(message)
                bad["parameters"] = {**bound, "desired_replicas": invalid}
                validate_message(bad)
        for incomplete in ({"desired_replicas": 3}, {"baseline_id": "BASE-1", "desired_replicas": 3},
                           {**bound, "desired_replicas": 3, "replicas": 3},
                           {**bound, "template_sha256": "not-a-digest", "desired_replicas": 3}):
            with self.subTest(parameters=incomplete), self.assertRaises(ContractError):
                validate_message({**message, "parameters": incomplete})
        message["parameters"] = {**bound, "desired_replicas": 3}
        validate_message(message)
        message["parameters"]["desired_replicas"] = 10
        validate_message(message)

    def test_invalid_template_digest_is_rejected(self):
        message = self.proposal()
        message["parameters"]["template_sha256"] = "not-a-template-hash"
        with self.assertRaises(ContractError):
            validate_message(message)

    def test_unknown_contract_version_is_rejected(self):
        message = self.proposal()
        message["schema_version"] = "2.0"
        with self.assertRaises(ContractError):
            validate_message(message)

    def test_outcome_window_cannot_run_backwards(self):
        message = json.loads((ROOT / "data/sentinel/samples/messages/outcome.json").read_text())
        message["window_end"] = "2026-10-10T12:00:00Z"
        with self.assertRaises(ContractError):
            validate_message(message)

    def test_naive_observation_time_is_rejected(self):
        message = json.loads((ROOT / "data/sentinel/samples/messages/incident.json").read_text())
        message["observed_at"] = "2026-10-10T12:00:30"
        with self.assertRaises(ContractError):
            validate_message(message)


if __name__ == "__main__":
    unittest.main()
