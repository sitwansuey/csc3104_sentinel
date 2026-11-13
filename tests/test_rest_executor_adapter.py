"""The Executor's structured Kubernetes REST transport (P5.08), offline.

The REST adapter talks real HTTP to tests/support/fake_apiserver.py, which
fronts the same simulated Deployment (tests/support/sim_apiserver.py) that the
kubectl stand-in SimKubectl drives. TransportParityMixin runs one set of
Executor scenarios over both transports: their outcomes agree everywhere
except where the REST transport knows more, a definite rejection, which it
reports at once instead of leaving the write in doubt. Nothing here contacts a
cluster, and offline is not live.
"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from experiments.distributed.topology import find_openssl, make_certificates
from experiments.validation.demo_guard import load_fixtures
from runtime.executor_pipeline import GuardAuthorizer
from services.executor.core import (
    ACTION_ANNOTATION, SEAL_ATTEMPTS, ConditionalWriteConflict, ExecutionAdapters, ReadUnavailable, TargetAbsent,
    WriteRejected, execute, reconcile, seal,
)
from services.executor.kubernetes_adapter import KubectlDeploymentAPI, LiveAdapterError, WriteOutcomeUnknown
from services.executor.reconciliation import (
    FENCE_ANNOTATION, Basis, Decision, Observation, classify, fence_marker, observe,
)
from services.executor.rest_adapter import (
    DEFINITE_REJECTIONS, FIELD_MANAGER, KubernetesRESTDeploymentAPI, kubeconfig_credentials,
)
from services.executor.store import LocalTargetCoordinator, SQLiteExecutionLedger, TargetBusy, fingerprint
from services.guard.core import proposal_fingerprint
from shared.canonical import canonical_json
from shared.contracts import validate_message
from tests.support.fake_apiserver import PEER_ATTEMPT, PRIVATE, FakeAPIServer, bump
from tests.support.sim_apiserver import SimAPIServer, SimKubectl
import tests.test_executor as executor_suite  # A module import, so its TestCases are not collected twice.
from tests.test_k8s_executor_adapter import Runner
from tests.test_scale_action import (
    approval_for, baseline as scale_baseline, dependencies_for, live_state as scale_live_state, scale_policy,
    scale_proposal,
)


NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=UTC)
TOKEN = "fixture-bearer-" + "t" * 40  # A test value; no cluster ever sees it.


def basis(result):
    details = result["details"]
    return details[len("[basis="):details.index("]")] if details.startswith("[basis=") else None


def dead_port():
    """A loopback port with nothing listening, so a connection is refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def captured_logs():
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler, logger = Keep(level=logging.DEBUG), logging.getLogger("sentinel")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def log_text(records):
    return "\n".join(record.getMessage() + " " + json.dumps(getattr(record, "sentinel_fields", {}), default=str)
                     for record in records)


def evidence_for(target, template, *, action_id="ACT-REST-001"):
    return {"action_id": action_id, "proposal_sha256": "a" * 64, "template_sha256": fingerprint(template),
            "target_uid": target["uid"], "attempt_id": "b" * 32}


