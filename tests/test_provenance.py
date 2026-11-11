"""Provenance stamped into run summaries: revision and dirty state, never identity or paths."""

import getpass
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

from shared import provenance

REVISION = "0123456789abcdef0123456789abcdef01234567"


def fake_runner(outputs):
    """Answer by command prefix; anything unlisted fails like a missing tool."""
    calls = []

    def run(command, cwd=None):
        calls.append(command)
        for prefix, (code, stdout) in outputs.items():
            if tuple(command[:len(prefix)]) == prefix:
                return subprocess.CompletedProcess(command, code, stdout, "")
        return subprocess.CompletedProcess(command, 127, "", "")
    run.calls = calls
    return run


class ProvenanceTests(unittest.TestCase):
    def test_clean_tree_records_revision_and_not_dirty(self):
        runner = fake_runner({("git", "rev-parse"): (0, REVISION + "\n"), ("git", "status"): (0, "")})
        block = provenance.collect(Path("."), runner=runner)
        self.assertEqual(block["source_revision"], REVISION)
        self.assertIs(block["dirty"], False)
        self.assertIsNone(block["dirty_paths_sha256"])
        self.assertEqual(block["dirty_path_count"], 0)
        self.assertEqual(block["schema"], provenance.SCHEMA)
        self.assertEqual(block["tools"], {})

    def test_dirty_tree_hashes_sorted_paths_without_listing_them(self):
        status = " M services/b.py\n?? notes/a.txt\n"
        runner = fake_runner({("git", "rev-parse"): (0, REVISION), ("git", "status"): (0, status)})
        block = provenance.collect(Path("."), runner=runner)
        self.assertIs(block["dirty"], True)
        self.assertEqual(block["dirty_path_count"], 2)
        self.assertEqual(block["dirty_paths_sha256"],
                         hashlib.sha256("notes/a.txt\nservices/b.py".encode("utf-8")).hexdigest())
        self.assertNotIn("services/b.py", json.dumps(block))

    def test_without_git_revision_is_unknown_and_nothing_raises(self):
        block = provenance.collect(Path("."), runner=fake_runner({}))
        self.assertEqual(block["source_revision"], "unknown")
        self.assertIsNone(block["dirty"])
        self.assertIsNone(block["dirty_paths_sha256"])

    def test_non_revision_output_is_not_trusted(self):
        runner = fake_runner({("git", "rev-parse"): (0, "fatal: not a git repository\n"), ("git", "status"): (0, "")})
        self.assertEqual(provenance.source_revision(Path("."), runner), "unknown")

    def test_default_runner_turns_a_missing_tool_into_a_failure(self):
        result = provenance._default_runner(["sentinel-tool-that-does-not-exist-" + "x" * 8, "--version"])
        self.assertNotEqual(result.returncode, 0)

    def test_tool_versions_keep_only_the_version_number(self):
        runner = fake_runner({
            ("kubectl",): (0, "Client Version: v1.31.0\nKustomize Version: v5.4.2\n"),
            ("kind",): (0, "kind v0.27.0 go1.23.6 windows/amd64 at C:\\Users\\someone\\bin on build-host-7\n"),
            ("helm",): (1, "error"),
        })
        versions = provenance.tool_versions(("kubectl", "kind", "helm", "docker", "not-a-tool"), runner)
        self.assertEqual(versions, {"kubectl": "1.31.0", "kind": "0.27.0", "helm": "unavailable",
                                    "docker": "unavailable", "not-a-tool": "unavailable"})
        self.assertNotIn(["not-a-tool"], [call[:1] for call in runner.calls])

    def test_output_never_contains_paths_hostnames_or_user_names(self):
        runner = fake_runner({
            ("git", "rev-parse"): (0, REVISION),
            ("git", "status"): (0, " M C:/Users/alice/private.txt\n?? /home/alice/notes.md\n"),
            ("kubectl",): (0, "Client Version: v1.31.0 (/home/alice/bin/kubectl on host-xyz)\n"),
        })
        text = json.dumps(provenance.collect(Path("C:/Users/alice/repo"), tools=("kubectl",), runner=runner))
        for forbidden in ("alice", "host-xyz", "/home/", "Users", "private.txt", "repo"):
            self.assertNotIn(forbidden, text)

    def test_real_collection_has_no_local_identity_or_absolute_path(self):
        block = provenance.collect(provenance.ROOT)
        text = json.dumps(block).lower()
        for forbidden in {socket.gethostname(), getpass.getuser(), str(Path.home()), str(provenance.ROOT),
                          str(provenance.ROOT).replace("\\", "\\\\")}:
            if forbidden and len(forbidden) >= 3:
                self.assertNotIn(forbidden.lower(), text)
        self.assertRegex(block["host_class"], r"^[a-z0-9_]+-[a-z0-9_]+$")
        self.assertRegex(block["source_revision"], r"^([0-9a-f]{40}|unknown)$")


