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

    def test_scale_uses_explicit_nonnegative_integer(self):
        message = self.proposal()
        message["action"] = "SCALE"
        for invalid in [-1, True, 1.5, "3"]:
            with self.subTest(value=invalid), self.assertRaises(ContractError):
                bad = copy.deepcopy(message)
                bad["parameters"] = {"desired_replicas": invalid}
                validate_message(bad)
        message["parameters"] = {"desired_replicas": 3}
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
