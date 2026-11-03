"""Offline dashboard lifecycle checks; cluster data is explicitly fake here."""

from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.request import urlopen

from scripts.boutique_dashboard import BoutiqueDashboard
from scripts.guard_rejection_demo import UNCHANGED_FIELDS
from services.coordinator import SQLiteStore


class BoutiqueDashboardTests(unittest.TestCase):
    def test_live_mode_reads_exact_run_without_creating_duplicate_incidents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / "assets"
            assets.mkdir()
            (assets / "index.html").write_text('<html><script src="/assets/test.js"></script></html>')
            with SQLiteStore(root / "coordinator.sqlite") as store:
                self.assertEqual(store.db.execute("SELECT count(*) FROM incidents").fetchone()[0], 0)
            cluster = {"schema_version": "1.0", "cluster_id": "test-cluster", "namespace": "test-namespace",
                       "observed_at": "2026-10-25T00:00:00Z", "sources": {}, "nodes": [], "pods": [],
                       "events": [], "summary": {}, "truncation": {}}
            dashboard = BoutiqueDashboard(root, context="test-context", cluster_id="test-cluster",
                                          namespace="test-namespace", port=0)
            with patch("scripts.boutique_dashboard.ensure_frontend"), \
                    patch("scripts.boutique_dashboard.collect_cluster_snapshot", return_value=cluster) as collect, \
                    patch("runtime.dashboard.ASSETS", assets):
                try:
                    dashboard.start()
                    deadline = time.monotonic() + 5
                    while True:
                        with urlopen(dashboard.url + "/api/snapshot", timeout=2) as response:
                            snapshot = json.load(response)
                        if snapshot["cluster_history"] or time.monotonic() > deadline:
                            break
                        time.sleep(.02)
                    self.assertEqual(snapshot["mode"], "recorded/live producers")
                    self.assertTrue(snapshot["sources"]["coordinator"]["connected"])
                    self.assertTrue(snapshot["sources"]["telemetry"]["connected"])
                    self.assertEqual(snapshot["cluster_history"][0]["cluster_id"], "test-cluster")
                    self.assertEqual(snapshot["incidents"], [])
                    collect.assert_called_with(context="test-context", cluster_id="test-cluster", namespace="test-namespace")
                finally:
                    dashboard.close()
                self.assertFalse(any(thread.is_alive() for thread in dashboard.threads))
                with self.assertRaises(URLError):
                    urlopen(dashboard.url, timeout=1)

    def test_unstarted_session_can_be_closed_after_frontend_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            dashboard = BoutiqueDashboard(directory, context="test", cluster_id="test", namespace="test", port=0)
            with patch("scripts.boutique_dashboard.ensure_frontend", side_effect=RuntimeError("build failed")):
                with self.assertRaisesRegex(RuntimeError, "build failed"):
                    dashboard.start()
            dashboard.close()
            self.assertEqual(dashboard.threads, [])

    def test_control_audit_failure_does_not_skip_server_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            control = Mock()
            control.finish.side_effect = OSError("audit disk unavailable")
            dashboard = BoutiqueDashboard(directory, context="test", cluster_id="test", namespace="test",
                                          lab_control=control)
            dashboard.server = Mock()
            with self.assertRaisesRegex(OSError, "audit disk unavailable"):
                dashboard.close()
            self.assertTrue(dashboard.stop.is_set())
            dashboard.server.server_close.assert_called_once()


