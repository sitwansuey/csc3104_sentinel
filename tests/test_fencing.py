"""P3.04: target-ownership epochs and the executor.seal command that fences abandoned Executor writes.

The hazard: revocation is check-then-act. An Executor passes its last lease and
approval check, then its kubectl patch can land seconds later. The patch tests
the resourceVersion it read, which still matches if nobody else wrote, so the
write lands after the revocation. A seal is a metadata-only patch with the
same UID and resourceVersion tests, sent by another Executor on the
Coordinator's command; once it commits, the old patch can never commit.

The hazard tests run the production kubectl adapter against the file cluster
(tests/support/file_cluster.py), the Coordinator's real delivery transport and
the real Executor code, in one process with one thread per Executor host.
Executor A is stopped at the real fault point executor.before_patch.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from experiments.distributed import fixtures
from runtime import fault_points
from runtime.remote_adapters import ExecutorAdapter
from runtime.remote_worker import RemoteWorker, WorkerJournal
from runtime.worker_http import StoreWatch
from runtime.service_identity import ProtocolError
from runtime.worker_transport import Delivery
from services.coordinator.approvals import ApprovalAccess
from services.coordinator.core import LocalEventBus, handle, record_message, reseal
from services.coordinator.store import (
    MIGRATIONS, CoordinatorError, SQLiteStore, SupersededResult, migrate, schema_version,
)
from services.executor.core import (
    SEAL_ATTEMPTS, ConditionalWriteConflict, ExecutionAdapters, GuardCheck, seal, settle,
)
from services.executor.delivery_store import execution_delivery_reviews
from services.executor.kubernetes_adapter import KubectlDeploymentAPI
from services.executor.reconciliation import FENCE_ANNOTATION, FENCE_EPOCH_ANNOTATION
from services.executor.store import LocalTargetCoordinator, SQLiteExecutionLedger, TargetBusy, target_key
from shared.contracts import ContractError, validate_message
from tests.support.executor_fake import FakeDeploymentAPI
from tests.support.file_cluster import FileCluster

ROOT = Path(__file__).resolve().parents[1]


def sample(name):
    return json.loads((ROOT / "data/sentinel/samples/messages" / (name + ".json")).read_text(encoding="utf-8"))


class Authorizer:
    """Fresh Guard validation stand-in: always the stored ALLOW. Approval is checked by the Coordinator."""

    def __init__(self, decision, baseline):
        self.decision, self.baseline = decision, baseline

    def validate(self, proposal, live_state, *, now):
        return GuardCheck(decision=deepcopy(self.decision), baseline=deepcopy(self.baseline), inputs={})

    def assert_usable(self, check, *, now):
        return None


class Lease:
    """RemoteWorker's lease check, over its own Coordinator connection (one per thread, like a process)."""

    def __init__(self, path, clock, identity, delivery):
        self.path, self.clock, self.identity, self.delivery = path, clock, identity, delivery
        self.checks = 0

    def ensure_current(self):
        self.checks += 1
        with SQLiteStore(self.path) as store:
            return Delivery(store, clock=self.clock, lease_seconds=30).renew(
                self.identity, "executor", self.delivery["delivery_id"], self.delivery["lease_token"])


