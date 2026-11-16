"""Offline connected acceptance variants; no cluster or model calls."""
from copy import deepcopy
from contextlib import ExitStack
from datetime import datetime, timedelta, UTC
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from experiments.boutique.harness.connected import ConnectedBoutiqueExperiment
from experiments.boutique.harness.live import BoutiqueExperiment
from services.coordinator.core import handle
from tests.test_live_agent import OfflineAgentLab
from tests.test_verifier_pipeline import grpc_policy, grpc_reading, load


@unittest.skipUnless(shutil.which("powershell"), "needs Windows PowerShell; no cluster commands")
class PowerShellOutcomeOptionsTests(unittest.TestCase):
    def test_launcher_parses_and_forwards_the_variants_and_operator_before_setup(self):
        launcher = Path(__file__).resolve().parents[1] / "scripts/test-boutique-live.ps1"
        # Execute only the real parameter validation and argument forwarding blocks.
        script = r"""
$ErrorActionPreference = 'Stop'
$source = Get-Content -LiteralPath '__LAUNCHER__' -Raw
$start = $source.IndexOf('    $testOptions = @()')
$end = $source.IndexOf('    & $pythonTool.Path @pythonPrefix')
$safe = [scriptblock]::Create($source.Substring(0, $source.IndexOf('$projectRoot =')) +
    $source.Substring($start, $end - $start) + 'ConvertTo-Json -InputObject @($testOptions) -Compress')
foreach ($variant in @('unhealthy','unavailable-evidence')) {
    & $safe -Connected -Agent -Target cartservice -Scenario C1 -OutcomeVariant $variant -Operator operator-alice
}
try { & $safe -Agent -OutcomeVariant unhealthy; throw 'invalid mode accepted' }
catch { if ($_.Exception.Message -notlike '*requires -Agent -Connected*') { throw } }
try { & $safe -Connected -Agent -OutcomeVariant unhealthy -Expect recovered; throw 'explicit expectation accepted' }
catch { if ($_.Exception.Message -notlike '*cannot combine with -Expect*') { throw } }
exit 0
""".replace("__LAUNCHER__", str(launcher).replace("'", "''"))
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        options = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(options), 2)
        for args, variant in zip(options, ("unhealthy", "unavailable-evidence"), strict=True):
            self.assertIn("--agent", args)
            self.assertIn("--connected", args)
            for flag, value in (("--outcome-variant", variant), ("--target", "cartservice"), ("--scenario", "C1"),
                                ("--approval", "interactive"), ("--operator", "operator-alice")):
                self.assertEqual(args[args.index(flag) + 1], value)


