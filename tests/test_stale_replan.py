"""A resourceVersion that moves after planning, as on sentinel-dist (docs/distributed-acceptance.md).

The first live S0 runs on the four-node lab (13 Nov 2026) never repaired: the
tokenless lab Planner binds its proposal to the incident's
observed_resource_version, the target's Recreate rollout then moved the
Deployment's resourceVersion with a status write, and the Guard refused the
proposal with STALE_RESOURCE_VERSION, once on its first pass and once on the
re-check after the operator's approval. These tests replay both over loopback
HTTP with the real Planner, Guard and Executor adapters and a fake Kubernetes
API whose version moves (FakeDeploymentAPI.external_write, a write that is not
Sentinel's). With the Coordinator's default (stale_replans=0) the refusal is
final, as before; with the sentinel-dist setting the Guard's live read goes
back to the Planner as a bounded re-plan, which the Guard checks again and the
operator approves again. A re-plan is only for status churn inside the
proposal's first approval cycle: a change to the target's spec, a revoked
approval, an earlier ALLOW or a seal leaves the stale refusal final, as in
S8 (revoke, the other Executor's seal moves the version) and when an
operator's fix lands between planning and the Guard.
"""
from contextlib import ExitStack, redirect_stderr
from datetime import timedelta
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock

from experiments.distributed import invariants
from experiments.validation.demo_offline_flow import OfflineFlow, fixture
from runtime.approval_http import ApprovalClient
from runtime.coordinator_server import servers
from runtime.remote_adapters import DeterministicProvider, ExecutorAdapter, GuardAdapter, PlannerAdapter
from runtime.remote_worker import RemoteIngress, RemoteWorker, WorkerJournal
from runtime.worker_http import WorkerClient
from runtime.worker_transport import Delivery, digest
from services.coordinator.core import (
    MAX_STALE_REPLANS, STALE_REPLAN_CAUSE, LocalEventBus, dispatch_pending, record_message, replan_after_stale,
)
from services.coordinator.store import SQLiteStore
from services.guard.common import json_sha256
from services.guard.core import evaluate_with_report
from services.guard.decisions import STALE_READ_SPEC
from services.observer.core import detect
from services.planner.core import mock_provider, retrieve_runbooks
from services.planner.store import PlannerStore
from services.planner.worker import ReplanProvider, planning_request, prepare, replan_key
from runtime.planner_pipeline import process_planner_requests
from services.verifier.core import inspect_dependencies

ROOT = Path(__file__).resolve().parents[1]


def sentinel_dist_stale_replans():
    """The --stale-replans value the sentinel-dist Coordinator manifest passes (0 when absent)."""
    text = (ROOT / "deploy/sentinel/sentinel-dist/coordinator.yaml").read_text(encoding="utf-8")
    match = re.search(r'"--stale-replans=(\d+)"', text)
    return int(match.group(1)) if match else 0


