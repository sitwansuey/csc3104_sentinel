"""Model checking the Executor against writes that may still be in flight.

The simulator and checker live in tests/support. The checker drives the real
execute() and reconcile() of two hosts; see executor_model.INVARIANTS. Nothing
here contacts a cluster. SENTINEL_MODEL_SEEDS widens the seed range locally.
"""

from contextlib import redirect_stderr, suppress
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
from uuid import uuid4

from services.executor import core
from services.executor.kubernetes_adapter import KubectlDeploymentAPI, WriteOutcomeUnknown
from services.executor.reconciliation import FENCE_ANNOTATION, Basis, Decision, classify, fence_marker
from tests.support import executor_model as model
from tests.support.sim_apiserver import (
    ConditionalWriteConflict, PatchFailed, SimAPIServer, SimDeploymentAPI, SimKubectl, SimTimeout,
    SimWriteRejected, apply_json_patch, fence_patch, is_fence, restore_patch,
)


ROOT = Path(__file__).resolve().parents[1]
PRE_FIX = "d5b2ed9"  # main before the reconciliation decision table


def _commit_available(revision):
    try:
        found = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", revision + "^{commit}"],
                               capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return found.returncode == 0


class JsonPatchTests(unittest.TestCase):
    document = {"metadata": {"uid": "u1", "resourceVersion": "7", "annotations": {"a/b": "x"}},
                "spec": {"list": [1, 2], "flag": True}}

    def test_test_add_and_replace_apply_to_a_copy(self):
        result = apply_json_patch(self.document, [
            {"op": "test", "path": "/metadata/uid", "value": "u1"},
            {"op": "replace", "path": "/spec/flag", "value": False},
            {"op": "add", "path": "/metadata/annotations/new", "value": "y"},
            {"op": "add", "path": "/spec/list/-", "value": 3},
            {"op": "add", "path": "/spec/list/0", "value": 0},
            {"op": "replace", "path": "/spec/list/1", "value": 10},
        ])
        self.assertEqual(result["spec"], {"list": [0, 10, 2, 3], "flag": False})
        self.assertEqual(result["metadata"]["annotations"], {"a/b": "x", "new": "y"})
        self.assertEqual(self.document["spec"], {"list": [1, 2], "flag": True})

    def test_a_failing_operation_commits_nothing(self):
        for operations in (
            [{"op": "replace", "path": "/spec/flag", "value": False},
             {"op": "test", "path": "/metadata/resourceVersion", "value": "6"}],
            [{"op": "replace", "path": "/spec/missing", "value": 1}],
            [{"op": "add", "path": "/metadata/labels/app", "value": "x"}],
            [{"op": "test", "path": "/spec/flag", "value": 1}],  # true is not the number 1
            [{"op": "test", "path": "/metadata/missing", "value": None}],
            [{"op": "replace", "path": "/spec/list/2", "value": 1}],
            [{"op": "add", "path": "/spec/list/01", "value": 1}],
        ):
            with self.subTest(operations=operations), self.assertRaises(PatchFailed):
                apply_json_patch(self.document, operations)

    def test_pointer_escapes_follow_rfc_6901(self):
        result = apply_json_patch(self.document, [{"op": "test", "path": "/metadata/annotations/a~1b", "value": "x"},
                                                  {"op": "add", "path": "/metadata/annotations/c~0d", "value": "z"}])
        self.assertEqual(result["metadata"]["annotations"]["c~d"], "z")

    def test_unmodelled_operations_and_root_paths_are_refused(self):
        for operation in ({"op": "remove", "path": "/spec/flag"}, {"op": "move", "from": "/a", "path": "/b"},
                          {"op": "replace", "path": "", "value": {}}, {"op": "add", "path": "/spec/flag"}):
            with self.subTest(operation=operation), self.assertRaises(PatchFailed):
                apply_json_patch(self.document, [operation])
        with self.assertRaises(PatchFailed):
            apply_json_patch(self.document, {"op": "test"})


class SimAPIServerTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = model.load_fixtures()
        self.server = SimAPIServer.from_snapshot(self.fixtures["live_state"])
        self.v0 = self.server.resource_version

    def guarded(self, version, uid=None):
        return [{"op": "test", "path": "/metadata/uid", "value": uid or self.server.uid},
                {"op": "test", "path": "/metadata/resourceVersion", "value": version},
                {"op": "add", "path": "/metadata/annotations", "value": {"ours": "1"}}]

    def test_versions_only_increase_and_never_repeat(self):
        seen = [int(self.v0)]
        self.server.external_write(lambda d: d["spec"].update(replicas=3))
        seen.append(int(self.server.resource_version))
        self.assertEqual(self.server.deployment["metadata"]["generation"], 3)
        self.assertFalse(self.server.external_write(lambda d: None))  # No change, no new version.
        self.assertEqual(int(self.server.resource_version), seen[-1])
        self.server.delete()
        self.server.recreate("uid-2")
        seen.append(int(self.server.resource_version))
        self.server.recreate("uid-3")
        seen.append(int(self.server.resource_version))
        self.assertEqual(seen, sorted(set(seen)))
        self.assertEqual(self.server.uid, "uid-3")
        self.assertEqual(self.server.deployment["metadata"]["generation"], 1)

    def test_request_in_flight_commits_only_while_its_tests_pass(self):
        late = self.server.submit("A", "act", self.guarded(self.v0))
        self.assertTrue(self.server.can_commit(late))
        self.assertEqual(self.server.resource_version, self.v0)  # A dry run changes nothing.
        self.assertTrue(self.server.deliver(late))
        self.assertEqual(late.state, "committed")
        self.assertEqual(late.resource_version, self.server.resource_version)
        stale = self.server.submit("B", "act", self.guarded(self.v0))
        self.assertFalse(self.server.can_commit(stale))
        self.assertFalse(self.server.deliver(stale))
        self.assertEqual(stale.state, "rejected")
        self.assertEqual(self.server.committed("act"), [late])

    def test_a_lost_request_never_commits_and_cannot_be_delivered(self):
        request = self.server.submit("A", "act", self.guarded(self.v0))
        self.server.drop(request)
        self.assertFalse(self.server.can_commit(request))
        self.assertEqual(self.server.in_flight(), [])
        with self.assertRaises(ValueError):
            self.server.deliver(request)

    def test_a_fence_and_a_write_tested_at_the_same_version_cannot_both_commit(self):
        target = {"uid": self.server.uid}
        template = json.loads(json.dumps(self.server.deployment["spec"]["template"]))
        template.setdefault("metadata", {})["labels"] = {"restored": "1"}
        write = self.server.submit("A", "act", restore_patch(target, self.v0, template, {"attempt": "1"},
                                                             self.server.deployment["metadata"].get("annotations")))
        fence = self.server.submit("A", "act", fence_patch(target, self.v0, fence_marker("act", "a" * 32),
                                                           self.server.deployment["metadata"].get("annotations")),
                                   kind="fence")
        self.assertEqual((self.server.in_flight("act"), self.server.in_flight("act", kind=None)), ([write], [write, fence]))
        spec = json.loads(json.dumps(self.server.deployment["spec"]))
        self.assertTrue(self.server.deliver(fence))
        self.assertFalse(self.server.deliver(write))  # The fence moved the version past V0.
        self.assertEqual((self.server.committed("act"), self.server.committed("act", kind="fence")), ([], [fence]))
        self.assertEqual(self.server.deployment["spec"], spec)  # Metadata only, so no rollout.
        self.assertIn(FENCE_ANNOTATION, self.server.deployment["metadata"]["annotations"])

    def test_admission_can_deny_a_template_change_but_allow_a_fence(self):
        self.server.admission = lambda old, new: old["spec"]["template"] == new["spec"]["template"]
        template = json.loads(json.dumps(self.server.deployment["spec"]["template"]))
        template.setdefault("metadata", {})["labels"] = {"restored": "1"}
        operations = restore_patch({"uid": self.server.uid}, self.v0, template, {"attempt": "1"}, None)
        self.assertFalse(self.server.allowed(operations))
        with self.assertRaises(PatchFailed):
            self.server.patch(operations)
        self.assertEqual(self.server.resource_version, self.v0)  # A denial commits nothing.
        fence = fence_patch({"uid": self.server.uid}, self.v0, fence_marker("act", "a" * 32), None)
        self.assertTrue(self.server.allowed(fence))
        self.assertTrue(self.server.patch(fence))
        self.assertNotEqual(self.server.resource_version, self.v0)

    def test_delete_and_recreate_defeat_old_requests(self):
        request = self.server.submit("A", "act", self.guarded(self.v0))
        self.server.delete()
        self.assertFalse(self.server.can_commit(request))
        self.server.recreate("uid-new")
        self.assertFalse(self.server.can_commit(request))
        self.assertFalse(self.server.deliver(request))
        self.server.delete()
        with self.assertRaises(PatchFailed):
            self.server.patch([])
        with self.assertRaises(PatchFailed):
            self.server.external_write(lambda d: None)


