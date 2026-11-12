"""Provider mode (scripts/boutique_provider.py) approves through the authenticated session.

The real run_provider_case, durable case, recovery Experiment, Coordinator,
Guard, approval_http child process (LocalApprovalAuthority) and Executor core
run. The provider is a real subprocess script; Kubernetes is a fake, and the
Verifier outcome is a fixed passing record so the run can reach RECOVERED offline.
"""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from scripts.boutique_provider import run_provider_case
from services.coordinator.core import handle
# Imported as a module, so unittest does not collect the agent-mode tests here again.
from tests import test_live_agent

# Captured before setUp patches subprocess.run to refuse cluster commands.
REAL_RUN = subprocess.run
PROVIDER = """import json, sys
context = json.load(sys.stdin)
baseline = context["baselines"][0]
print(json.dumps({"action": "ROLLBACK",
                  "parameters": {key: baseline[key] for key in ("baseline_id", "template_sha256")},
                  "rationale": "Restore the measured healthy template",
                  "evidence_refs": [signal["evidence_id"] for signal in context["incident"]["signals"]]}))
"""


class ProviderApprovalSessionTests(unittest.TestCase):
    # The same offline lab as agent mode, built on the recovery Experiment without the agent mixin.
    setUp = test_live_agent.AgentApprovalSessionTests.setUp
    dependencies = test_live_agent.AgentApprovalSessionTests.dependencies
    build_adapters = test_live_agent.AgentApprovalSessionTests.build_adapters
    verified = test_live_agent.AgentApprovalSessionTests.verified

    def experiment(self, approval):
        from experiments.recovery import live as recovery
        script = self.root / "provider.py"
        script.write_text(PROVIDER, encoding="utf-8")
        self.provider_calls = []

        def run(command, *args, **kwargs):
            # Only the configured provider script may run; any cluster command still fails.
            if command[1:] != [str(script)]:
                raise AssertionError("cluster command attempted")
            self.provider_calls.append(command)
            return REAL_RUN(command, *args, **kwargs)
        patcher = patch("subprocess.run", side_effect=run)
        patcher.start()
        self.addCleanup(patcher.stop)
        kind = type("OfflineProviderExperiment", (recovery.Experiment,), {
            "deployment": lambda lab: self.cluster.read(lab.target)["deployment"],
            "dependency_result": lambda lab, proposal: self.dependencies(proposal),
            "complete_recovery": lambda lab, folder, proposal, execution: self.verified(lab, proposal, execution),
            "provider_script": script, "provider_policy": None})
        lab = kind(self.root / "run")
        self.addCleanup(lab.store.close)
        self.addCleanup(lab.close_approval_authority)
        lab.target, lab.baseline, lab.policy = self.incident["target"], self.baseline, self.policy
        lab.configure_approval(approval)
        handle(self.incident, lab.store, lab.bus)
        return lab

    def test_provider_mode_approves_through_the_session_and_records_the_strategy(self):
        from runtime.approval_http import ApprovalClient
        from runtime.approval_session import AUTOMATED_LAB, LAB_EXECUTOR
        lab = self.experiment("auto-lab")
        with patch.object(lab, "request_approval", wraps=lab.request_approval) as asked:
            run_provider_case(lab, self.incident)
        self.assertEqual(lab.summary["provider_state"], "RECOVERED")
        self.assertEqual(len(self.provider_calls), 1)
        self.assertEqual(self.provider_calls[0][0], sys.executable)
        action_id = lab.store.incident(self.incident["incident_id"])["active_action_id"]
        [(proposal, decision)] = [call.args for call in asked.call_args_list]
        self.assertEqual((proposal["action_id"], decision["reason_codes"]),
                         (action_id, ["OPERATOR_APPROVAL_REQUIRED"]))
        # The approval was created through approval_http, under the automated-lab identity.
        self.assertTrue(lab.store.has_managed_approval(action_id))
        saved = json.loads((lab.directory / "provider-recovery/approval.json").read_text(encoding="utf-8"))
        self.assertEqual((saved["action_id"], saved["operator_id"]), (action_id, AUTOMATED_LAB))
        summary = lab.summary["operator_approval"]
        self.assertEqual((summary["mode"], summary["via"], summary["human"], summary["identity"]),
                         ("auto-lab", "approval_http", False, AUTOMATED_LAB))
        self.assertEqual([item["action_id"] for item in summary["approvals"]], [action_id])
        # The Executor's final lookup reads that approval through ApprovalClient, and it wrote once.
        self.assertEqual([(type(reader), reader.identity) for reader in self.built], [(ApprovalClient, LAB_EXECUTOR)])
        self.assertEqual(len(self.cluster.writes), 1)
        status = json.loads((lab.directory / "provider-recovery/planning-status.json").read_text(encoding="utf-8"))
        self.assertEqual((status["attempts"], status["delivered"], status["error_code"]), (1, 1, None))

    def test_refused_approval_in_provider_mode_gives_zero_writes(self):
        lab = self.experiment("reject")
        with self.assertRaisesRegex(RuntimeError, "Provider recovery stopped at AWAITING_APPROVAL"):
            run_provider_case(lab, self.incident)
        action_id = lab.store.incident(self.incident["incident_id"])["active_action_id"]
        self.assertFalse(lab.store.has_managed_approval(action_id))
        self.assertFalse(lab.store.pending_routes("executor.request"))
        self.assertEqual((self.built, self.cluster.writes), ([], []))
        summary = lab.summary["operator_approval"]
        self.assertEqual((summary["mode"], summary["identity"], summary["approvals"]), ("reject", None, []))
        self.assertEqual([item["action_id"] for item in summary["refusals"]], [action_id])
        self.assertFalse(Path(lab.directory / "provider-recovery/approval.json").exists())


if __name__ == "__main__":
    unittest.main()
