"""Fault points are inert outside lab runs, fire once when armed, and only at named points."""
from contextlib import ExitStack
from datetime import UTC, datetime
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, call, patch

from runtime import fault_points


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
        self.assertEqual(order, ["start_write", "executor.after_start_write", "executor.before_patch", "patch",
                                 "executor.after_patch_before_save"])
        self.assertEqual(fault_points.CheckpointLedger(ledger).other, "kept")


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
