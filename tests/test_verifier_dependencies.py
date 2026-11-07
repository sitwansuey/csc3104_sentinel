import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from services.verifier.core import VerifierInputError, inspect_dependencies
from shared.contracts import validate_message

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)


def sample(name):
    return json.loads((ROOT / "data/sentinel/samples" / name).read_text())


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.proposal = sample("messages/proposal.json")
        self.graph = sample("dependencies.json")
        self.state = {}

    def inspect(self):
        result = inspect_dependencies(self.proposal, self.graph, self.state, now=NOW)
        validate_message(result)
        return result

    def test_sample_graph_is_clear_despite_target_being_unhealthy(self):
        result = self.inspect()
        self.assertEqual(result["status"], "CLEAR")
        self.assertEqual(result["affected_services"], ["demo-api", "mock-db"])
        self.assertEqual(result["checked_at"], "2026-10-10T12:00:40Z")

    def test_unhealthy_dependency_blocks(self):
        self.graph["known_health"]["mock-db"] = False
        self.assertEqual(self.inspect()["blockers"], ["mock-db"])
        self.assertEqual(self.inspect()["status"], "BLOCKED")

    def test_unknown_health_is_not_clear(self):
        for value in (None, "healthy", 1):
            with self.subTest(value=value):
                self.graph["known_health"]["mock-db"] = value
                self.assertEqual(self.inspect()["status"], "UNKNOWN")
        self.graph.pop("known_health")
        self.assertEqual(self.inspect()["status"], "UNKNOWN")

    def test_live_health_overrides_fixture_health(self):
        self.state["known_health"] = {"mock-db": False}
        self.assertEqual(self.inspect()["status"], "BLOCKED")
        self.state["known_health"] = {"mock-db": None}
        self.assertEqual(self.inspect()["status"], "UNKNOWN")

    def test_upstream_and_transitive_dependencies_are_checked(self):
        self.graph["nodes"] += ["checkout", "database"]
        self.graph["edges"] += [{"from": "checkout", "to": "demo-api"},
                                {"from": "mock-db", "to": "database"}]
        self.graph["known_health"].update(checkout=True, database=False)
        result = self.inspect()
        self.assertEqual(result["affected_services"], ["checkout", "database", "demo-api", "mock-db"])
        self.assertEqual(result["blockers"], ["database"])

    def test_cycles_self_edges_and_duplicate_edges_terminate(self):
        self.graph["edges"] += [{"from": "mock-db", "to": "demo-api"},
                                {"from": "demo-api", "to": "demo-api"},
                                {"from": "demo-api", "to": "mock-db"}]
        self.assertEqual(self.inspect()["status"], "CLEAR")
        self.assertEqual(len(self.inspect()["affected_services"]), 2)

    def test_disconnected_unhealthy_node_does_not_block(self):
        self.graph["nodes"].append("unrelated")
        self.graph["known_health"]["unrelated"] = False
        self.assertEqual(self.inspect()["status"], "CLEAR")

    def test_missing_target_and_dangling_edges_are_unknown(self):
        self.graph["nodes"].remove("demo-api")
        self.assertEqual(self.inspect()["status"], "UNKNOWN")
        self.graph = sample("dependencies.json")
        self.graph["edges"].append({"from": "demo-api", "to": "missing"})
        self.assertEqual(self.inspect()["status"], "UNKNOWN")

    def test_blocked_takes_precedence_over_unknown(self):
        self.graph["known_health"]["mock-db"] = False
        self.graph["nodes"].append("unknown")
        self.graph["edges"].append({"from": "unknown", "to": "demo-api"})
        result = self.inspect()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("unknown", result["unknowns"])

    def test_valid_single_service_graph_is_clear(self):
        self.graph.update(nodes=["demo-api"], edges=[], known_health={})
        self.assertEqual(self.inspect()["status"], "CLEAR")

    def test_live_identity_and_version_mismatch_are_unknown(self):
        self.state = sample("observations.json")["failed-rollout"][-1]
        self.assertEqual(self.inspect()["status"], "CLEAR")
        self.state["deployment"]["metadata"]["uid"] = "recreated"
        self.assertEqual(self.inspect()["status"], "UNKNOWN")
        self.state = sample("observations.json")["failed-rollout"][-1]
        self.state["deployment"]["metadata"]["resourceVersion"] = "another-version"
        self.assertEqual(self.inspect()["status"], "UNKNOWN")

    def test_explicit_target_mismatch_is_unknown(self):
        self.state["target"] = dict(self.proposal["target"], namespace="other")
        self.assertEqual(self.inspect()["status"], "UNKNOWN")

    def test_future_or_invalid_state_time_is_unknown(self):
        for at in ("2026-10-10T12:00:41Z", "invalid", None):
            with self.subTest(at=at):
                self.state["captured_at"] = at
                self.assertEqual(self.inspect()["status"], "UNKNOWN")

    def test_non_recovery_proposal_is_unknown(self):
        self.proposal.update(action="ESCALATE", parameters={})
        self.assertEqual(self.inspect()["status"], "UNKNOWN")

    def test_invalid_graph_shapes_raise(self):
        for change in ({"edge_direction": "reverse"}, {"nodes": "demo-api"},
                       {"nodes": [None]}, {"edges": [{}]}, {"edges": None},
                       {"known_health": []}):
            with self.subTest(change=change):
                self.graph = dict(sample("dependencies.json"), **change)
                with self.assertRaises(VerifierInputError):
                    self.inspect()

    def test_invalid_live_state_or_message_kind_raise(self):
        with self.assertRaises(VerifierInputError):
            inspect_dependencies(self.proposal, self.graph, None)
        self.state = {"known_health": []}
        with self.assertRaises(VerifierInputError):
            self.inspect()
        with self.assertRaises(VerifierInputError):
            inspect_dependencies(sample("messages/execution-result.json"), self.graph, {})

    def test_non_utc_clock_raises(self):
        with self.assertRaises(VerifierInputError):
            inspect_dependencies(self.proposal, self.graph, {}, now=datetime(2026, 10, 10))

    def test_inputs_are_unchanged_and_identity_is_copied(self):
        original = deepcopy((self.proposal, self.graph, self.state))
        result = self.inspect()
        self.assertEqual((self.proposal, self.graph, self.state), original)
        result["target"]["uid"] = "changed"
        self.assertEqual(self.proposal, original[0])