class AdapterFidelityTests(unittest.TestCase):
    """The simulated adapter sends exactly what the production kubectl adapter sends."""

    def setUp(self):
        fixtures = model.load_fixtures()
        self.snapshot = fixtures["live_state"]
        self.proposal = fixtures["proposal"]
        self.target = self.proposal["target"]
        self.template = fixtures["baseline"]["pod_template"]
        self.evidence = {"action_id": self.proposal["action_id"], "proposal_sha256": "a" * 64,
                         "target_uid": self.target["uid"],
                         "template_sha256": self.proposal["parameters"]["template_sha256"], "attempt_id": "b" * 32}

    def production(self, server, before=None):
        runner = SimKubectl(server, host="A", action_id=self.proposal["action_id"], before=before)
        api = KubectlDeploymentAPI(context="sim", cluster_id=self.target["cluster_id"], kubeconfig="unused-kubeconfig",
                                   allowed_targets=[self.target], runner=runner)
        return api, runner

    def simulated(self, server):
        return SimDeploymentAPI(server, "A", action_id=self.proposal["action_id"])

    def restore(self, api):
        return api.conditional_restore(target=self.target, version=self.proposal["based_on_resource_version"],
                                       template=self.template, evidence=self.evidence)

    def test_same_operations_and_result_with_and_without_annotations(self):
        for annotations in (None, {"example.org/existing": "1"}):
            with self.subTest(annotations=annotations):
                servers = []
                for build in (self.production, self.simulated):
                    server = SimAPIServer.from_snapshot(self.snapshot)
                    if annotations is not None:
                        server.deployment["metadata"]["annotations"] = dict(annotations)
                    api = build(server)
                    self.restore(api[0] if isinstance(api, tuple) else api)
                    servers.append(server)
                production, simulated = servers
                self.assertEqual(production.requests[0].operations, simulated.requests[0].operations)
                self.assertEqual(production.deployment, simulated.deployment)
                self.assertEqual(production.committed()[0].state, "committed")

    def test_same_outcomes_for_a_race_and_a_missing_or_replaced_target(self):
        # rv-race: another writer bumps the version between the pre-read and the patch.
        server = SimAPIServer.from_snapshot(self.snapshot)

        def race(verb, command):
            if verb == "patch":
                server.external_write(lambda d: d["metadata"].setdefault("annotations", {}).update(bump="1"))
        api, _ = self.production(server, before=race)
        with self.assertRaises(WriteOutcomeUnknown):  # kubectl exits 1: rejection and loss look alike.
            self.restore(api)
        self.assertEqual(server.committed(), [])
        server = SimAPIServer.from_snapshot(self.snapshot)
        simulated = self.simulated(server)
        simulated.fate = "race"
        with self.assertRaises(SimWriteRejected):
            self.restore(simulated)
        self.assertEqual(server.committed(), [])
        for change in ("delete", "recreate"):
            for build in (lambda s: self.production(s)[0], self.simulated):
                with self.subTest(change=change, adapter=build):
                    server = SimAPIServer.from_snapshot(self.snapshot)
                    server.delete()
                    if change == "recreate":
                        server.recreate("uid-new")
                    with self.assertRaises(ConditionalWriteConflict):
                        self.restore(build(server))
                    self.assertEqual(server.requests, [])

    def test_same_fence_operations_and_result_with_and_without_annotations(self):
        marker = fence_marker(self.proposal["action_id"], "c" * 32)
        for annotations in (None, {"example.org/existing": "1"}):
            with self.subTest(annotations=annotations):
                servers = []
                for build in (lambda s: self.production(s)[0], self.simulated):
                    server = SimAPIServer.from_snapshot(self.snapshot)
                    if annotations is not None:
                        server.deployment["metadata"]["annotations"] = dict(annotations)
                    build(server).fence(target=self.target, version=self.proposal["based_on_resource_version"],
                                        marker=marker)
                    servers.append(server)
                production, simulated = servers
                self.assertEqual(production.requests[0].operations, simulated.requests[0].operations)
                self.assertEqual([r.kind for r in production.requests + simulated.requests], ["fence", "fence"])
                self.assertTrue(is_fence(production.requests[0].operations))
                self.assertEqual(production.deployment, simulated.deployment)
                self.assertEqual(production.committed(), [])  # A fence is not a write of the action.

    def test_fence_fates_mirror_a_lost_response_a_request_in_flight_and_a_dropped_request(self):
        version = self.proposal["based_on_resource_version"]
        marker = fence_marker(self.proposal["action_id"], "d" * 32)
        for fate, state in (("lost", "committed"), ("queued", "in_flight"), ("dropped", "dropped")):
            with self.subTest(fate=fate):
                server = SimAPIServer.from_snapshot(self.snapshot)
                simulated = self.simulated(server)
                simulated.fence_fate = fate
                with self.assertRaises(SimTimeout):
                    simulated.fence(target=self.target, version=version, marker=marker)
                self.assertEqual(server.requests[0].state, state)
                self.assertEqual(simulated.fence_fate, "ok")
        server = SimAPIServer.from_snapshot(self.snapshot)
        server.external_write(lambda d: d["metadata"].setdefault("annotations", {}).update(bump="1"))
        with self.assertRaises(ConditionalWriteConflict):  # The pre-read sees the moved version.
            self.simulated(server).fence(target=self.target, version=version, marker=marker)
        self.assertEqual(server.requests, [])

    def test_peer_race_lands_a_peer_write_before_our_pre_send_read(self):
        server = SimAPIServer.from_snapshot(self.snapshot)
        peer = server.submit("B", self.proposal["action_id"], [{"op": "test", "path": "/metadata/uid",
                                                                "value": self.target["uid"]},
                                                               {"op": "add", "path": "/metadata/annotations",
                                                                "value": {"peer": "1"}}])
        other = server.submit("B", "another-action", [{"op": "add", "path": "/metadata/labels",
                                                       "value": {"other": "1"}}])
        simulated = self.simulated(server)
        simulated.fate = "peer-race"
        with self.assertRaises(ConditionalWriteConflict):
            self.restore(simulated)
        self.assertEqual(server.committed(), [peer])  # Never another action's request, and ours was not sent.
        self.assertEqual((other.state, simulated.fate, len(server.requests)), ("in_flight", "ok", 2))
        # With no peer write in flight it behaves like ok.
        server = SimAPIServer.from_snapshot(self.snapshot)
        simulated = self.simulated(server)
        simulated.fate = "peer-race"
        self.restore(simulated)
        self.assertEqual([request.host for request in server.committed()], ["A"])


