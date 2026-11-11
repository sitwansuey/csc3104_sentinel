"""Offline checks for the live Executor wedge harness; no cluster is contacted.

A fake kubectl backed by the simulated API server stands in for the cluster, so
the guards, both scenarios, cleanup and the summary can be checked end to end.
"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.support.sim_apiserver import SimAPIServer, SimKubectl


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/validation"))
import test_executor_wedge_live as harness  # noqa: E402

sys.path.remove(str(ROOT / "scripts/validation"))

CONTEXT = "kind-sentinel-boutique-connected"
TOKEN = "fake-token-" + "z" * 40
SERVER = "https://127.0.0.1:65000"
CA = "RkFLRS1DQS1EQVRB"


def quiet_main(argv, runner):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return harness.main(argv, runner=runner)


def _merge(target, patch):
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


class FakeCluster(SimKubectl):
    """kubectl for one namespace; cluster is the cluster name its context points at."""

    def __init__(self, *, cluster="kind-sentinel-boutique-connected", fail=None):
        super().__init__(SimAPIServer(cluster_id=CONTEXT, deployment=None, revision=500))
        self.cluster, self.fail = cluster, fail
        self.namespaces, self.uids = set(), 0

    def __call__(self, command, **kwargs):
        command = [str(item) for item in command]
        options, words = self._split(command)
        verb = words[0] if words else ""
        if self.fail and self.fail == words[:2]:
            self.calls.append(command)
            return self._reply(command, 1, stderr=b"injected failure")
        if words[:2] == ["config", "view"]:
            self.calls.append(command)
            view = {"contexts": [{"name": options["context"], "context": {"cluster": self.cluster, "user": "admin"}}],
                    "clusters": [{"name": self.cluster,
                                  "cluster": {"server": SERVER, "certificate-authority-data": CA}}],
                    "users": [{"name": "admin", "user": {"client-key-data": "secret-key"}}]}
            return self._reply(command, stdout=json.dumps(view).encode())
        if words[:2] in (["create", "namespace"], ["label", "namespace"]):
            self.calls.append(command)
            self.namespaces.add(words[2])
            return self._reply(command)
        if words[:2] == ["delete", "namespace"]:
            self.calls.append(command)
            self.namespaces.discard(words[2])
            self.server.deployment = None
            return self._reply(command)
        if verb == "apply":
            self.calls.append(command)
            for item in json.loads(Path(words[2]).read_text(encoding="utf-8"))["items"]:
                if item["kind"] == "Deployment" and not self.server.present:
                    self.uids += 1
                    self.server.create(item, f"uid-{self.uids}")
            return self._reply(command)
        if words[:2] == ["create", "token"]:
            self.calls.append(command)
            return self._reply(command, stdout=(TOKEN + "\n").encode())
        if verb == "patch" and options.get("type") == "merge":
            self.calls.append(command)
            self.server.external_write(lambda d: _merge(d, json.loads(words[words.index("--patch") + 1])))
            return self._reply(command)
        return super().__call__(command, **kwargs)


class GuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.run_dir = Path(temporary.name) / "run"

    def test_context_and_scenario_are_required(self):
        fake = FakeCluster()
        for argv in ([], ["--scenario", "rv-race"], ["--context", CONTEXT],
                     ["--context", CONTEXT, "--scenario", "apiserver-pause"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as raised:
                quiet_main(argv, fake)
            self.assertEqual(raised.exception.code, 2)
        self.assertEqual(fake.calls, [])

    def test_contexts_outside_the_allowlist_are_refused_before_any_command(self):
        fake = FakeCluster()
        for context in ("kind-sentinel-lab", "sentinel-lab", "KIND-SENTINEL-LAB", "kind-sentinel-lab-copy",
                        "docker-desktop", "kind-other", " ", "kind-sentinel-boutique-connected-2"):
            with self.subTest(context=context):
                code = quiet_main(["--context", context, "--scenario", "rv-race", "--run-dir", str(self.run_dir)],
                                  fake)
                self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])
        self.assertFalse(self.run_dir.exists())
        with self.assertRaises(harness.Refused):
            harness.WedgeHarness("kind-sentinel-lab", "rv-race", self.run_dir, runner=fake)

    def test_an_allowed_context_that_points_at_the_lab_cluster_is_refused(self):
        fake = FakeCluster(cluster="kind-sentinel-lab")
        code = quiet_main(["--context", CONTEXT, "--scenario", "rv-race", "--run-dir", str(self.run_dir)], fake)
        self.assertEqual(code, 2)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][1:2], ["--context=" + CONTEXT])
        self.assertEqual(fake.namespaces, set())
        summary = json.loads((self.run_dir / "rv-race.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "REFUSED")
        self.assertNotIn("namespace_deleted", summary)

    def test_sanitise_drops_secret_keys_and_blanks_secrets_and_paths(self):
        home = str(Path.home())
        value = {"token": "t", "nested": [{"server": "s", "note": f"used {TOKEN} from {home}"}],
                 "Kubeconfig": "k", "keep": 3}
        self.assertEqual(harness.sanitise(value, secrets=[TOKEN], paths=[home]),
                         {"nested": [{"note": "used [redacted] from <path>"}], "keep": 3})


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.run_dir = Path(temporary.name) / "run"

    def run_harness(self, scenario, fake):
        code = quiet_main(["--context", CONTEXT, "--scenario", scenario, "--run-dir", str(self.run_dir)], fake)
        text = (self.run_dir / (scenario + ".json")).read_text(encoding="utf-8")
        return code, json.loads(text), text

    def assertContained(self, fake, text):
        # Every admin command names the allowed context; every Executor command uses the restricted one.
        for command in fake.calls:
            context = [item for item in command if item.startswith("--context=")]
            self.assertEqual(len(context), 1, command)
            if context[0] == "--context=" + harness.RESTRICTED_CONTEXT:
                self.assertIn("--kubeconfig=" + str(self.run_dir.resolve() / ".executor-kubeconfig.json"), command)
                self.assertTrue(any(item.startswith("--namespace=sentinel-wedge-") for item in command), command)
            else:
                self.assertEqual(context[0], "--context=" + CONTEXT, command)
        self.assertEqual(fake.calls[-1][3:5], ["delete", "namespace"])
        self.assertTrue(fake.calls[-1][5].startswith("sentinel-wedge-"))
        self.assertEqual(fake.namespaces, set())
        self.assertFalse((self.run_dir / ".executor-kubeconfig.json").exists())
        for secret in (TOKEN, SERVER, CA, "secret-key", str(Path.home()), str(self.run_dir)):
            self.assertNotIn(secret, text)

    def test_rv_race_supersedes_then_admits_a_new_proposal(self):
        fake = FakeCluster()
        code, summary, text = self.run_harness("rv-race", fake)
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["results"][0]["label"], "execute")
        self.assertEqual((summary["results"][1]["status"], summary["results"][1]["basis"]),
                         ("SUPERSEDED", "ADVANCED_WITHOUT_EVIDENCE"))
        self.assertEqual(summary["results"][-1]["status"], "APPLIED")
        self.assertEqual(summary["patch_processes_for_first_action"], 1)
        self.assertTrue(all(check["passed"] for check in summary["checks"]))
        self.assertTrue(summary["namespace_deleted"] and summary["private_credentials_removed"])
        self.assertEqual(len(fake.server.committed()), 1)  # Only the second action's write.
        self.assertContained(fake, text)

    def test_delete_recreate_supersedes_without_a_patch(self):
        fake = FakeCluster()
        code, summary, text = self.run_harness("delete-recreate", fake)
        self.assertEqual(code, 0, summary)
        self.assertEqual((summary["results"][0]["status"], summary["results"][0]["basis"]),
                         ("SUPERSEDED", "TARGET_REPLACED"))
        self.assertEqual(summary["patch_processes_for_first_action"], 0)
        self.assertEqual(summary["results"][-1]["status"], "APPLIED")
        truth = summary["ground_truth"]
        self.assertFalse(truth[0]["uid_is_original"])
        self.assertTrue(truth[-1]["template_is_baseline"])
        self.assertContained(fake, text)

    def test_cleanup_runs_when_the_scenario_fails(self):
        fake = FakeCluster(fail=["create", "token"])
        code, summary, text = self.run_harness("rv-race", fake)
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "FAILED")
        self.assertEqual(summary["error_type"], "RuntimeError")
        self.assertTrue(summary["namespace_deleted"])
        self.assertEqual(fake.namespaces, set())
        self.assertEqual(fake.calls[-1][3:5], ["delete", "namespace"])
        self.assertFalse((self.run_dir / ".executor-kubeconfig.json").exists())

    def test_a_failed_check_is_reported_as_fail(self):
        # A pre-fix Executor would stay in doubt; simulate that by refusing the expected basis.
        fake = FakeCluster()
        original = harness.EXPECTED["rv-race"]
        harness.EXPECTED["rv-race"] = ("NEEDS_RECONCILIATION", "UNCHANGED_IN_DOUBT")
        self.addCleanup(harness.EXPECTED.__setitem__, "rv-race", original)
        code, summary, _ = self.run_harness("rv-race", fake)
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "FAIL")
        self.assertTrue(summary["namespace_deleted"])


class EntryPointTests(unittest.TestCase):
    def test_help_runs_without_a_cluster(self):
        completed = subprocess.run([sys.executable, str(ROOT / "scripts/validation/test_executor_wedge_live.py"),
                                    "--help"], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--context", completed.stdout)


if __name__ == "__main__":
    unittest.main()
