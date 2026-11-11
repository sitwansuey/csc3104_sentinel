"""Reconciliation decision table: pure-table properties and executor scenarios.

Kubernetes is a fake API that advances resourceVersion on every committed change,
like a real API server. Nothing here contacts a cluster.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path
import sqlite3
import tempfile
import unittest

from experiments.validation.demo_guard import load_fixtures
from runtime.executor_pipeline import GuardAuthorizer
from runtime.remote_adapters import ExecutorAdapter
from services.executor.core import (
    ExecutionAdapters, encode_evidence, execute, reconcile, unchanged_uncertainty,
)
from services.executor.reconciliation import (
    ABSENT, ACTION_ANNOTATION, FENCE_ANNOTATION, Basis, Decision, Observation, action_attempt, classify,
    fence_marker, fence_nonce, observe, valid_fence_marker,
)
from services.executor.store import (
    LocalTargetCoordinator, SQLiteExecutionLedger, TargetBusy, canonical_json, fingerprint,
)
from services.guard.core import proposal_fingerprint
from shared.contracts import validate_message
from tests.support.executor_fake import FakeDeploymentAPI


NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)
STATUSES = {"APPLIED", "NOOP", "FAILED", "CONFLICT", "SUPERSEDED", "NEEDS_RECONCILIATION"}
DONE = STATUSES - {"NEEDS_RECONCILIATION"}


class SimulatedCrash(BaseException):
    """Skip ordinary exception handling to model process interruption."""


class CrashBeforeCommit(FakeDeploymentAPI):
    def conditional_restore(self, **kwargs):
        self.write_attempts.append(deepcopy(kwargs))
        raise SimulatedCrash()


class CrashAfterCommit(FakeDeploymentAPI):
    def conditional_restore(self, **kwargs):
        super().conditional_restore(**kwargs)
        raise SimulatedCrash()


class UnknownTimeout(FakeDeploymentAPI):
    def conditional_restore(self, **kwargs):
        self.write_attempts.append(deepcopy(kwargs))
        self.pending = deepcopy(kwargs)
        raise TimeoutError("Request may still be in flight")


def evidence_for(proposal, *, attempt="a" * 32, **changes):
    evidence = {"action_id": proposal["action_id"], "proposal_sha256": fingerprint(proposal),
                "target_uid": proposal["target"]["uid"],
                "template_sha256": proposal["parameters"]["template_sha256"], "attempt_id": attempt}
    evidence.update(changes)
    return evidence


class DecisionTableTests(unittest.TestCase):
    """classify() is total and never claims more than the observation proves."""

    def setUp(self):
        self.proposal = load_fixtures()["proposal"]
        self.v0 = self.proposal["based_on_resource_version"]
        self.uid = self.proposal["target"]["uid"]
        self.template = self.proposal["parameters"]["template_sha256"]
        self.own = evidence_for(self.proposal, attempt="0" * 32)
        self.row = {"expected_version": self.v0, "evidence": self.own}

    def annotation(self, kind):
        return {
            "ours": encode_evidence(self.own),
            "peer": encode_evidence(evidence_for(self.proposal, attempt="1" * 32)),
            "other-action": encode_evidence(evidence_for(self.proposal, action_id="ACT-OTHER")),
            "forged-hash": encode_evidence(evidence_for(self.proposal, proposal_sha256="f" * 64)),
            "other-template": encode_evidence(evidence_for(self.proposal, template_sha256="e" * 64)),
            "other-uid": encode_evidence(evidence_for(self.proposal, target_uid="other-uid")),
            "bad-nonce": encode_evidence(evidence_for(self.proposal, attempt="not-a-nonce")),
            "extra-field": encode_evidence(dict(self.own, command="x")),
            "non-canonical": encode_evidence(self.own).replace(",", ", "),
            "action-id-only": canonical_json({"action_id": self.proposal["action_id"]}),
            "malformed": "not json",
            "absent": None,
        }[kind]

    def observation(self, present, uid, version, evidence, template):
        if not present:
            return ABSENT
        return Observation(present=True, uid=self.uid if uid == "same" else "recreated-uid",
                           resource_version=self.v0 if version == "V0" else "999",
                           evidence_annotation=self.annotation(evidence),
                           template_sha256=self.template if template == "match" else "d" * 64)

    def test_each_row_in_order(self):
        cases = [
            ("R0 read unavailable", None, "NEEDS_RECONCILIATION", Basis.READ_UNAVAILABLE),
            ("R1 deleted", (False, "same", "V0", "absent", "match"), "SUPERSEDED", Basis.TARGET_DELETED),
            ("R2 replaced", (True, "other", "next", "ours", "match"), "SUPERSEDED", Basis.TARGET_REPLACED),
            ("R3 applied", (True, "same", "next", "ours", "match"), "APPLIED", Basis.EVIDENCE_MATCH),
            ("R3 peer applied", (True, "same", "next", "peer", "match"), "APPLIED", Basis.EVIDENCE_MATCH),
            ("R3 overwritten", (True, "same", "next", "ours", "mismatch"), "SUPERSEDED",
             Basis.COMMITTED_THEN_OVERWRITTEN),
            ("R3 impossible", (True, "same", "V0", "ours", "match"), "NEEDS_RECONCILIATION", Basis.INCONSISTENT),
            ("R5 advanced", (True, "same", "next", "absent", "match"), "SUPERSEDED", Basis.ADVANCED_WITHOUT_EVIDENCE),
            ("R6 in doubt", (True, "same", "V0", "absent", "match"), "NEEDS_RECONCILIATION",
             Basis.UNCHANGED_IN_DOUBT),
        ]
        for label, facts, status, basis in cases:
            with self.subTest(row=label):
                observation = None if facts is None else self.observation(*facts)
                decision = classify(self.row, self.proposal, observation)
                self.assertEqual((decision.status, decision.basis), (status, basis))
                self.assertEqual(decision.may_fence, basis is Basis.UNCHANGED_IN_DOUBT)

    def test_writer_is_own_or_peer(self):
        own = classify(self.row, self.proposal, self.observation(True, "same", "next", "ours", "match"))
        peer = classify(self.row, self.proposal, self.observation(True, "same", "next", "peer", "match"))
        stateless = classify(None, self.proposal, self.observation(True, "same", "next", "ours", "match"))
        self.assertEqual(own.writer, "own")
        self.assertEqual(peer.writer, "peer:" + "1" * 32)
        self.assertEqual(stateless.writer, "peer:" + "0" * 32)
        self.assertEqual(stateless.status, "APPLIED")

    def fenced(self, version="next", *, nonce="a" * 32, action_id=None, **observed):
        marker = canonical_json(fence_marker(action_id or self.proposal["action_id"], nonce))
        facts = vars(self.observation(True, "same", version, "absent", "match")) | observed
        return Observation(**dict(facts, fence_annotation=marker))

    def test_fence_row_needs_this_hosts_journaled_nonce_or_a_confirmed_fence(self):
        nonces = frozenset({"a" * 32})
        self.assertEqual(classify(self.row, self.proposal, self.fenced(), fence_nonces=nonces),
                         Decision("CONFLICT", Basis.FENCED, "999"))
        # A marker this host never journaled (a peer's fence, or a copy) proves nothing to it.
        for observation, journaled in ((self.fenced(), frozenset()), (self.fenced(), frozenset({"b" * 32})),
                                       (self.fenced(action_id="ACT-OTHER"), nonces)):
            with self.subTest(observation=observation.fence_annotation, journaled=journaled):
                decision = classify(self.row, self.proposal, observation, fence_nonces=journaled)
                self.assertEqual(decision.basis, Basis.ADVANCED_WITHOUT_EVIDENCE)
        # A fence whose own response showed it committed still counts if another writer stripped the marker.
        stripped = self.observation(True, "same", "next", "absent", "match")
        self.assertEqual(classify(self.row, self.proposal, stripped, fence_confirmed=True).basis, Basis.FENCED)
        # A committed fence always moves the version, so a marker at V0 is impossible.
        at_v0 = classify(self.row, self.proposal, self.fenced("V0"), fence_nonces=nonces)
        self.assertEqual((at_v0.status, at_v0.basis), ("NEEDS_RECONCILIATION", Basis.INCONSISTENT))
        self.assertFalse(at_v0.may_fence)
        # Rows before R4 still win: deletion, replacement and this action's own committed evidence.
        self.assertEqual(classify(self.row, self.proposal, ABSENT, fence_confirmed=True).basis, Basis.TARGET_DELETED)
        replaced = self.fenced(uid="recreated-uid")
        self.assertEqual(classify(self.row, self.proposal, replaced, fence_nonces=nonces).basis, Basis.TARGET_REPLACED)
        applied = self.fenced(evidence_annotation=encode_evidence(self.own))
        self.assertEqual(classify(self.row, self.proposal, applied, fence_nonces=nonces).status, "APPLIED")

    def test_definite_rejection_rows_stay_reserved(self):
        in_doubt = self.observation(True, "same", "V0", "absent", "match")
        advanced = self.observation(True, "same", "next", "absent", "match")
        self.assertEqual(classify(self.row, self.proposal, advanced, "rejected").status, "CONFLICT")
        self.assertEqual(classify(self.row, self.proposal, in_doubt, "rejected").status, "FAILED")

    def test_exhaustive_fence_product_gives_conflict_only_with_proof(self):
        markers = {"none": None, "ours": canonical_json(fence_marker(self.proposal["action_id"], "a" * 32)),
                   "unjournaled": canonical_json(fence_marker(self.proposal["action_id"], "b" * 32)),
                   "other-action": canonical_json(fence_marker("ACT-OTHER", "a" * 32)),
                   "non-canonical": canonical_json(fence_marker(self.proposal["action_id"], "a" * 32)).replace(",", ", "),
                   "malformed": "not json"}
        for present, uid, version, evidence, marker, confirmed in product(
                (True, False), ("same", "other"), ("V0", "next"), ("ours", "absent"), markers, (False, True)):
            facts = dict(present=present, uid=uid, version=version, evidence=evidence, marker=marker,
                         confirmed=confirmed)
            with self.subTest(**facts):
                observation = self.observation(present, uid, version, evidence, "match")
                if present:
                    observation = Observation(**dict(vars(observation), fence_annotation=markers[marker]))
                decision = classify(self.row, self.proposal, observation, fence_nonces=frozenset({"a" * 32}),
                                    fence_confirmed=confirmed)
                proof = confirmed or marker == "ours"
                if decision.status == "CONFLICT":
                    self.assertIs(decision.basis, Basis.FENCED)
                    self.assertTrue(proof and present and uid == "same" and version == "next" and evidence == "absent")
                if present and uid == "same" and version == "next" and evidence == "absent":
                    self.assertEqual(decision.status == "CONFLICT", proof)
                if present and uid == "same" and version == "V0":
                    self.assertNotIn(decision.status, DONE)
                self.assertEqual(decision.may_fence, decision.basis is Basis.UNCHANGED_IN_DOUBT)

    def test_fence_markers_are_canonical_and_bound_to_the_action(self):
        action = self.proposal["action_id"]
        self.assertEqual(fence_nonce(canonical_json(fence_marker(action, "a" * 32)), action), "a" * 32)
        self.assertEqual(fence_nonce(canonical_json(fence_marker(action, "a" * 32, epoch=7)), action), "a" * 32)
        for annotation in (None, "", "not json", "[" * 5000, canonical_json({"action_id": action}),
                           canonical_json({"action_id": action, "nonce": "A" * 32}),
                           canonical_json({"action_id": action, "nonce": "a" * 32, "epoch": 1.5}),
                           canonical_json({"action_id": action, "nonce": "a" * 32, "extra": 1}),
                           canonical_json(fence_marker("ACT-OTHER", "a" * 32)),
                           canonical_json(fence_marker(action, "a" * 32)).replace(":", ": ")):
            with self.subTest(annotation=annotation):
                self.assertIsNone(fence_nonce(annotation, action))
        self.assertTrue(valid_fence_marker(fence_marker(action, "a" * 32, epoch=0)))
        self.assertFalse(valid_fence_marker(fence_marker(action, "a" * 32, epoch=False)))
        snapshot = deepcopy(load_fixtures()["live_state"])
        snapshot["deployment"]["metadata"]["annotations"] = {FENCE_ANNOTATION: "marker"}
        self.assertEqual(observe(snapshot, self.proposal["target"]).fence_annotation, "marker")
        snapshot["deployment"]["metadata"]["annotations"] = {FENCE_ANNOTATION: 7}
        self.assertIsNone(observe(snapshot, self.proposal["target"]).fence_annotation)

    def test_journal_version_that_differs_from_the_proposal_is_inconsistent(self):
        row = dict(self.row, expected_version="something-else")
        decision = classify(row, self.proposal, self.observation(True, "same", "next", "absent", "match"))
        self.assertEqual((decision.status, decision.basis), ("NEEDS_RECONCILIATION", Basis.INCONSISTENT))

    def test_exhaustive_product_never_claims_more_than_the_evidence(self):
        evidence_kinds = ("ours", "peer", "other-action", "forged-hash", "other-template", "other-uid",
                          "bad-nonce", "extra-field", "non-canonical", "action-id-only", "malformed", "absent")
        rows = {"journaled": self.row, "adopted": {"expected_version": self.v0, "evidence": None}, "none": None}
        count = 0
        for (row_kind, row), present, uid, version, evidence, template in product(
                rows.items(), (True, False), ("same", "other"), ("V0", "next"), evidence_kinds, ("match", "mismatch")):
            facts = dict(row=row_kind, present=present, uid=uid, version=version, evidence=evidence, template=template)
            with self.subTest(**facts):
                decision = classify(row, self.proposal, self.observation(present, uid, version, evidence, template))
                count += 1
                self.assertIn(decision.status, STATUSES)
                self.assertIsInstance(decision.basis, Basis)
                # CONFLICT means 'proven not committed': it needs a fence or a definite rejection.
                self.assertNotEqual(decision.status, "CONFLICT")
                self.assertNotIn(decision.basis, {Basis.FENCED, Basis.DEFINITE_REJECTION})
                if decision.status == "APPLIED":
                    self.assertTrue(present and uid == "same" and version == "next" and template == "match")
                    self.assertIn(evidence, {"ours", "peer"})
                if present and uid == "same" and version == "V0":
                    # A request in flight could still commit: never conclude.
                    self.assertNotIn(decision.status, DONE)
                if decision.status == "SUPERSEDED":
                    self.assertTrue(not present or uid == "other" or version == "next")
                if decision.status == "NEEDS_RECONCILIATION":
                    self.assertTrue(present and uid == "same")
        self.assertEqual(count, 3 * 2 * 2 * 2 * len(evidence_kinds) * 2)

    def test_action_level_evidence_rejects_forgery_and_other_actions(self):
        self.assertEqual(action_attempt(self.annotation("ours"), self.proposal), "0" * 32)
        for kind in ("other-action", "forged-hash", "other-template", "other-uid", "bad-nonce",
                     "extra-field", "non-canonical", "action-id-only", "malformed", "absent"):
            with self.subTest(kind=kind):
                self.assertIsNone(action_attempt(self.annotation(kind), self.proposal))
        changed = dict(self.proposal, rationale="changed intent")
        self.assertIsNone(action_attempt(self.annotation("ours"), changed))
        self.assertIsNone(action_attempt("[" * 5000, self.proposal))

    def test_observe_refuses_snapshots_that_cannot_support_a_decision(self):
        snapshot = load_fixtures()["live_state"]
        self.assertEqual(observe(snapshot, self.proposal["target"]).resource_version, self.v0)
        for change in ("name", "namespace", "uid", "resourceVersion"):
            with self.subTest(change=change):
                broken = deepcopy(snapshot)
                broken["deployment"]["metadata"][change] = "" if change in {"uid", "resourceVersion"} else "other"
                self.assertIsNone(observe(broken, self.proposal["target"]))
        self.assertIsNone(observe({"deployment": None}, self.proposal["target"]))
        odd = deepcopy(snapshot)
        odd["deployment"]["metadata"]["annotations"] = ["invalid"]
        self.assertIsNone(observe(odd, self.proposal["target"]).evidence_annotation)


class ReconciliationScenarioTests(unittest.TestCase):
    """execute()/reconcile() wired through the table, with the real Guard."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.fixtures = load_fixtures()
        self.proposal = self.fixtures["proposal"]
        self.now = NOW
        self.approval = {
            "action_id": self.proposal["action_id"],
            "proposal_sha256": proposal_fingerprint(self.proposal),
            "policy_version": self.fixtures["policy"]["policy_version"],
            "operator_id": "fabricated-test-operator", "decision": "APPROVED",
            "approved_at": (NOW - timedelta(seconds=5)).isoformat(),
            "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
        }
        self.authorizer = GuardAuthorizer(
            policy_reader=lambda p: deepcopy(self.fixtures["policy"]),
            baseline_reader=lambda p: deepcopy(self.fixtures["baseline"]),
            dependency_reader=lambda p: deepcopy(self.fixtures["dependencies"]),
            approval_reader=lambda p: deepcopy(self.approval),
        )
        self.ledger = self.open_ledger("host-a")

    def open_ledger(self, name):
        ledger = SQLiteExecutionLedger(self.directory / (name + ".db"))
        self.addCleanup(ledger.close)
        return ledger

    def adapters(self, cluster, *, ledger=None, host="host-a", **changes):
        values = dict(ledger=ledger or self.ledger, coordination=LocalTargetCoordinator(self.directory / host),
                      cluster=cluster, authorizer=self.authorizer, clock=lambda: self.now)
        values.update(changes)
        return ExecutionAdapters(**values)

    def basis(self, result):
        return result["details"][len("[basis="):result["details"].index("]")]

    def in_doubt(self, cluster_type=UnknownTimeout):
        cluster = cluster_type(self.fixtures["live_state"], clock=lambda: self.now)
        try:
            result = execute(deepcopy(self.proposal), self.adapters(cluster))
        except SimulatedCrash:
            result = None
        return cluster, result

    def rival(self):
        rival = deepcopy(self.proposal)
        rival["action_id"] = "rival-action"
        return rival

    def test_external_version_bump_supersedes_and_releases_the_workload(self):
        # Crash after write intent; then a 422-style race: another writer moves the version.
        cluster, _ = self.in_doubt(CrashBeforeCommit)
        with self.assertRaises(TargetBusy):
            self.ledger.prepare(self.rival())
        cluster.external_write(lambda deployment: deployment["metadata"].setdefault(
            "annotations", {}).update({"example.org/bump": "1"}))
        result = reconcile(deepcopy(self.proposal), self.adapters(cluster))
        validate_message(result)
        self.assertEqual(result["status"], "SUPERSEDED")
        self.assertEqual(self.basis(result), "ADVANCED_WITHOUT_EVIDENCE")
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["phase"], "DONE")
        self.assertEqual(self.ledger.prepare(self.rival())["phase"], "PREPARED")
        self.assertEqual(cluster.writes, [])
        self.assertEqual(len(cluster.write_attempts), 1)

    def test_deleted_and_recreated_targets_supersede_without_writes(self):
        for case in ("deleted", "recreated"):
            with self.subTest(case=case):
                self.ledger = self.open_ledger(case)
                cluster, result = self.in_doubt()
                self.assertEqual(result["status"], "NEEDS_RECONCILIATION")
                cluster.delete()
                if case == "recreated":
                    cluster.recreate("recreated-uid")
                result = reconcile(deepcopy(self.proposal), self.adapters(cluster))
                self.assertEqual(result["status"], "SUPERSEDED")
                self.assertEqual(self.basis(result), "TARGET_DELETED" if case == "deleted" else "TARGET_REPLACED")
                self.assertEqual(len(cluster.write_attempts), 1)
                self.assertEqual(cluster.writes, [])
                self.assertEqual(self.ledger.prepare(self.rival())["phase"], "PREPARED")

    def test_unchanged_version_stays_in_doubt_and_is_deduplicated(self):
        cluster, first = self.in_doubt()
        self.assertEqual(self.basis(first), "UNCHANGED_IN_DOUBT")
        for _ in range(5):
            self.now += timedelta(seconds=61)
            self.assertEqual(reconcile(deepcopy(self.proposal), self.adapters(cluster)), first)
        self.assertEqual(self.ledger.results(self.proposal["action_id"]), [first])
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["phase"], "UNCERTAIN")
        with self.assertRaises(TargetBusy):
            self.ledger.prepare(self.rival())
        # A changed basis is a new fact and is recorded.
        cluster.read = lambda target: (_ for _ in ()).throw(OSError("read outage"))
        self.now += timedelta(seconds=61)
        outage = reconcile(deepcopy(self.proposal), self.adapters(cluster))
        self.assertEqual(self.basis(outage), "READ_UNAVAILABLE")
        self.assertEqual(len(self.ledger.results(self.proposal["action_id"])), 2)
        self.assertEqual(cluster.writes, [])

    def test_late_commit_after_dedupe_is_applied(self):
        cluster, first = self.in_doubt()
        self.now += timedelta(seconds=61)
        reconcile(deepcopy(self.proposal), self.adapters(cluster))
        FakeDeploymentAPI.conditional_restore(cluster, **cluster.pending)
        self.now += timedelta(seconds=61)
        applied = reconcile(deepcopy(self.proposal), self.adapters(cluster))
        self.assertEqual(applied["status"], "APPLIED")
        self.assertEqual([r["status"] for r in self.ledger.results(self.proposal["action_id"])],
                         ["NEEDS_RECONCILIATION", "APPLIED"])

    def test_escalation_is_emitted_exactly_once(self):
        cluster, first = self.in_doubt()
        self.assertNotIn("Operator action required", first["details"])
        adapters = self.adapters(cluster, reconcile_escalation_seconds=120)
        self.now += timedelta(seconds=61)
        self.assertEqual(reconcile(deepcopy(self.proposal), adapters), first)
        self.now += timedelta(seconds=61)
        escalated = reconcile(deepcopy(self.proposal), adapters)
        self.assertEqual(escalated["status"], "NEEDS_RECONCILIATION")
        self.assertIn("Operator action required", escalated["details"])
        for _ in range(4):
            self.now += timedelta(seconds=61)
            self.assertEqual(reconcile(deepcopy(self.proposal), adapters), escalated)
        notes = [r for r in self.ledger.results(self.proposal["action_id"])
                 if "Operator action required" in r["details"]]
        self.assertEqual(len(notes), 1)
        with self.assertRaises(ValueError):
            reconcile(deepcopy(self.proposal), self.adapters(cluster, reconcile_escalation_seconds=0))

    def test_backoff_doubles_from_two_to_sixty_seconds(self):
        cluster, _ = self.in_doubt()
        action_id = self.proposal["action_id"]
        self.assertTrue(self.ledger.reconcile_due(action_id, self.now))
        delays = []
        for _ in range(8):
            reconcile(deepcopy(self.proposal), self.adapters(cluster))
            due = datetime.fromisoformat(self.ledger.get(action_id)["next_reconcile_at"].replace("Z", "+00:00"))
            delays.append((due - self.now).total_seconds())
            self.assertFalse(self.ledger.reconcile_due(action_id, self.now))
            self.assertEqual(self.ledger.pending_proposals(due_at=self.now), [])
            self.now = due
            self.assertTrue(self.ledger.reconcile_due(action_id, self.now))
        self.assertEqual(delays, [2, 4, 8, 16, 32, 60, 60, 60])

    def test_peer_adoption_gives_applied_with_zero_writes_on_host_b(self):
        cluster, _ = self.in_doubt(CrashAfterCommit)
        self.assertEqual(len(cluster.writes), 1)
        peer = self.open_ledger("host-b")
        result = execute(deepcopy(self.proposal), self.adapters(cluster, ledger=peer, host="host-b"))
        self.assertEqual(result["status"], "APPLIED")
        self.assertIn("adopted peer write", result["details"])
        self.assertEqual(len(cluster.write_attempts), 1)
        self.assertEqual(len(cluster.writes), 1)
        self.assertEqual(peer.attempts(self.proposal["action_id"]), [])
        # Host A reconciles its own attempt to the same conclusion.
        own = reconcile(deepcopy(self.proposal), self.adapters(cluster))
        self.assertEqual(own["status"], "APPLIED")
        self.assertNotIn("adopted", own["details"])

    def test_peer_commit_during_our_conditional_write_is_adopted(self):
        proposal = self.proposal

        class PeerWinsRace(FakeDeploymentAPI):
            def conditional_restore(api, **kwargs):
                peer = dict(kwargs, evidence=dict(kwargs["evidence"], attempt_id="9" * 32))
                FakeDeploymentAPI.conditional_restore(api, **peer)
                return super().conditional_restore(**kwargs)

        cluster = PeerWinsRace(self.fixtures["live_state"], clock=lambda: self.now)
        result = execute(deepcopy(proposal), self.adapters(cluster))
        self.assertEqual(result["status"], "APPLIED")
        self.assertIn("adopted peer write", result["details"])
        self.assertEqual(len(cluster.writes), 1)

    def test_forged_proposal_hash_is_not_adopted(self):
        cluster = FakeDeploymentAPI(self.fixtures["live_state"], clock=lambda: self.now)
        forged = evidence_for(self.proposal, proposal_sha256="f" * 64)
        cluster.conditional_restore(target=self.proposal["target"], version=self.proposal["based_on_resource_version"],
                                    template=self.fixtures["baseline"]["pod_template"], evidence=forged)
        result = execute(deepcopy(self.proposal), self.adapters(cluster))
        self.assertEqual(result["status"], "CONFLICT")
        peer = self.open_ledger("host-b")
        stateless = reconcile(deepcopy(self.proposal), self.adapters(cluster, ledger=peer, host="host-b"))
        self.assertEqual(stateless["status"], "SUPERSEDED")
        self.assertEqual(len(cluster.write_attempts), 1)

    def test_applied_then_overwritten_is_superseded(self):
        cluster, _ = self.in_doubt(CrashAfterCommit)

        def change_image(deployment):
            deployment["spec"]["template"]["spec"]["containers"][0]["image"] = "external:change"

        cluster.external_write(change_image)
        self.assertIn(ACTION_ANNOTATION, cluster.snapshot["deployment"]["metadata"]["annotations"])
        result = reconcile(deepcopy(self.proposal), self.adapters(cluster))
        self.assertEqual(result["status"], "SUPERSEDED")
        self.assertEqual(self.basis(result), "COMMITTED_THEN_OVERWRITTEN")
        self.assertEqual(len(cluster.write_attempts), 1)

    def test_reconcile_on_a_host_with_no_row_returns_a_result(self):
        cluster, _ = self.in_doubt(CrashAfterCommit)
        for host, expected in (("host-b", "APPLIED"), ("host-c", "SUPERSEDED")):
            with self.subTest(host=host):
                if expected == "SUPERSEDED":
                    cluster.external_write(lambda deployment: deployment["metadata"]["annotations"].pop(
                        ACTION_ANNOTATION))
                ledger = self.open_ledger(host)
                result = reconcile(deepcopy(self.proposal), self.adapters(cluster, ledger=ledger, host=host))
                validate_message(result)
                self.assertEqual(result["status"], expected)
                self.assertEqual(ledger.get(self.proposal["action_id"])["phase"], "DONE")
                self.assertEqual(ledger.attempts(self.proposal["action_id"]), [])
        self.assertEqual(len(cluster.write_attempts), 1)

    def test_stateless_reconcile_blocks_a_later_write_on_that_host(self):
        cluster = FakeDeploymentAPI(self.fixtures["live_state"], clock=lambda: self.now)
        result = reconcile(deepcopy(self.proposal), self.adapters(cluster))
        self.assertEqual((result["status"], self.basis(result)), ("NEEDS_RECONCILIATION", "UNCHANGED_IN_DOUBT"))
        self.assertIn("no journaled write", result["details"])
        self.assertEqual(execute(deepcopy(self.proposal), self.adapters(cluster))["status"], "NEEDS_RECONCILIATION")
        self.assertEqual(cluster.write_attempts, [])

    def test_remote_adapter_leaves_unchanged_uncertainty_for_transport_retry(self):
        cluster, first = self.in_doubt()

        class Lease:
            def ensure_current(self):
                return {}

        adapter = ExecutorAdapter(self.adapters(cluster))
        delivery = {"topic": "executor.reconcile", "payload": {"proposal": deepcopy(self.proposal), "execution": first}}
        self.assertIsNone(adapter.process(deepcopy(delivery), Lease()))
        cluster.external_write()
        result, report = adapter.process(deepcopy(delivery), Lease())
        self.assertEqual(result["status"], "SUPERSEDED")
        self.assertIsNone(report)
        self.assertEqual(cluster.writes, [])


