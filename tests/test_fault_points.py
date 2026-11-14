"""Fault points are inert outside lab runs, fire once when armed, and only at named points."""
from contextlib import ExitStack
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, call, patch

from runtime import fault_points
from services.executor.core import ConditionalWriteConflict
from services.executor.kubernetes_adapter import KubectlDeploymentAPI, WriteOutcomeUnknown
from services.executor.reconciliation import fence_marker
from tests.support import executor_model as model
from tests.support.sim_apiserver import SimAPIServer, SimKubectl


class FaultPointTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_dir = Path(directory.name)
        self.lab = {fault_points.ENV_DIRECTORY: str(self.run_dir),
                    fault_points.ENV_CLUSTER: "file-sentinel-distributed",
                    fault_points.ENV_PROCESS: "executor-a"}

    def environment(self, **changes):
        values = dict(self.lab, **changes)
        cleared = {key: "" for key in (fault_points.ENV_DIRECTORY, fault_points.ENV_CLUSTER,
                                       fault_points.ENV_PROCESS, fault_points.ENV_CLOCK_OFFSET)}
        return patch.dict(os.environ, {**cleared, **values})

    def arm(self, stem, action="exit"):
        (self.run_dir / (stem + ".action")).write_text(action, encoding="utf-8")

    def test_inert_without_the_variables_an_allowlisted_cluster_or_the_directory(self):
        self.arm("executor.before_patch")
        cases = ({fault_points.ENV_DIRECTORY: ""}, {fault_points.ENV_CLUSTER: ""},
                 {fault_points.ENV_CLUSTER: "sentinel-lab"}, {fault_points.ENV_CLUSTER: "kind-sentinel-lab"},
                 {fault_points.ENV_DIRECTORY: str(self.run_dir / "missing")})
        for changes in cases:
            with self.subTest(changes=changes), self.environment(**changes), patch("os._exit") as exit_:
                self.assertFalse(fault_points.enabled())
                self.assertIsNone(fault_points.checkpoint("executor.before_patch"))
                exit_.assert_not_called()
        self.assertTrue((self.run_dir / "executor.before_patch.action").exists())

    def test_bind_ties_the_gate_to_the_configured_cluster_and_context(self):
        lab = {"cluster_id": "file-sentinel-distributed", "context": "file-cluster"}
        off = {fault_points.ENV_DIRECTORY: "", fault_points.ENV_CLUSTER: ""}
        with self.environment(**off):
            self.assertIs(fault_points.bind({"cluster_id": "sentinel-lab"}), False)  # No lab variable: an ordinary run.
        with self.environment():
            self.assertIs(fault_points.bind(lab), True)
            self.assertIs(fault_points.bind({"cluster_id": "file-sentinel-distributed"}), True)
        refused = [({}, {"cluster_id": "sentinel-lab", "context": "kind-sentinel-lab"}),
                   ({}, dict(lab, context="kind-sentinel-lab")), ({}, dict(lab, context="prod")),
                   ({}, {"context": "file-cluster"}), ({}, dict(lab, cluster_id=["file-sentinel-distributed"])),
                   ({fault_points.ENV_CLUSTER: "sentinel-lab"}, {"cluster_id": "sentinel-lab"}),
                   ({fault_points.ENV_CLUSTER: "sentinel-boutique-connected"}, lab),
                   ({fault_points.ENV_DIRECTORY: str(self.run_dir / "missing")}, lab),
                   (dict(off, **{fault_points.ENV_CLOCK_OFFSET: "5"}), lab)]
        for changes, config in refused:
            with self.subTest(changes=changes, config=config), self.environment(**changes):
                with self.assertRaises(ValueError):
                    fault_points.bind(config)

    def test_the_sentinel_dist_lab_binds_only_its_own_in_pod_context(self):
        dist = {fault_points.ENV_CLUSTER: "sentinel-dist"}
        with self.environment(**dist):
            self.assertIs(fault_points.bind({"cluster_id": "sentinel-dist", "context": "kind-sentinel-dist"}), True)
            self.assertIs(fault_points.bind({"cluster_id": "sentinel-dist"}), True)  # In-cluster readers name none.
        refused = [{"cluster_id": "sentinel-dist", "context": "kind-sentinel-boutique-connected"},
                   {"cluster_id": "sentinel-dist", "context": "kind-sentinel-lab"},
                   {"cluster_id": "sentinel-boutique-connected", "context": "kind-sentinel-dist"},
                   {"cluster_id": "file-sentinel-distributed", "context": "file-cluster"}]
        for config in refused:
            with self.subTest(config=config), self.environment(**dist), self.assertRaises(ValueError):
                fault_points.bind(config)

    def test_an_unknown_point_is_refused_even_when_inert(self):
        with self.environment(**{fault_points.ENV_DIRECTORY: ""}), self.assertRaises(ValueError):
            fault_points.checkpoint("executor.anywhere")

    def test_exit_fires_once_records_the_process_and_exits_with_the_crash_code(self):
        self.arm("executor.after_start_write")
        with self.environment(), patch("os._exit", side_effect=SystemExit) as exit_:
            with self.assertRaises(SystemExit):
                fault_points.checkpoint("executor.after_start_write")
            exit_.assert_called_once_with(fault_points.EXIT_CODE)
            self.assertIsNone(fault_points.checkpoint("executor.after_start_write"))  # Taken: one shot.
        reached = self.run_dir / "executor.after_start_write.reached"
        self.assertEqual(reached.read_text(encoding="utf-8"), "executor-a")

    def test_block_waits_for_the_release_file(self):
        self.arm("executor.before_patch", "block")
        finished = threading.Event()

        def run():
            with self.environment():
                fault_points.checkpoint("executor.before_patch")
            finished.set()
        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 5
        while not (self.run_dir / "executor.before_patch.reached").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(finished.wait(0.3))
        (self.run_dir / "executor.before_patch.release").write_text("go", encoding="utf-8")
        self.assertTrue(finished.wait(5))
        worker.join(5)

    def test_a_qualified_point_fires_only_for_its_qualifier(self):
        self.arm("coordinator.after_commit_before_reply@execution_result")
        with self.environment(), patch("os._exit", side_effect=SystemExit):
            self.assertIsNone(fault_points.checkpoint("coordinator.after_commit_before_reply", qualifier="proposal"))
            with self.assertRaises(SystemExit):
                fault_points.checkpoint("coordinator.after_commit_before_reply", qualifier="execution_result")

    def test_an_invalid_action_is_refused(self):
        self.arm("planner.mid_inference", "explode")
        with self.environment(), self.assertRaises(ValueError):
            fault_points.checkpoint("planner.mid_inference")

    def test_the_clock_offset_is_gated_and_bounded(self):
        base = Mock(return_value=datetime(2026, 11, 12, 12, 0, tzinfo=UTC))
        with self.environment(**{fault_points.ENV_CLOCK_OFFSET: "-300"}):
            self.assertEqual(fault_points.offset_clock(base)(), datetime(2026, 11, 12, 11, 55, tzinfo=UTC))
        with self.environment(**{fault_points.ENV_CLOCK_OFFSET: "-300", fault_points.ENV_CLUSTER: "sentinel-lab"}):
            self.assertIs(fault_points.offset_clock(base), base)
        for value in ("nan", "90000"):
            with self.subTest(value=value), self.environment(**{fault_points.ENV_CLOCK_OFFSET: value}):
                with self.assertRaises(ValueError):
                    fault_points.clock_offset()

    def test_executor_proxies_stop_after_the_intent_and_around_the_patch(self):
        order = []
        ledger = Mock(start_write=Mock(side_effect=lambda *a: order.append("start_write")), other="kept")
        cluster = Mock(conditional_restore=Mock(side_effect=lambda **k: order.append("patch") or "applied"))
        with patch.object(fault_points, "checkpoint", side_effect=order.append):
            fault_points.CheckpointLedger(ledger).start_write("A", "1")
            self.assertEqual(fault_points.CheckpointCluster(cluster).conditional_restore(target={}), "applied")
            cluster.before_submit()  # The hook the proxy gave the adapter: its point inside conditional_restore.
        self.assertEqual(order, ["start_write", "executor.after_start_write", "executor.before_patch", "patch",
                                 "executor.after_patch_before_save", "executor.before_submit"])
        self.assertEqual(fault_points.CheckpointLedger(ledger).other, "kept")


