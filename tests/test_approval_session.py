"""Per-run approval authority child process, approver strategies and the harness path.

The authority is a real `python -m runtime.approval_http` child on a free
loopback port, against a file-backed Coordinator database. Guard, Coordinator
and Executor are the real code; only Kubernetes is the in-memory fake. The
sample records are moved to the current time because the child uses the real
clock for approval timestamps and expiry.
"""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from runtime.approval_http import ApprovalClient
from runtime.approval_session import (
    AUTOMATED_LAB, LAB_EXECUTOR, LAB_GUARD, LAB_OPERATOR, AuthorityUnavailable, AutoLabApprover,
    InteractiveApprover, LocalApprovalAuthority, RejectingApprover, approver_for,
    executor_client_from_environment, operator_identity, require_attended,
)
from runtime.executor_pipeline import GuardAuthorizer, process_executor_requests
from runtime.guard_pipeline import process_guard_requests
from runtime.recovery_runner import advance
from runtime.service_identity import ProtocolError
from services.coordinator.core import LocalEventBus, SQLiteStore, approve, handle
from services.coordinator.store import _digest
from services.executor.core import ExecutionAdapters, execute
from services.executor.store import LocalTargetCoordinator, SQLiteExecutionLedger
from shared import log
from tests.support.executor_fake import FakeDeploymentAPI

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_NOW = datetime(2026, 10, 10, 12, 0, 36, tzinfo=UTC)


def shifted(value, delta):
    """Move every UTC timestamp in a sample record by delta, keeping its format."""
    if isinstance(value, dict):
        return {key: shifted(item, delta) for key, item in value.items()}
    if isinstance(value, list):
        return [shifted(item, delta) for item in value]
    if isinstance(value, str) and len(value) >= 20 and value[4:5] == "-" and value[10:11] == "T":
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if parsed.utcoffset() == timedelta(0):
            moved = (parsed + delta).isoformat()
            return moved.replace("+00:00", "Z") if value.endswith("Z") else moved
    return value