class TransportParityMixin:
    """One set of Executor scenarios, run over each transport by a concrete subclass.

    A subclass sets EXPECTED for the scenarios whose outcome depends on what
    the transport can know, and implements make_cluster, inject and patches.
    """

    EXPECTED = {}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.ledger = SQLiteExecutionLedger(self.directory / "executor.db")
        self.addCleanup(self.ledger.close)
        self.coordination = LocalTargetCoordinator(self.directory / "locks")
        self.fixtures = load_fixtures()
        self.proposal = self.fixtures["proposal"]
        self.approval = {
            "action_id": self.proposal["action_id"], "proposal_sha256": proposal_fingerprint(self.proposal),
            "policy_version": self.fixtures["policy"]["policy_version"],
            "operator_id": "fabricated-test-operator", "decision": "APPROVED",
            "approved_at": (NOW - timedelta(seconds=5)).isoformat(),
            "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
        }
        self.authorizer = GuardAuthorizer(
            policy_reader=lambda p: deepcopy(self.fixtures["policy"]),
            baseline_reader=lambda p: deepcopy(self.fixtures["baseline"]),
            dependency_reader=lambda p: deepcopy(self.fixtures["dependencies"]),
            approval_reader=lambda p: deepcopy(self.approval))
        self.use(SimAPIServer.from_snapshot(self.fixtures["live_state"]))

    def use(self, sim):
        self.sim = sim
        self.cluster = self.make_cluster(sim, [deepcopy(self.proposal["target"])])

    def adapters(self, **changes):
        values = dict(ledger=self.ledger, coordination=self.coordination, cluster=self.cluster,
                      authorizer=self.authorizer, clock=lambda: NOW)
        values.update(changes)
        return ExecutionAdapters(**values)

    def run_action(self, **changes):
        return execute(deepcopy(self.proposal), self.adapters(**changes))

    def outcome(self, result):
        validate_message(result)
        return result["status"], basis(result)

    def write_commits(self):
        return [request for request in self.sim.requests if request.state == "committed" and request.kind == "write"]

    # Scenarios -------------------------------------------------------------------------------------------

    def test_rollback_applies_with_one_patch_and_a_replay_sends_no_other(self):
        result = self.run_action()
        self.assertEqual(self.outcome(result), ("APPLIED", "EVIDENCE_MATCH"))
        self.assertEqual(self.patches(), 1)
        deployment = self.sim.deployment
        self.assertEqual(deployment["spec"]["template"], self.fixtures["baseline"]["pod_template"])
        self.assertIn(ACTION_ANNOTATION, deployment["metadata"]["annotations"])
        self.assertEqual(self.run_action(), result)
        self.assertEqual(self.patches(), 1)

    def test_scale_writes_only_the_replica_count_with_one_patch(self):
        live = scale_live_state(0, autoscalers=None)
        self.use(SimAPIServer(cluster_id=live["cluster_id"], deployment=deepcopy(live["deployment"]),
                              revision=int(live["deployment"]["metadata"]["resourceVersion"])))
        proposal, policy, base = self.scale_case(live)
        template = deepcopy(self.sim.deployment["spec"]["template"])
        result = execute(proposal, self.adapters(authorizer=self.scale_authorizer(proposal, policy, base)))
        self.assertEqual(self.outcome(result), ("APPLIED", "EVIDENCE_MATCH"))
        self.assertEqual(self.patches(), 1)
        self.assertEqual(self.sim.deployment["spec"]["replicas"], 1)
        self.assertEqual(self.sim.deployment["spec"]["template"], template)
        evidence = json.loads(self.sim.deployment["metadata"]["annotations"][ACTION_ANNOTATION])
        self.assertEqual((evidence["action"], evidence["replicas"]), ("SCALE", 1))

    def test_scale_with_an_autoscaler_escalates_and_sends_no_patch(self):
        live = scale_live_state(0, autoscalers=None)
        self.use(SimAPIServer(cluster_id=live["cluster_id"], deployment=deepcopy(live["deployment"]),
                              revision=int(live["deployment"]["metadata"]["resourceVersion"]),
                              autoscalers=[{"metadata": {"name": "demo-api-hpa"}, "spec": {"scaleTargetRef": {
                                  "apiVersion": "apps/v1", "kind": "Deployment", "name": "demo-api"}}}]))
        proposal, policy, base = self.scale_case(live)
        result = execute(proposal, self.adapters(authorizer=self.scale_authorizer(proposal, policy, base)))
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("SCALE_AUTOSCALER_PRESENT", result["details"])
        self.assertEqual(self.patches(), 0)

    def scale_case(self, live):
        policy, base = scale_policy(), scale_baseline()
        return scale_proposal(live, base), policy, base

    def scale_authorizer(self, proposal, policy, base):
        return GuardAuthorizer(policy_reader=lambda p: deepcopy(policy), baseline_reader=lambda p: deepcopy(base),
                               dependency_reader=dependencies_for,
                               approval_reader=lambda p: approval_for(proposal, policy))

    def test_a_version_moved_before_the_read_is_conflict_without_a_patch(self):
        self.sim.external_write(bump)
        self.assertEqual(self.run_action()["status"], "CONFLICT")
        self.assertEqual(self.patches(), 0)

    def test_a_version_moved_between_the_pre_read_and_the_patch(self):
        self.inject("bump-first")
        result = self.run_action()
        self.assertEqual(self.outcome(result), self.EXPECTED["bump-first"])
        self.assertEqual(self.patches(), 1)
        self.assertEqual(self.write_commits(), [])
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["phase"], "DONE")

    def test_an_admission_denial_with_the_version_unchanged(self):
        self.sim.admission = lambda old, new: old["spec"] == new["spec"]  # Template writes denied, metadata allowed.
        version = self.sim.resource_version
        result = self.run_action(fence_after_seconds=0)
        self.assertEqual(self.outcome(result), self.EXPECTED["deny"])
        self.assertEqual(self.sim.resource_version, version)
        self.assertEqual(self.patches(), 1)
        again = reconcile(deepcopy(self.proposal), self.adapters(fence_after_seconds=0))
        self.assertEqual(self.outcome(again), self.EXPECTED["deny-then-reconcile"])
        self.assertEqual(self.patches(), self.EXPECTED["deny-patches"])
        self.assertEqual(self.write_commits(), [])

    def test_a_target_deleted_before_the_patch_is_superseded(self):
        self.inject("delete-first")
        result = self.run_action()
        self.assertEqual(self.outcome(result), ("SUPERSEDED", "TARGET_DELETED"))
        self.assertEqual(self.patches(), 1)

    def test_a_commit_whose_response_is_lost_is_applied_without_a_second_patch(self):
        self.inject("commit-then-drop")
        result = self.run_action()
        self.assertEqual(self.outcome(result), ("APPLIED", "EVIDENCE_MATCH"))
        self.assertEqual((self.patches(), len(self.write_commits())), (1, 1))

    def test_an_error_after_the_commit_is_applied_without_a_second_patch(self):
        self.inject("error-after-commit")
        result = self.run_action()
        self.assertEqual(self.outcome(result), ("APPLIED", "EVIDENCE_MATCH"))
        self.assertEqual((self.patches(), len(self.write_commits())), (1, 1))

    def test_a_request_lost_before_commit_stays_in_doubt_then_is_fenced(self):
        self.inject("drop-before-commit")
        result = self.run_action(fence_after_seconds=0)
        self.assertEqual(self.outcome(result), ("NEEDS_RECONCILIATION", "UNCHANGED_IN_DOUBT"))
        fenced = reconcile(deepcopy(self.proposal), self.adapters(fence_after_seconds=0))
        self.assertEqual(self.outcome(fenced), ("CONFLICT", "FENCED"))
        self.assertEqual(self.patches(), 2)  # The write and one fence.
        self.assertIn(FENCE_ANNOTATION, self.sim.deployment["metadata"]["annotations"])
        self.assertEqual(self.write_commits(), [])

    def test_a_seal_fences_an_abandoned_write(self):
        result = seal(deepcopy(self.proposal), 3, self.adapters())
        self.assertEqual(result["outcome"], "SEALED")
        self.assertEqual(self.sim.deployment["metadata"]["annotations"]["sentinel.io/fence-epoch"], "3")
        self.assertEqual(self.patches(), 1)


class KubectlTransportParityTests(TransportParityMixin, unittest.TestCase):
    """kubectl cannot tell a rejection from a lost response, so both stay unknown until state decides."""

    EXPECTED = {"bump-first": ("SUPERSEDED", "ADVANCED_WITHOUT_EVIDENCE"),
                "deny": ("NEEDS_RECONCILIATION", "UNCHANGED_IN_DOUBT"),
                "deny-then-reconcile": ("CONFLICT", "FENCED"), "deny-patches": 2}

    def make_cluster(self, sim, targets):
        self.faults = []
        self.kubectl = SimKubectl(sim, before=self.fault)
        return KubectlDeploymentAPI(context="kind-team", cluster_id=targets[0]["cluster_id"],
                                    kubeconfig="restricted credentials.json", allowed_targets=targets,
                                    runner=self.kubectl, clock=lambda: NOW)

    def inject(self, mode):
        self.faults.append(mode)

    def patches(self):
        return sum(1 for command in self.kubectl.calls if "patch" in command)

    def fault(self, verb, command):
        if verb != "patch" or not self.faults:
            return None
        mode = self.faults.pop(0)
        if mode == "bump-first":
            self.sim.external_write(bump)
        elif mode == "delete-first":
            self.sim.delete()
        elif mode == "drop-before-commit":
            raise subprocess.TimeoutExpired(command, 15)
        else:  # commit-then-drop, error-after-commit
            operations = json.loads(Path(command[command.index("--patch-file") + 1]).read_text(encoding="utf-8"))
            self.sim.deliver(self.sim.submit("kubectl", "unknown", operations))
            if mode == "commit-then-drop":
                raise subprocess.TimeoutExpired(command, 15)
            return subprocess.CompletedProcess(command, 1, b"", b"Error from server (InternalError)")
        return None