class StaleReplanFlow(unittest.TestCase):
    """Planner, Guard and Executor workers over loopback HTTP; only Kubernetes is fake."""

    stale_replans = sentinel_dist_stale_replans()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.f = OfflineFlow(directory)
        self.stack.callback(self.f.close)
        self.incident = detect(fixture("observations.json")["failed-rollout"], self.f.policy)
        self.services = {role: (role, role[0] * 40) for role in ("observer", "planner", "guard", "executor", "verifier")}
        self.approval_services = {role: (role, "a" + role[0] * 40) for role in ("operator", "guard", "executor")}
        permissions = {role: [{"target": self.incident["target"], "operations": (
            ["create", "read", "revoke", "history"] if role == "operator" else ["read"])}]
            for role in self.approval_services}
        workers, approvals, _, _ = self.stack.enter_context(servers(
            self.f.path / "coordinator.db", self.services, self.approval_services, permissions,
            port=0, approval_port=0, clock=lambda: self.f.now, lease_seconds=2, stale_replans=self.stale_replans))
        self.url = f"http://127.0.0.1:{workers.server_port}"
        self.approval_url = f"http://127.0.0.1:{approvals.server_port}"
        # The deterministic lab planner: no Kubernetes read, so it binds the incident's version.
        self.provider = DeterministicProvider()
        self.planning = PlannerStore(self.f.path / "planner.db", clock=lambda: self.f.now)
        self.stack.callback(self.planning.close)
        self.planner = self.worker("planner", PlannerAdapter(
            self.planning, baseline_reader=lambda _: [self.f.baseline], provider=self.provider))
        self.guard = self.worker("guard", GuardAdapter(
            state_reader=lambda p: self.f.cluster.read(p["target"]),
            policy_reader=lambda _: self.f.policy, baseline_reader=lambda _: self.f.baseline,
            dependency_reader=lambda p: inspect_dependencies(p, self.f.graph, self.f.cluster.read(p["target"]),
                                                             now=self.f.now),
            approval_reader=self.approval_client("guard"), clock=lambda: self.f.now))
        self.f.authorizer.approval_reader = self.approval_client("executor")
        self.executor = self.worker("executor", ExecutorAdapter(self.f.adapters))

    def worker(self, role, adapter):
        journal = WorkerJournal(self.f.path / (role + "-transport.db"))
        self.stack.callback(journal.close)
        return RemoteWorker(WorkerClient(self.url, role, self.services[role][1], timeout=2), journal, adapter,
                            renewal_interval=0.1)

    def approval_client(self, role):
        return ApprovalClient(self.approval_url, role, self.approval_services[role][1], timeout=2)

    def incident_row(self):
        return self.f.store.incident(self.incident["incident_id"])

    def state(self):
        return self.incident_row()["state"]

    def active(self):
        return self.f.store.action(self.incident_row()["active_action_id"])

    def live_version(self):
        return self.f.cluster.read(self.incident["target"])["deployment"]["metadata"]["resourceVersion"]

    def status_write(self):
        """The Deployment controller's status write after the snapshot: the version moves, the template does not."""
        before = self.f.cluster.read(self.incident["target"])["deployment"]["spec"]["template"]
        self.f.cluster.external_write()
        self.assertEqual(self.f.cluster.read(self.incident["target"])["deployment"]["spec"]["template"], before)

    def spec_change(self, image="registry.example/demo-api:operator-hotfix"):
        """Someone else's change to the spec (an operator's fix, a deploy): the template and generation move."""
        def hotfix(deployment):
            deployment["spec"]["template"]["spec"]["containers"][0]["image"] = image
            deployment["metadata"]["generation"] += 1
        self.f.cluster.external_write(hotfix)

    def revoke(self, proposal):
        """Revoke the latest approval through the approval API, as Lab.revoke does in S8."""
        latest = self.f.store.approval_row(proposal["action_id"])["id"]
        self.approval_client("operator").post("revoke", {
            "action_id": proposal["action_id"], "proposal_sha256": digest(proposal),
            "approval_id": latest, "reason": "withdrawn by the operator"})

    def assert_final(self, refused):
        """The stale refusal stands: no re-plan, the incident stays REJECTED, nothing written or queued."""
        self.assertEqual(self.state(), "REJECTED")
        self.assertEqual(self.active()["action_id"], refused["action_id"])
        self.assertEqual(self.f.store.reopenings(self.incident["incident_id"], STALE_REPLAN_CAUSE), 0)
        self.assertEqual(self.planner.once()["status"], "idle")
        self.assertEqual(self.guard.once()["status"], "idle")
        self.assertEqual(len(self.f.store.actions_for_incident(self.incident["incident_id"])), 1)
        self.assertEqual(self.f.cluster.write_attempts, [])

    def finding(self, proposal):
        """The Verifier's dependency finding for the proposal on the current live state, a second later."""
        self.f.now += timedelta(seconds=1)
        RemoteIngress(WorkerClient(self.url, "verifier", self.services["verifier"][1], timeout=2))(
            inspect_dependencies(proposal, self.f.graph, self.f.cluster.read(proposal["target"]), now=self.f.now))

    def plan(self):
        self.assertEqual(self.planner.once()["status"], "accepted")
        return self.active()["proposal"]

    def guard_pass(self):
        """One Guard pass on the active proposal, after the Verifier's finding the Guard needs."""
        self.finding(self.active()["proposal"])
        self.assertEqual(self.guard.once()["status"], "accepted")
        return self.active()["guard_result"]

    def approve(self, proposal):
        self.f.now += timedelta(seconds=1)
        self.approval_client("operator").post("create", {
            "action_id": proposal["action_id"], "proposal_sha256": digest(proposal),
            "policy_version": self.f.policy["policy_version"],
            "expires_at": (self.f.now + timedelta(seconds=120)).isoformat()})

    def records(self):
        rows = self.f.store.db.execute
        return {"incidents": [dict(row) for row in rows("SELECT incident_id, state, active_action_id FROM incidents")],
                "transitions": [dict(row) for row in rows("SELECT * FROM transitions ORDER BY seq")],
                "outbox": [dict(row) for row in rows("SELECT id, topic, action_id, delivered FROM outbox ORDER BY id")]}

    def assert_replanned(self, refused, replanned, attempt=1):
        """The re-plan is a new proposal on the live version; the refused one and its decision stay as they were."""
        old = self.f.store.action(refused["action_id"])
        self.assertEqual(old["state"], "REJECTED")
        self.assertEqual(old["proposal"], refused)
        self.assertEqual(old["guard_result"]["reason_codes"], ["STALE_RESOURCE_VERSION"])
        self.assertNotEqual(replanned["action_id"], refused["action_id"])
        self.assertEqual(replanned["based_on_resource_version"], old["guard_result"]["validated_resource_version"])
        self.assertEqual({key: replanned[key] for key in ("action", "parameters", "target", "evidence_refs")},
                         {key: refused[key] for key in ("action", "parameters", "target", "evidence_refs")})
        self.assertTrue(replanned["rationale"].startswith(DeterministicProvider.RATIONALE_LABEL))
        self.assertIn(f"Re-planned (attempt {attempt})", replanned["rationale"])
        self.assertTrue(invariants.x4(self.records())["passed"], invariants.x4(self.records())["detail"])