def sample(name, delta):
    return shifted(json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8")), delta)


def real_now():
    return datetime.now(UTC)


def sample_delta():
    """Whole seconds that make the samples' 'now' one second ago."""
    return timedelta(seconds=int((real_now() - SAMPLE_NOW).total_seconds()) - 1)


class AuthorityCase(unittest.TestCase):
    operator = LAB_OPERATOR

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_dir = Path(directory.name) / "run"
        self.run_dir.mkdir()
        delta = sample_delta()
        self.db = self.run_dir / "coordinator.sqlite"
        self.store = SQLiteStore(self.db)
        self.addCleanup(lambda: self.store.close())
        self.bus = LocalEventBus()
        self.incident = sample("messages/incident.json", delta)
        self.proposal = sample("messages/proposal.json", delta)
        self.policy = sample("policy.json", delta)
        self.baseline = sample("baseline.json", delta)
        self.dependencies = sample("messages/dependency-result.json", delta)
        self.cluster = FakeDeploymentAPI(sample("observations.json", delta)["failed-rollout"][-1], clock=real_now)
        for message in (self.incident, self.proposal, sample("messages/guard-result.json", delta)):
            handle(message, self.store, self.bus)
        self.authority = LocalApprovalAuthority(self.run_dir, self.db, self.proposal["target"], operator=self.operator)
        self.authority.start()
        self.addCleanup(self.authority.close)
        self.tokens = dict(self.authority._tokens)
        self.binding = {"action_id": self.proposal["action_id"], "proposal_sha256": _digest(self.proposal)}

    def state(self):
        return self.store.action(self.proposal["action_id"])["state"]

    def guard(self):
        return process_guard_requests(
            self.store, self.bus, state_reader=lambda p: self.cluster.read(p["target"]),
            policy_reader=lambda p: self.policy, baseline_reader=lambda p: self.baseline,
            dependency_reader=lambda p: self.dependencies, clock=real_now)

    def adapters(self):
        ledger = SQLiteExecutionLedger(self.run_dir / "executor.db")
        self.addCleanup(ledger.close)
        authorizer = GuardAuthorizer(
            policy_reader=lambda p: self.policy, baseline_reader=lambda p: self.baseline,
            dependency_reader=lambda p: self.dependencies, approval_reader=self.authority.executor_client())
        return ExecutionAdapters(ledger=ledger, coordination=LocalTargetCoordinator(self.run_dir / "locks"),
                                 cluster=self.cluster, authorizer=authorizer, clock=real_now)

    def step(self, approver, adapters=None):
        """One pass of the shared recovery runner with the given approver."""
        adapters = adapters or self.adapters()
        self.executed = []
        return advance(self.store, self.incident["incident_id"], plan=lambda: self.fail("already planned"),
                       assess=lambda p: None, guard=self.guard, approve=approver,
                       execute=lambda p: self.executed.extend(process_executor_requests(
                           self.store, self.bus, adapters=adapters)),
                       verify=lambda p, r: None)

    def history(self):
        return self.authority.operator_client().post("history", self.binding)["history"]

    def assert_status(self, status, function):
        with self.assertRaises(ProtocolError) as error:
            function()
        self.assertEqual(error.exception.status, status)


class AuthorityProcessTests(AuthorityCase):
    def test_child_binds_a_free_port_answers_readyz_and_close_terminates_it(self):
        self.assertTrue(self.authority.running())
        self.assertGreater(self.authority.port, 0)
        self.assertEqual(self.authority.url, f"http://127.0.0.1:{self.authority.port}")
        self.assertTrue(self.authority.ready())
        self.assertIn("--port", self.authority.process.args)
        self.assertEqual(self.authority.process.args[self.authority.process.args.index("--port") + 1], "0")
        self.authority.close()
        self.assertFalse(self.authority.running())
        self.assertFalse(self.authority.ready())
        with self.assertRaises(AuthorityUnavailable):
            self.authority.executor_client()
        self.authority.close()  # Idempotent.

    def test_written_configuration_names_one_exact_target_and_no_secret(self):
        identities = json.loads(self.authority.identities_path.read_text(encoding="utf-8"))
        permissions = json.loads(self.authority.permissions_path.read_text(encoding="utf-8"))
        self.assertEqual({name: entry["role"] for name, entry in identities.items()},
                         {LAB_OPERATOR: "operator", LAB_GUARD: "guard", LAB_EXECUTOR: "executor"})
        self.assertTrue(all(set(entry) == {"role", "token_env"} for entry in identities.values()))
        self.assertEqual({grant["target"]["uid"] for grants in permissions.values() for grant in grants},
                         {self.proposal["target"]["uid"]})
        self.assertEqual(permissions[LAB_EXECUTOR][0]["operations"], ["read"])
        self.assertEqual(permissions[LAB_GUARD][0]["operations"], ["read"])
        self.assertEqual(len(set(self.tokens.values())), 3)

    def test_failed_start_reports_unavailable_and_leaves_nothing_running(self):
        broken = LocalApprovalAuthority(self.run_dir / "other", self.run_dir / "other" / "db.sqlite",
                                        self.proposal["target"], python=sys.executable, startup_timeout=10)
        # An identity name the child's loader refuses: it exits before it reports a port.
        broken.roles = {LAB_OPERATOR: "operator", "bad name": "guard", LAB_EXECUTOR: "executor"}
        with self.assertRaises(AuthorityUnavailable):
            broken.start()
        self.assertFalse(broken.running())
        with self.assertRaises(ValueError):
            LocalApprovalAuthority(self.run_dir, self.db, dict(self.proposal["target"], uid=""))
        with self.assertRaises(ValueError):
            LocalApprovalAuthority(self.run_dir, self.db, self.proposal["target"], operator=LAB_EXECUTOR)

    def test_reader_identities_cannot_create_or_revoke_and_tokens_do_not_cross_identities(self):
        created = InteractiveApprover(self.authority).create(
            self.proposal, self.store.action(self.proposal["action_id"])["guard_result"])
        self.assertEqual(created["operator_id"], LAB_OPERATOR)
        approval_id = self.history()[0]["approval_id"]
        bodies = {"create": dict(self.binding, policy_version=self.policy["policy_version"],
                                 expires_at=(real_now() + timedelta(seconds=60)).isoformat()),
                  "revoke": dict(self.binding, approval_id=approval_id, reason="reader attempt")}
        for client in (self.authority.executor_client(), self.authority.guard_client()):
            for operation, body in bodies.items():
                with self.subTest(identity=client.identity, operation=operation):
                    self.assert_status(403, lambda c=client, o=operation, b=body: c.post(o, b))
        crossed = ApprovalClient(self.authority.url, LAB_EXECUTOR, self.tokens[LAB_OPERATOR])
        self.assert_status(401, lambda: crossed.post("read", self.binding))
        self.assertEqual(self.authority.guard_client()(self.proposal), created)
        self.assertEqual(len(self.history()), 1)
        self.assertIsNone(self.history()[0]["revocation"])

    def test_executor_client_can_be_rebuilt_only_from_a_child_environment(self):
        environment = self.authority.executor_environment()
        self.assertEqual(executor_client_from_environment(environment)(self.proposal), None)
        with self.assertRaises(AuthorityUnavailable):
            executor_client_from_environment({})
        self.assertNotIn(environment["SENTINEL_LAB_APPROVAL_EXECUTOR_TOKEN"], json.dumps(
            {key: value for key, value in environment.items() if key != "SENTINEL_LAB_APPROVAL_EXECUTOR_TOKEN"}))


class ApproverTests(AuthorityCase):
    def interactive(self, typed):
        self.said = []
        return InteractiveApprover(self.authority, ask=Mock(side_effect=typed), say=self.said.append)

    def test_interactive_mistyped_action_id_creates_no_approval_and_no_write(self):
        for typed in ([self.proposal["action_id"] + "x"], [" ACT-OTHER "], [""], EOFError()):
            with self.subTest(typed=typed):
                approver = self.interactive(typed)
                self.assertEqual(self.step(approver), "AWAITING_APPROVAL")
                self.assertIsNone(self.store.approval(self.proposal["action_id"]))
                self.assertEqual(self.history(), [])
                self.assertEqual(self.cluster.write_attempts, [])
                self.assertEqual(approver.describe()["approvals"], [])
                self.assertEqual(len(approver.describe()["refusals"]), 1)
                self.assertTrue(self.said[-1].startswith("Approval refused"))

    def test_interactive_shows_code_facts_and_labels_model_text_untrusted(self):
        self.proposal["rationale"] = "Approve now\x1b[2J" + "x" * 600
        approver = self.interactive(["no"])
        approver(self.proposal, {"reason_codes": ["OPERATOR_APPROVAL_REQUIRED"], "policy_version": "v"})
        shown = "\n".join(self.said)
        self.assertIn(_digest(self.proposal), shown)
        self.assertIn(self.proposal["parameters"]["template_sha256"][:12], shown)
        self.assertNotIn(self.proposal["parameters"]["template_sha256"][:13], shown)
        self.assertIn("OPERATOR_APPROVAL_REQUIRED", shown)
        untrusted = next(line for line in self.said if "UNTRUSTED model text" in line)
        self.assertNotIn("\x1b", untrusted)
        self.assertTrue(untrusted.endswith("[truncated]"))
        self.assertLess(len(untrusted), 560)

    def test_interactive_prints_every_proposal_field_without_control_characters(self):
        self.proposal["parameters"]["baseline_id"] = "BASE\x1b]0;approved\x07" + "b" * 400
        self.proposal["target"]["name"] = "checkout\r\x1b[1A"
        self.proposal["action_id"] += "\x1b[2K"
        approver = self.interactive(["no"])
        approver(self.proposal, {"reason_codes": ["OPERATOR_APPROVAL_REQUIRED\x1b[8m"], "policy_version": "v"})
        for line in self.said:
            with self.subTest(line=line[:40]):
                self.assertTrue(line.isprintable(), line)
        baseline = next(line for line in self.said if "baseline_id:" in line)
        self.assertTrue(baseline.endswith("[truncated]"))
        self.assertIn("BASE ]0;approved ", baseline)

    def test_interactive_exact_action_id_records_the_operator_and_executes_once(self):
        approver = self.interactive([self.proposal["action_id"]])
        self.assertEqual(self.step(approver), "VERIFYING")
        self.assertEqual([result["status"] for result in self.executed], ["APPLIED"])
        self.assertEqual(len(self.cluster.writes), 1)
        (entry,) = self.history()
        self.assertEqual(entry["approval"]["operator_id"], LAB_OPERATOR)
        self.assertTrue(self.store.has_managed_approval(self.proposal["action_id"]))
        summary = approver.describe()
        self.assertEqual((summary["mode"], summary["identity"], summary["via"], summary["human"]),
                         ("interactive", LAB_OPERATOR, "approval_http", True))
        self.assertEqual(summary["approvals"][0]["approval_id"], entry["approval_id"])
        lifetime = (datetime.fromisoformat(entry["approval"]["expires_at"].replace("Z", "+00:00"))
                    - datetime.fromisoformat(entry["approval"]["approved_at"].replace("Z", "+00:00")))
        self.assertLessEqual(lifetime, timedelta(seconds=181))

    def test_changed_proposal_between_display_and_confirm_is_refused_with_409(self):
        decision = self.store.action(self.proposal["action_id"])["guard_result"]
        shown = dict(self.proposal, rationale="Text the operator saw, not the stored proposal")
        approver = self.interactive([self.proposal["action_id"]])
        self.assert_status(409, lambda: approver(shown, decision))
        self.assertEqual(self.history(), [])
        self.assertEqual(self.state(), "AWAITING_APPROVAL")

    def test_reject_mode_never_approves(self):
        approver = approver_for("reject", self.authority)
        self.assertIsInstance(approver, RejectingApprover)
        self.assertEqual(self.step(approver), "AWAITING_APPROVAL")
        self.assertEqual(self.history(), [])
        self.assertEqual(self.cluster.write_attempts, [])
        self.assertEqual((approver.describe()["mode"], approver.describe()["identity"]), ("reject", None))

    def test_auto_lab_needs_the_automated_identity(self):
        with self.assertRaises(ValueError):
            AutoLabApprover(self.authority)
        self.assertEqual(operator_identity("auto-lab"), AUTOMATED_LAB)
        self.assertEqual(operator_identity("interactive"), LAB_OPERATOR)
        self.assertEqual(operator_identity("interactive", "operator-2"), "operator-2")
        for mode, identity in (("auto-lab", LAB_OPERATOR), ("interactive", AUTOMATED_LAB),
                               ("reject", AUTOMATED_LAB), ("anything", None)):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                operator_identity(mode, identity)
        with self.assertRaises(ValueError):
            InteractiveApprover(self.authority, lifetime_seconds=301)

    def test_operator_identity_refuses_reader_names_and_names_the_authority_would_reject(self):
        for mode in ("interactive", "reject"):
            for identity in (LAB_GUARD, LAB_EXECUTOR):
                with self.subTest(mode=mode, identity=identity), self.assertRaisesRegex(ValueError, "reserved"):
                    operator_identity(mode, identity)
            for identity in ("", " ", "-leading-dash", "has space", "line\nbreak", "x" * 129, "café", 7):
                with self.subTest(mode=mode, identity=identity), self.assertRaisesRegex(ValueError, "1 to 128"):
                    operator_identity(mode, identity)
        for identity in ("a", "operator_2.lab-B", "x" * 128):
            self.assertEqual(operator_identity("interactive", identity), identity)

    def test_revocation_after_allow_before_write_gives_zero_writes(self):
        adapters = self.adapters()
        approver = self.interactive([self.proposal["action_id"]] * 2)
        approver(self.proposal, self.store.action(self.proposal["action_id"])["guard_result"])
        self.assertEqual(self.guard()[0]["verdict"], "ALLOW")
        self.assertEqual(self.state(), "READY")
        first = self.history()[0]
        self.authority.operator_client().post("revoke", dict(
            self.binding, approval_id=first["approval_id"], reason="Operator withdrew before execution"))
        self.assertEqual(self.state(), "VALIDATING")
        self.assertEqual(process_executor_requests(self.store, self.bus, adapters=adapters), [])
        self.assertEqual(execute(self.proposal, adapters)["status"], "FAILED")
        self.assertEqual(self.cluster.write_attempts, [])
        self.assertEqual(self.guard()[0]["reason_codes"], ["OPERATOR_APPROVAL_REQUIRED"])
        self.assertEqual(len(self.history()), 1)
        self.assertEqual(self.history()[0]["revocation"]["operator_id"], LAB_OPERATOR)

    def test_revocation_then_reapproval_through_the_runner_executes_exactly_once(self):
        approver = self.interactive([self.proposal["action_id"]] * 2)
        approver(self.proposal, self.store.action(self.proposal["action_id"])["guard_result"])
        self.assertEqual(self.guard()[0]["verdict"], "ALLOW")
        self.authority.operator_client().post("revoke", dict(
            self.binding, approval_id=self.history()[0]["approval_id"], reason="Withdrawn"))
        self.assertEqual(self.step(approver), "VERIFYING")
        self.assertEqual(len(self.cluster.write_attempts), 1)
        history = self.history()
        self.assertEqual([entry["revocation"] is None for entry in history], [False, True])

    def test_final_lookup_revocation_after_journal_gives_zero_writes(self):
        approver = self.interactive([self.proposal["action_id"]])
        approver(self.proposal, self.store.action(self.proposal["action_id"])["guard_result"])
        self.assertEqual(self.guard()[0]["verdict"], "ALLOW")
        adapters = self.adapters()
        original = adapters.ledger.start_write

        def revoke_after_journal(*args):
            original(*args)
            self.authority.operator_client().post("revoke", dict(
                self.binding, approval_id=self.history()[0]["approval_id"], reason="Withdrawn at the cutoff"))

        with patch.object(adapters.ledger, "start_write", side_effect=revoke_after_journal):
            result = execute(self.proposal, adapters)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.cluster.write_attempts, [])

    def test_authority_down_after_allow_gives_zero_writes(self):
        approver = self.interactive([self.proposal["action_id"]])
        approver(self.proposal, self.store.action(self.proposal["action_id"])["guard_result"])
        self.assertEqual(self.guard()[0]["verdict"], "ALLOW")
        adapters = self.adapters()
        self.authority.stop()
        self.assertFalse(self.authority.running())
        results = process_executor_requests(self.store, self.bus, adapters=adapters)
        self.assertEqual([result["status"] for result in results], ["FAILED"])
        self.assertIn("no write was attempted", results[0]["details"])
        self.assertEqual(self.cluster.write_attempts, [])
        self.assertEqual(self.state(), "FAILED")

    def test_authority_down_at_the_final_lookup_gives_zero_writes(self):
        approver = self.interactive([self.proposal["action_id"]])
        approver(self.proposal, self.store.action(self.proposal["action_id"])["guard_result"])
        self.assertEqual(self.guard()[0]["verdict"], "ALLOW")
        adapters = self.adapters()
        original = adapters.ledger.start_write

        def stop_after_journal(*args):
            original(*args)
            self.authority.stop()

        with patch.object(adapters.ledger, "start_write", side_effect=stop_after_journal):
            result = execute(self.proposal, adapters)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.cluster.write_attempts, [])

    def test_legacy_local_approval_is_not_promoted_on_the_harness_path(self):
        approve(self.proposal["action_id"], "local-demo", self.store)
        self.assertEqual(self.guard()[0]["verdict"], "ALLOW")  # The in-process Guard reads the local record.
        results = process_executor_requests(self.store, self.bus, adapters=self.adapters())
        self.assertEqual([result["status"] for result in results], ["FAILED"])
        self.assertEqual(self.cluster.write_attempts, [])
        self.assertIsNone(self.authority.executor_client()(self.proposal))

    def test_tokens_never_reach_run_folder_logs_or_argv(self):
        stream = io.StringIO()
        approver = self.interactive([self.proposal["action_id"]])
        with log.configured("test", level="DEBUG", stream=stream):
            self.assertEqual(self.step(approver), "VERIFYING")
            self.assert_status(401, lambda: ApprovalClient(
                self.authority.url, LAB_OPERATOR, "w" * 48).post("read", self.binding))
        argv = list(self.authority.process.args)
        summary = json.dumps(approver.describe())
        self.authority.close()
        self.store.close()
        files = [path for path in self.run_dir.rglob("*") if path.is_file()]
        self.assertTrue(any(path.name == "approval-authority.log" and path.stat().st_size for path in files))
        for name, token in self.tokens.items():
            with self.subTest(identity=name):
                for path in files:
                    self.assertNotIn(token.encode(), path.read_bytes(), path.name)
                self.assertNotIn(token, " ".join(argv))
                self.assertNotIn(token, stream.getvalue())
                self.assertNotIn(token, summary)
        self.assertEqual(self.authority._tokens, {})


