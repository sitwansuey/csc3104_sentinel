"""A new rollout fault during verification must survive and recover separately."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from experiments.validation.demo_offline_flow import OfflineFlow, fixture
from runtime.planner_pipeline import process_planner_requests
from services.coordinator.core import approve, handle
from services.observer.core import detect
from services.planner.core import mock_provider, retrieve_runbooks
from tests.test_verifier_pipeline import grpc_policy, grpc_samples


class OverlappingRecoveryTests(unittest.TestCase):
    def test_second_fault_invalidates_first_outcome_then_recovers_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            flow = OfflineFlow(Path(directory))
            try:
                flow.policy["health"] = grpc_policy()["health"]
                flow.prepare()
                first_id = flow.incident["incident_id"]
                flow.authorize()
                flow.execute()
                first = flow.store.action(flow.proposal["action_id"])["execution_result"]
                self.assertEqual(flow.state(), "VERIFYING")

                # A second external configuration fault arrives while the first
                # action is still being verified, before its healthy window ends.
                flow.now += timedelta(seconds=5)
                flow.cluster.snapshot["deployment"]["spec"]["template"] = deepcopy(
                    fixture("observations.json")["failed-rollout"][-1]["deployment"]["spec"]["template"])
                meta = flow.cluster.snapshot["deployment"]["metadata"]
                meta.update(generation=3, resourceVersion="second-fault-version")
                flow.cluster.snapshot["deployment"]["status"]["observedGeneration"] = 3
                observations = [flow.cluster.read(first["target"])]
                flow.now += timedelta(seconds=30)
                observations.append(flow.cluster.read(first["target"]))
                second = detect(observations, flow.policy)
                self.assertIsNotNone(second)
                handle(second, flow.store, flow.bus)
                provider = Mock(side_effect=mock_provider)
                def plan():
                    return process_planner_requests(flow.store, flow.bus,
                        baseline_reader=lambda _: [flow.baseline], retriever=retrieve_runbooks, provider=provider)
                self.assertEqual(plan(), [])
                provider.assert_not_called()

                readings = grpc_samples(first, flow.proposal["parameters"]["template_sha256"])
                for reading in readings[1:]:
                    reading.update(grpc_status=14, ready_replicas=0, template_sha256="b" * 64)
                self.assertEqual(flow.verify(readings)[0]["status"], "NOT_RECOVERED")
                self.assertEqual(flow.store.incident(first_id)["state"], "FAILED")

                flow.restart()
                flow.incident = second
                flow.proposal = plan()[0]
                flow.guard()
                self.assertEqual(flow.state(), "AWAITING_APPROVAL")
                flow.now += timedelta(seconds=1)
                approve(flow.proposal["action_id"], "offline-operator", flow.store)
                flow.now += timedelta(seconds=1)
                flow.guard()
                self.assertEqual(flow.state(), "READY")
                flow.execute()
                self.assertEqual(flow.state(), "VERIFYING")
                execution = flow.store.action(flow.proposal["action_id"])["execution_result"]
                flow.now += timedelta(seconds=12)
                self.assertEqual(flow.verify(grpc_samples(execution,
                    flow.proposal["parameters"]["template_sha256"]))[0]["status"], "RECOVERED")
                self.assertEqual(flow.state(), "RECOVERED")
                self.assertEqual(flow.store.incident(first_id)["state"], "FAILED")
                self.assertEqual(flow.execute(), [])
                self.assertEqual(len(flow.cluster.writes), 2)
                self.assertEqual(provider.call_count, 1)
            finally:
                flow.close()