class SentinelDistSettingTests(StaleReplanFlow):
    def test_the_lab_coordinator_is_configured_for_bounded_replans_and_the_planner_stays_tokenless(self):
        self.assertTrue(1 <= self.stale_replans <= MAX_STALE_REPLANS)
        planner = (ROOT / "deploy/sentinel/sentinel-dist/planner.yaml").read_text(encoding="utf-8")
        self.assertIn("automountServiceAccountToken: false", planner)
        self.assertIn('"provider": "deterministic"', planner)

    def test_a_version_moved_between_planning_and_the_guard_is_replanned_and_repaired_once(self):
        RemoteIngress(WorkerClient(self.url, "observer", self.services["observer"][1], timeout=2))(self.incident)
        first = self.plan()
        self.assertEqual(first["based_on_resource_version"], self.incident["observed_resource_version"])
        self.status_write()  # The live symptom: the rollout writes status after the incident's snapshot.
        refusal = self.guard_pass()
        self.assertEqual((refusal["verdict"], refusal["reason_codes"]), ("REJECT", ["STALE_RESOURCE_VERSION"]))
        self.assertEqual(refusal["validated_resource_version"], self.live_version())
        self.assertEqual(self.state(), "OPEN")  # Reopened for the re-plan, never left REJECTED and silent.
        second = self.plan()
        self.assert_replanned(first, second)
        self.assertEqual(self.guard_pass()["reason_codes"], ["OPERATOR_APPROVAL_REQUIRED"])
        self.approve(second)
        self.assertEqual(self.guard_pass()["verdict"], "ALLOW")
        self.assertEqual(self.state(), "READY")
        self.assertEqual(self.executor.once()["status"], "accepted")
        self.assertEqual(self.state(), "VERIFYING")
        self.assertEqual(len(self.f.cluster.writes), 1)
        self.assertEqual(self.f.store.reopenings(self.incident["incident_id"], STALE_REPLAN_CAUSE), 1)
        # The re-plan is its own journal row; the first saved proposal never changed.
        self.assertEqual(self.planning.get(self.incident["incident_id"])["proposal"], first)
        saved = self.planning.get(replan_key(self.incident["incident_id"], 1))
        self.assertEqual(saved["proposal"], second)
        self.assertEqual(saved["delivered"], 1)
        self.assertTrue(self.planning.attempts(replan_key(self.incident["incident_id"], 1))[0]["usage"]
                        ["synthetic_planner"])

    def test_a_version_moved_between_approval_and_the_recheck_needs_a_new_proposal_and_a_new_approval(self):
        RemoteIngress(WorkerClient(self.url, "observer", self.services["observer"][1], timeout=2))(self.incident)
        first = self.plan()
        self.assertEqual(self.guard_pass()["reason_codes"], ["OPERATOR_APPROVAL_REQUIRED"])
        self.approve(first)
        self.status_write()  # Between the approval and the Guard's re-check, as live at 10:52:49Z.
        refusal = self.guard_pass()
        self.assertEqual((refusal["verdict"], refusal["reason_codes"]), ("REJECT", ["STALE_RESOURCE_VERSION"]))
        second = self.plan()
        self.assert_replanned(first, second)
        # The approval was bound to the refused proposal's digest: the re-plan is not approved by it.
        self.assertEqual(self.guard_pass()["reason_codes"], ["OPERATOR_APPROVAL_REQUIRED"])
        self.assertEqual(self.f.cluster.write_attempts, [])
        self.approve(second)
        self.assertEqual(self.guard_pass()["verdict"], "ALLOW")
        self.assertEqual(self.executor.once()["status"], "accepted")
        self.assertEqual(len(self.f.cluster.writes), 1)

    def test_the_guard_records_the_generation_its_stale_read_saw_without_changing_the_decision(self):
        RemoteIngress(WorkerClient(self.url, "observer", self.services["observer"][1], timeout=2))(self.incident)
        self.plan()
        self.status_write()
        refusal = self.guard_pass()
        report = self.f.store.guard_report(digest(refusal), refusal["action_id"])
        [record] = [check for check in report["checks"] if check["code"] == STALE_READ_SPEC]
        self.assertEqual(record["status"], "SKIP")
        self.assertEqual(record["actual"]["generation"], self.incident["observed_generation"])
        self.assertEqual(refusal["reason_codes"], ["STALE_RESOURCE_VERSION"])

    def test_a_spec_change_between_planning_and_the_guard_is_not_replanned(self):
        """A ROLLBACK planned from the incident's evidence must not overwrite someone's later change."""
        RemoteIngress(WorkerClient(self.url, "observer", self.services["observer"][1], timeout=2))(self.incident)
        first = self.plan()
        self.spec_change()
        refusal = self.guard_pass()
        self.assertEqual((refusal["verdict"], refusal["reason_codes"]), ("REJECT", ["STALE_RESOURCE_VERSION"]))
        self.assert_final(first)

    def test_a_spec_change_between_approval_and_the_recheck_is_not_replanned(self):
        RemoteIngress(WorkerClient(self.url, "observer", self.services["observer"][1], timeout=2))(self.incident)
        first = self.plan()
        self.assertEqual(self.guard_pass()["reason_codes"], ["OPERATOR_APPROVAL_REQUIRED"])
        self.approve(first)
        self.spec_change()
        self.assertEqual(self.guard_pass()["reason_codes"], ["STALE_RESOURCE_VERSION"])
        self.assert_final(first)

    def test_a_spec_change_after_a_replan_ends_the_replans(self):
        RemoteIngress(WorkerClient(self.url, "observer", self.services["observer"][1], timeout=2))(self.incident)
        self.plan()
        self.status_write()
        self.assertEqual(self.guard_pass()["reason_codes"], ["STALE_RESOURCE_VERSION"])
        second = self.plan()
        self.spec_change()
        self.assertEqual(self.guard_pass()["reason_codes"], ["STALE_RESOURCE_VERSION"])
        self.assertEqual(self.state(), "REJECTED")
        self.assertEqual(self.active()["action_id"], second["action_id"])
        self.assertEqual(self.planner.once()["status"], "idle")
        self.assertEqual(self.f.store.reopenings(self.incident["incident_id"], STALE_REPLAN_CAUSE), 1)
        self.assertEqual(self.f.cluster.write_attempts, [])

    def test_s8_a_revoked_approval_then_a_moved_version_is_final(self):
        """S8: revoke while READY, the other Executor's seal moves the version, the re-check refuses it stale."""
        RemoteIngress(WorkerClient(self.url, "observer", self.services["observer"][1], timeout=2))(self.incident)
        first = self.plan()
        self.assertEqual(self.guard_pass()["reason_codes"], ["OPERATOR_APPROVAL_REQUIRED"])
        self.approve(first)
        self.assertEqual(self.guard_pass()["verdict"], "ALLOW")
        self.assertEqual(self.state(), "READY")
        self.revoke(first)
        self.status_write()  # The seal's annotation PATCH, or the Recreate status write.
        self.assertEqual(self.guard_pass()["reason_codes"], ["STALE_RESOURCE_VERSION"])
        self.assert_final(first)

    def test_a_revocation_before_the_recheck_is_final_even_without_an_allow(self):
        RemoteIngress(WorkerClient(self.url, "observer", self.services["observer"][1], timeout=2))(self.incident)
        first = self.plan()
        self.assertEqual(self.guard_pass()["reason_codes"], ["OPERATOR_APPROVAL_REQUIRED"])
        self.approve(first)
        self.revoke(first)
        self.status_write()
        self.assertEqual(self.guard_pass()["reason_codes"], ["STALE_RESOURCE_VERSION"])
        self.assert_final(first)

    def test_a_stale_recheck_after_an_allow_is_final(self):
        """New dependency evidence after the ALLOW: the version the action was allowed on is gone (P3.04)."""
        RemoteIngress(WorkerClient(self.url, "observer", self.services["observer"][1], timeout=2))(self.incident)
        first = self.plan()
        self.assertEqual(self.guard_pass()["reason_codes"], ["OPERATOR_APPROVAL_REQUIRED"])
        self.approve(first)
        self.assertEqual(self.guard_pass()["verdict"], "ALLOW")
        self.status_write()
        self.assertEqual(self.guard_pass()["reason_codes"], ["STALE_RESOURCE_VERSION"])
        self.assert_final(first)

    def test_replans_are_bounded_and_the_last_stale_refusal_stands(self):
        RemoteIngress(WorkerClient(self.url, "observer", self.services["observer"][1], timeout=2))(self.incident)
        proposal = self.plan()
        for attempt in range(1, self.stale_replans + 1):
            self.status_write()
            self.assertEqual(self.guard_pass()["reason_codes"], ["STALE_RESOURCE_VERSION"])
            replanned = self.plan()
            self.assert_replanned(proposal, replanned, attempt)
            proposal = replanned
        self.status_write()
        self.assertEqual(self.guard_pass()["reason_codes"], ["STALE_RESOURCE_VERSION"])
        self.assertEqual(self.state(), "REJECTED")
        self.assertEqual(self.planner.once()["status"], "idle")
        self.assertEqual(self.guard.once()["status"], "idle")
        self.assertEqual(len(self.f.store.actions_for_incident(self.incident["incident_id"])), self.stale_replans + 1)
        self.assertEqual(self.f.cluster.write_attempts, [])