def _legacy_classify(row, proposal, observation, rejection=None, **ignored):
    """Main's binary rule: APPLIED on this host's exact evidence, otherwise in doubt."""
    if observation is None or not observation.present:
        return Decision("NEEDS_RECONCILIATION", Basis.READ_UNAVAILABLE, None)
    decision = classify(row, proposal, observation)
    if decision.status == "APPLIED" and decision.writer == "own":
        return decision
    return Decision("NEEDS_RECONCILIATION", Basis.UNCHANGED_IN_DOUBT, observation.resource_version)


def _replace_in_doubt(status):
    def variant(row, proposal, observation, rejection=None, **options):
        decision = classify(row, proposal, observation, rejection, **options)
        if decision.basis is Basis.UNCHANGED_IN_DOUBT:
            return Decision(status, Basis.UNCHANGED_IN_DOUBT, decision.version)
        return decision
    return variant


def _applied_on_any_move(row, proposal, observation, rejection=None, **options):
    """Bad: trusts a moved resourceVersion instead of this action's evidence."""
    decision = classify(row, proposal, observation, rejection, **options)
    if decision.basis is Basis.ADVANCED_WITHOUT_EVIDENCE:
        return Decision("APPLIED", Basis.EVIDENCE_MATCH, decision.version)
    return decision