class AutoLabTests(AuthorityCase):
    operator = AUTOMATED_LAB

    def test_auto_lab_authenticates_as_the_automated_identity_and_says_so(self):
        approver = approver_for("auto-lab", self.authority, ask=self.fail, say=self.fail)
        self.assertEqual(self.step(approver), "VERIFYING")
        self.assertEqual(self.history()[0]["approval"]["operator_id"], AUTOMATED_LAB)
        summary = approver.describe()
        self.assertEqual((summary["mode"], summary["identity"], summary["human"]), ("auto-lab", AUTOMATED_LAB, False))
        self.assertIn("no person decided", summary["decided_by"])
        self.assertEqual(len(self.cluster.writes), 1)


class AttendedTests(unittest.TestCase):
    def test_interactive_mode_needs_a_terminal(self):
        with self.assertRaisesRegex(ValueError, "needs a terminal"):
            require_attended("interactive", io.StringIO())
        require_attended("interactive", Mock(isatty=Mock(return_value=True)))
        for mode in ("auto-lab", "reject"):
            require_attended(mode, io.StringIO())
        with self.assertRaises(ValueError):
            require_attended("pre-authorized", io.StringIO())


class HarnessApprovalTests(unittest.TestCase):
    """experiments/recovery/live.py routes case() approval and the summary through the authority."""

    def setUp(self):
        from experiments.recovery.live import Experiment
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        delta = sample_delta()
        cluster = FakeDeploymentAPI(sample("observations.json", delta)["failed-rollout"][-1], clock=real_now)
        proposal = sample("messages/proposal.json", delta)
        self.incident = sample("messages/incident.json", delta)
        policy = sample("policy.json", delta)

        class OfflineExperiment(Experiment):
            def deployment(self):
                return cluster.read(proposal["target"])["deployment"]

            def build_proposal(self, incident, folder, rationale=None):
                return deepcopy(proposal)

            def dependency_result(self, current):
                return sample("messages/dependency-result.json", delta)

        # Any cluster or container command means the test went too far.
        for patcher in (patch("subprocess.run", side_effect=AssertionError("cluster command attempted")),
                        patch("shared.provenance.collect", return_value={"source_revision": "test"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.experiment_type, self.proposal, self.policy, self.delta = OfflineExperiment, proposal, policy, delta

    def make(self):
        experiment = self.experiment_type(self.root / "run")
        experiment.target, experiment.policy = self.proposal["target"], self.policy
        experiment.baseline = sample("baseline.json", self.delta)
        self.addCleanup(experiment.store.close)
        self.addCleanup(experiment.close_approval_authority)
        handle(self.incident, experiment.store, experiment.bus)
        return experiment

    def test_auto_lab_case_approval_is_managed_recorded_and_labelled(self):
        experiment = self.make()
        experiment.configure_approval("auto-lab")
        self.assertEqual(experiment.summary["operator_approval"],
                         {"mode": "auto-lab", "identity": AUTOMATED_LAB, "via": "approval_http"})
        folder, proposal = experiment.case("approved-rollback", self.incident, coordinator=True)
        saved = json.loads((folder / "approval.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["operator_id"], AUTOMATED_LAB)
        self.assertTrue(experiment.store.has_managed_approval(proposal["action_id"]))
        self.assertEqual(experiment.store.action(proposal["action_id"])["state"], "READY")
        summary = experiment.summary["operator_approval"]
        self.assertEqual((summary["mode"], summary["human"], summary["approvals"][0]["operator_id"]),
                         ("auto-lab", False, AUTOMATED_LAB))
        reader = experiment.authority.executor_client()
        self.assertEqual(reader(proposal), saved)
        experiment.close_approval_authority()
        self.assertTrue(experiment.summary["operator_approval"]["authority_stopped"])
        with self.assertRaises(OSError):
            reader(proposal)  # Authority gone: the Executor's lookup fails closed.

    def test_reject_mode_case_stops_before_any_write_route(self):
        experiment = self.make()
        experiment.configure_approval("reject")
        with self.assertRaisesRegex(RuntimeError, "did not approve"):
            experiment.case("approved-rollback", self.incident, coordinator=True)
        folder = experiment.directory / "approved-rollback"
        self.assertIsNone(json.loads((folder / "approval.json").read_text(encoding="utf-8")))
        action_id = json.loads((folder / "proposal.json").read_text(encoding="utf-8"))["action_id"]
        self.assertEqual(experiment.store.action(action_id)["state"], "AWAITING_APPROVAL")
        self.assertFalse(experiment.store.pending_routes("executor.request"))
        self.assertEqual(len(experiment.summary["operator_approval"]["refusals"]), 1)

    def test_executor_adapters_and_crash_worker_read_approval_through_the_authority(self):
        from experiments.recovery import live
        experiment = self.make()
        experiment.configure_approval("auto-lab")
        with patch.object(live, "build_adapters") as build:
            experiment.adapters(experiment.directory, coordinator=True)
            experiment.adapters(experiment.directory)
        coordinated, file_backed = (call.kwargs["approval_reader"] for call in build.call_args_list)
        self.assertIsInstance(coordinated, ApprovalClient)
        self.assertEqual((coordinated.identity, coordinated.url.port), (LAB_EXECUTOR, experiment.authority.port))
        self.assertIsNone(file_backed)  # File-backed negative cases keep their fixed record.
        config = experiment.directory / "worker.json"
        config.write_text(json.dumps({"directory": str(experiment.directory), "folder": str(experiment.directory),
                                      "credentials": "unused"}), encoding="utf-8")
        with patch.dict("os.environ", {}, clear=True), patch.object(live, "build_adapters") as build:
            with self.assertRaises(AuthorityUnavailable):
                live.crash_worker(config)
            build.assert_not_called()
        with patch.dict("os.environ", experiment.authority.executor_environment()), \
                patch.object(live, "build_adapters", side_effect=RuntimeError("stop before Kubernetes")) as build:
            with self.assertRaisesRegex(RuntimeError, "stop before Kubernetes"):
                live.crash_worker(config)
        reader = build.call_args.kwargs["approval_reader"]
        self.assertEqual((type(reader), reader.identity), (ApprovalClient, LAB_EXECUTOR))

    def test_crash_restart_passes_the_executor_token_only_through_the_worker_environment(self):
        from experiments.recovery import live
        experiment = self.make()
        experiment.configure_approval("auto-lab")
        launched, worker_output = [], io.StringIO()

        def worker(command, **kwargs):
            launched.append((command, kwargs))
            # Run the real worker entry in-process with exactly the environment it was given,
            # stopping before Kubernetes, so its output is captured like the child's would be.
            with patch.dict("os.environ", kwargs.get("env") or {}, clear=True), \
                    patch.object(live, "build_adapters", side_effect=RuntimeError("stop before Kubernetes")) as build, \
                    patch("sys.stdout", worker_output), patch("sys.stderr", worker_output):
                with self.assertRaisesRegex(RuntimeError, "stop before Kubernetes"):
                    live.crash_worker(command[-1])
            reader = build.call_args.kwargs["approval_reader"]
            self.assertEqual((type(reader), reader.identity), (ApprovalClient, LAB_EXECUTOR))
            return Mock(returncode=73, stdout=worker_output.getvalue(), stderr="")

        with patch.object(experiment, "fault", return_value=self.incident), \
                patch.object(live.subprocess, "run", side_effect=worker), \
                patch.object(live, "SQLiteExecutionLedger", side_effect=RuntimeError("stop after the worker")):
            tokens = None
            original = experiment.approval_authority

            def authority():
                nonlocal tokens
                started = original()
                tokens = dict(started._tokens)
                return started

            with patch.object(experiment, "approval_authority", side_effect=authority):
                with self.assertRaisesRegex(RuntimeError, "stop after the worker"):
                    experiment.crash_restart()
        self.assertEqual(len(launched), 1)
        command, kwargs = launched[0]
        self.assertIn("--crash-worker", command)
        environment = kwargs.get("env")
        self.assertIsNotNone(environment, "The crash worker must receive an explicit environment")
        self.assertEqual(environment["SENTINEL_LAB_APPROVAL_EXECUTOR_TOKEN"], tokens[LAB_EXECUTOR])
        self.assertEqual((environment["SENTINEL_LAB_APPROVAL_IDENTITY"], environment["SENTINEL_LAB_APPROVAL_URL"]),
                         (LAB_EXECUTOR, experiment.authority.url))
        self.assertEqual(set(tokens), {AUTOMATED_LAB, LAB_GUARD, LAB_EXECUTOR})
        for name in (AUTOMATED_LAB, LAB_GUARD):  # Only the read-only Executor token leaves the harness.
            self.assertNotIn(tokens[name], environment.values())
        config = Path(command[-1])
        self.assertEqual(config, experiment.directory / "crash-after-write" / "worker.json")
        self.assertEqual(set(json.loads(config.read_text(encoding="utf-8"))),
                         {"directory", "folder", "credentials", "cluster_id", "context"})
        summary = json.dumps(experiment.summary)
        experiment.close_approval_authority()
        files = [path for path in experiment.directory.rglob("*") if path.is_file()]
        for name, token in tokens.items():
            with self.subTest(identity=name):
                self.assertNotIn(token, " ".join(command))
                self.assertNotIn(token, worker_output.getvalue())
                self.assertNotIn(token, summary)
                for path in files:
                    self.assertNotIn(token.encode(), path.read_bytes(), str(path.relative_to(experiment.directory)))

    def test_a_scenario_without_approval_is_recorded_as_such_and_cannot_start_the_authority(self):
        experiment = self.make()
        experiment.approval_not_requested("no approval was requested in this scenario")
        summary = experiment.summary["operator_approval"]
        self.assertEqual((summary["mode"], summary["identity"], summary["via"], summary["human"]),
                         ("not-requested", None, None, False))
        with self.assertRaisesRegex(RuntimeError, "requests no approval"):
            experiment.case("approved-rollback", self.incident, coordinator=True)
        self.assertIsNone(experiment.authority)
        self.assertFalse((experiment.directory / "approval-identities.json").exists())

    def test_mode_is_fixed_once_the_authority_runs(self):
        experiment = self.make()
        experiment.configure_approval("auto-lab")
        experiment.approval_authority()
        with self.assertRaisesRegex(RuntimeError, "cannot change"):
            experiment.configure_approval("interactive")


class EntryPointRefusalTests(unittest.TestCase):
    """Non-interactive runs without an explicit approval mode refuse before any cluster change."""

    def run_main(self, module, arguments):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run_dir = Path(temp.name) / "run"
        with patch("sys.argv", ["harness", "--run-dir", str(run_dir), *arguments]), \
                patch("sys.stdin", io.StringIO()), patch("sys.stderr", io.StringIO()) as stderr, \
                patch("subprocess.run", side_effect=AssertionError("cluster command attempted")):
            with self.assertRaises(SystemExit) as stopped:
                module.main()
        self.assertNotEqual(stopped.exception.code, 0)
        self.assertFalse(run_dir.exists())
        return stderr.getvalue()

    def test_agent_harness_without_approval_refuses_without_a_terminal(self):
        from experiments.boutique.harness import live
        self.assertIn("needs a terminal", self.run_main(live, ["--agent"]))
        self.assertIn("needs a terminal", self.run_main(live, ["--agent", "--approval", "interactive"]))
        self.assertIn("reserved", self.run_main(live, ["--approval", "interactive", "--operator", AUTOMATED_LAB]))
        self.assertIn("automated-lab identity", self.run_main(live, ["--approval", "auto-lab", "--operator", "me"]))

    def test_executor_experiment_without_approval_refuses_without_a_terminal(self):
        from experiments.recovery import live
        self.assertIn("needs a terminal", self.run_main(live, []))

    def test_guard_rejection_run_records_that_no_approval_was_requested(self):
        from experiments.boutique.harness import live
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run_dir = Path(temp.name) / "run"
        with patch("sys.argv", ["harness", "--run-dir", str(run_dir), "--guard-rejection"]), \
                patch("sys.stdin", io.StringIO()), patch("sys.stderr", io.StringIO()), \
                patch("sys.stdout", io.StringIO()), \
                patch("subprocess.run", side_effect=AssertionError("cluster command attempted")), \
                patch("shared.provenance.collect", return_value={"source_revision": "test"}), \
                patch.object(live, "tool_versions", return_value={}), \
                patch.object(live.BoutiqueExperiment, "prepare", side_effect=RuntimeError("stop before the cluster")):
            self.assertEqual(live.main(), 1)
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))["operator_approval"]
        self.assertEqual(summary["mode"], "not-requested")  # Not 'interactive' under a person's identity.
        self.assertEqual((summary["identity"], summary["via"], summary["human"]), (None, None, False))
        self.assertIn("no approval was requested", summary["decided_by"])
        self.assertFalse((run_dir / "approval-identities.json").exists())

    def test_both_harnesses_refuse_a_reserved_or_malformed_operator_before_any_cluster_change(self):
        from experiments.boutique.harness import live as boutique
        from experiments.recovery import live as recovery
        for module in (boutique, recovery):
            for identity in (LAB_GUARD, LAB_EXECUTOR):
                with self.subTest(module=module.__name__, identity=identity):
                    self.assertIn("reserved", self.run_main(module, ["--approval", "interactive", "--operator", identity]))
            with self.subTest(module=module.__name__, identity="has space"):
                self.assertIn("1 to 128", self.run_main(module, ["--approval", "interactive", "--operator", "has space"]))


if __name__ == "__main__":
    unittest.main()
