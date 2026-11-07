from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

from services.verifier.live import LiveEvidenceCollector, collect_window
from services.verifier.core import verify
from tests.verifier.test_verifier_traces import export

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def fixture(name):
    return json.loads((Path(__file__).resolve().parents[2] / "data/sentinel/samples" / name).read_text(encoding="utf-8"))


def configuration():
    return {"cluster_id": "test-cluster", "services": {
        name: {"target": {"cluster_id": "test-cluster", "namespace": "demo", "kind": "Deployment",
                          "name": name, "uid": name + "-uid"}, "health_url": "http://127.0.0.1/" + name}
        for name in ("checkout", "payment")}}


def deployment(name):
    return {"kind": "Deployment", "metadata": {"namespace": "demo", "name": name, "uid": name + "-uid",
            "generation": 2, "resourceVersion": "opaque"}, "spec": {"replicas": 1},
            "status": {"observedGeneration": 2, "readyReplicas": 1, "availableReplicas": 1, "updatedReplicas": 1}}


class Kubernetes:
    def __init__(self):
        self.resources = {name: deployment(name) for name in ("checkout", "payment")}
        self.after_read = None

    def get(self, path):
        name = path.rsplit("/", 1)[-1]
        result = deepcopy(self.resources[name])
        if self.after_read:
            self.after_read(self.resources[name])
        return result


class HTTP:
    def __init__(self):
        self.statuses = {"checkout": 200, "payment": 200}

    def health(self, url):
        status = self.statuses[url.rsplit("/", 1)[-1]]
        return {"http_status": status, "error": None if status else "unavailable"}


class LiveReadinessTests(unittest.TestCase):
    def setUp(self):
        self.kubernetes, self.http = Kubernetes(), HTTP()
        self.collector = LiveEvidenceCollector(configuration(), self.kubernetes, http=self.http, clock=lambda: NOW)

    def test_complete_health_is_bound_to_target_and_time(self):
        result = self.collector.read_service("payment")
        self.assertTrue(result["known_health"])
        self.assertEqual(result["target"]["uid"], "payment-uid")
        self.assertEqual(result["observed_at"], "2026-10-26T00:00:00Z")

    def test_http_failure_or_unready_rollout_is_unhealthy(self):
        self.http.statuses["payment"] = 503
        self.assertFalse(self.collector.read_service("payment")["known_health"])
        self.http.statuses["payment"] = 200
        self.kubernetes.resources["payment"]["status"]["updatedReplicas"] = 0
        self.assertFalse(self.collector.read_service("payment")["known_health"])

    def test_unknown_http_or_missing_deployment_data_is_not_healthy(self):
        self.http.statuses["payment"] = None
        self.assertIsNone(self.collector.read_service("payment")["known_health"])
        self.kubernetes.resources["payment"].pop("status")
        self.assertIsNone(self.collector.read_service("payment")["known_health"])

    def test_recreated_target_and_generation_race_are_unknown(self):
        self.kubernetes.resources["payment"]["metadata"]["uid"] = "recreated"
        self.assertIsNone(self.collector.read_service("payment")["known_health"])
        self.kubernetes.resources["payment"] = deployment("payment")
        self.kubernetes.after_read = lambda value: value["metadata"].update(generation=3)
        self.assertIsNone(self.collector.read_service("payment")["known_health"])

    def test_invalid_target_configuration_is_rejected(self):
        config = configuration()
        config["services"]["payment"]["target"]["cluster_id"] = "other"
        with self.assertRaises(ValueError):
            LiveEvidenceCollector(config, self.kubernetes)
        config = configuration()
        config["declared_edges"] = [{"from": [], "to": "payment"}]
        with self.assertRaises(ValueError):
            LiveEvidenceCollector(config, self.kubernetes)


class LiveGraphTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration()
        self.config.update(declared_edges=[{"from": "checkout", "to": "payment"}],
                           dependency_inventory_complete=True)
        self.kubernetes, self.http = Kubernetes(), HTTP()

    def collector(self, **kwargs):
        return LiveEvidenceCollector(self.config, self.kubernetes, http=self.http, clock=lambda: NOW, **kwargs)

    def proposal(self):
        proposal = fixture("messages/proposal.json")
        proposal["target"] = self.config["services"]["checkout"]["target"]
        return proposal

    def test_declared_graph_yields_linked_clear_result(self):
        result = self.collector().dependencies(self.proposal())
        self.assertEqual(result["status"], "CLEAR")
        self.assertEqual(result["affected_services"], ["checkout", "payment"])

    def test_dependency_failure_blocks_and_is_in_sample(self):
        self.http.statuses["payment"] = 503
        collector = self.collector()
        self.assertEqual(collector.dependencies(self.proposal())["status"], "BLOCKED")
        self.assertFalse(collector.collect(self.proposal()["target"])["sample"]["dependencies"]["payment"])

    def test_missing_inventory_or_required_traces_cannot_claim_clear(self):
        for changes in ({"dependency_inventory_complete": False}, {"require_traces": True}):
            with self.subTest(changes=changes):
                self.config.update(dependency_inventory_complete=True, require_traces=False)
                self.config.update(changes)
                collector = self.collector()
                self.assertEqual(collector.dependencies(self.proposal())["status"], "UNKNOWN")
                self.assertIsNone(collector.collect(self.proposal()["target"])["sample"]["dependencies"])

    def test_otlp_edges_merge_with_declarations_and_retain_provenance(self):
        self.config["declared_edges"] = []
        self.config["services"]["checkout"]["trace_service"] = "caller-otel"
        self.config["services"]["payment"]["trace_service"] = "callee-otel"
        evidence = self.collector(trace_reader=export).collect(self.proposal()["target"])
        self.assertEqual(evidence["graph"]["edges"], [{"from": "checkout", "to": "payment"}])
        self.assertEqual(evidence["graph"]["edge_evidence"][0]["source"], "otlp_parent_span")

    def test_unavailable_trace_source_cannot_claim_complete_evidence(self):
        def broken():
            raise OSError("collector unavailable")
        self.assertEqual(self.collector(trace_reader=broken).dependencies(self.proposal())["status"], "UNKNOWN")

    def test_incomplete_live_sample_cannot_report_recovery(self):
        self.config["dependency_inventory_complete"] = False
        execution = fixture("messages/execution-result.json")
        execution["target"] = self.proposal()["target"]
        sample = self.collector().collect(execution["target"])["sample"]
        rows = [dict(sample, at=f"2026-10-26T00:00:{second:02d}Z") for second in (0, 10, 20)]
        self.assertEqual(verify(execution, rows, fixture("policy.json"))["status"], "UNKNOWN")

    def test_collection_budget_failure_is_explicit(self):
        clock = iter((0, 21, 22, 23))
        result = self.collector(monotonic=lambda: next(clock)).collect(self.proposal()["target"])
        self.assertIn("Collection time budget exceeded", result["graph"]["unknowns"])
        self.assertIsNone(result["sample"]["dependencies"])

    def test_wrong_target_cannot_select_arbitrary_endpoint(self):
        target = dict(self.proposal()["target"], uid="other")
        with self.assertRaises(ValueError):
            self.collector().collect(target)

    def test_window_applies_policy_to_actual_collector_readings(self):
        clock = [NOW]
        def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)
        collector = LiveEvidenceCollector(self.config, self.kubernetes, http=self.http, clock=lambda: clock[0])
        execution = fixture("messages/execution-result.json")
        execution["target"] = self.proposal()["target"]
        result = collect_window(collector, execution, fixture("policy.json"), count=3, interval_seconds=10, sleep=sleep)
        self.assertEqual(result["outcome"]["status"], "RECOVERED")
        self.assertEqual(len(result["samples"]), 3)

    def test_insufficient_window_and_invalid_interval(self):
        execution = fixture("messages/execution-result.json")
        execution["target"] = self.proposal()["target"]
        self.assertEqual(collect_window(self.collector(), execution, fixture("policy.json"), count=1)["outcome"]["status"], "UNKNOWN")
        for interval in (0, 16, float("nan")):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                collect_window(self.collector(), execution, fixture("policy.json"), interval_seconds=interval)

    def test_early_reading_cannot_be_relabeled_fresh_after_slow_collection(self):
        times = iter((NOW, NOW, NOW + timedelta(seconds=11), NOW + timedelta(seconds=11)))
        collector = LiveEvidenceCollector(self.config, self.kubernetes, http=self.http, clock=lambda: next(times))
        result = collector.collect(self.proposal()["target"])
        self.assertIn("Stale or future health evidence: checkout", result["graph"]["unknowns"])
        self.assertIsNone(result["sample"]["dependencies"])