class DefaultSettingTests(StaleReplanFlow):
    """The Coordinator's default and Phase 3 file and kind modes: a stale refusal is final, as before."""

    stale_replans = 0

    def test_without_replans_the_stale_refusal_is_final_and_nothing_is_written(self):
        RemoteIngress(WorkerClient(self.url, "observer", self.services["observer"][1], timeout=2))(self.incident)
        self.plan()
        self.status_write()
        self.assertEqual(self.guard_pass()["reason_codes"], ["STALE_RESOURCE_VERSION"])
        self.assertEqual(self.state(), "REJECTED")
        self.assertEqual(self.planner.once()["status"], "idle")
        self.assertEqual(self.f.store.reopenings(self.incident["incident_id"], STALE_REPLAN_CAUSE), 0)
        self.assertEqual(self.f.cluster.write_attempts, [])


def stale_report(message, generation):
    """The Guard's report record of a stale read (services/guard/decisions.py::STALE_READ_SPEC)."""
    return {"schema_version": "1.0", "kind": "guard_report", "decision_sha256": digest(message), "checks": [
        {"code": "STALE_RESOURCE_VERSION", "status": "FAIL", "path": "live_state.deployment.metadata.resourceVersion",
         "actual": message["validated_resource_version"], "expected": "x", "message": "x"},
        {"code": STALE_READ_SPEC, "status": "SKIP", "path": "live_state.deployment.metadata.generation",
         "actual": {"generation": generation, "template_sha256": "0" * 64}, "expected": "x", "message": "x"}]}


