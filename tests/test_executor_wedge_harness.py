"""Offline checks for the live Executor wedge harness; no cluster is contacted.

A fake kubectl backed by the simulated API server stands in for the cluster, so
the guards, both scenarios, cleanup and the summary can be checked end to end.
"""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, UTC
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from services.executor.rest_adapter import KubernetesRESTDeploymentAPI
from tests.support.fake_apiserver import FakeAPIServer
from tests.support.sim_apiserver import SimAPIServer, SimKubectl


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/validation"))
import test_executor_wedge_live as harness  # noqa: E402

sys.path.remove(str(ROOT / "scripts/validation"))

CONTEXT = "kind-sentinel-boutique-connected"
TOKEN = "fake-token-" + "z" * 40
SERVER = "https://127.0.0.1:65000"
CA = "RkFLRS1DQS1EQVRB"


def quiet_main(argv, runner, **options):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return harness.main(argv, runner=runner, **options)


class FakeTime:
    """A clock that only moves when the harness sleeps, so the 30 s settle delay costs nothing."""

    def __init__(self):
        self.now, self.sleeps = datetime(2026, 11, 12, 9, 0, tzinfo=UTC), []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


def _merge(target, patch):
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


class FakeCluster(SimKubectl):
    """kubectl for one namespace; cluster is the cluster name its context points at."""

    def __init__(self, *, cluster="kind-sentinel-boutique-connected", fail=None, enforce_after=1):
        super().__init__(SimAPIServer(cluster_id=CONTEXT, deployment=None, revision=500))
        self.cluster, self.fail = cluster, fail
        self.namespaces, self.uids = set(), 0
        # Admission policies take effect only after enforce_after probes, like a real API server's delay.
        self.policies, self.enforce_after, self.probes = set(), enforce_after, 0  # (kind, name)

    def _admission(self):
        kinds = {kind for kind, _ in self.policies}
        if {"ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"} <= kinds:
            self.server.admission = lambda old, new: old["spec"]["template"] == new["spec"]["template"]
        else:
            self.server.admission = None

    def __call__(self, command, **kwargs):
        command = [str(item) for item in command]
        options, words = self._split(command)
        verb = words[0] if words else ""
        if self.fail and self.fail == words[:2]:
            self.calls.append(command)
            return self._reply(command, 1, stderr=b"injected failure")
        if verb == "delete" and words[1:2] in (["validatingadmissionpolicy"], ["validatingadmissionpolicybinding"]):
            self.calls.append(command)
            self.policies = {(kind, name) for kind, name in self.policies
                             if not (name == words[2] and kind.lower() == words[1])}
            self._admission()
            return self._reply(command)
        if verb == "patch" and options.get("dry-run") == "server":
            self.calls.append(command)
            self.probes += 1
            candidate = json.loads(json.dumps(self.server.deployment))
            _merge(candidate, json.loads(words[words.index("--patch") + 1]))
            denied = (self.server.admission is not None and self.probes > self.enforce_after
                      and not self.server.admission(self.server.deployment, candidate))
            return self._reply(command, 1 if denied else 0)
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
                if item["kind"].startswith("ValidatingAdmissionPolicy"):
                    self.policies.add((item["kind"], item["metadata"]["name"]))
            self._admission()
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

    def test_sanitise_ignores_a_bare_root_path(self):
        # A HOME of "/" must not turn every slash in the summary into <path>.
        anchor = Path.cwd().anchor
        for root in ("/", Path("/"), anchor, Path(anchor)):
            with self.subTest(root=root):
                self.assertEqual(harness.sanitise({"note": "a/b " + anchor + "x"}, paths=[root]),
                                 {"note": "a/b " + anchor + "x"})


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.run_dir = Path(temporary.name) / "run"

    def run_harness(self, scenario, fake, **options):
        code = quiet_main(["--context", CONTEXT, "--scenario", scenario, "--run-dir", str(self.run_dir)], fake,
                          **options)
        text = (self.run_dir / (scenario + ".json")).read_text(encoding="utf-8")
        return code, json.loads(text), text

    @staticmethod
    def rows(summary):
        return {row["label"]: row for row in summary["results"]}

    @staticmethod
    def counts(row):
        return row["write_attempts"], row["successful_writes"], row["patch_processes"]

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
        rows = self.rows(summary)
        # execute sent the one patch the race rejected; reconcile only replays the DONE row.
        self.assertEqual(self.counts(rows["execute"]), (1, 0, 1))
        self.assertEqual(self.counts(rows["reconcile"]), (0, 0, 0))
        self.assertEqual(rows["reconcile"]["passes"], harness.RECONCILE_PASSES)
        self.assertEqual(self.counts(rows["second execute"]), (1, 1, 1))
        self.assertTrue(all(check["passed"] for check in summary["checks"]))
        self.assertTrue(summary["namespace_deleted"] and summary["private_credentials_removed"])
        self.assertEqual(len(fake.server.committed()), 1)  # Only the second action's write.
        self.assertContained(fake, text)

    def test_delete_recreate_supersedes_without_a_patch(self):
        fake = FakeCluster()
        code, summary, text = self.run_harness("delete-recreate", fake)
        self.assertEqual(code, 0, summary)
        rows = self.rows(summary)
        self.assertEqual([row["label"] for row in summary["results"]],
                         ["execute (stopped before send)", "reconcile", "second execute"])
        self.assertEqual((rows["reconcile"]["status"], rows["reconcile"]["basis"]),
                         ("SUPERSEDED", "TARGET_REPLACED"))
        self.assertEqual(summary["patch_processes_for_first_action"], 0)
        # The stopped execute counted its attempt but never started kubectl patch; reconcile sent nothing.
        stopped = rows["execute (stopped before send)"]
        self.assertIsNone(stopped["status"])
        self.assertEqual(self.counts(stopped), (1, 0, 0))
        self.assertEqual(self.counts(rows["reconcile"]), (0, 0, 0))
        self.assertEqual(sum(row["patch_processes"] for row in summary["results"][:2]), 0)
        self.assertEqual(summary["results"][-1]["status"], "APPLIED")
        truth = summary["ground_truth"]
        self.assertFalse(truth[0]["uid_is_original"])
        self.assertTrue(truth[-1]["template_is_baseline"])
        self.assertContained(fake, text)

    def test_admission_deny_is_fenced_after_the_settle_delay_then_admits_a_new_proposal(self):
        fake, time = FakeCluster(enforce_after=2), FakeTime()
        code, summary, text = self.run_harness("admission-deny", fake, clock=time.clock, sleep=time.sleep)
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["status"], "PASS")
        rows = self.rows(summary)
        self.assertEqual([row["label"] for row in summary["results"]],
                         ["execute", "reconcile before the settle delay", "reconcile", "second execute"])
        # The API server denied the write: in doubt, version unchanged, and no fence before 30 s.
        self.assertEqual((rows["execute"]["status"], rows["execute"]["basis"]),
                         ("NEEDS_RECONCILIATION", "UNCHANGED_IN_DOUBT"))
        self.assertEqual(self.counts(rows["execute"]), (1, 0, 1))
        early = rows["reconcile before the settle delay"]
        self.assertEqual((early["status"], early["fence_attempts"], early["patch_processes"]),
                         ("NEEDS_RECONCILIATION", 0, 0))
        # After the delay one fence commits; the write is proven never to have committed.
        fenced = rows["reconcile"]
        self.assertEqual((fenced["status"], fenced["basis"]), ("CONFLICT", "FENCED"))
        self.assertEqual((fenced["fence_attempts"], fenced["successful_fences"], fenced["write_attempts"],
                          fenced["patch_processes"]), (1, 1, 0, 1))
        self.assertIn(harness.FENCE_AFTER_SECONDS + 1, time.sleeps)
        truth = {row["label"]: row for row in summary["ground_truth"]}
        before, after = truth["before first action"], truth["after first action"]
        self.assertEqual(after["spec_sha256"], before["spec_sha256"])
        # The annotation-only fence still bumps generation, as a real Deployment does.
        self.assertEqual(after["generation"], before["generation"] + 1)
        self.assertNotEqual(after["resource_version"], before["resource_version"])
        self.assertEqual(truth["after the denied write"]["resource_version"], before["resource_version"])
        self.assertTrue(after["fence_action_id"].startswith("ACT-WEDGE-first-"))
        self.assertEqual((after["template_is_baseline"], truth["after second action"]["template_is_baseline"]),
                         (False, True))
        self.assertEqual((summary["policy_enforced_after_probes"], summary["policy_lifted_after_probes"]), (3, 1))
        self.assertTrue(summary["policy_deleted"])
        self.assertEqual(fake.policies, set())
        self.assertEqual(rows["second execute"]["status"], "APPLIED")
        self.assertEqual(len(fake.server.committed()), 1)  # Only the second action's write.
        self.assertEqual(len(fake.server.committed(kind="fence")), 1)
        self.assertContained(fake, text)
        for command in fake.calls:  # The policy is removed by the administrator context, never the Executor.
            if "validatingadmissionpolicy" in command:
                self.assertIn("--context=" + CONTEXT, command)

    def test_admission_deny_without_fencing_stays_in_doubt_and_fails(self):
        fake, time = FakeCluster(), FakeTime()
        adapters = harness.WedgeHarness.adapters

        def without_fencing(wedge, target, runner):  # An Executor before the fenced abort.
            return replace(adapters(wedge, target, runner), fence_after_seconds=None)

        with mock.patch.object(harness.WedgeHarness, "adapters", without_fencing):
            code, summary, _ = self.run_harness("admission-deny", fake, clock=time.clock, sleep=time.sleep)
        self.assertEqual((code, summary["status"]), (1, "FAIL"))
        failed = {check["name"] for check in summary["checks"] if not check["passed"]}
        self.assertIn("first action resolves as CONFLICT/FENCED", failed)
        self.assertIn("a new proposal for the workload is admitted", failed)
        self.assertEqual(self.rows(summary)["reconcile"]["status"], "NEEDS_RECONCILIATION")
        self.assertEqual(fake.policies, set())  # Removed even though the scenario failed.

    def test_admission_policy_that_never_takes_effect_fails_and_is_removed(self):
        fake, time = FakeCluster(enforce_after=10 ** 6), FakeTime()
        code, summary, _ = self.run_harness("admission-deny", fake, clock=time.clock, sleep=time.sleep)
        self.assertEqual((code, summary["status"], summary["error_type"]), (1, "FAILED", "RuntimeError"))
        self.assertEqual(len(time.sleeps), harness.POLICY_WAIT_ATTEMPTS)
        self.assertTrue(summary["policy_deleted"])
        self.assertEqual(fake.policies, set())
        self.assertEqual(fake.server.requests, [])  # The Executor never ran.
        self.assertEqual(fake.calls[-1][3:5], ["delete", "namespace"])

    def test_admission_policy_matches_only_the_harness_namespace_and_target(self):
        policy, binding = harness.admission_policy("sentinel-wedge-1234abcd-deny-template", "sentinel-wedge-1234abcd")
        self.assertEqual(policy["spec"]["matchConstraints"]["resourceRules"],
                         [{"apiGroups": ["apps"], "apiVersions": ["v1"], "operations": ["UPDATE"],
                           "resources": ["deployments"]}])
        self.assertEqual(policy["spec"]["matchConditions"][0]["expression"], "object.metadata.name == 'wedge-target'")
        self.assertEqual(policy["spec"]["validations"][0]["expression"],
                         "object.spec.template == oldObject.spec.template")
        self.assertEqual(binding["spec"]["matchResources"]["namespaceSelector"]["matchLabels"],
                         {"kubernetes.io/metadata.name": "sentinel-wedge-1234abcd"})
        self.assertEqual((binding["spec"]["policyName"], binding["spec"]["validationActions"]),
                         (policy["metadata"]["name"], ["Deny"]))

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

    def test_a_failed_namespace_create_is_still_cleaned_up(self):
        # kubectl can time out after the server created the namespace, so cleanup must still try.
        fake = FakeCluster(fail=["create", "namespace"])
        code, summary, _ = self.run_harness("rv-race", fake)
        self.assertEqual((code, summary["status"]), (1, "FAILED"))
        self.assertEqual(fake.calls[-1][3:5], ["delete", "namespace"])
        self.assertIn("--ignore-not-found", fake.calls[-1])
        self.assertTrue(summary["namespace_deleted"])

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