class ConnectedOutcomeTests(unittest.TestCase):
    def run_variant(self, variant, *, target="shippingservice", healthy=False, collection_fails=False,
                    cleanup_fails=False):
        with tempfile.TemporaryDirectory() as root, \
                patch("shared.provenance.collect", return_value={"source_revision": "test"}), \
                patch("subprocess.run", side_effect=AssertionError("cluster command attempted")), ExitStack() as cleanup:
            lab = ConnectedBoutiqueExperiment(Path(root) / "run")
            cleanup.callback(lab.store.close)
            cleanup.callback(lab.close_approval_authority)
            lab.select_target(target)
            lab.outcome_variant = variant
            lab.configure_approval("auto-lab")
            lab.event = Mock()
            lab.target = dict(load("messages/proposal.json")["target"], cluster_id=lab.cluster_id,
                              namespace=lab.namespace, name=target)
            lab.baseline, lab.policy = load("baseline.json"), load("policy.json")
            lab.baseline.update(target=lab.target, health_evidence={"evidence_id": "HEALTH-offline"})
            lab.policy.update(targets=[lab.target], health=dict(grpc_policy()["health"],
                              required_dependencies=["adservice"], convergence_grace_seconds=15,
                              verification_timeout_seconds=30))
            deployment = load("observations.json")["failed-rollout"][-1]["deployment"]
            deployment["metadata"].update(lab.target)
            lab.deployment = Mock(return_value=deployment)
            lab.dependency_result = lambda proposal: dict(load("messages/dependency-result.json"),
                incident_id=proposal["incident_id"], action_id=proposal["action_id"], target=proposal["target"],
                affected_services=[target], checked_at=datetime.now(UTC).isoformat())
            dependency = {"metadata": {"name": "adservice", "uid": "ad-uid", "generation": 1,
                                       "resourceVersion": "10"}, "spec": {"replicas": 2},
                          "status": {"observedGeneration": 1, "readyReplicas": 2, "availableReplicas": 2}}
            calls = []

            def kubectl(*args, **kwargs):
                calls.append(args)
                if args[:3] == ("get", "deployment", "adservice"):
                    return SimpleNamespace(stdout=json.dumps(dependency))
                if args[0] == "scale":
                    count = int(args[2].split("=")[1])
                    if count and cleanup_fails:
                        raise RuntimeError("cleanup refused")
                    dependency["spec"]["replicas"] = count
                    dependency["metadata"]["resourceVersion"] = str(int(dependency["metadata"]["resourceVersion"]) + 1)
                    dependency["status"].update(readyReplicas=count, availableReplicas=count)
                return SimpleNamespace(stdout="")

            lab.kubectl = kubectl
            incident = load("messages/incident.json")
            incident.update(target=lab.target)
            handle(incident, lab.store, lab.bus)
            folder, proposal = lab.case("approved-rollback", incident, coordinator=True)
            execution = load("messages/execution-result.json")
            start = datetime.now(UTC)
            execution.update(incident_id=incident["incident_id"], action_id=proposal["action_id"], target=lab.target,
                             recorded_at=start.isoformat().replace("+00:00", "Z"))
            handle(execution, lab.store, lab.bus)
            wall = [start]

            def readings(current):
                if collection_fails:
                    raise RuntimeError("collector failed")
                for offset in range(1, 32, 5):
                    wall[0] = start + timedelta(seconds=offset)
                    yield grpc_reading(current, lab.baseline["template_sha256"], offset,
                                       dependencies={"adservice": healthy})

            # Unavailable variant uses the actual disabled collector and a virtual clock through its deadline.
            def sleep(seconds):
                wall[0] += timedelta(seconds=seconds)

            if variant != "unavailable-evidence" or healthy or collection_fails:
                lab.verification_samples = readings
            error = None
            with patch("experiments.boutique.harness.connected.time.sleep", side_effect=sleep), \
                    patch("experiments.boutique.harness.connected.time.monotonic",
                          side_effect=lambda: (wall[0] - start).total_seconds()), \
                    patch("experiments.boutique.harness.connected.utc", side_effect=lambda: wall[0].isoformat()):
                try:
                    lab.complete_recovery(folder, proposal, execution, clock=lambda: wall[0])
                except RuntimeError as exc:
                    error = str(exc)
            outcome_path = folder / "outcome.json"
            result = {"error": error, "summary": deepcopy(lab.summary), "calls": calls,
                      "outcome": json.loads(outcome_path.read_text()) if outcome_path.exists() else None,
                      "samples": json.loads((folder / "post-health.json").read_text())
                      if (folder / "post-health.json").exists() else [],
                      "state": lab.store.incident(incident["incident_id"])["state"],
                      "elapsed": (wall[0] - start).total_seconds()}
            return result

    def test_unhealthy_dependency_requires_not_recovered_and_failed_for_both_targets(self):
        for target in ("shippingservice", "cartservice"):
            with self.subTest(target=target):
                run = self.run_variant("unhealthy", target=target)
                self.assertIsNone(run["error"])
                self.assertEqual((run["outcome"]["status"], run["state"]), ("NOT_RECOVERED", "FAILED"))
                self.assertTrue(run["summary"]["outcome_expectation"]["matched_expected"])
                self.assertTrue(run["summary"]["outcome_injection"]["restored"])
                scales = [call for call in run["calls"] if call[0] == "scale"]
                self.assertEqual([call[2] for call in scales], ["--replicas=0", "--replicas=2"])
                self.assertTrue(all(call[1] == "deployment/adservice" for call in scales))

    def test_unavailable_health_stays_unknown_until_the_deadline_for_both_targets(self):
        for target in ("shippingservice", "cartservice"):
            with self.subTest(target=target):
                run = self.run_variant("unavailable-evidence", target=target)
                self.assertIsNone(run["error"])
                self.assertEqual((run["outcome"]["status"], run["state"]), ("UNKNOWN", "NEEDS_REVIEW"))
                self.assertGreaterEqual(run["elapsed"], 30)
                self.assertTrue(run["samples"])
                self.assertTrue(all(row["collection_error"] == "LabHealthCollectionDisabled" for row in run["samples"]))
                self.assertFalse(any(call[0] in ("scale", "get") for call in run["calls"]))

    def test_an_unexpected_recovery_is_recorded_and_rejected_in_both_variants(self):
        for variant in ("unhealthy", "unavailable-evidence"):
            with self.subTest(variant=variant):
                run = self.run_variant(variant, healthy=True)
                self.assertIsNotNone(run["error"])
                self.assertEqual((run["outcome"]["status"], run["state"]), ("RECOVERED", "RECOVERED"))
                self.assertFalse(run["summary"]["outcome_expectation"]["matched_expected"])

    def test_dependency_restores_after_collection_failure_and_cleanup_failure_fails(self):
        run = self.run_variant("unhealthy", collection_fails=True)
        self.assertEqual(run["error"], "collector failed")
        self.assertTrue(run["summary"]["outcome_injection"]["restored"])
        run = self.run_variant("unhealthy", cleanup_fails=True)
        self.assertEqual(run["error"], "cleanup refused")
        self.assertFalse(run["summary"]["outcome_injection"]["restored"])