class RESTTransportParityTests(TransportParityMixin, unittest.TestCase):
    """The REST transport sees the status code: a definite rejection is decided at once, with no fence."""

    EXPECTED = {"bump-first": ("CONFLICT", "DEFINITE_REJECTION"),
                "deny": ("FAILED", "DEFINITE_REJECTION"),
                "deny-then-reconcile": ("FAILED", "DEFINITE_REJECTION"), "deny-patches": 1}

    def make_cluster(self, sim, targets):
        self.server = FakeAPIServer(sim, token=TOKEN).start()
        self.addCleanup(self.server.close)
        return KubernetesRESTDeploymentAPI(server=self.server.url, cluster_id=targets[0]["cluster_id"],
                                           allowed_targets=targets, token=TOKEN, clock=lambda: NOW)

    def inject(self, mode):
        self.server.fail_next_patch(mode)

    def patches(self):
        return self.server.count("PATCH")


class RESTBackedCluster:
    """The REST adapter in front of a FakeAPIServer, inspected like tests/support/executor_fake.py.

    It gives tests/test_executor.py's ExecutorTests the attributes they read
    (snapshot, write_attempts and writes) while every read and write goes
    over HTTP through KubernetesRESTDeploymentAPI.
    """

    def __init__(self, snapshot, target, test, *, clock):
        self.sim = SimAPIServer.from_snapshot(snapshot)
        self.server = FakeAPIServer(self.sim, token=TOKEN).start()
        test.addCleanup(self.server.close)
        self.api = KubernetesRESTDeploymentAPI(server=self.server.url, cluster_id=snapshot["cluster_id"],
                                               allowed_targets=[deepcopy(target)], token=TOKEN, clock=clock)

    def __getattr__(self, name):
        return getattr(self.api, name)  # read, autoscalers, conditional_restore, conditional_scale, fence, stats

    @property
    def snapshot(self):
        """The stored object itself, so a test can change it in place as it does with the in-memory fake."""
        with self.server.lock:
            if not self.sim.present:
                return None
            return {"cluster_id": self.sim.cluster_id, "deployment": self.sim.deployment}

    @snapshot.setter
    def snapshot(self, value):
        with self.server.lock:
            self.sim.deployment = deepcopy(value["deployment"])

    @property
    def write_attempts(self):
        """Spec write PATCHes the server received (fences excluded)."""
        return [request for request in self.sim.requests if request.kind == "write"]

    @property
    def writes(self):
        return self.sim.committed(kind="write")


class RESTExecutorSuiteTests(executor_suite.ExecutorTests):
    """tests/test_executor.py's ExecutorTests with its default cluster replaced by the REST transport.

    Tests that build their own in-memory fault cluster still use it; every
    other test reads and writes over HTTP, and must reach the same result.
    """

    def setUp(self):
        super().setUp()
        self.cluster = RESTBackedCluster(self.fixtures["live_state"], self.proposal["target"], self,
                                         clock=lambda: NOW)

    def test_fake_api_rejects_cas_mismatch_and_preserves_unrelated_annotations(self):
        """The same check, with valid evidence: the adapter refuses empty evidence before any request."""
        self.cluster.snapshot["deployment"]["metadata"]["annotations"] = {"example.org/owner": "team"}
        target, template = self.proposal["target"], self.fixtures["baseline"]["pod_template"]
        with self.assertRaises(ConditionalWriteConflict):
            self.cluster.conditional_restore(target=target, version="stale", template=template,
                                             evidence=evidence_for(target, template))
        with self.assertRaises(LiveAdapterError):
            self.cluster.conditional_restore(target=target, version=self.cluster.sim.resource_version,
                                             template=template, evidence={})
        self.assertEqual((self.cluster.writes, self.cluster.server.count("PATCH")), ([], 0))
        self.assertEqual(self.run_action()["status"], "APPLIED")
        self.assertEqual(self.cluster.snapshot["deployment"]["metadata"]["annotations"]["example.org/owner"], "team")

    def test_the_default_cluster_is_the_rest_transport(self):
        self.assertIsInstance(self.adapters().cluster.api, KubernetesRESTDeploymentAPI)
        self.assertEqual(self.run_action()["status"], "APPLIED")
        self.assertEqual((self.cluster.server.count("PATCH"), len(self.cluster.writes)), (1, 1))