class FencingLab(unittest.TestCase):
    """One Coordinator database, one file cluster and Executor hosts with their own ledgers and locks."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.inputs = fixtures.build()
        self.target = self.inputs["target"]
        self.version = self.inputs["snapshot"]["deployment"]["metadata"]["resourceVersion"]
        self.cluster_path = self.directory / "cluster.db"
        FileCluster.create(self.cluster_path, self.inputs["snapshot"]).close()
        self.cluster = self.open_cluster()
        self.coordinator_path = self.directory / "coordinator.db"
        self.store = SQLiteStore(self.coordinator_path)
        self.stack.callback(self.store.close)
        self.now = 1000.0
        self.delivery = Delivery(self.store, clock=lambda: self.now, lease_seconds=30)
        self.incident, self.proposal, self.guard = self.messages()

    def open_cluster(self):
        cluster = FileCluster(self.cluster_path)
        self.stack.callback(cluster.close)
        return cluster

    def messages(self, incident_id="INC-DEMO-001", action_id="ACT-DEMO-001", target=None):
        target = deepcopy(target or self.target)
        incident = sample("incident")
        incident.update(incident_id=incident_id, target=target, observed_resource_version=self.version)
        proposal = sample("proposal")
        proposal.update(incident_id=incident_id, action_id=action_id, target=target,
                        based_on_resource_version=self.version,
                        parameters={"baseline_id": self.inputs["baseline"]["baseline_id"],
                                    "template_sha256": self.inputs["baseline"]["template_sha256"]})
        guard = sample("guard-result")
        guard.update(incident_id=incident_id, action_id=action_id, target=target, verdict="ALLOW",
                     reason_codes=["VALIDATION_PASSED"], validated_resource_version=self.version)
        return incident, proposal, guard

    def ready(self, messages=None):
        """Record incident, proposal and Guard ALLOW, so an executor.request waits in the outbox."""
        incident, proposal, guard = messages or (self.incident, self.proposal, self.guard)
        bus = LocalEventBus()
        for message in (incident, proposal, guard):
            handle(message, self.store, bus)
        self.assertEqual(self.state(proposal), "READY")

    def state(self, proposal=None):
        return self.store.action((proposal or self.proposal)["action_id"])["state"]

    def owner(self, target=None):
        return self.store.target_ownership(target or self.target)

    def claim(self, identity):
        return self.delivery.claim(identity, "executor")["delivery"]

    def finish(self, identity, delivery, result):
        return self.delivery.finish(identity, "executor", delivery["delivery_id"], delivery["lease_token"], result)

    def adapters(self, host, cluster=None, *, fault_points_on=False, runner=None, close=True):
        """A host's Executor: its own ledger and lock directory, the production adapter on the file cluster."""
        state = self.directory / host
        state.mkdir(exist_ok=True)
        cluster = cluster or self.cluster
        api = KubectlDeploymentAPI(context="file-cluster", cluster_id=fixtures.CLUSTER_ID,
                                   kubeconfig=str(self.cluster_path), allowed_targets=[self.target],
                                   runner=runner or cluster.kubectl(host))
        ledger = SQLiteExecutionLedger(state / "executor.db")
        if close:  # A thread that opened its own ledger closes it itself.
            self.stack.callback(ledger.close)
        return ExecutionAdapters(
            ledger=ledger, coordination=LocalTargetCoordinator(state / "locks"),
            cluster=fault_points.CheckpointCluster(api) if fault_points_on else api,
            authorizer=Authorizer(self.guard, self.inputs["baseline"]))

    def lease(self, identity, delivery):
        return Lease(self.coordinator_path, lambda: self.now, identity, delivery)

    def revoke(self):
        """The operator's revocation through the target-scoped approval API (services/coordinator/approvals.py)."""
        with self.store.transaction():
            self.store.add_approval(self.proposal["action_id"], {"operator_id": "operator-1", "decision": "APPROVED"})
        approval_id = self.store.approval_row(self.proposal["action_id"])["id"]
        access = ApprovalAccess(self.store, {"operator-1": [{"target": self.target, "operations": ["revoke"]}]})
        from services.coordinator.store import _digest
        return access.call("operator-1", "operator", "revoke", {
            "action_id": self.proposal["action_id"], "proposal_sha256": _digest(self.proposal),
            "approval_id": approval_id, "reason": "withdrawn while the Executor was writing"})["revocation"]

    def seal_now(self, host="executor-b", cluster=None):
        """Another Executor claims the seal the Coordinator queued and performs it."""
        delivery = self.claim(host)
        self.assertIsNotNone(delivery, "no seal was queued")
        self.assertEqual(delivery["topic"], "executor.seal")
        result, report = ExecutorAdapter(self.adapters(host, cluster)).process(delivery, self.lease(host, delivery))
        self.assertIsNone(report)
        self.assertTrue(self.finish(host, delivery, result)["accepted"])
        return delivery, result

    def template_writes(self):
        return self.cluster.committed_writes()

    def fences(self):
        return [row for row in self.cluster.requests("fence") if row["state"] == "committed"]

    def annotations(self):
        return self.cluster.snapshot(self.target)["deployment"]["metadata"].get("annotations", {})