class BeforeSubmitTests(unittest.TestCase):
    """executor.before_submit holds the real adapter after its pre-read, so a released PATCH carries a stale version.

    The adapter is the production KubectlDeploymentAPI, the API server the
    test simulator (tests/support/sim_apiserver.py), which refuses a JSON
    Patch whose test operation fails and commits nothing, as the API server
    does with 409/422.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_dir = Path(directory.name)
        fixtures = model.load_fixtures()
        self.proposal = fixtures["proposal"]
        self.target = self.proposal["target"]
        self.template = fixtures["baseline"]["pod_template"]
        self.evidence = {"action_id": self.proposal["action_id"], "proposal_sha256": "a" * 64,
                         "target_uid": self.target["uid"],
                         "template_sha256": self.proposal["parameters"]["template_sha256"], "attempt_id": "b" * 32}
        self.server = SimAPIServer.from_snapshot(fixtures["live_state"])
        self.version = self.proposal["based_on_resource_version"]
        self.assertEqual(self.server.resource_version, self.version)

    def environment(self, **changes):
        values = {fault_points.ENV_DIRECTORY: str(self.run_dir), fault_points.ENV_CLUSTER: "sentinel-dist",
                  fault_points.ENV_PROCESS: "executor-a", fault_points.ENV_CLOCK_OFFSET: "", **changes}
        return patch.dict(os.environ, values)

    def adapter(self, host):
        runner = SimKubectl(self.server, host=host, action_id=self.proposal["action_id"])
        return KubectlDeploymentAPI(context="sim", cluster_id=self.target["cluster_id"], kubeconfig="unused",
                                    allowed_targets=[self.target], runner=runner), runner

    def restore(self, cluster):
        return cluster.conditional_restore(target=self.target, version=self.version, template=self.template,
                                           evidence=self.evidence)

    def test_the_point_is_registered_and_the_adapter_hook_is_unset_outside_the_lab_proxy(self):
        self.assertIn("executor.before_submit", fault_points.NAMES)
        api, _ = self.adapter("A")
        self.assertIsNone(api.before_submit)  # Production builds never set it (runtime/worker_service.py, lab only).
        fault_points.CheckpointCluster(api)
        self.assertIsNotNone(api.before_submit)

    def test_inert_when_the_gate_is_off_even_when_armed(self):
        (self.run_dir / "executor.before_submit.action").write_text("block", encoding="utf-8")
        for changes in ({fault_points.ENV_DIRECTORY: ""}, {fault_points.ENV_CLUSTER: "sentinel-lab"}):
            with self.subTest(changes=changes):
                self.server = SimAPIServer.from_snapshot(model.load_fixtures()["live_state"])
                api, runner = self.adapter("A")
                with self.environment(**changes):
                    self.restore(fault_points.CheckpointCluster(api))
                self.assertEqual([request.state for request in self.server.requests], ["committed"])
        self.assertTrue((self.run_dir / "executor.before_submit.action").exists())  # Never taken.

    def held_restore(self, move):
        """Hold a restore at executor.before_submit, move the target, release.

        Returns the adapter's runner, what the release raised, the kubectl
        verbs sent while held and the Deployment as the move left it.
        """
        for old in ("reached", "release"):  # As the harness arms a point: no earlier hold passes this one through.
            (self.run_dir / f"executor.before_submit.{old}").unlink(missing_ok=True)
        (self.run_dir / "executor.before_submit.action").write_text("block", encoding="utf-8")
        api, runner = self.adapter("A")
        cluster = fault_points.CheckpointCluster(api)
        outcome = {}

        def run():
            with self.environment():
                try:
                    outcome["result"] = self.restore(cluster)
                except Exception as error:  # noqa: BLE001 - the test inspects whatever the release raised
                    outcome["error"] = error
        worker = threading.Thread(target=run)
        worker.start()
        reached = self.run_dir / "executor.before_submit.reached"
        deadline = time.monotonic() + 10
        while not reached.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(reached.exists(), "the restore never reached executor.before_submit")
        held = [command[command.index("--request-timeout=10s") + 1] for command in runner.calls]
        move()
        sealed = json.loads(json.dumps(self.server.deployment))
        (self.run_dir / "executor.before_submit.release").write_text("go", encoding="utf-8")
        worker.join(10)
        self.assertFalse(worker.is_alive())
        return runner, outcome.get("error"), held, sealed

    def test_a_held_restore_has_read_and_its_release_sends_the_stale_patch_which_the_server_refuses(self):
        def status_write():  # A crash-looping target's own status write: the seal would send nothing.
            self.server.external_write(lambda d: d.setdefault("status", {}).update(restarts=7), actor="controller")

        def fence():  # The other Executor's seal finds V0 and sends its metadata-only fence.
            other, _ = self.adapter("B")
            other.fence(target=self.target, version=self.version, marker=fence_marker(
                self.proposal["action_id"], "c" * 32, 2))
        for name, move in (("advanced_without_evidence", status_write), ("fenced", fence)):
            with self.subTest(seal_path=name):
                self.server = SimAPIServer.from_snapshot(model.load_fixtures()["live_state"])
                runner, error, held, sealed = self.held_restore(move)
                self.assertEqual(held, ["get"])  # Held after its pre-read, before any PATCH.
                self.assertNotEqual(self.server.resource_version, self.version)
                patches = [request for request in self.server.requests if request.host == "A"]
                self.assertEqual(len(patches), 1)  # Released, it still sent its PATCH: the race reached the server.
                self.assertIn({"op": "test", "path": "/metadata/resourceVersion", "value": self.version},
                              patches[0].operations)
                self.assertEqual(patches[0].state, "rejected")  # The API server's RV test refused it.
                self.assertIsInstance(error, WriteOutcomeUnknown)  # kubectl's error: never retried, reconciled.
                self.assertEqual(self.server.committed(host="A", kind=None), [])
                self.assertEqual(self.server.deployment, sealed)  # Nothing changed after the seal, generation too.
                self.assertEqual(sealed["spec"]["template"],
                                 model.load_fixtures()["live_state"]["deployment"]["spec"]["template"])
                self.assertEqual(len(self.server.committed(host="B", kind="fence")), int(name == "fenced"))
                self.assertEqual([command[command.index("--request-timeout=10s") + 1] for command in runner.calls],
                                 ["get", "patch"])  # One read, one PATCH, no retry.

    def test_a_restore_that_finds_the_target_moved_at_its_pre_read_never_reaches_the_point(self):
        (self.run_dir / "executor.before_submit.action").write_text("block", encoding="utf-8")
        self.server.external_write(lambda d: d.setdefault("status", {}).update(restarts=7), actor="controller")
        api, _ = self.adapter("A")
        with self.environment(), self.assertRaises(ConditionalWriteConflict):
            self.restore(fault_points.CheckpointCluster(api))
        self.assertFalse((self.run_dir / "executor.before_submit.reached").exists())
        self.assertEqual(self.server.requests, [])  # Zero-write: the 7ce9863 S8 holder's path.


class CoordinatorFaultPointTests(unittest.TestCase):
    """The Coordinator stops between the commit of a result and its reply, nowhere else."""

    def test_only_an_accepted_result_reaches_the_coordinator_point(self):
        from experiments.validation.demo_offline_flow import OfflineFlow, fixture
        from runtime.coordinator_server import servers
        from runtime.remote_adapters import PlannerAdapter
        from runtime.remote_worker import RemoteIngress, RemoteWorker, WorkerJournal
        from runtime.worker_http import WorkerClient, _result_kind
        from services.observer.core import detect
        from services.planner.core import mock_provider
        from services.planner.store import PlannerStore
        with ExitStack() as stack:
            directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            flow = OfflineFlow(directory)
            stack.callback(flow.close)
            incident = detect(fixture("observations.json")["failed-rollout"], flow.policy)
            services = {"observer": ("observer", "o" * 40), "planner": ("planner", "p" * 40)}
            approvals = {"operator": ("operator", "a" * 40)}
            permissions = {"operator": [{"target": incident["target"], "operations": ["read"]}]}
            workers, _, _, _ = stack.enter_context(servers(directory / "coordinator.db", services, approvals,
                                                           permissions, port=0, approval_port=0))
            url = f"http://127.0.0.1:{workers.server_port}"
            planning = PlannerStore(directory / "planner.db")
            stack.callback(planning.close)
            journal = WorkerJournal(directory / "transport.db")
            stack.callback(journal.close)
            checkpoint = stack.enter_context(patch("runtime.worker_http.fault_points.checkpoint"))
            RemoteIngress(WorkerClient(url, "observer", "o" * 40, timeout=5))(incident)
            planner = RemoteWorker(WorkerClient(url, "planner", "p" * 40, timeout=5), journal, PlannerAdapter(
                planning, baseline_reader=lambda _: [flow.baseline], provider=mock_provider), renewal_interval=0.1)
            self.assertEqual(planner.once()["status"], "accepted")
        self.assertEqual(checkpoint.call_args_list,
                         [call("coordinator.after_commit_before_reply", qualifier="proposal")])
        self.assertIsNone(_result_kind({"result": "not an object"}))
        self.assertIsNone(_result_kind(None))


if __name__ == "__main__":
    unittest.main()