def _no_adoption_after_write_conflict():
    """Bad: after a refused conditional write, records CONFLICT without checking for a peer's commit."""
    adopt = core._adopted_write

    def variant(proposal, adapters, snapshot=None):
        # Only the ConditionalWriteConflict path calls this without a snapshot.
        return None if snapshot is None else adopt(proposal, adapters, snapshot)
    return variant


def _retrying_execute(proposal, adapters):
    """Bad: treats a timed-out write as failed and sends it again."""
    result = core.execute(proposal, adapters)
    if result["status"] == "NEEDS_RECONCILIATION":
        adapters.ledger.db.execute("UPDATE executor_actions SET phase='PREPARED' WHERE action_id=?",
                                   (proposal["action_id"],))
        result = core.execute(proposal, adapters)
    return result


class ModelCheckerTests(unittest.TestCase):
    def assertHolds(self, report, codes):
        failing = [code for code in codes if report.failures[code]]
        if failing:
            self.fail("\n\n".join(model.explain(report.first[code], code) for code in failing))

    def test_invariants_and_liveness_hold_for_the_ci_seeds(self):
        report = model.check(model.seeds_from_environment())  # Fencing on, as the default.
        self.assertHolds(report, model.SAFETY + model.LIVENESS + model.ADOPTION + model.FENCING)
        self.assertGreaterEqual(report.seeds, 1)
        if report.seeds >= model.DEFAULT_SEEDS:
            # The schedules reach every outcome the table can produce today.
            for status in ("APPLIED/EVIDENCE_MATCH", "APPLIED/EVIDENCE_MATCH (peer write)", "CONFLICT", "FAILED",
                           "SUPERSEDED/ADVANCED_WITHOUT_EVIDENCE", "SUPERSEDED/COMMITTED_THEN_OVERWRITTEN",
                           "SUPERSEDED/TARGET_DELETED", "SUPERSEDED/TARGET_REPLACED", "CONFLICT/FENCED",
                           "NEEDS_RECONCILIATION/UNCHANGED_IN_DOUBT", "NEEDS_RECONCILIATION/READ_UNAVAILABLE",
                           "process crash"):
                self.assertGreater(report.observed[status], 10, status)
            self.assertGreater(report.categories["provable"], 0)
            # Fences turn almost every lost-write run into a proof. What is left ends with no host in doubt
            # (for example a FAILED before any write); L1 would fail otherwise.
            self.assertLess(report.categories["unprovable"], 5)

    def test_fencing_resolves_the_runs_that_were_unprovable_without_it(self):
        seeds = range(400)
        without = {seed: model.check_seed(seed, fence_after=None) for seed in seeds}
        unprovable = [seed for seed, outcome in without.items() if outcome.category == "unprovable"]
        self.assertGreater(len(unprovable), 5)
        # Without fencing most of these runs end with a host still in doubt, so the workload stays excluded.
        wedged = {seed: [host for host, statuses in without[seed].statuses.items()
                         if statuses[-1] == "NEEDS_RECONCILIATION/UNCHANGED_IN_DOUBT"] for seed in unprovable}
        wedged = {seed: hosts for seed, hosts in wedged.items() if hosts}
        self.assertGreater(len(wedged), 5)
        for seed in unprovable:
            with self.subTest(seed=seed):
                fenced = model.check_seed(seed)
                self.assertEqual(without[seed].schedule, fenced.schedule)  # Fencing does not change the schedule.
                self.assertEqual(fenced.codes, set())
                if seed in wedged:
                    self.assertEqual(fenced.category, "provable")
                    final = {host: statuses[-1] for host, statuses in fenced.statuses.items()}
                    self.assertIn("CONFLICT/FENCED", final.values())
                    for host in wedged[seed]:
                        self.assertFalse(final[host].startswith("NEEDS_RECONCILIATION"), final)

    def test_known_bad_fence_without_journaling_the_nonce_first_is_caught(self):
        def fence_then_journal(proposal, adapters):
            """Bad: sends the fence, then journals its nonce once the call returns."""
            action_id, nonce = proposal["action_id"], uuid4().hex
            with suppress(ConditionalWriteConflict, SimTimeout, SimWriteRejected):  # Outcome ignored on purpose.
                adapters.cluster.fence(target=proposal["target"], version=proposal["based_on_resource_version"],
                                       marker=fence_marker(action_id, nonce))
            adapters.ledger.record_fence(action_id, nonce, adapters.clock())
            row = adapters.ledger.get(action_id)
            return core._classify(row, proposal, core._observation(proposal, adapters), adapters)

        with mock.patch.object(core, "_fence", fence_then_journal):
            report = model.check(range(400))
            self.assertGreater(report.failures["F2"], 0)
            self.assertFalse(report.fencing_holds)
            self.assertHolds(report, model.SAFETY)  # Forgetting a fence is safe, but loses the proof.
            self.assertIn("before journaling its nonce", model.explain(report.first["F2"], "F2"))
            # A crash between the fence landing and the late journal entry: the restarted host can only
            # say SUPERSEDED although its own fence is on the object.
            crashed = model.check_seed(985)
        self.assertIn("ledger.record_fence.before", " ".join(crashed.trace))
        self.assertTrue(any("own committed fence" in message for _, _, message in crashed.violations))
        self.assertEqual(model.check_seed(985).codes, set())

    def test_known_bad_fence_assumed_to_have_committed_is_caught(self):
        fence = core._fence

        def assume_committed(proposal, adapters):
            """Bad: treats a fence whose outcome is unknown as committed."""
            decision = fence(proposal, adapters)
            if decision.basis is Basis.UNCHANGED_IN_DOUBT:
                return Decision("CONFLICT", Basis.FENCED, decision.version)
            return decision

        with mock.patch.object(core, "_fence", assume_committed):
            report = model.check(range(400))
            explanation = model.explain(report.first["F1"], "F1")
        self.assertGreater(report.failures["F1"], 0)
        self.assertGreater(report.failures["I3"], 0)  # Our write could still land after the false CONFLICT.
        self.assertFalse(report.safety_holds)
        self.assertIn("none of its fences committed", explanation)

    def test_schedules_are_deterministic(self):
        self.assertEqual(model.generate(17), model.generate(17))
        self.assertNotEqual(model.generate(17), model.generate(18))
        self.assertEqual(model.check_seed(17).trace, model.check_seed(17).trace)

    def test_known_bad_classifier_conflict_on_an_unchanged_timeout_is_caught(self):
        with mock.patch.object(core, "classify", _replace_in_doubt("CONFLICT")):
            report = model.check(range(200))
            self.assertGreater(report.failures["I3"], 0)
            explanation = model.explain(report.first["I3"], "I3")
        self.assertIn("seed " + str(report.first["I3"].schedule.seed), explanation)
        self.assertIn("can still commit", explanation)
        minimal = explanation.split("Minimal trace:\n")[1]
        self.assertLessEqual(len([line for line in minimal.splitlines() if line.strip()[:2].isdigit()]), 3)

    def test_each_safety_invariant_catches_its_own_bug(self):
        cases = [("I1", {"execute": _retrying_execute}, None),
                 ("I2", {}, _applied_on_any_move),
                 ("I3", {}, _replace_in_doubt("SUPERSEDED")),
                 ("I5", {}, _replace_in_doubt("FAILED"))]
        for code, options, classifier in cases:
            with self.subTest(code=code):
                with mock.patch.object(core, "classify", classifier or core.classify):
                    report = model.check(range(200), **options)
                self.assertGreater(report.failures[code], 0)
                self.assertFalse(report.safety_holds)

    def test_conflict_while_a_peer_commit_is_still_on_the_object_is_caught(self):
        # The host's own write never committed, so I1-I5 all hold; only P1 sees the missed adoption.
        with mock.patch.object(core, "_adopted_write", _no_adoption_after_write_conflict()):
            report = model.check(range(600))
            self.assertGreater(report.failures["P1"], 0)
            self.assertHolds(report, model.SAFETY)
            explanation = model.explain(report.first["P1"], "P1")
        self.assertFalse(report.adoption_holds)
        self.assertIn("peer's write of the action committed", explanation)
        self.assertIn("peer-race -> CONFLICT", explanation)

    def test_main_binary_rule_stays_safe_but_wedges(self):
        with mock.patch.object(core, "classify", _legacy_classify):
            report = model.check(range(300), fence_after=None)  # main had no fencing
            self.assertHolds(report, model.SAFETY)
            self.assertGreater(report.failures["I4"], 0)
            self.assertGreater(report.failures["L1"], 0)
            self.assertIn("TargetBusy", model.explain(report.first["L1"], "L1", fence_after=None))
            # Only this host's evidence counts, so a peer's applied write reads as a conflict.
            self.assertGreater(report.failures["P1"], 0)

    @unittest.skipUnless(_commit_available(PRE_FIX), "pre-fix commit is not in this clone")
    def test_pre_fix_main_executor_wedges_but_stays_safe(self):
        # The real d5b2ed9 services/ and shared/ packages, exported with git archive.
        completed = subprocess.run([sys.executable, str(ROOT / "scripts/validation/executor_model_check.py"),
                                    "--against", PRE_FIX, "--seeds", "300"],
                                   capture_output=True, text=True, timeout=300, check=False)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["executor"], "pre-table")
        self.assertFalse(summary["fencing"])  # The pre-fix Executor has no fence to turn on.
        self.assertTrue(summary["safety_holds"])
        self.assertGreater(summary["failing_seeds"]["L1"], 0)
        self.assertGreater(summary["failing_seeds"]["I4"], 0)
        self.assertGreater(summary["failing_seeds"]["P1"], 0)
        self.assertFalse(summary["adoption_holds"])

    def test_seed_range_comes_from_the_environment(self):
        for value, expected in (("", range(model.DEFAULT_SEEDS)), ("50", range(50)), ("10:20", range(10, 20))):
            with self.subTest(value=value), mock.patch.dict("os.environ", {"SENTINEL_MODEL_SEEDS": value}):
                self.assertEqual(model.seeds_from_environment(), expected)


class ModelCheckCommandTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "scripts/validation"))
        self.addCleanup(sys.path.remove, str(ROOT / "scripts/validation"))
        import executor_model_check
        self.command = executor_model_check

    def test_unsafe_revisions_are_refused_before_git_runs(self):
        calls = []
        for revision in ("--output=/tmp/x", "a..b", "", "x" * 101, "rev;rm"):
            with self.subTest(revision=revision):
                with redirect_stderr(io.StringIO()):
                    code = self.command.main(["--against=" + revision, "--seeds", "1"],
                                             runner=lambda *a, **k: calls.append(a))
                self.assertEqual(code, 3)
        self.assertEqual(calls, [])

    def test_failed_export_stops_before_running_anything(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 128, b"", b"fatal")
        with redirect_stderr(io.StringIO()):
            code = self.command.main(["--against", "no-such-rev", "--seeds", "1"], runner=runner)
        self.assertEqual(code, 3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:3], ["git", "-C", str(ROOT)])
        self.assertIn("services", calls[0])


if __name__ == "__main__":
    unittest.main()