class PausedWriterHazardTests(FencingLab):
    """Executor A is held at executor.before_patch after its last check; the operator revokes."""

    def setUp(self):
        super().setUp()
        self.faults = self.directory / "faults"
        self.faults.mkdir()
        self.stack.enter_context(patch.dict(os.environ, {
            fault_points.ENV_DIRECTORY: str(self.faults), fault_points.ENV_CLUSTER: fault_points.FILE_CLUSTER,
            fault_points.ENV_PROCESS: "executor-a"}))
        (self.faults / "executor.before_patch.action").write_text("block", encoding="utf-8")
        self.ready()
        self.first = self.claim("executor-a")
        self.assertEqual((self.first["topic"], self.first["fence_epoch"]), ("executor.request", 1))
        self.outcome = {}
        self.writer = threading.Thread(target=self.run_writer, daemon=True)
        self.writer.start()
        deadline = time.monotonic() + 20
        while not (self.faults / "executor.before_patch.reached").exists():
            self.assertLess(time.monotonic(), deadline, "Executor A never reached its patch")
            time.sleep(0.02)

    def run_writer(self):
        """Executor A's process: its own connections, the real ExecutorAdapter and the fault-point proxy."""
        try:
            with FileCluster(self.cluster_path) as cluster:
                adapters = self.adapters("executor-a", cluster, fault_points_on=True, close=False)
                try:
                    lease = self.lease("executor-a", self.first)
                    result, _ = ExecutorAdapter(adapters).process(self.first, lease)
                    self.outcome.update(result=result, lease_checks=lease.checks, ledger=adapters.ledger.get(
                        self.proposal["action_id"])["phase"])
                finally:
                    adapters.ledger.close()
        except Exception as error:  # noqa: BLE001 - a test thread hands any failure to the main thread
            self.outcome["error"] = error

    def release_writer(self):
        (self.faults / "executor.before_patch.release").write_text("release", encoding="utf-8")
        self.writer.join(30)
        self.assertFalse(self.writer.is_alive())
        self.assertNotIn("error", self.outcome)
        return self.outcome["result"]

    def test_the_seal_stops_the_paused_write_with_zero_template_writes(self):
        revoked = self.revoke()
        delivery, sealed = self.seal_now()
        self.assertEqual((sealed["outcome"], sealed["epoch"], delivery["fence_epoch"]), ("SEALED", 2, 2))
        (fence,) = self.fences()
        self.assertEqual((fence["host"], fence["from_version"]), ("executor-b", self.version))
        self.assertEqual(self.annotations()[FENCE_EPOCH_ANNOTATION], "2")
        self.assertEqual(json.loads(self.annotations()[FENCE_ANNOTATION])["epoch"], 2)
        result = self.release_writer()
        # A passed its lease and approval checks before the pause, then its patch is refused: zero writes.
        self.assertGreaterEqual(self.outcome["lease_checks"], 2)
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(self.outcome["ledger"], "DONE")
        self.assertEqual(self.template_writes(), [])
        self.assertEqual(self.cluster.snapshot(self.target)["deployment"]["spec"],
                         self.inputs["snapshot"]["deployment"]["spec"])
        with self.assertRaises(ProtocolError) as refused:  # Its command was withdrawn: the result is refused.
            self.finish("executor-a", self.first, result)
        self.assertEqual(refused.exception.status, 409)
        self.assertEqual(self.state(), "FAILED")  # Never RECOVERED; the seal moved the planned-on version.
        self.assertEqual((self.owner()["seal_required"], self.owner()["sealed_epoch"]), (0, 2))
        self.assertTrue(revoked["revoked_at"])

    def test_a_revocation_after_the_paused_holders_lease_expired_still_seals(self):
        # A is still paused at its patch, but its lease ran out before the operator revoked.
        self.now += 31
        self.revoke()
        delivery, sealed = self.seal_now()
        self.assertEqual((sealed["outcome"], sealed["epoch"], delivery["fence_epoch"]), ("SEALED", 2, 2))
        (fence,) = self.fences()
        self.assertEqual(fence["host"], "executor-b")
        result = self.release_writer()
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(self.template_writes(), [])
        self.assertEqual(self.cluster.snapshot(self.target)["deployment"]["spec"],
                         self.inputs["snapshot"]["deployment"]["spec"])
        with self.assertRaises(ProtocolError) as refused:
            self.finish("executor-a", self.first, result)
        self.assertEqual(refused.exception.status, 409)
        self.assertEqual(self.state(), "FAILED")
        self.assertEqual((self.owner()["seal_required"], self.owner()["sealed_epoch"]), (0, 2))

    def test_the_paused_patch_itself_raises_conditional_write_conflict_after_the_seal(self):
        seen = []
        original = KubectlDeploymentAPI.conditional_restore

        def recording(api, **kwargs):
            try:
                return original(api, **kwargs)
            except ConditionalWriteConflict as error:
                seen.append(error)
                raise
        self.revoke()
        self.seal_now()
        with patch.object(KubectlDeploymentAPI, "conditional_restore", recording):
            self.release_writer()
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.template_writes(), [])

    def test_without_a_seal_the_same_paused_write_lands_after_the_revocation(self):
        # The hazard as it is when nobody seals (main before P3.04): one template write after revocation.
        revoked = self.revoke()
        revoked_at = datetime.fromisoformat(revoked["revoked_at"].replace("Z", "+00:00")).timestamp()
        result = self.release_writer()
        self.assertEqual(result["status"], "APPLIED")
        (write,) = self.template_writes()
        self.assertEqual((write["host"], write["action_id"]), ("executor-a", self.proposal["action_id"]))
        self.assertGreater(write["decided_at"], revoked_at)
        # The Coordinator still refuses the result and has queued the seal, which then reports the landed write.
        with self.assertRaises(ProtocolError):
            self.finish("executor-a", self.first, result)
        _, found = self.seal_now()
        self.assertEqual(found["outcome"], "APPLIED")
        self.assertEqual(self.state(), "NEEDS_REVIEW")