class UnchangedUncertaintyTests(unittest.TestCase):
    """Which reconcile results add nothing to Coordinator's latest execution result."""

    def result(self, status, recorded_at, details="[basis=UNCHANGED_IN_DOUBT] x"):
        return {"action_id": "ACT-1", "status": status, "resource_version": "7",
                "recorded_at": recorded_at, "details": details}

    def test_equal_and_older_uncertainty_are_unchanged(self):
        known = self.result("NEEDS_RECONCILIATION", "2026-10-10T12:01:41Z", "[basis=UNCHANGED_IN_DOUBT] peer")
        self.assertTrue(unchanged_uncertainty(deepcopy(known), known))
        self.assertTrue(unchanged_uncertainty(self.result("NEEDS_RECONCILIATION", "2026-10-10T12:00:40Z"), known))
        self.assertTrue(unchanged_uncertainty(self.result("NEEDS_RECONCILIATION", "2026-10-10T12:01:41Z"), known))

    def test_newer_uncertainty_and_any_resolution_are_delivered(self):
        known = self.result("NEEDS_RECONCILIATION", "2026-10-10T12:01:41Z")
        self.assertFalse(unchanged_uncertainty(
            self.result("NEEDS_RECONCILIATION", "2026-10-10T12:01:41.000001Z"), known))
        for status in DONE:
            self.assertFalse(unchanged_uncertainty(self.result(status, "2026-10-10T12:00:40Z"), known))

    def test_missing_or_malformed_known_result_is_not_unchanged(self):
        mine = self.result("NEEDS_RECONCILIATION", "2026-10-10T12:00:40Z")
        self.assertFalse(unchanged_uncertainty(mine, None))
        self.assertFalse(unchanged_uncertainty(mine, {"status": "NEEDS_RECONCILIATION"}))
        self.assertFalse(unchanged_uncertainty(mine, {"recorded_at": "not a time"}))