def guard_result(proposal, verdict, reasons, version):
    return {"schema_version": "1.0", "kind": "guard_result", "incident_id": proposal["incident_id"],
            "action_id": proposal["action_id"], "target": proposal["target"], "verdict": verdict,
            "reason_codes": reasons, "policy_version": "policy-1", "validated_resource_version": version,
            "checked_at": "2026-10-10T12:00:40Z"}


class CoordinatorRuleTests(unittest.TestCase):
    """replan_after_stale in the Coordinator's own transaction, without workers."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.f = OfflineFlow(directory.name)
        self.addCleanup(self.f.close)
        self.store = self.f.store
        self.incident = detect(fixture("observations.json")["failed-rollout"], self.f.policy)

    def record(self, message, limit):
        with self.store.transaction():
            record_message(message, self.store, stale_replans=limit)

    def proposal(self, incident=None, action="ROLLBACK"):
        incident = incident or self.incident
        provider = mock_provider if action == "ROLLBACK" else (lambda context: {
            "action": "ESCALATE", "parameters": {}, "rationale": "x",
            "evidence_refs": [item["evidence_id"] for item in context["incident"]["signals"]]})
        journal = PlannerStore(":memory:", clock=lambda: self.f.now)
        self.addCleanup(journal.close)
        return prepare(incident, journal=journal, baseline_reader=lambda _: [self.f.baseline],
                       retriever=retrieve_runbooks, provider=provider)

    def refused(self, reasons=("STALE_RESOURCE_VERSION",), limit=2, generation="observed", report=True,
                before=None):
        """Record the incident, a proposal and the Guard's refusal; before(proposal) runs ahead of the refusal."""
        self.record(self.incident, limit)
        proposal = self.proposal()
        self.record(proposal, limit)
        if before is not None:
            before(proposal)
        message = guard_result(proposal, "REJECT", list(reasons), "rv-live")
        if report:
            with self.store.transaction():
                self.store.save_guard_report(proposal["action_id"], stale_report(
                    message, self.incident["observed_generation"] if generation == "observed" else generation))
        self.record(message, limit)
        return proposal

    def planner_requests(self):
        return [json.loads(row["payload"]) for row in self.store.db.execute(
            "SELECT payload FROM outbox WHERE topic='planner.request' ORDER BY id")]

    def test_only_a_refusal_for_staleness_alone_is_replanned(self):
        self.refused(("STALE_RESOURCE_VERSION", "TARGET_UID_MISMATCH"))
        self.assertEqual(self.store.incident(self.incident["incident_id"])["state"], "REJECTED")
        self.assertEqual(len(self.planner_requests()), 1)

    def test_a_refusal_without_the_guards_record_of_the_spec_is_final(self):
        for options in ({"report": False}, {"generation": None}, {"generation": "3"}):
            with self.subTest(**options):
                self.setUp()
                self.refused(**options)
                self.assertEqual(self.store.incident(self.incident["incident_id"])["state"], "REJECTED")
                self.assertEqual(len(self.planner_requests()), 1)

    def test_a_refusal_after_the_spec_moved_is_final(self):
        self.refused(generation=self.incident["observed_generation"] + 1)
        self.assertEqual(self.store.incident(self.incident["incident_id"])["state"], "REJECTED")
        self.assertEqual(len(self.planner_requests()), 1)

    def test_a_refusal_of_an_action_sealed_once_is_final(self):
        def sealed(proposal):
            with self.store.transaction():
                self.store.require_seal(self.store.action(proposal["action_id"]))
                self.store.finish_seal(proposal["target"], 0, release=True)
        self.refused(before=sealed)
        self.assertEqual(self.store.incident(self.incident["incident_id"])["state"], "REJECTED")
        self.assertEqual(len(self.planner_requests()), 1)

    def test_a_disabled_rule_queues_nothing(self):
        self.refused(limit=0)
        self.assertEqual(self.store.incident(self.incident["incident_id"])["state"], "REJECTED")
        self.assertEqual(len(self.planner_requests()), 1)

    def test_the_replan_request_carries_the_recorded_incident_and_the_guards_read(self):
        proposal = self.refused()
        first, replan = self.planner_requests()
        self.assertEqual(replan, {"incident": first["incident"], "replan": {
            "attempt": 1, "after_action_id": proposal["action_id"], "resource_version": "rv-live"}})
        incident, key, attempt = planning_request(replan)
        self.assertEqual((incident["observed_resource_version"], key, attempt),
                         ("rv-live", replan_key(self.incident["incident_id"], 1), 1))
        self.assertEqual({k: v for k, v in incident.items() if k != "observed_resource_version"},
                         {k: v for k, v in self.incident.items() if k != "observed_resource_version"})
        transition = self.store.transitions(self.incident["incident_id"])[-1]
        self.assertEqual((transition["from_state"], transition["to_state"], transition["cause"], transition["action_id"]),
                         ("REJECTED", "OPEN", STALE_REPLAN_CAUSE, proposal["action_id"]))

    def test_a_replayed_refusal_queues_one_replan(self):
        proposal = self.refused()
        with self.store.transaction():
            self.assertFalse(record_message(guard_result(proposal, "REJECT", ["STALE_RESOURCE_VERSION"], "rv-live"),
                                            self.store, stale_replans=2))
        self.assertEqual(len(self.planner_requests()), 2)

    def test_the_replan_request_is_withdrawn_once_the_incident_moved_on(self):
        proposal = self.refused()
        replanned = self.proposal(planning_request(self.planner_requests()[1])[0])
        self.record(replanned, 2)
        bus = LocalEventBus()
        dispatch_pending(self.store, bus)
        self.assertNotIn("planner.request", [event["topic"] for event in bus.events])
        self.assertNotEqual(replanned["action_id"], proposal["action_id"])

    def test_the_rule_ignores_restarts_other_actions_and_unmoved_versions(self):
        self.record(self.incident, 2)
        proposal = self.proposal()
        self.record(proposal, 2)
        same = guard_result(proposal, "REJECT", ["STALE_RESOURCE_VERSION"], proposal["based_on_resource_version"])
        with self.store.transaction():
            self.store.save_guard_result(proposal["action_id"], same)
            self.store.set_state(self.incident["incident_id"], proposal["action_id"], "REJECTED")
            self.assertFalse(replan_after_stale(same, self.store, limit=2))
            restart = dict(proposal, action="RESTART")
            self.store.db.execute("UPDATE actions SET proposal=? WHERE action_id=?",
                                  (json.dumps(restart), proposal["action_id"]))
            moved = dict(same, validated_resource_version="rv-live")
            self.store.save_guard_result(proposal["action_id"], moved)
            self.assertFalse(replan_after_stale(moved, self.store, limit=2))
        self.assertEqual(self.store.incident(self.incident["incident_id"])["state"], "REJECTED")

    def test_the_bound_is_validated_where_it_is_configured(self):
        with SQLiteStore(":memory:") as store:
            for value in (-1, MAX_STALE_REPLANS + 1, True, 1.0):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    Delivery(store, stale_replans=value)
        from runtime.coordinator_server import main
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            main(["--db", "x", "--identities", "x", "--approval-identities", "x", "--approval-permissions", "x",
                  "--stale-replans", str(MAX_STALE_REPLANS + 1)])