class SealOutcomeTests(FencingLab):
    def test_a_seal_landing_between_the_pre_read_and_the_patch_is_enforced_by_the_server(self):
        # The resourceVersion test inside A's patch, not A's own pre-read, refuses the write here.
        self.ready()
        first = self.claim("executor-a")
        sealed = {}

        def runner(command, **_):
            if "patch" in command and not sealed:
                self.revoke()
                sealed.update(zip(("delivery", "result"), self.seal_now(), strict=True))
            return self.cluster.run([str(item) for item in command], host="executor-a")
        adapters = self.adapters("executor-a", runner=runner)
        result, _ = ExecutorAdapter(adapters).process(first, self.lease("executor-a", first))
        self.assertEqual(sealed["result"]["outcome"], "SEALED")
        (rejected,) = self.cluster.requests("write")
        self.assertEqual((rejected["host"], rejected["state"]), ("executor-a", "rejected"))
        self.assertEqual(self.template_writes(), [])
        self.assertEqual(result["status"], "SUPERSEDED")  # Its reconcile read sees the version moved by the seal.

    def test_a_seal_that_finds_the_landed_write_routes_the_action_to_review(self):
        self.ready()
        first = self.claim("executor-a")

        def runner(command, **_):
            reply = self.cluster.run([str(item) for item in command], host="executor-a")
            if "patch" in command:
                self.revoke()  # Revoked after the write committed but before its result reached the Coordinator.
            return reply
        result, _ = ExecutorAdapter(self.adapters("executor-a", runner=runner)).process(
            first, self.lease("executor-a", first))
        self.assertEqual(result["status"], "APPLIED")
        _, found = self.seal_now()
        self.assertEqual((found["outcome"], found["resource_version"]), ("APPLIED", result["resource_version"]))
        self.assertEqual(self.fences(), [])  # Nothing to fence: the write is already there.
        self.assertEqual(self.state(), "NEEDS_REVIEW")
        (review,) = execution_delivery_reviews(self.store)
        self.assertEqual((review["action_id"], review["result"]["outcome"]), (self.proposal["action_id"], "APPLIED"))
        self.assertIn("landed after the action was abandoned", review["reason"])
        self.assertEqual(self.owner()["seal_required"], 0)
        reviews = self.store.pending_routes("incident.review", action_id=self.proposal["action_id"])
        self.assertEqual(len(reviews), 1)

    def test_a_seal_retries_past_a_concurrent_version_change_and_then_seals(self):
        fake = RacingFence(self.inputs["snapshot"])
        result = seal(self.proposal, 4, self.fake_adapters(fake))
        self.assertEqual((result["outcome"], result["epoch"]), ("SEALED", 4))
        self.assertEqual(len(fake.fence_attempts), 1)  # Refused once; the next read sees the version moved.
        self.assertEqual(fake.writes, [])

    def test_a_seal_whose_fences_never_land_gives_up_unsealed_and_goes_to_review(self):
        fake = BrokenFence(self.inputs["snapshot"])
        result = seal(self.proposal, 3, self.fake_adapters(fake))
        self.assertEqual(result["outcome"], "UNSEALED")
        self.assertEqual(len(fake.fence_attempts), SEAL_ATTEMPTS)
        validate_message(result)
        # Through the Coordinator: review, NEEDS_REVIEW, and the workload stays blocked until a seal reports.
        self.ready()
        self.claim("executor-a")
        self.revoke()
        delivery = self.claim("executor-b")
        unsealed = dict(result, epoch=delivery["fence_epoch"])
        self.finish("executor-b", delivery, unsealed)
        self.assertEqual(self.state(), "NEEDS_REVIEW")
        self.assertEqual(self.owner()["seal_required"], 1)
        self.assertIn("bounded attempts", execution_delivery_reviews(self.store)[0]["reason"])
        self.assertIsNotNone(self.store.planning_blocker(self.incident["incident_id"]))
        self.assertTrue(reseal(self.proposal["action_id"], self.store))  # An operator queues another seal.
        self.assertFalse(reseal(self.proposal["action_id"], self.store))  # Only one at a time.
        _, sealed = self.seal_now("executor-c")
        self.assertEqual(sealed["outcome"], "SEALED")
        self.assertIsNone(self.store.planning_blocker(self.incident["incident_id"]))
        with self.assertRaises(CoordinatorError):
            reseal(self.proposal["action_id"], self.store)

    def test_a_seal_of_a_deleted_target_sends_nothing(self):
        fake = FakeDeploymentAPI(self.inputs["snapshot"])
        fake.delete()
        result = seal(self.proposal, 1, self.fake_adapters(fake))
        self.assertEqual((result["outcome"], result["resource_version"]), ("SEALED", None))
        self.assertEqual(fake.fence_attempts, [])

    def test_a_seal_never_writes_a_template_and_checks_its_lease_before_each_fence(self):
        fake = FakeDeploymentAPI(self.inputs["snapshot"])
        checks = []
        result = seal(self.proposal, 9, self.fake_adapters(fake), check=lambda: checks.append(1))
        self.assertEqual(result["outcome"], "SEALED")
        self.assertEqual((len(checks), fake.writes), (1, []))
        (fenced,) = fake.fences
        metadata = fenced["deployment"]["metadata"]
        self.assertEqual(metadata["annotations"][FENCE_EPOCH_ANNOTATION], "9")
        self.assertEqual(fenced["deployment"]["spec"], self.inputs["snapshot"]["deployment"]["spec"])
        with self.assertRaises(ValueError):
            seal(self.proposal, 0, self.fake_adapters(FakeDeploymentAPI(self.inputs["snapshot"])))

    def fake_adapters(self, fake):
        state = self.directory / "fake"
        state.mkdir(exist_ok=True)
        ledger = SQLiteExecutionLedger(state / "executor.db")
        self.stack.callback(ledger.close)
        return ExecutionAdapters(ledger=ledger, coordination=LocalTargetCoordinator(state / "locks"), cluster=fake,
                                 authorizer=Authorizer(self.guard, self.inputs["baseline"]))


class RacingFence(FakeDeploymentAPI):
    """Another writer moves the version just before the seal's first fence arrives."""

    def fence(self, **kwargs):
        if not self.fence_attempts:
            self.external_write(lambda deployment: deployment["metadata"].setdefault("labels", {}).update(x="1"))
        return super().fence(**kwargs)


class BrokenFence(FakeDeploymentAPI):
    """Every fence fails with an unknown outcome and commits nothing (an API server refusing writes)."""

    def fence(self, **kwargs):
        self.fence_attempts.append(deepcopy(kwargs))
        raise OSError("connection reset")