class HarnessStampTests(unittest.TestCase):
    def test_experiment_summary_carries_provenance_and_an_empty_timeline(self):
        from scripts.test_boutique_live import BoutiqueExperiment
        with tempfile.TemporaryDirectory() as directory:
            experiment = BoutiqueExperiment(Path(directory) / "run")
            try:
                self.assertEqual(experiment.summary["provenance"]["schema"], provenance.SCHEMA)
                self.assertEqual(experiment.summary["provenance"]["captured_by"], "harness")
                self.assertEqual(experiment.summary["timeline"], [])
                experiment.phase("fault_injected", "primary")
                step = experiment.summary["timeline"][0]
                self.assertEqual((step["phase"], step["scenario"]), ("fault_injected", "primary"))
                self.assertTrue(step["at"].endswith("Z"))
            finally:
                experiment.store.close()

    def test_fault_and_case_record_lifecycle_phases(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from experiments.recovery import live
        pods = {"items": [{"status": {"containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}]}
        incident = {"incident_id": "INC-1"}
        observer = SimpleNamespace(observe=lambda observation: incident)
        with tempfile.TemporaryDirectory() as directory:
            experiment = live.Experiment(Path(directory) / "run")
            experiment.policy = {"policy_version": "test-1"}
            proposal = {"incident_id": "INC-1", "action_id": "ACT-1", "action": "ROLLBACK"}
            try:
                with patch.object(experiment, "inject_fault"), patch.object(experiment, "deployment", return_value={}), \
                        patch.object(experiment, "kubectl", return_value=SimpleNamespace(stdout=json.dumps(pods))), \
                        patch.object(live, "Observer", return_value=observer), \
                        patch.object(live, "deployment_to_observation", return_value={}), \
                        patch.object(live, "handle"), \
                        patch.object(experiment, "build_proposal", return_value=proposal), \
                        patch.object(experiment, "dependency_result", return_value={}), \
                        patch.object(live, "proposal_fingerprint", return_value="f" * 64):
                    self.assertIs(experiment.fault("primary"), incident)
                    experiment.case("missing-approval", incident, approval=False)
            finally:
                experiment.store.close()
        self.assertEqual([(step["phase"], step["scenario"]) for step in experiment.summary["timeline"]],
                         [("fault_injected", "primary"), ("incident_observed", "primary"),
                          ("proposal_saved", "missing-approval")])

    def test_chaos_batch_records_provenance_without_running_cluster_tools(self):
        from experiments.boutique.harness import chaos_demo
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(chaos_demo.provenance, "tool_versions", return_value={}) as tools:
            self.assertEqual(chaos_demo.main(["--dry-run", "--runs-dir", directory]), 0)
            batch = json.loads(next(Path(directory).glob("*/batch.json")).read_text(encoding="utf-8"))
        self.assertEqual(batch["provenance"]["captured_by"], "chaos-demo")
        self.assertEqual(tools.call_args.args[0], ())


if __name__ == "__main__":
    unittest.main()