class BoutiqueDashboardEvidenceTests(unittest.TestCase):
    """The final refusal must not invalidate, or borrow proof from, earlier rounds."""

    def setUp(self):
        target = {"cluster_id": "test-cluster", "namespace": "test-namespace", "kind": "Deployment",
                  "name": "shippingservice", "uid": "shipping-uid"}
        self.snapshot = {
            "mode": "recorded/live producers",
            "sources": {name: {"connected": True} for name in ("coordinator", "telemetry")},
            "incidents": [], "actions": [], "messages": [], "approvals": [], "outbox": [],
            "reports": [], "telemetry": [],
            "cluster_history": [{"cluster_id": "test-cluster", "namespace": "test-namespace",
                                 "pods": [{"name": "shippingservice-healthy-pod"}]}],
        }
        for index in range(3):
            rejected = index == 2
            identity = {"incident_id": "INC-" + str(index), "action_id": "ACT-" + str(index),
                        "target": deepcopy(target)}
            state = "REJECTED" if rejected else "RECOVERED"
            guard = dict(identity, kind="guard_result", verdict="REJECT" if rejected else "ALLOW",
                         reason_codes=["ROLLBACK_ALREADY_AT_BASELINE"] if rejected else ["VALIDATION_PASSED"],
                         checked_at="2026-10-26T10:00:00Z", inputs_sha256=str(index) * 64)
            self.snapshot["incidents"].append({"incident_id": identity["incident_id"], "target": deepcopy(target),
                                               "state": state, "active_action_id": identity["action_id"]})
            self.snapshot["actions"].append({"incident_id": identity["incident_id"], "action_id": identity["action_id"],
                                             "state": state, "proposal": dict(identity, kind="proposal"),
                                             "guard_result": guard,
                                             "execution_result": None if rejected else dict(
                                                 identity, kind="execution_result", status="APPLIED")})
            self.snapshot["reports"].append({key: guard[key] for key in (
                "incident_id", "action_id", "verdict", "checked_at", "inputs_sha256")})
            self.snapshot["messages"].append({"message": dict(identity, kind="dependency_result", status="CLEAR")})
            if not rejected:
                self.snapshot["approvals"].append({"record": {"action_id": identity["action_id"]}})
                self.snapshot["outbox"].append({"topic": "executor.request", "action_id": identity["action_id"]})
                self.snapshot["messages"].append({"message": dict(identity, kind="outcome", status="RECOVERED",
                                                                  checks=[{"passed": True}])})
        self.snapshot["telemetry"].append({"component": 5, "incident_id": "INC-2", "action_id": "ACT-2",
                                            "target": deepcopy(target), "summary": "0 patch attempts; unchanged"})
        before = {"uid": target["uid"], "resource_version": "102", "generation": 3,
                  "template_sha256": "a" * 64, "pod_uids": ["healthy-pod-uid"],
                  "desired": 1, "ready": 1, "available": 1, "observed_generation": 3}
        self.proof = {
            "incident_id": "INC-2", "action_id": "ACT-2", "guard_verdict": "REJECT",
            "approved_guard_verdict": "REJECT", "reason_code": "ROLLBACK_ALREADY_AT_BASELINE",
            "executor_status": "FAILED", "executor_patch_attempts": 0,
            "executor_journal_attempts": 0, "executor_routes": 0,
            "before": before, "after": deepcopy(before), "unchanged": {key: True for key in UNCHANGED_FIELDS},
        }

    def verify(self, *, rejection_only=False):
        class Response(BytesIO):
            status = 200

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "unnecessary-rollback").mkdir()
            (root / "unnecessary-rollback" / "proof.json").write_text(json.dumps(self.proof), encoding="utf-8")
            dashboard = BoutiqueDashboard(root, context="test-context", cluster_id="test-cluster",
                                          namespace="test-namespace", port=0)
            dashboard.url = "http://127.0.0.1:9999"
            with patch("scripts.boutique_dashboard.urlopen", side_effect=[
                Response(b'<html><script src="/assets/test.js"></script></html>'),
                Response(json.dumps(self.snapshot).encode("utf-8")),
            ]) as fetch:
                result = dashboard.verify_rejection() if rejection_only else dashboard.verify()
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(json.loads((root / "dashboard-validation.json").read_text()), result)
            self.assertEqual(json.loads((root / "dashboard-snapshot.json").read_text()), self.snapshot)
            return result

    def test_combined_run_keeps_prior_execution_routes_and_outcomes_and_proves_final_refusal(self):
        # API rows arrive newest first; verification must follow identities, not list positions.
        self.snapshot["incidents"].reverse()
        self.snapshot["actions"].reverse()
        result = self.verify()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual((result["incidents"], result["applied_actions"], result["rejected_actions"]), (3, 2, 1))
        self.assertEqual((result["role6_outcomes"], result["dependency_results"]), (2, 3))
        self.assertEqual(result["executor_patch_attempts_for_rejected_action"], 0)

    def test_standalone_refusal_still_validates_one_rejected_proposal(self):
        for field in ("incidents", "actions", "reports"):
            self.snapshot[field] = self.snapshot[field][-1:]
        self.snapshot["messages"] = [item for item in self.snapshot["messages"]
                                     if item["message"]["action_id"] == "ACT-2"]
        self.snapshot["approvals"], self.snapshot["outbox"] = [], []
        self.assertEqual(self.verify(rejection_only=True)["scenario"], "guard-rejection")

    def test_rejected_third_action_cannot_claim_execution_or_receive_executor_route(self):
        original = deepcopy(self.snapshot)
        for mutation in ("result", "route", "execution message", "outcome"):
            with self.subTest(mutation=mutation):
                self.snapshot = deepcopy(original)
                action = self.snapshot["actions"][2]
                message = dict(action["proposal"], kind="execution_result", status="APPLIED")
                if mutation == "result":
                    action["execution_result"] = message
                elif mutation == "route":
                    self.snapshot["outbox"].append({"topic": "executor.request", "action_id": action["action_id"]})
                else:
                    if mutation == "outcome":
                        message.update(kind="outcome", status="RECOVERED", checks=[{"passed": True}])
                    self.snapshot["messages"].append({"message": message})
                with self.assertRaisesRegex(RuntimeError, "must not"):
                    self.verify()

    def test_refusal_proof_cannot_be_borrowed_from_another_incident_action_or_target(self):
        original = deepcopy(self.proof)
        for field in ("incident_id", "action_id", "uid"):
            with self.subTest(field=field):
                self.proof = deepcopy(original)
                if field == "uid":
                    self.proof["before"][field] = self.proof["after"][field] = "other-target"
                else:
                    self.proof[field] = "other-run"
                with self.assertRaises(RuntimeError):
                    self.verify()

    def test_refusal_requires_exact_rejected_guard_report(self):
        original = deepcopy(self.snapshot)
        for field in ("incident_id", "action_id", "verdict", "inputs_sha256", "checked_at"):
            with self.subTest(field=field):
                self.snapshot = deepcopy(original)
                self.snapshot["reports"][2][field] = "different"
                with self.assertRaisesRegex(RuntimeError, "exact rejected Guard findings"):
                    self.verify()

    def test_zero_write_telemetry_must_link_to_refused_incident_action_and_target(self):
        original = deepcopy(self.snapshot)
        for field in ("component", "incident_id", "action_id", "target"):
            with self.subTest(field=field):
                self.snapshot = deepcopy(original)
                self.snapshot["telemetry"][0][field] = "different"
                with self.assertRaisesRegex(RuntimeError, "refusal evidence"):
                    self.verify()

    def test_success_outcomes_and_dependencies_must_match_their_incident_and_target(self):
        original = deepcopy(self.snapshot)
        for kind in ("outcome", "dependency_result"):
            for field in ("incident_id", "target"):
                with self.subTest(kind=kind, field=field):
                    self.snapshot = deepcopy(original)
                    item = next(row["message"] for row in self.snapshot["messages"] if row["message"]["kind"] == kind)
                    item[field] = "other-incident"
                    with self.assertRaisesRegex(RuntimeError, "Role 6 evidence"):
                        self.verify()

    def test_each_round_requires_its_own_dependency_result_and_each_recovery_its_own_outcome(self):
        original = deepcopy(self.snapshot)
        for kind in ("outcome", "dependency_result"):
            with self.subTest(kind=kind):
                self.snapshot = deepcopy(original)
                rows = [row for row in self.snapshot["messages"] if row["message"]["kind"] == kind]
                rows[-1]["message"] = deepcopy(rows[0]["message"])
                with self.assertRaisesRegex(RuntimeError, "Role 6"):
                    self.verify()

    def test_recovered_execution_must_match_its_action_incident_and_target(self):
        original = deepcopy(self.snapshot)
        for field in ("incident_id", "action_id", "target"):
            with self.subTest(field=field):
                self.snapshot = deepcopy(original)
                self.snapshot["actions"][0]["execution_result"][field] = "different"
                with self.assertRaisesRegex(RuntimeError, "matching real execution"):
                    self.verify()

    def test_refusal_must_use_unnecessary_rollback_reason_and_zero_write_proof(self):
        self.snapshot["actions"][2]["guard_result"]["reason_codes"] = ["ACTION_NOT_ALLOWED"]
        with self.assertRaisesRegex(RuntimeError, "Unnecessary-rollback reason"):
            self.verify()
        self.snapshot["actions"][2]["guard_result"]["reason_codes"] = ["ROLLBACK_ALREADY_AT_BASELINE"]
        self.proof["executor_patch_attempts"] = 1
        with self.assertRaisesRegex(RuntimeError, "zero counters"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