class OwnershipTests(FencingLab):
    def test_one_live_executor_claim_per_workload_while_other_workloads_proceed(self):
        self.ready()
        first = self.claim("executor-a")
        second = self.same_target_request("INC-SECOND", "ACT-SECOND")
        other = self.messages("INC-OTHER", "ACT-OTHER", dict(self.target, name="other-api", uid="other-uid"))
        self.assertIsNone(self.claim("executor-b"))  # Same workload, lease live: not claimable.
        self.ready(other)
        independent = self.claim("executor-b")
        self.assertEqual(independent["payload"]["proposal"]["action_id"], "ACT-OTHER")
        self.assertEqual(independent["fence_epoch"], 1)  # Epochs are per workload.
        self.now += 31  # executor-a's lease expired: ownership may move, under the next epoch.
        moved = self.claim("executor-b")
        self.assertIn(moved["event_id"], {first["event_id"], second})
        self.assertEqual(moved["fence_epoch"], 2)
        self.assertEqual(self.owner()["holder"], "executor-b")
        with self.assertRaises(ProtocolError) as stale:  # The command now belongs to executor-b.
            self.finish("executor-a", first, sample_result(self.proposal))
        self.assertEqual(stale.exception.status, 403)

    def test_the_two_boutique_targets_keep_separate_epochs_and_seals(self):
        """P3.09's two recovery targets in one namespace: a seal on one never holds the other."""
        from experiments.boutique.harness.targets import target_names
        self.assertEqual(target_names(), ("cartservice", "shippingservice"))
        ship = dict(self.target, namespace="boutique", name="shippingservice", uid="ship-uid")
        cart = dict(self.target, namespace="boutique", name="cartservice", uid="cart-uid")
        self.target = ship
        self.incident, self.proposal, self.guard = self.messages("INC-SHIP", "ACT-SHIP", ship)
        cart_messages = self.messages("INC-CART", "ACT-CART", cart)
        self.ready()
        self.ready(cart_messages)
        shipping = self.claim("executor-a")
        carting = self.claim("executor-b")
        self.assertEqual([item["payload"]["proposal"]["target"]["name"] for item in (shipping, carting)],
                         ["shippingservice", "cartservice"])
        self.assertEqual((shipping["fence_epoch"], carting["fence_epoch"]), (1, 1))
        self.revoke()  # Withdraws ACT-SHIP while executor-a holds it: shippingservice needs a seal.
        seal = self.claim("executor-c")  # Not held by executor-a's live lease, nor by cart's.
        self.assertEqual((seal["topic"], seal["payload"]["proposal"]["target"]["name"], seal["fence_epoch"]),
                         ("executor.seal", "shippingservice", 2))
        self.assertEqual((self.owner(ship)["seal_required"], self.owner(cart)["seal_required"]), (1, 0))
        self.assertIsNone(self.store.planning_blocker("INC-CART"))
        self.assertEqual(self.store.planning_blocker("INC-SHIP"), "INC-SHIP")
        accepted = self.finish("executor-b", carting, sample_result(cart_messages[1]))
        self.assertTrue(accepted["accepted"])  # cart's owner still reports under its own epoch.
        self.assertEqual(self.state(cart_messages[1]), "VERIFYING")
        self.assertEqual((self.owner(ship)["epoch"], self.owner(cart)["epoch"]), (2, 1))
        self.assertNotEqual(self.owner(ship)["target_key"], self.owner(cart)["target_key"])

    def test_no_other_command_for_a_workload_is_claimed_while_its_seal_is_due(self):
        self.ready()
        self.claim("executor-a")
        second = self.same_target_request("INC-SECOND", "ACT-SECOND")
        self.revoke()  # The seal is queued after ACT-SECOND's request.
        self.now += 31  # No lease is live any more: only the due seal holds ACT-SECOND back.
        sealing = self.claim("executor-b")
        self.assertEqual(sealing["topic"], "executor.seal")
        self.assertEqual(self.owner()["seal_required"], 1)
        self.now += 31  # The seal's lease expired unfinished: it is still due, and still first.
        sealing = self.claim("executor-c")
        self.assertEqual(sealing["topic"], "executor.seal")
        self.finish("executor-c", sealing, fence_result(self.proposal, sealing["fence_epoch"]))
        released = self.claim("executor-b")
        self.assertEqual((released["event_id"], released["fence_epoch"]), (second, sealing["fence_epoch"] + 1))

    def same_target_request(self, incident_id, action_id):
        """A second READY action on the same workload, written directly (planning_blocker would forbid it)."""
        incident, proposal, guard = self.messages(incident_id, action_id)
        with self.store.transaction():
            self.store.add_incident(incident_id, incident["target"], "OPEN")
            self.store.add_action(action_id, incident_id, proposal, "READY")
            self.store.activate_action(incident_id, action_id, "READY")
            self.store.save_guard_result(action_id, guard)
            self.store.enqueue("executor.request", {"proposal": proposal, "guard_result": guard,
                                                    "approval": None}, action_id)
        return self.store.newest_request(action_id, "executor.request")

    def test_epochs_persist_across_a_coordinator_restart_and_never_go_back(self):
        self.ready()
        self.assertEqual(self.claim("executor-a")["fence_epoch"], 1)
        self.store.close()
        self.store = SQLiteStore(self.coordinator_path)
        self.stack.callback(self.store.close)
        self.delivery = Delivery(self.store, clock=lambda: self.now, lease_seconds=30)
        self.assertEqual(self.owner()["epoch"], 1)
        self.now += 31
        again = self.claim("executor-a")  # The same identity claiming again is a new lease: a new epoch.
        self.assertEqual(again["fence_epoch"], 2)
        self.assertEqual(self.finish("executor-a", again, sample_result(self.proposal))["accepted"], True)
        self.assertEqual(self.owner()["epoch"], 2)

    def test_a_fence_result_with_a_stale_epoch_gets_409(self):
        self.ready()
        self.claim("executor-a")
        self.revoke()
        delivery = self.claim("executor-b")
        stale = fence_result(self.proposal, delivery["fence_epoch"] - 1)
        with self.assertRaises(ProtocolError) as refused:
            self.finish("executor-b", delivery, stale)
        self.assertEqual((refused.exception.status, str(refused.exception)), (409, "Stale ownership epoch"))
        with self.store.transaction(), self.assertRaises(SupersededResult):  # The lifecycle rule refuses it too.
            record_message(stale, self.store, answers=delivery["event_id"], producer="executor-b")
        self.assertEqual(self.owner()["seal_required"], 1)
        self.assertTrue(self.finish("executor-b", delivery, fence_result(self.proposal, delivery["fence_epoch"]))
                        ["accepted"])
        self.assertEqual(self.owner()["seal_required"], 0)

    def test_a_seal_lease_that_expires_is_claimed_again_under_a_new_epoch(self):
        self.ready()
        self.claim("executor-a")
        self.revoke()
        first = self.claim("executor-b")
        self.now += 31
        second = self.claim("executor-c")
        self.assertEqual((second["topic"], second["fence_epoch"]), ("executor.seal", first["fence_epoch"] + 1))
        with self.assertRaises(ProtocolError):  # The first seal's lease was replaced.
            self.finish("executor-b", first, fence_result(self.proposal, first["fence_epoch"]))

    def test_planning_waits_for_the_seal_and_resumes_after_sealed(self):
        self.ready()
        self.claim("executor-a")
        self.revoke()
        self.assertIsNone(self.store.planning_blocker(self.incident["incident_id"]))  # Not yet withdrawn.
        delivery = self.claim("executor-b")
        self.assertEqual(self.store.planning_blocker(self.incident["incident_id"]), self.incident["incident_id"])
        later, _, _ = self.messages("INC-LATER", "ACT-LATER")
        self.delivery.ingest("observer", later)
        self.assertIsNone(self.delivery.claim("planner-1", "planner")["delivery"])  # Held while sealing.
        self.assertIsNotNone(self.store.planning_blocker("INC-LATER"))
        self.finish("executor-b", delivery, fence_result(self.proposal, delivery["fence_epoch"]))
        self.assertIsNone(self.store.planning_blocker("INC-LATER"))
        planned = self.delivery.claim("planner-1", "planner")["delivery"]
        self.assertEqual(planned["payload"]["incident"]["incident_id"], "INC-LATER")

    def test_a_proposal_for_a_workload_being_sealed_is_refused(self):
        self.ready()
        self.claim("executor-a")
        self.revoke()
        self.claim("executor-b")
        later, proposal, _ = self.messages("INC-LATER", "ACT-LATER")
        handle(later, self.store, LocalEventBus())
        with self.assertRaisesRegex(CoordinatorError, "proposal must wait"):
            handle(proposal, self.store, LocalEventBus())

    def test_a_revocation_with_no_executor_lease_queues_no_seal(self):
        self.ready()
        self.revoke()
        self.assertIsNone(self.claim("executor-a"))  # The request was withdrawn and nothing else waits.
        self.assertEqual(self.store.pending_routes("executor.seal"), [])
        self.assertIsNone(self.owner())
        self.assertIsNone(self.store.planning_blocker(self.incident["incident_id"]))
        self.assertEqual(self.state(), "VALIDATING")  # Unchanged path: the Guard re-evaluates.

    def test_a_failed_execution_after_an_earlier_holder_lost_its_lease_is_sealed(self):
        self.ready()
        self.claim("executor-a")
        self.now += 31
        second = self.claim("executor-b")
        self.finish("executor-b", second, sample_result(self.proposal, status="FAILED", resource_version=self.version))
        (row,) = self.store.pending_routes("executor.seal")
        self.assertEqual(row["action_id"], self.proposal["action_id"])
        self.assertEqual(self.owner()["seal_required"], 1)

    def test_a_failed_execution_by_the_only_holder_needs_no_seal(self):
        self.ready()
        first = self.claim("executor-a")
        self.finish("executor-a", first, sample_result(self.proposal, status="FAILED", resource_version=self.version))
        self.assertEqual(self.store.pending_routes("executor.seal"), [])
        self.assertEqual(self.owner()["seal_required"], 0)

    def test_a_version_5_database_migrates_to_the_ownership_table(self):
        import sqlite3
        path = self.directory / "old.db"
        db = sqlite3.connect(str(path), isolation_level=None)
        migrate(db, MIGRATIONS[:5])
        self.assertEqual(schema_version(db), 5)
        db.close()
        with SQLiteStore(path) as store:
            self.assertEqual(schema_version(store.db), 6)
            self.assertIsNone(store.target_ownership(self.target))
            self.assertEqual(store.claim_target(self.target, "executor-a", "d1", 1), 1)
        with SQLiteStore(path) as store:  # Reopening applies nothing again and keeps the epoch.
            self.assertEqual(store.target_ownership(self.target)["epoch"], 1)
            self.assertEqual(store.target_ownership(self.target)["target_key"], target_key(self.target))