class AgentOutcomeTests(OfflineAgentLab, unittest.TestCase):
    def test_durable_agent_accepts_only_the_expected_post_repair_outcome(self):
        for variant, state in (("unhealthy", "FAILED"), ("unavailable-evidence", "NEEDS_REVIEW")):
            with self.subTest(variant=variant):
                lab = self.experiment(run=variant)
                lab.outcome_variant = variant
                lab.profile = SimpleNamespace(label="test target")
                lab.lab_control = None
                lab.kubectl = Mock()
                lab.policy["health"] = dict(grpc_policy()["health"], required_dependencies=["adservice"],
                                            convergence_grace_seconds=1, verification_timeout_seconds=11)
                wall = [self.real_now() + timedelta(seconds=1)]

                def samples(execution, wall=wall, variant=variant):
                    start = datetime.fromisoformat(execution["recorded_at"].replace("Z", "+00:00"))
                    for offset in (1, 6, 11):
                        wall[0] = start + timedelta(seconds=offset)
                        if variant == "unhealthy":
                            yield grpc_reading(execution, self.baseline["template_sha256"], offset,
                                               dependencies={"adservice": False})
                        else:
                            yield {"at": wall[0].isoformat(), "service": execution["target"]["name"]}

                lab.verification_samples = samples
                def complete(folder, proposal, execution, lab=lab, wall=wall):
                    wall[0] = datetime.fromisoformat(execution["recorded_at"].replace("Z", "+00:00"))
                    return BoutiqueExperiment.complete_recovery(lab, folder, proposal, execution, clock=lambda: wall[0])

                lab.complete_recovery = complete
                self.recover(lab, self.model())
                self.assertEqual(self.state(lab), state)
                self.assertEqual(lab.summary["expected_outcome"], variant)
                self.assertTrue(lab.summary["outcome_expectation"]["matched_expected"])
                lab.emergency_restore.assert_not_called()
                # Reset the fake Deployment for the next independent agent run.
                self.cluster.snapshot = self.sample("observations.json")["failed-rollout"][-1]

    def test_planning_escalation_does_not_pass_as_unavailable_post_repair_evidence(self):
        lab = self.experiment()
        lab.outcome_variant = "unavailable-evidence"
        with self.assertRaisesRegex(RuntimeError, "independently verified post-repair outcome"):
            self.recover(lab, self.model(abstain=True))
        self.assertEqual(self.state(lab), "NEEDS_REVIEW")
        self.assertEqual(self.cluster.writes, [])
        self.assertNotIn("outcome_expectation", lab.summary)