class LedgerMigrationTests(unittest.TestCase):
    OLD_SCHEMA = """
        CREATE TABLE executor_actions (
            action_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
            proposal TEXT NOT NULL, target_key TEXT NOT NULL, phase TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            expected_version TEXT, evidence TEXT, guard_result TEXT, result TEXT,
            delivered INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO executor_actions(action_id,fingerprint,proposal,target_key,phase,attempts,expected_version)
            VALUES('ACT-OLD','f','{"action_id":"ACT-OLD"}','k','UNCERTAIN',1,'102');
    """

    def test_old_schema_gains_columns_without_data_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            connection = sqlite3.connect(path)
            connection.executescript(self.OLD_SCHEMA)
            connection.close()
            with SQLiteExecutionLedger(path) as ledger:
                row = ledger.get("ACT-OLD")
                self.assertEqual((row["phase"], row["attempts"], row["expected_version"]), ("UNCERTAIN", 1, "102"))
                self.assertEqual(row["proposal"], {"action_id": "ACT-OLD"})
                self.assertIsNone(row["write_started_at"])
                self.assertEqual(row["reconcile_attempts"], 0)
                self.assertIsNone(row["next_reconcile_at"])
                self.assertTrue(ledger.reconcile_due("ACT-OLD", NOW))
                ledger.schedule_reconcile("ACT-OLD", NOW)
            with SQLiteExecutionLedger(path) as ledger:  # Reopening is idempotent.
                row = ledger.get("ACT-OLD")
                self.assertEqual((row["phase"], row["attempts"], row["reconcile_attempts"]), ("UNCERTAIN", 1, 1))
                self.assertEqual(row["write_started_at"], "2026-10-10T12:00:40.000000Z")
                self.assertEqual(row["next_reconcile_at"], "2026-10-10T12:00:42.000000Z")
                self.assertEqual(ledger.pending_proposals(), [{"action_id": "ACT-OLD"}])
                # The fence journal is added too, and an old in-doubt row can be fenced.
                self.assertEqual(ledger.fences("ACT-OLD"), [])
                ledger.record_fence("ACT-OLD", "a" * 32, NOW)
                ledger.confirm_fence("ACT-OLD", "a" * 32, "103")
                self.assertEqual(ledger.fences("ACT-OLD"), [{"nonce": "a" * 32, "started_at": "2026-10-10T12:00:40.000000Z",
                                                              "committed_version": "103"}])

    def test_only_an_action_still_in_doubt_can_be_fenced(self):
        with tempfile.TemporaryDirectory() as directory, SQLiteExecutionLedger(Path(directory) / "l.db") as ledger:
            proposal = load_fixtures()["proposal"]
            with self.assertRaises(RuntimeError):
                ledger.record_fence(proposal["action_id"], "a" * 32, NOW)  # Unknown action.
            ledger.prepare(proposal)
            with self.assertRaises(RuntimeError):
                ledger.record_fence(proposal["action_id"], "a" * 32, NOW)  # Never written: nothing to fence.
            ledger.adopt(proposal, NOW)
            ledger.record_fence(proposal["action_id"], "a" * 32, NOW)
            with self.assertRaises(sqlite3.IntegrityError):
                ledger.record_fence(proposal["action_id"], "a" * 32, NOW)  # A nonce is journaled once.
            ledger.finish(proposal["action_id"], {"status": "CONFLICT", "action_id": proposal["action_id"]})
            with self.assertRaises(RuntimeError):
                ledger.record_fence(proposal["action_id"], "b" * 32, NOW)
            self.assertEqual(len(ledger.fences(proposal["action_id"])), 1)


if __name__ == "__main__":
    unittest.main()