class TransportClient:
    """WorkerClient's operations, answered by the lab's Delivery in process, for one Executor identity."""

    def __init__(self, lab, identity):
        self.lab, self.identity, self.calls, self.failing_results = lab, identity, [], 0

    def post(self, operation, body):
        self.calls.append(operation)
        delivery, ids = self.lab.delivery, (body.get("delivery_id"), body.get("lease_token"))
        if operation == "claim":
            return delivery.claim(self.identity, "executor")
        if operation == "clock":
            return {}
        if operation == "renew":
            return delivery.renew(self.identity, "executor", *ids)
        if operation == "retry":
            return delivery.finish(self.identity, "executor", *ids, None, retry=True)
        if operation == "result":
            if self.failing_results:
                self.failing_results -= 1
                raise ConnectionError("reply lost")
            return delivery.finish(self.identity, "executor", *ids, body["result"])
        raise AssertionError(operation)


class SweepRecorder:
    """The Executor adapter, with each sweep recorded in the client's call list."""

    def __init__(self, adapter, calls):
        self.adapter, self.calls = adapter, calls

    def sweep(self):
        self.calls.append("sweep")
        return self.adapter.sweep()

    def process(self, delivery, lease):
        return self.adapter.process(delivery, lease)

    def acknowledge(self, result):
        return self.adapter.acknowledge(result)