class RESTExecutorTests(unittest.TestCase):
    """The P5.08 acceptance cases, through execute() and reconcile() with the real Guard."""

    setUp = TransportParityMixin.setUp
    use = TransportParityMixin.use
    adapters = TransportParityMixin.adapters
    run_action = TransportParityMixin.run_action
    outcome = TransportParityMixin.outcome
    write_commits = TransportParityMixin.write_commits
    make_cluster = RESTTransportParityTests.make_cluster

    def test_422_from_a_failed_test_op_is_conflict_with_exactly_one_patch(self):
        self.server.fail_next_patch("bump-first")
        with captured_logs() as records:
            result = self.run_action(fence_after_seconds=0)
        self.assertEqual(self.outcome(result), ("CONFLICT", "DEFINITE_REJECTION"))
        self.assertEqual(self.server.count("PATCH"), 1)
        self.assertEqual(self.cluster.stats["rejected_writes"], 1)
        self.assertEqual(self.cluster.stats["fence_attempts"], 0)
        rejected = [record for record in records if record.getMessage() == "executor.rest.rejected"]
        self.assertEqual([(r.sentinel_fields["http_status"], r.sentinel_fields["reason"],
                           r.sentinel_fields["operation"]) for r in rejected], [(422, "Invalid", "write_attempts")])
        handled = [record for record in records if record.getMessage() == "executor.write.rejected"]
        self.assertEqual([r.sentinel_fields["error_type"] for r in handled], ["WriteRejected"])

    def test_a_peer_commit_of_this_action_before_our_422_is_applied_not_rejected(self):
        # Row R3 runs before R5 and R6: this action's evidence on the object beats our own rejection.
        self.server.fail_next_patch("peer-commits-first")
        result = self.run_action(fence_after_seconds=0)
        self.assertEqual(self.outcome(result), ("APPLIED", "EVIDENCE_MATCH"))
        self.assertIn("adopted peer write", result["details"])
        self.assertEqual(self.server.count("PATCH"), 1)
        self.assertEqual([request.host for request in self.write_commits()], ["peer"])
        self.assertEqual(self.cluster.stats["rejected_writes"], 1)
        self.assertEqual(self.cluster.stats["fence_attempts"], 0)
        self.assertEqual(self.ledger.fences(self.proposal["action_id"]), [])

    def test_403_with_the_version_unchanged_fails_at_once_with_no_fence_and_no_wait(self):
        self.server.fail_next_patch("forbidden")
        version = self.sim.resource_version
        started = time.monotonic()
        result = self.run_action(fence_after_seconds=0)
        self.assertEqual(self.outcome(result), ("FAILED", "DEFINITE_REJECTION"))
        self.assertEqual(result["resource_version"], version)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["phase"], "DONE")
        self.assertEqual(self.ledger.fences(self.proposal["action_id"]), [])
        self.assertEqual(self.server.count("PATCH"), 1)
        # The workload is released: a second action for it is admitted.
        rival = deepcopy(self.proposal)
        rival["action_id"] = "rival-after-rejection"
        self.assertEqual(self.ledger.prepare(rival)["phase"], "PREPARED")

    def test_a_refused_connection_is_failed_with_no_patch_received(self):
        refused = dead_port()
        real = self.cluster._connection

        def connection(method):
            if method == "PATCH":
                import http.client
                return http.client.HTTPConnection("127.0.0.1", refused, timeout=5)
            return real(method)
        self.cluster._connection = connection
        result = self.run_action(fence_after_seconds=0)
        self.assertEqual(self.outcome(result), ("FAILED", "DEFINITE_REJECTION"))
        self.assertEqual(self.server.count("PATCH"), 0)
        self.assertEqual(self.cluster.stats["write_attempts"], 1)
        self.assertEqual(self.sim.requests, [])

    def test_commit_then_drop_reconciles_to_applied_with_no_second_patch(self):
        self.server.fail_next_patch("commit-then-drop")
        result = self.run_action(fence_after_seconds=0)
        self.assertEqual(self.outcome(result), ("APPLIED", "EVIDENCE_MATCH"))
        self.assertEqual(self.server.count("PATCH"), 1)
        self.assertEqual(self.cluster.stats["fence_attempts"], 0)

    def test_a_commit_after_the_client_timeout_is_unknown_then_applied(self):
        self.use(self.sim)
        self.cluster.timeout = 0.3
        self.server.fail_next_patch("delay-then-commit", delay=1.0)
        result = self.run_action()
        self.assertEqual(self.outcome(result), ("NEEDS_RECONCILIATION", "UNCHANGED_IN_DOUBT"))
        self.assertTrue(self.server.landed.wait(5))
        result = reconcile(deepcopy(self.proposal), self.adapters())
        self.assertEqual(self.outcome(result), ("APPLIED", "EVIDENCE_MATCH"))
        self.assertEqual((self.server.count("PATCH"), len(self.write_commits())), (1, 1))

    def test_a_fence_that_wins_against_a_late_commit_leaves_no_spec_commit(self):
        self.use(self.sim)
        self.cluster.timeout = 0.3
        self.server.fail_next_patch("delay-then-commit", delay=1.5)
        self.assertEqual(self.run_action(fence_after_seconds=0)["status"], "NEEDS_RECONCILIATION")
        self.cluster.timeout = 10
        fenced = reconcile(deepcopy(self.proposal), self.adapters(fence_after_seconds=0))
        self.assertEqual(self.outcome(fenced), ("CONFLICT", "FENCED"))
        time.sleep(1.6)  # The late write now arrives and fails its resourceVersion test.
        self.assertEqual(self.write_commits(), [])
        self.assertEqual(self.server.count("PATCH"), 2)

    def test_a_rejected_fence_neither_confirms_the_fence_nor_releases_the_action(self):
        # The fence's own 403 or 422 is about the fence, never about the write: the action stays in doubt.
        for mode in ("forbidden", "status=422"):
            with self.subTest(mode=mode):
                self.setUp()
                self.server.fail_next_patch("drop-before-commit")
                self.assertEqual(self.run_action(fence_after_seconds=0)["status"], "NEEDS_RECONCILIATION")
                self.server.fail_next_patch(mode)
                with captured_logs() as records:
                    result = reconcile(deepcopy(self.proposal), self.adapters(fence_after_seconds=0))
                self.assertEqual(self.outcome(result), ("NEEDS_RECONCILIATION", "UNCHANGED_IN_DOUBT"))
                self.assertEqual(self.server.count("PATCH"), 2)  # The write and the refused fence.
                self.assertEqual([fence["committed_version"] for fence in
                                  self.ledger.fences(self.proposal["action_id"])], [None])
                self.assertEqual(self.ledger.get(self.proposal["action_id"])["phase"], "UNCERTAIN")
                self.assertIn("executor.fence.refused", [record.getMessage() for record in records])
                rival = deepcopy(self.proposal)
                rival["action_id"] = "rival-while-in-doubt"
                with self.assertRaises(TargetBusy):
                    self.ledger.prepare(rival)

    def test_a_seal_whose_fences_are_rejected_stays_unsealed(self):
        for _attempt in range(SEAL_ATTEMPTS):
            self.server.fail_next_patch("forbidden")
        with captured_logs() as records:
            result = seal(deepcopy(self.proposal), 3, self.adapters())
        self.assertEqual(result["outcome"], "UNSEALED")
        self.assertEqual(self.server.count("PATCH"), SEAL_ATTEMPTS)
        self.assertNotIn(FENCE_ANNOTATION, self.sim.deployment["metadata"].get("annotations", {}))
        messages = [record.getMessage() for record in records]
        self.assertEqual(messages.count("executor.seal.refused"), SEAL_ATTEMPTS)
        self.assertNotIn("executor.seal.indeterminate", messages)

    def test_a_transport_that_misreports_a_rejection_leaves_the_write_unknown(self):
        def misreport(**kwargs):
            raise WriteRejected(500, "InternalError")  # A transport bug: the ValueError makes it unknown.
        self.cluster.conditional_restore = misreport
        result = self.run_action()
        self.assertEqual(self.outcome(result), ("NEEDS_RECONCILIATION", "UNCHANGED_IN_DOUBT"))

    def test_500_after_commit_is_unknown_and_never_a_rejection(self):
        target = self.proposal["target"]
        self.server.fail_next_patch("error-after-commit")
        template = self.fixtures["baseline"]["pod_template"]
        with self.assertRaises(WriteOutcomeUnknown):
            self.cluster.conditional_restore(target=target, version=self.sim.resource_version, template=template,
                                             evidence=evidence_for(target, template))
        self.assertEqual(len(self.write_commits()), 1)
        self.assertEqual(self.cluster.stats["rejected_writes"], 0)

    def test_no_token_or_status_message_reaches_errors_logs_results_or_the_ledger(self):
        for mode in ("forbidden", "bump-first", "status=500", "status=401"):
            with self.subTest(mode=mode):
                self.setUp()
                self.server.fail_next_patch(mode)
                with captured_logs() as records:
                    result = self.run_action(fence_after_seconds=0)
                text = "\n".join([log_text(records), json.dumps(result),
                                  json.dumps(self.ledger.get(self.proposal["action_id"]), default=str)])
                self.assertNotIn(TOKEN, text)
                self.assertNotIn("PRIVATE", text)
        target = self.proposal["target"]
        template = self.fixtures["baseline"]["pod_template"]
        for mode in ("forbidden", "status=422", "status=503", "commit-then-drop"):
            with self.subTest(adapter=mode):
                self.server.fail_next_patch(mode)
                with self.assertRaises((WriteRejected, WriteOutcomeUnknown)) as caught:
                    self.cluster.conditional_restore(target=target, version=self.sim.resource_version,
                                                     template=template, evidence=evidence_for(target, template))
                for text in (str(caught.exception), repr(caught.exception), repr(caught.exception.__context__)):
                    self.assertNotIn(TOKEN, text)
                    self.assertNotIn(PRIVATE, text)
                self.assertIsNone(caught.exception.__cause__)