class GuardRecordTests(unittest.TestCase):
    """The Guard's report records the spec a stale read saw (STALE_READ_SPEC); the decision is unchanged."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.f = OfflineFlow(directory.name)
        self.addCleanup(self.f.close)
        incident = detect(fixture("observations.json")["failed-rollout"], self.f.policy)
        journal = PlannerStore(":memory:", clock=lambda: self.f.now)
        self.addCleanup(journal.close)
        self.proposal = prepare(incident, journal=journal, baseline_reader=lambda _: [self.f.baseline],
                                retriever=retrieve_runbooks, provider=mock_provider)

    def evaluate(self):
        live = self.f.cluster.read(self.proposal["target"])
        dependencies = inspect_dependencies(self.proposal, self.f.graph, live, now=self.f.now)
        decision, report = evaluate_with_report(self.proposal, live, self.f.policy, self.f.baseline, dependencies,
                                                now=self.f.now)
        return live, decision, [check for check in report["checks"] if check["code"] == STALE_READ_SPEC]

    def test_only_a_stale_read_is_recorded_and_it_never_becomes_a_reason(self):
        _, decision, records = self.evaluate()
        self.assertEqual(records, [])
        self.assertNotIn(STALE_READ_SPEC, decision["reason_codes"])
        self.f.cluster.external_write()
        live, decision, [record] = self.evaluate()
        self.assertEqual(decision["reason_codes"], ["STALE_RESOURCE_VERSION"])
        self.assertEqual(record["status"], "SKIP")
        self.assertEqual(record["actual"], {"generation": live["deployment"]["metadata"]["generation"],
                                            "template_sha256": json_sha256(live["deployment"]["spec"]["template"])})

    def test_a_missing_or_malformed_generation_is_recorded_as_unknown(self):
        for value in ("2", 0, True, None):
            with self.subTest(generation=value):
                def change(deployment, value=value):
                    deployment["metadata"]["generation"] = value
                self.f.cluster.external_write(change)
                _, decision, [record] = self.evaluate()
                self.assertIsNone(record["actual"]["generation"])


class LocalPipelineTests(unittest.TestCase):
    def test_the_local_pipeline_refuses_a_replan_request_in_both_modes(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        f = OfflineFlow(directory.name)
        self.addCleanup(f.close)
        incident = detect(fixture("observations.json")["failed-rollout"], f.policy)
        with f.store.transaction():
            record_message(incident, f.store)
            f.store.db.execute("UPDATE outbox SET delivered=1 WHERE topic='planner.request'")
            f.store.enqueue("planner.request", {"incident": incident, "replan": {
                "attempt": 1, "after_action_id": "ACT-1", "resource_version": "rv-live"}})
        journal = PlannerStore(":memory:", clock=lambda: f.now)
        self.addCleanup(journal.close)
        for options in ({}, {"journal": journal}):
            with self.subTest(durable=bool(options)), self.assertRaisesRegex(ValueError, "re-plan"):
                process_planner_requests(f.store, LocalEventBus(), baseline_reader=lambda _: [f.baseline],
                                         retriever=retrieve_runbooks, provider=mock_provider, **options)


class PlannerRequestTests(unittest.TestCase):
    def setUp(self):
        self.incident = detect(fixture("observations.json")["failed-rollout"], fixture("policy.json"))

    def request(self, **replan):
        values = {"attempt": 1, "after_action_id": "ACT-1", "resource_version": "rv-live", **replan}
        return {"incident": self.incident, "replan": values}

    def test_a_first_request_plans_the_incident_as_it_was_observed(self):
        self.assertEqual(planning_request({"incident": self.incident}), (self.incident, None, None))

    def test_a_malformed_replan_is_refused(self):
        for bad in ({"attempt": 0}, {"attempt": 6}, {"attempt": True}, {"resource_version": ""},
                    {"resource_version": self.incident["observed_resource_version"]}, {"after_action_id": 7},
                    {"extra": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                planning_request(self.request(**bad))
        with self.assertRaises(ValueError):
            planning_request({"incident": self.incident, "replan": "rv-live"})

    def test_the_replan_note_keeps_the_providers_usage(self):
        provider = DeterministicProvider()
        wrapped = ReplanProvider(provider, 2)
        context = {"incident": self.incident, "baselines": [fixture("baseline.json")], "runbooks": []}
        answer = wrapped(context)
        self.assertTrue(answer["rationale"].endswith(
            "Re-planned (attempt 2) after the Guard refused the previous proposal because the target's "
            "resourceVersion had moved since the incident; bound to the version the Guard read."))
        self.assertIs(wrapped.usage, provider.usage)
        self.assertIsNone(ReplanProvider(Mock(spec=[]), 1).usage)


if __name__ == "__main__":
    unittest.main()