class SealDeliveryGateTests(FencingLab):
    """A seal is an ordinary Executor command in RemoteWorker's pass order (P3.06 claim gating).

    A saved fence_result is re-sent first, and a new seal is claimed, like any
    other command, only when no store change is recorded and the clock is in
    bound; a pass refused by either never sweeps the ledger or claims.
    """

    def worker(self, host, *, store_change=None, clock=None, **options):
        client = TransportClient(self, host)
        if clock is not None:
            client.clock = clock
        journal = WorkerJournal(self.directory / (host + "-transport.db"))
        self.stack.callback(journal.close)
        if store_change is not None:
            client.store = StoreWatch()
            journal.record_store_change(store_change)
        adapter = SweepRecorder(ExecutorAdapter(self.adapters(host)), client.calls)
        return RemoteWorker(client, journal, adapter, **options), client

    def queue_seal(self):
        self.ready()
        self.assertEqual(self.claim("executor-a")["topic"], "executor.request")
        self.revoke()  # Withdraws the claimed request; the next transport transaction queues the seal.

    def test_a_saved_fence_result_is_resent_before_any_sweep_or_claim(self):
        self.queue_seal()
        worker, client = self.worker("executor-b")
        client.failing_results = 1
        with self.assertRaises(ConnectionError):
            worker.once()  # Sealed and saved; the reply to the result is lost.
        self.assertEqual(client.calls[:2], ["sweep", "claim"])
        self.assertEqual((client.calls[-1], len(self.fences()), self.owner()["seal_required"]), ("result", 1, 1))
        client.calls.clear()
        self.assertEqual(worker.once(), {"status": "accepted", "replay": False})
        self.assertEqual(client.calls, ["result"])  # The saved fence_result first; the pass ends there.
        self.assertEqual((self.owner()["seal_required"], self.state()), (0, "FAILED"))
        self.assertEqual(len(self.fences()), 1)
        self.assertEqual(self.template_writes(), [])

    def test_a_recorded_store_change_claims_no_seal_and_sweeps_nothing(self):
        self.queue_seal()
        change = {"reason": "rolled_back", "generation": "g", "commit": 1,
                  "previous_generation": "g", "previous_commit": 9}
        worker, client = self.worker("executor-b", store_change=change)
        self.assertEqual(worker.once(), {"status": "store_changed", "reason": "rolled_back"})
        self.assertEqual(client.calls, [])  # A journal read only: no sweep, no claim.
        self.assertEqual(self.claim("executor-c")["topic"], "executor.seal")  # Still due, for a gated-in peer.

    def test_a_clock_outside_the_bound_claims_no_seal_and_sweeps_nothing(self):
        self.queue_seal()

        class Skewed:
            def within(self, bound):
                return False

            def estimate(self):
                return 300.0, 0.01

        worker, client = self.worker("executor-b", clock=Skewed(), max_clock_skew=2)
        self.assertEqual(worker.once()["status"], "clock_skew")
        self.assertEqual(set(client.calls), {"clock"})  # Measured again; no sweep, no claim.
        self.assertEqual(self.claim("executor-c")["topic"], "executor.seal")

    def test_a_seal_saved_under_an_expired_lease_is_sealed_again_under_the_new_epoch(self):
        """The saved epoch-2 result can never be accepted once the claim moved on; re-sending it would loop."""
        self.queue_seal()
        worker, client = self.worker("executor-b")
        client.failing_results = 1
        with self.assertRaises(ConnectionError):
            worker.once()  # Sealed and saved under epoch 2; the reply to the result is lost.
        (row,) = worker.journal.pending()
        self.assertEqual(row["result"]["epoch"], 2)
        self.now += 31  # Its lease expires before the saved result is re-sent.
        self.assertEqual(worker.once(), {"status": "accepted", "replay": False})
        self.assertEqual(worker.journal.get(row["request_key"])["result"]["epoch"], 3)
        self.assertEqual((self.owner()["seal_required"], self.owner()["epoch"], self.state()), (0, 3, "FAILED"))
        self.assertEqual(len(self.fences()), 1)  # The second seal reads the first fence and sends nothing.
        self.assertEqual(self.template_writes(), [])

    def test_the_journal_discards_only_a_saved_seal(self):
        journal = WorkerJournal(self.directory / "journal.db")
        self.stack.callback(journal.close)
        key = journal.bind({"topic": "executor.request", "payload": {"proposal": self.proposal}})
        with self.assertRaises(ValueError):  # Nothing saved yet.
            journal.discard_seal(key)
        journal.save(key, sample_result(self.proposal))
        with self.assertRaises(ValueError):  # An execution_result stays immutable.
            journal.discard_seal(key)
        self.assertEqual(journal.get(key)["result"]["kind"], "execution_result")

    def test_the_executor_adapter_checks_its_lease_before_the_seal_fences(self):
        self.queue_seal()
        delivery = self.claim("executor-b")

        class Lost:
            def ensure_current(self):
                raise ProtocolError(409, "Lease expired")

        with self.assertRaises(ProtocolError):
            ExecutorAdapter(self.adapters("executor-b")).process(delivery, Lost())
        self.assertEqual(self.cluster.requests("fence"), [])  # The lease check came first: no fence was sent.


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.proposal = sample("proposal")

    def test_a_fence_result_is_validated_strictly(self):
        message = fence_result(self.proposal, 2)
        validate_message(message)
        for change in ({"extra": 1}, {"epoch": 0}, {"epoch": True}, {"epoch": "2"}, {"outcome": "DONE"},
                       {"resource_version": ""}, {"recorded_at": "2026-10-10T12:00:00"},
                       {"target": dict(self.proposal["target"], uid="")}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_message(dict(message, **change))
        for field in ("epoch", "outcome", "resource_version", "recorded_at", "action_id"):
            with self.subTest(missing=field), self.assertRaises(ContractError):
                validate_message({key: value for key, value in message.items() if key != field})
        validate_message(dict(message, resource_version=None))

    def test_the_committed_sample_is_a_valid_fence_result(self):
        message = sample("fence-result")
        validate_message(message)
        self.assertEqual(message["kind"], "fence_result")


class OrphanedLedgerTests(FencingLab):
    """A4.4: a crashed host's in-doubt row closes from live state once its peer finished the action."""

    def crashed_host(self):
        adapters = self.adapters("executor-a")
        row = adapters.ledger.prepare(self.proposal)
        adapters.ledger.start_write(self.proposal["action_id"], self.version,
                                    {"action_id": self.proposal["action_id"], "proposal_sha256": row["fingerprint"],
                                     "target_uid": self.target["uid"],
                                     "template_sha256": self.proposal["parameters"]["template_sha256"],
                                     "attempt_id": "a" * 32}, self.guard,
                                    datetime.now(UTC))
        return adapters

    def peer_writes(self):
        from services.executor.core import execute
        result = execute(self.proposal, self.adapters("executor-b"))
        self.assertEqual(result["status"], "APPLIED")

    def test_the_sweep_closes_a_row_its_peer_finished_and_frees_the_workload(self):
        adapters = self.crashed_host()
        self.peer_writes()
        with self.assertRaises(TargetBusy):  # Before: the orphaned row refuses the next action on the workload.
            adapters.ledger.prepare(dict(self.proposal, action_id="ACT-NEXT"))
        self.assertEqual(ExecutorAdapter(adapters).sweep(), 1)
        row = adapters.ledger.get(self.proposal["action_id"])
        self.assertEqual((row["phase"], row["result"]["status"]), ("DONE", "APPLIED"))
        self.assertIn("adopted peer write", row["result"]["details"])
        adapters.ledger.prepare(dict(self.proposal, action_id="ACT-NEXT"))  # No TargetBusy any more.
        self.assertEqual(len(self.template_writes()), 1)

    def test_the_sweep_closes_a_row_whose_target_was_sealed(self):
        adapters = self.crashed_host()
        seal(self.proposal, 2, self.adapters("executor-b"))
        self.assertEqual(ExecutorAdapter(adapters).sweep(), 1)
        self.assertEqual(adapters.ledger.get(self.proposal["action_id"])["result"]["status"], "SUPERSEDED")

    def test_the_sweep_never_writes_and_leaves_an_undecided_row_in_doubt(self):
        adapters = self.crashed_host()
        self.assertEqual(ExecutorAdapter(adapters).sweep(), 0)
        self.assertEqual(adapters.ledger.get(self.proposal["action_id"])["phase"], "WRITE_STARTED")
        self.assertEqual(self.cluster.requests(), [])  # No patch, no fence: only the seal or its command may.
        self.assertEqual(ExecutorAdapter(adapters).sweep(), 0)  # Backed off: not even read again at once.
        self.assertIsNone(settle(dict(self.proposal, action_id="ACT-UNKNOWN"), adapters))

    def test_the_remote_worker_sweeps_before_it_claims(self):
        calls = []

        class Component:
            def sweep(self):
                calls.append("sweep")
                raise RuntimeError("cluster unavailable")  # A failed sweep never stops the claim.

            def process(self, delivery, lease):
                return None

        class Client:
            def post(self, operation, body):
                calls.append(operation)
                return {"delivery": None}

        class Journal:
            def pending(self):
                return []

        worker = RemoteWorker(Client(), Journal(), Component())
        self.assertEqual(worker.once(), {"status": "idle"})
        self.assertEqual(calls, ["sweep", "claim"])


def sample_result(proposal, status="APPLIED", resource_version="999"):
    result = sample("execution-result")
    result.update(incident_id=proposal["incident_id"], action_id=proposal["action_id"],
                  target=deepcopy(proposal["target"]), status=status, resource_version=resource_version,
                  recorded_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"))
    return result


def fence_result(proposal, epoch, outcome="SEALED"):
    return {"schema_version": "1.0", "kind": "fence_result", "incident_id": proposal["incident_id"],
            "action_id": proposal["action_id"], "target": deepcopy(proposal["target"]), "epoch": epoch,
            "outcome": outcome, "resource_version": "9001",
            "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")}


if __name__ == "__main__":
    unittest.main()