class RESTAdapterTests(unittest.TestCase):
    """The adapter on its own: the status table, request shape, reads, credentials and TLS."""

    def setUp(self):
        self.fixtures = load_fixtures()
        self.target = self.fixtures["proposal"]["target"]
        self.template = self.fixtures["baseline"]["pod_template"]
        self.sim = SimAPIServer.from_snapshot(self.fixtures["live_state"])
        self.server = FakeAPIServer(self.sim, token=TOKEN).start()
        self.addCleanup(self.server.close)
        self.adapter = self.make()

    def make(self, **changes):
        values = dict(server=self.server.url, cluster_id=self.target["cluster_id"], allowed_targets=[self.target],
                      token=TOKEN, clock=lambda: NOW)
        values.update(changes)
        return KubernetesRESTDeploymentAPI(**values)

    def restore(self, adapter=None, version=None):
        return (adapter or self.adapter).conditional_restore(
            target=self.target, version=version or self.sim.resource_version, template=self.template,
            evidence=evidence_for(self.target, self.template))

    def test_status_codes_split_into_definite_rejections_and_unknown_outcomes(self):
        self.assertEqual(DEFINITE_REJECTIONS, {400, 401, 403, 404, 409, 422, 429})
        for code in sorted(DEFINITE_REJECTIONS):
            with self.subTest(code=code):
                self.server.fail_next_patch(f"status={code}")
                with self.assertRaises(WriteRejected) as caught:
                    self.restore()
                self.assertEqual(caught.exception.status, code)
        for code in (201, 302, 408, 500, 502, 503, 504):
            with self.subTest(code=code):
                self.server.fail_next_patch(f"status={code}")
                with self.assertRaises(WriteOutcomeUnknown):
                    self.restore()
        self.assertEqual(self.sim.requests, [])  # None of them applied anything.

    def test_a_rejection_keeps_only_an_allowlisted_reason(self):
        self.server.fail_next_patch("status=422")
        with self.assertRaises(WriteRejected) as caught:
            self.restore()
        self.assertEqual((caught.exception.status, caught.exception.reason), (422, "Invalid"))
        self.server.fail_next_patch("status=502")
        with self.assertRaises(WriteOutcomeUnknown):
            self.restore()
        self.assertEqual(WriteRejected(403, "PRIVATE detail").reason, "Unknown")
        for status in (500, 302, 200, "422", None, True):
            with self.subTest(status=status), self.assertRaises(ValueError):
                WriteRejected(status, "Forbidden")

    def test_the_patch_is_the_kubectl_transports_patch_with_field_manager_and_timeout(self):
        runner = Runner(self.fixtures["live_state"]["deployment"])
        kubectl = KubectlDeploymentAPI(context="kind-team", cluster_id=self.target["cluster_id"],
                                       kubeconfig="restricted.json", allowed_targets=[self.target], runner=runner,
                                       clock=lambda: NOW)
        kubectl.conditional_restore(target=self.target, version=self.sim.resource_version, template=self.template,
                                    evidence=evidence_for(self.target, self.template))
        applied = self.restore()
        self.assertEqual(applied["deployment"]["spec"]["template"], self.template)
        [patch] = [request for request in self.sim.requests]
        self.assertEqual(patch.operations, runner.patches[0])
        [request] = [item for item in self.server.requests if item["method"] == "PATCH"]
        self.assertEqual(request["path"], "/apis/apps/v1/namespaces/sentinel-demo/deployments/demo-api")
        self.assertEqual(request["query"], {"fieldManager": [FIELD_MANAGER], "timeout": ["10s"]})
        self.assertEqual(request["headers"]["Content-Type"], "application/json-patch+json")
        self.assertEqual({item["method"] for item in self.server.requests}, {"GET", "PATCH"})
        self.assertEqual(self.adapter.stats["successful_writes"], 1)

    def test_scale_and_fence_use_the_same_inherited_patches(self):
        fenced = self.adapter.fence(target=self.target, version=self.sim.resource_version,
                                    marker=fence_marker("ACT-REST-001", "c" * 32))
        self.assertIn(FENCE_ANNOTATION, fenced["deployment"]["metadata"]["annotations"])
        self.assertEqual(self.adapter.stats["successful_fences"], 1)
        self.server.fail_next_patch("forbidden")
        with self.assertRaises(WriteRejected):
            self.adapter.fence(target=self.target, version=self.sim.resource_version,
                               marker=fence_marker("ACT-REST-001", "d" * 32))
        with self.assertRaises(ConditionalWriteConflict):  # Local precondition: no PATCH at all.
            self.adapter.fence(target=self.target, version="1", marker=fence_marker("ACT-REST-001", "e" * 32))
        self.assertEqual(self.server.count("PATCH"), 2)

    def test_reads_tell_an_absent_deployment_from_an_unexplained_404(self):
        self.assertEqual(self.adapter.read(self.target)["deployment"]["metadata"]["uid"], self.target["uid"])
        other = dict(self.target, name="other-api")
        adapter = self.make(allowed_targets=[self.target, other])
        with self.assertRaises(ReadUnavailable):  # A 404 that does not name this Deployment proves nothing.
            adapter.read(other)
        version = self.sim.resource_version
        self.sim.delete()
        with self.assertRaises(TargetAbsent):
            self.adapter.read(self.target)
        with self.assertRaises(ConditionalWriteConflict):
            self.restore(version=version)
        self.assertEqual(self.server.count("PATCH"), 0)

    def test_failed_reads_are_unavailable_and_targets_are_allowlisted(self):
        with self.assertRaises(ReadUnavailable):
            self.make(token="wrong-token-" + "w" * 30).read(self.target)
        self.server.close()
        with self.assertRaises(ReadUnavailable):
            self.adapter.read(self.target)
        with self.assertRaises(LiveAdapterError):
            self.adapter.read(dict(self.target, uid="another-uid"))

    def test_autoscalers_are_listed_read_only(self):
        self.assertEqual(self.adapter.autoscalers(self.target), [])
        self.sim.autoscalers = [{"metadata": {"name": "demo-api-hpa"},
                                 "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "demo-api"}}},
                                {"metadata": {"name": "other"},
                                 "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "other"}}}]
        self.assertEqual(self.adapter.autoscalers(self.target), [{"name": "demo-api-hpa"}])
        self.sim.autoscalers = [{"metadata": {}}]
        with self.assertRaises(LiveAdapterError):
            self.adapter.autoscalers(self.target)
        self.server.close()
        with self.assertRaises(ReadUnavailable):
            self.adapter.autoscalers(self.target)

    def test_a_success_body_that_does_not_show_the_change_is_unknown(self):
        self.sim.admission = None
        with mock.patch.object(KubernetesRESTDeploymentAPI, "_exchange",
                               side_effect=[(200, json.dumps(self.sim.deployment).encode()),
                                            (200, b"{not json")]):
            with self.assertRaises(WriteOutcomeUnknown):
                self.restore()

    def test_the_rest_transport_never_runs_kubectl(self):
        with self.assertRaises(LiveAdapterError):
            self.adapter._command(self.target)

    def test_plain_http_is_refused_unless_the_server_is_a_loopback_address(self):
        for server in ("http://10.0.0.1:6443", "http://example.test:80", "http://localhost:8080",
                       "ftp://127.0.0.1", "https://user:pw@127.0.0.1:6443", "https://127.0.0.1:6443/proxy",
                       "https://127.0.0.1:6443?x=1", "https://", "not a url", None):
            with self.subTest(server=server), self.assertRaises(LiveAdapterError):
                self.make(server=server)
        self.make(server="http://127.0.0.1:1")
        self.make(server="http://[::1]:1")
        https = self.make(server="https://10.0.0.1:6443")
        self.assertEqual(https._tls.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(https._tls.check_hostname)
        self.assertGreaterEqual(https._tls.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_constructor_requires_cluster_targets_credentials_and_a_sane_timeout(self):
        for changes in ({"cluster_id": ""}, {"allowed_targets": []}, {"allowed_targets": [dict(self.target, kind="Pod")]},
                        {"token": None}, {"token": "has space"}, {"timeout": 0}, {"timeout": True},
                        {"timeout": 61}, {"token": None, "client_certificate": "x"},
                        {"token_file": ""}):
            with self.subTest(changes=changes), self.assertRaises(LiveAdapterError):
                self.make(**changes)
        self.assertEqual(self.server.requests, [])

    def test_a_token_file_is_read_again_for_every_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("first-" + "a" * 30 + "\n", encoding="ascii")
            self.server.token = None
            adapter = self.make(token=None, token_file=str(path))
            adapter.read(self.target)
            path.write_text("second-" + "b" * 30, encoding="ascii")
            adapter.read(self.target)
            seen = [request["headers"]["Authorization"] for request in self.server.requests]
            self.assertEqual(seen, ["Bearer first-" + "a" * 30, "Bearer second-" + "b" * 30])
            path.write_text("", encoding="ascii")
            with self.assertRaises(ReadUnavailable):
                adapter.read(self.target)
            with self.assertRaises(ReadUnavailable):  # The pre-read sends nothing, and so no PATCH follows.
                self.restore(self.make(token=None, token_file=str(Path(directory) / "missing")))
            self.assertEqual(len(self.server.requests), 2)

    def test_an_unreadable_token_at_patch_time_is_not_sent(self):
        adapter = self.make()
        adapter.read(self.target)
        with mock.patch.object(KubernetesRESTDeploymentAPI, "_headers", side_effect=OSError("gone")):
            with self.assertRaises(WriteRejected) as caught:
                adapter._submit(self.target, self.sim.resource_version, [], lambda admitted: True,
                                prefix="unused", counter="write_attempts")
        self.assertEqual(caught.exception.status, "not_sent")
        self.assertEqual(self.server.count("PATCH"), 0)


def kubeconfig(user=None, cluster=None, context="restricted"):
    return {"apiVersion": "v1", "kind": "Config",
            "clusters": [{"name": "c", "cluster": cluster or {"server": "https://127.0.0.1:6443"}}],
            "users": [{"name": "u", "user": user if user is not None else {"token": TOKEN}}],
            "contexts": [{"name": context, "context": {"cluster": "c", "user": "u"}}]}


class KubeconfigTests(unittest.TestCase):
    def setUp(self):
        self.target = load_fixtures()["proposal"]["target"]

    def test_only_token_token_file_and_client_certificate_users_are_supported(self):
        refused = [{"exec": {"command": "aws"}}, {"auth-provider": {"name": "oidc"}},
                   {"username": "admin", "password": "secret"}, {"as": "system:admin", "token": TOKEN}, {}]
        for user in refused:
            with self.subTest(user=list(user)), self.assertRaises(LiveAdapterError):
                KubernetesRESTDeploymentAPI.from_kubeconfig_data(
                    kubeconfig(user), context="restricted", cluster_id=self.target["cluster_id"],
                    allowed_targets=[self.target])

    def test_unverified_or_proxied_cluster_entries_are_refused(self):
        for cluster in ({"server": "https://127.0.0.1:6443", "insecure-skip-tls-verify": True},
                        {"server": "https://127.0.0.1:6443", "proxy-url": "http://proxy.test:3128"},
                        {"server": "https://127.0.0.1:6443", "tls-server-name": "kubernetes"},
                        {"server": "http://10.0.0.1:8080"}):
            with self.subTest(cluster=list(cluster)), self.assertRaises(LiveAdapterError):
                KubernetesRESTDeploymentAPI.from_kubeconfig_data(
                    kubeconfig(cluster=cluster), context="restricted", cluster_id=self.target["cluster_id"],
                    allowed_targets=[self.target])

    def test_the_context_must_exist_once(self):
        for config, context in ((kubeconfig(), "other"), ({"contexts": []}, "restricted"), ([], "restricted"),
                                (kubeconfig(), "")):
            with self.subTest(context=context), self.assertRaises(LiveAdapterError):
                kubeconfig_credentials(config, context)

    def test_credentials_are_decoded_from_data_or_read_from_files(self):
        import base64
        with tempfile.TemporaryDirectory() as directory:
            ca = Path(directory) / "ca.crt"
            ca.write_text("CA-PEM", encoding="ascii")
            settings = kubeconfig_credentials(kubeconfig(
                {"tokenFile": "/var/run/token", "client-certificate-data": base64.b64encode(b"CERT").decode(),
                 "client-key": str(ca)},
                {"server": "https://127.0.0.1:6443", "certificate-authority": str(ca)}), "restricted")
        self.assertEqual(settings, {"server": "https://127.0.0.1:6443", "certificate_authority": "CA-PEM",
                                    "client_certificate": "CERT", "client_key": "CA-PEM",
                                    "token_file": "/var/run/token"})
        for cluster in ({"server": "https://127.0.0.1:6443", "certificate-authority-data": "!!"},
                        {"server": "https://127.0.0.1:6443", "certificate-authority": str(Path(directory) / "gone")}):
            with self.subTest(cluster=list(cluster)), self.assertRaises(LiveAdapterError):
                kubeconfig_credentials(kubeconfig(cluster=cluster), "restricted")

    def test_from_kubeconfig_runs_one_read_only_config_view_and_nothing_else(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, json.dumps(kubeconfig()).encode(), b"")
        adapter = KubernetesRESTDeploymentAPI.from_kubeconfig(
            context="restricted", kubeconfig="restricted.json", cluster_id=self.target["cluster_id"],
            allowed_targets=[self.target], runner=runner)
        self.assertEqual(calls, [["kubectl", "config", "view", "--raw", "--minify", "--flatten", "--output=json",
                                  "--context=restricted", "--kubeconfig=restricted.json"]])
        self.assertEqual((adapter._scheme, adapter._host, adapter._port), ("https", "127.0.0.1", 6443))
        for result in (subprocess.CompletedProcess([], 1, b"", b"error"), subprocess.CompletedProcess([], 0, b"{", b""),
                       subprocess.TimeoutExpired("kubectl", 15)):
            def failing(command, result=result, **kwargs):
                if isinstance(result, BaseException):
                    raise result
                return result
            with self.subTest(result=type(result).__name__), self.assertRaises(LiveAdapterError) as caught:
                KubernetesRESTDeploymentAPI.from_kubeconfig(
                    context="restricted", kubeconfig="restricted.json", cluster_id=self.target["cluster_id"],
                    allowed_targets=[self.target], runner=failing)
            self.assertNotIn(TOKEN, str(caught.exception))
        with self.assertRaises(LiveAdapterError):
            KubernetesRESTDeploymentAPI.from_kubeconfig(context="", kubeconfig="k", cluster_id="c",
                                                        allowed_targets=[self.target], runner=runner)


@unittest.skipUnless(find_openssl(), "openssl is not installed")
class RESTTLSTests(unittest.TestCase):
    """TLS verification is on: the kubeconfig CA verifies the server, any other CA does not."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.certificates = make_certificates(self.directory / "right")
        self.other = make_certificates(self.directory / "wrong")
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(self.certificates["cert"], self.certificates["key"])
        fixtures = load_fixtures()
        self.target, self.template = fixtures["proposal"]["target"], fixtures["baseline"]["pod_template"]
        self.sim = SimAPIServer.from_snapshot(fixtures["live_state"])
        self.server = FakeAPIServer(self.sim, token=TOKEN, tls_context=server_context).start()
        self.addCleanup(self.server.close)

    def make(self, ca, **changes):
        values = dict(server=self.server.url, cluster_id=self.target["cluster_id"], allowed_targets=[self.target],
                      token=TOKEN, certificate_authority=Path(ca).read_text(encoding="ascii"), clock=lambda: NOW)
        values.update(changes)
        return KubernetesRESTDeploymentAPI(**values)

    def test_the_kubeconfig_ca_verifies_the_server(self):
        adapter = self.make(self.certificates["ca"])
        self.assertEqual(adapter.read(self.target)["deployment"]["metadata"]["uid"], self.target["uid"])

    def test_an_unverified_server_gets_no_request_and_a_write_is_not_sent(self):
        adapter = self.make(self.other["ca"])
        with self.assertRaises(ReadUnavailable):
            adapter.read(self.target)
        with self.assertRaises(WriteRejected) as caught:
            adapter._submit(self.target, self.sim.resource_version, [], lambda admitted: True, prefix="unused",
                            counter="write_attempts")
        self.assertEqual(caught.exception.status, "not_sent")
        self.assertEqual(self.server.requests, [])

    def test_client_certificate_files_are_private_and_gone_after_loading_and_on_error(self):
        private = self.directory / "private-temp"
        private.mkdir()
        pem = Path(self.certificates["cert"]).read_text(encoding="ascii")
        key = Path(self.certificates["key"]).read_text(encoding="ascii")
        with mock.patch.object(tempfile, "tempdir", str(private)):
            adapter = self.make(self.certificates["ca"], token=None, client_certificate=pem, client_key=key)
            self.assertEqual(list(private.iterdir()), [])
            with self.assertRaises(LiveAdapterError):
                self.make(self.certificates["ca"], token=None, client_certificate=pem, client_key="not a key")
            self.assertEqual(list(private.iterdir()), [])
        self.assertIsNotNone(adapter._tls)
        with self.assertRaises(LiveAdapterError):
            self.make(self.certificates["ca"], certificate_authority="not a CA")


class TransportSelectionTests(unittest.TestCase):
    """transport comes only from trusted configuration; kubectl stays the default."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        fixtures = load_fixtures()
        self.proposal, self.target = fixtures["proposal"], fixtures["proposal"]["target"]
        self.sim = SimAPIServer.from_snapshot(fixtures["live_state"])
        self.server = FakeAPIServer(self.sim, token=TOKEN).start()
        self.addCleanup(self.server.close)

    def view(self, command, **kwargs):
        config = kubeconfig(cluster={"server": self.server.url}, context="restricted-lab")
        return subprocess.CompletedProcess(command, 0, json.dumps(config).encode(), b"")

    def build(self, **changes):
        from runtime.live_executor import build_adapters
        values = dict(context="restricted-lab", cluster_id=self.target["cluster_id"], kubeconfig="restricted.json",
                      allowed_targets=[self.target], ledger=self.directory / "executor.db",
                      locks=self.directory / "locks", reconcile_only=True, runner=self.view, clock=lambda: NOW)
        values.update(changes)
        adapters = build_adapters(**values)
        self.addCleanup(adapters.ledger.close)
        return adapters

    def test_build_adapters_defaults_to_kubectl_and_builds_rest_only_when_asked(self):
        self.assertIs(type(self.build().cluster), KubectlDeploymentAPI)
        rest = self.build(transport="rest", ledger=self.directory / "rest.db").cluster
        self.assertIs(type(rest), KubernetesRESTDeploymentAPI)
        self.assertEqual(rest.read(self.target)["deployment"]["metadata"]["uid"], self.target["uid"])
        from services.guard.adapters import AdapterError
        for transport in ("grpc", "REST", None):
            with self.subTest(transport=transport), self.assertRaises(AdapterError):
                self.build(transport=transport, ledger=self.directory / "bad.db")

    def test_the_cli_switch_reaches_the_rest_transport(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        from runtime.live_executor import main
        proposal = self.directory / "proposal.json"
        proposal.write_text(json.dumps(self.proposal), encoding="utf-8")
        scope = self.directory / "targets.json"
        scope.write_text(json.dumps([self.target]), encoding="utf-8")
        arguments = ["--proposal", str(proposal), "--context", "restricted-lab", "--cluster-id",
                     self.target["cluster_id"], "--kubeconfig", str(self.directory / "restricted.json"),
                     "--allowed-targets", str(scope), "--ledger", str(self.directory / "cli.db"),
                     "--locks", str(self.directory / "cli-locks"), "--reconcile"]
        built = []
        original = KubernetesRESTDeploymentAPI.from_kubeconfig.__func__

        def from_kubeconfig(cls, **options):
            built.append(options["context"])
            return original(cls, **options, runner=self.view)
        with (mock.patch.object(KubernetesRESTDeploymentAPI, "from_kubeconfig", classmethod(from_kubeconfig)),
              redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO())):
            status = main(arguments + ["--transport", "rest"])
        self.assertEqual((status, built), (4, ["restricted-lab"]))  # In doubt: same UID at V0, no journal.
        self.assertEqual(json.loads(stdout.getvalue())["status"], "NEEDS_RECONCILIATION")
        self.assertEqual(self.server.count("GET"), 1)
        self.assertEqual(self.server.count("PATCH"), 0)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as stderr:
            self.assertEqual(main(arguments + ["--transport", "grpc"]), 1)
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "ARGUMENT_ERROR")

    def test_the_worker_takes_the_transport_from_its_trusted_config_and_a_lab_cluster_stays_on_kubectl(self):
        from contextlib import ExitStack
        import os
        from runtime import worker_service
        config = {"role": "executor", "context": "restricted-lab", "cluster_id": "sentinel-lab", "kubeconfig": "k",
                  "policy": "p", "baseline": "b", "allowed_targets": "t", "remote_dependencies": False,
                  "approvals": {"url": "https://127.0.0.1:1", "identity": "executor-1", "token_env": "TEST_TOKEN"}}
        for configured, expected in (({}, "kubectl"), ({"transport": "rest"}, "rest")):
            with (self.subTest(configured=configured),
                  mock.patch.dict(os.environ, {"TEST_TOKEN": "t" * 40}),
                  mock.patch("runtime.approval_http.ApprovalClient"),
                  mock.patch("runtime.live_executor.build_adapters") as build, ExitStack() as stack):
                worker_service.adapter(dict(config, **configured), self.directory, stack)
                self.assertEqual(build.call_args.kwargs["transport"], expected)
        with (mock.patch.dict(os.environ, {"TEST_TOKEN": "t" * 40}),
              mock.patch("runtime.approval_http.ApprovalClient"),
              mock.patch("runtime.worker_service.lab_cluster", return_value=mock.Mock()),
              mock.patch("runtime.live_executor.build_adapters") as build, ExitStack() as stack,
              self.assertRaises(ValueError)):
            worker_service.adapter(dict(config, transport="rest", identity="executor-1"), self.directory, stack)
        build.assert_not_called()


class DefiniteRejectionTableTests(unittest.TestCase):
    """classify() with a rejection: state evidence still wins over the response."""

    def test_rows_r0_r5_r6_with_a_rejection(self):
        fixtures = load_fixtures()
        proposal, snapshot = fixtures["proposal"], fixtures["live_state"]
        rejection = WriteRejected(403, "Forbidden")
        unchanged = observe(snapshot, proposal["target"])
        self.assertEqual(classify(None, proposal, unchanged, rejection).status, "FAILED")
        moved = deepcopy(snapshot)
        moved["deployment"]["metadata"]["resourceVersion"] = "999"
        self.assertEqual(classify(None, proposal, observe(moved, proposal["target"]), rejection).status, "CONFLICT")
        self.assertEqual(classify(None, proposal, None, rejection).status, "NEEDS_RECONCILIATION")

    def test_row_r3_evidence_beats_a_rejection(self):
        fixtures = load_fixtures()
        proposal = fixtures["proposal"]
        evidence = {"action_id": proposal["action_id"], "proposal_sha256": fingerprint(proposal),
                    "target_uid": proposal["target"]["uid"],
                    "template_sha256": proposal["parameters"]["template_sha256"], "attempt_id": PEER_ATTEMPT}
        moved = Observation(present=True, uid=proposal["target"]["uid"], resource_version="999",
                            evidence_annotation=canonical_json(evidence),
                            template_sha256=proposal["parameters"]["template_sha256"])
        for rejection in (WriteRejected(422, "Invalid"), WriteRejected(403, "Forbidden")):
            with self.subTest(rejection=rejection.status):
                self.assertEqual(classify(None, proposal, moved, rejection),
                                 Decision("APPLIED", Basis.EVIDENCE_MATCH, "999", writer="peer:" + PEER_ATTEMPT))
                overwritten = Observation(**dict(vars(moved), template_sha256="d" * 64))
                self.assertEqual(classify(None, proposal, overwritten, rejection).basis,
                                 Basis.COMMITTED_THEN_OVERWRITTEN)


if __name__ == "__main__":
    unittest.main()