class RESTTransportTests(unittest.TestCase):
    """--transport rest (P5.08): the same scenarios through the REST client and a fake API server on loopback."""

    rows = staticmethod(ScenarioTests.rows)
    counts = staticmethod(ScenarioTests.counts)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.run_dir = Path(temporary.name) / "run"

    def run_harness(self, scenario, fake, **options):
        server = FakeAPIServer(fake.server, token=TOKEN).start()
        self.addCleanup(server.close)

        class LoopbackREST(KubernetesRESTDeploymentAPI):
            """The harness's restricted kubeconfig, pointed at the fake server over loopback http."""

            @classmethod
            def from_kubeconfig_data(cls, config, **values):
                config = json.loads(json.dumps(config))
                cluster = config["clusters"][0]["cluster"]
                cluster["server"] = server.url
                cluster.pop("certificate-authority-data")
                return super().from_kubeconfig_data(config, **values)
        argv = ["--context", CONTEXT, "--scenario", scenario, "--run-dir", str(self.run_dir), "--transport", "rest"]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = harness.main(argv, runner=fake, rest_api=LoopbackREST, **options)
        text = (self.run_dir / (scenario + ".json")).read_text(encoding="utf-8")
        for secret in (TOKEN, SERVER, CA, "secret-key", server.url, str(self.run_dir)):
            self.assertNotIn(secret, text)
        self.assertFalse((self.run_dir / ".executor-kubeconfig.json").exists())
        return code, json.loads(text), server

    def test_rv_race_is_a_definite_conflict_with_one_patch(self):
        fake = FakeCluster()
        code, summary, server = self.run_harness("rv-race", fake)
        self.assertEqual((code, summary["status"], summary["transport"]), (0, "PASS", "rest"), summary)
        rows = self.rows(summary)
        self.assertEqual((rows["execute"]["status"], rows["execute"]["basis"]), ("CONFLICT", "DEFINITE_REJECTION"))
        self.assertEqual(self.counts(rows["execute"]), (1, 0, 1))
        self.assertEqual(self.counts(rows["reconcile"]), (0, 0, 0))
        self.assertEqual(rows["second execute"]["status"], "APPLIED")
        self.assertEqual(server.count("PATCH"), 2)  # The rejected first write and the second action's.
        self.assertFalse(any("patch" in command and "--context=" + harness.RESTRICTED_CONTEXT in command
                             for command in fake.calls))

    def test_admission_deny_fails_at_once_with_no_fence_and_no_settle_delay(self):
        fake, time = FakeCluster(enforce_after=2), FakeTime()
        code, summary, server = self.run_harness("admission-deny", fake, clock=time.clock, sleep=time.sleep)
        self.assertEqual((code, summary["status"]), (0, "PASS"), summary)
        rows = self.rows(summary)
        self.assertEqual([row["label"] for row in summary["results"]], ["execute", "reconcile", "second execute"])
        self.assertEqual((rows["execute"]["status"], rows["execute"]["basis"]), ("FAILED", "DEFINITE_REJECTION"))
        self.assertEqual(sum(row["fence_attempts"] for row in summary["results"]), 0)
        self.assertNotIn(harness.FENCE_AFTER_SECONDS + 1, time.sleeps)
        self.assertEqual(len(fake.server.committed(kind="fence")), 0)
        self.assertEqual(rows["second execute"]["status"], "APPLIED")
        self.assertEqual(fake.policies, set())

    def test_delete_recreate_sends_no_patch_for_the_first_action(self):
        fake = FakeCluster()
        code, summary, server = self.run_harness("delete-recreate", fake)
        self.assertEqual((code, summary["status"]), (0, "PASS"), summary)
        self.assertEqual(summary["patch_processes_for_first_action"], 0)
        self.assertEqual(server.count("PATCH"), 1)  # Only the second action's write.

    def test_an_unknown_transport_is_refused_before_any_command(self):
        fake = FakeCluster()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            harness.main(["--context", CONTEXT, "--scenario", "rv-race", "--run-dir", str(self.run_dir),
                          "--transport", "grpc"], runner=fake)
        with self.assertRaises(harness.Refused):
            harness.WedgeHarness(CONTEXT, "rv-race", self.run_dir, runner=fake, transport="grpc")
        self.assertEqual(fake.calls, [])


class EntryPointTests(unittest.TestCase):
    def test_help_runs_without_a_cluster(self):
        completed = subprocess.run([sys.executable, str(ROOT / "scripts/validation/test_executor_wedge_live.py"),
                                    "--help"], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--context", completed.stdout)


if __name__ == "__main__":
    unittest.main()
