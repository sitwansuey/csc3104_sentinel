"""Kind-mode lab tooling offline: bundle preparation, per-run fault injection and operator restore,
the kind-mode deadline and duplicate incidents in summary.json. No cluster is contacted; kubectl is
the fake lab Deployment of tests/test_topology.py, extended with status and the operator's commands."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from experiments.distributed import kind_bundle, run_scenarios, scenarios
from experiments.distributed.kind_bundle import KindLab, LabError
from experiments.distributed.topology import OBSERVER_FAILURE_SECONDS, ROOT, Topology
from tests.test_topology import FakeLabDeployment

CONTEXT = "kind-sentinel-boutique-connected"
LAB = "sentinel-boutique-connected"
TARGET = {"cluster_id": LAB, "namespace": LAB, "kind": "Deployment", "name": "shippingservice", "uid": "uid-ship"}
REPOSITORY = "us-central1-docker.pkg.dev/online-boutique-ci/microservices-demo/shippingservice"
HEALTHY = {"metadata": {"labels": {"app": "shippingservice"}},
           "spec": {"serviceAccountName": "shippingservice",
                    "containers": [{"name": "server", "image": REPOSITORY + "@sha256:" + "a" * 64,
                                    "env": [{"name": "PORT", "value": "50051"}]}]}}
STOREFRONT = "http://127.0.0.1:18631"
TOKEN = "eyJhbGciOiJSUzI1NiJ9.executor-token-" + "x" * 40
ADMIN_KEY = "admin-client-key-data-" + "k" * 40
CA = "Y2EtZGF0YQ=="


class Clock:
    """monotonic and sleep for KindLab: sleeping advances time, so waits end at once."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeKindLab(FakeLabDeployment):
    """The lab target and the frontend as kubectl shows them, plus the operator commands KindLab sends.

    The target is Available exactly while its template is the healthy one;
    the storefront page answers 200 then and 500 otherwise. set env and a
    JSON patch with uid and resourceVersion tests change the template the
    way the API server would.
    """

    def __init__(self, *, template=HEALTHY, rbac=None, apply_injection=True):
        super().__init__(TARGET, template)
        self.deployment["metadata"]["namespace"] = LAB
        self.frontend = {"metadata": {"uid": "uid-front", "name": "frontend", "generation": 1,
                                      "resourceVersion": "7"}, "spec": {"replicas": 1, "template": {}}}
        self.rbac = {"get deployment/shippingservice": "yes", "patch deployment/shippingservice": "yes",
                     **(rbac or {})}
        self.apply_injection = apply_injection
        self.storefront_up = None  # None: follows the template; True or False forces it.
        self.writes = []

    def healthy(self):
        return self.deployment["spec"]["template"] == HEALTHY

    def status(self, url):
        self.calls.append(["GET", url])
        up = self.healthy() if self.storefront_up is None else self.storefront_up
        return 200 if up else 500

    def shown(self):
        item = json.loads(json.dumps(self.deployment))
        replicas = item["spec"]["replicas"]
        item["status"] = {"observedGeneration": item["metadata"]["generation"], "updatedReplicas": replicas,
                          "readyReplicas": replicas if self.healthy() else 0,
                          "availableReplicas": replicas if self.healthy() else 0}
        return item

    def runner(self, command, **_options):
        self.calls.append(command)
        args = [item for item in command[1:] if not item.startswith("--") or item.startswith("--containers")
                or item.startswith("--type") or item.startswith("--patch") or item.startswith("--duration")]

        def done(stdout="", code=0):
            return subprocess.CompletedProcess(command, code, stdout, "")
        if args[:2] == ["get", "deployment"]:
            return done(json.dumps(self.shown() if args[2] == TARGET["name"] else self.frontend))
        if args[:2] == ["config", "view"]:
            return done(json.dumps({"clusters": [{"name": "kind-x", "cluster": {
                "server": "https://127.0.0.1:6443", "certificate-authority-data": CA}}],
                "users": [{"name": "kind-x", "user": {"client-key-data": ADMIN_KEY}}]}))
        if args[:2] == ["create", "token"]:
            return done(TOKEN + "\n")
        if args[:2] == ["auth", "can-i"]:
            return done(self.rbac.get(" ".join(args[2:4]), "no") + "\n")
        if args[:2] == ["set", "env"]:
            self.writes.append("inject")
            if self.apply_injection:
                name, value = args[-1].split("=", 1)
                template = json.loads(json.dumps(self.deployment["spec"]["template"]))
                template["spec"]["containers"][0]["env"] = [{"name": name, "value": value}]
                self.set_template(template)
            return done()
        if args[:2] == ["patch", "deployment"]:
            patch_ops = json.loads(next(item for item in command if item.startswith("--patch=")).split("=", 1)[1])
            metadata = self.deployment["metadata"]
            if any(op["op"] == "test" and op["value"] != metadata[op["path"].rsplit("/", 1)[1]] for op in patch_ops):
                return done(code=1)
            self.writes.append("restore")
            (replace_op,) = [op for op in patch_ops if op["op"] == "replace"]
            self.set_template(replace_op["value"])
            return done()
        raise AssertionError("Unexpected kubectl command: " + " ".join(command))


def prepare(lab, directory, **options):
    clock = Clock()
    return kind_bundle.prepare(CONTEXT, STOREFRONT, bundle=Path(directory) / "bundle", runner=lab.runner,
                               status=lab.status, sleep=clock.sleep, **options)


def files_text(directory):
    return {path: path.read_text(encoding="utf-8") for path in Path(directory).rglob("*") if path.is_file()}


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.lab = FakeKindLab()

    def test_a_bundle_holds_the_token_only_in_its_private_kubeconfig(self):
        summary = prepare(self.lab, self.directory)
        bundle = self.directory / "bundle"
        self.assertEqual(summary["bundle"], str(bundle.resolve()))
        written = files_text(bundle)
        self.assertEqual(sorted(path.name for path in written), summary["files"])
        for path, text in written.items():
            self.assertNotIn(ADMIN_KEY, text, path)  # The operator's own credentials are never written.
            self.assertEqual(TOKEN in text, path.name == "kubeconfig", path)
        self.assertNotIn(TOKEN, json.dumps(summary))
        kubeconfig = json.loads((bundle / "kubeconfig").read_text(encoding="utf-8"))
        self.assertEqual([context["name"] for context in kubeconfig["contexts"]], [CONTEXT])
        self.assertEqual(kubeconfig["users"], [{"name": "sentinel-executor", "user": {"token": TOKEN}}])
        self.assertEqual(kubeconfig["clusters"][0]["cluster"], {"server": "https://127.0.0.1:6443",
                                                                "certificate-authority-data": CA})
        lab = json.loads((bundle / "lab.json").read_text(encoding="utf-8"))
        self.assertEqual((lab["context"], lab["fault_scenario"], lab["target"]), (CONTEXT, "R1", TARGET))
        policy = json.loads((bundle / "policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["detection"], {"failure_duration_seconds": OBSERVER_FAILURE_SECONDS})
        self.assertEqual(policy["health"], kind_bundle.HEALTH)
        self.assertEqual(json.loads((bundle / "baseline.json").read_text(encoding="utf-8"))["pod_template"], HEALTHY)
        self.assertEqual([check["answer"] for check in summary["rbac"]], [c[2] for c in kind_bundle.RBAC_CHECKS])

    def test_preparation_reads_only_the_allowlisted_context_and_writes_nothing_to_the_cluster(self):
        prepare(self.lab, self.directory, token_duration="90m")
        commands = [call for call in self.lab.calls if call[0] == "kubectl"]
        self.assertTrue(all(f"--context={CONTEXT}" in call and f"--namespace={LAB}" in call for call in commands))
        self.assertEqual(self.lab.writes, [])
        self.assertEqual(self.lab.deployment["metadata"]["resourceVersion"], "100")
        (token_request,) = [call for call in commands if "token" in call]
        self.assertIn("--duration=90m", token_request)
        self.assertTrue(all("--kubeconfig=" in " ".join(call) for call in commands if "can-i" in call))

    def test_sentinel_lab_and_every_other_context_are_refused_before_any_command(self):
        for context in ("sentinel-lab", "kind-sentinel-lab", "kind-sentinel-lab-2", "kind-sentinel-boutique-lab",
                        "docker-desktop", "", None):
            with self.subTest(context=context), self.assertRaises(LabError):
                kind_bundle.prepare(context, STOREFRONT, bundle=self.directory / "b", runner=self.lab.runner,
                                    status=self.lab.status, sleep=Clock().sleep)
            with self.subTest(context=context), self.assertRaises(LabError):
                KindLab(context, TARGET, HEALTHY, {"target": "shippingservice"}, STOREFRONT, runner=self.lab.runner)
        # Even an allowlist entry naming it is refused: the forbidden name is checked first.
        with patch.dict(kind_bundle.LABS, {"kind-sentinel-lab": {"cluster_id": "sentinel-lab", "namespace": "x"}}), \
                patch("experiments.distributed.kind_bundle.KIND_CONTEXTS", frozenset({"kind-sentinel-lab"})), \
                self.assertRaises(LabError):
            kind_bundle.lab_scope("kind-sentinel-lab")
        self.assertEqual(self.lab.calls, [])
        self.assertFalse((self.directory / "b").exists())
        for argv in (["prepare", "--context", "kind-sentinel-lab", "--storefront", STOREFRONT],
                     ["prepare", "--context", CONTEXT, "--storefront", "http://10.0.0.5:80"],
                     ["prepare", "--context", CONTEXT, "--storefront", STOREFRONT, "--token-duration", "9h"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                kind_bundle.main(argv)
            self.assertEqual(stopped.exception.code, 2)

    def test_inputs_are_checked_before_anything_is_written(self):
        cases = ({"storefront": "http://user:pw@127.0.0.1:18631"}, {"storefront": "http://127.0.0.1:18631/cart"},
                 {"storefront": "https://shop.example:443"}, {"token_duration": "0h"}, {"token_duration": "1d"},
                 {"token_duration": "481m"}, {"scenario_id": "N1"}, {"scenario_id": "R9"})
        for case in cases:
            options = {"storefront": STOREFRONT, **case}
            with self.subTest(case=case), self.assertRaises(LabError):
                kind_bundle.prepare(CONTEXT, options.pop("storefront"), bundle=self.directory / "b",
                                    runner=self.lab.runner, status=self.lab.status, sleep=Clock().sleep, **options)
        self.assertFalse((self.directory / "b").exists())

    def test_a_bundle_inside_the_repository_is_refused(self):
        inside = ROOT / "runs" / "kind-bundle-test"
        with self.assertRaises(LabError):
            kind_bundle.prepare(CONTEXT, STOREFRONT, bundle=inside, runner=self.lab.runner, status=self.lab.status,
                                sleep=Clock().sleep)
        self.assertFalse(inside.exists())

    def test_the_default_bundle_directory_is_refused_when_the_temporary_directory_is_in_the_repository(self):
        inside = ROOT / "runs" / "kind-bundle-tmp-test"
        with patch.object(kind_bundle.tempfile, "gettempdir", return_value=str(inside)), \
                patch.object(kind_bundle.tempfile, "mkdtemp") as made, self.assertRaises(LabError):
            kind_bundle.bundle_directory(None)
        made.assert_not_called()
        self.assertFalse(inside.exists())
        with patch.object(kind_bundle.tempfile, "gettempdir", return_value=str(self.directory)):
            fresh = kind_bundle.bundle_directory(None)
        self.assertEqual(fresh.parent, self.directory.resolve())
        self.assertTrue(fresh.name.startswith("sentinel-kind-bundle-"))

    def test_an_unhealthy_unpinned_or_changing_baseline_writes_no_bundle(self):
        unpinned = json.loads(json.dumps(HEALTHY))
        unpinned["spec"]["containers"][0]["image"] = REPOSITORY + ":v0.10.0"
        down = FakeKindLab()
        down.storefront_up = False
        for name, lab in (("storefront down", down), ("unpinned", FakeKindLab(template=unpinned))):
            with self.subTest(name), self.assertRaises(LabError):
                prepare(lab, self.directory)
            self.assertFalse((self.directory / "bundle").exists())

    def test_a_restricted_identity_that_may_do_more_leaves_no_kubeconfig(self):
        lab = FakeKindLab(rbac={"get secrets": "yes"})
        with self.assertRaises(LabError):
            prepare(lab, self.directory)
        bundle = self.directory / "bundle"
        self.assertFalse((bundle / "kubeconfig").exists())
        self.assertFalse(any(TOKEN in text for text in files_text(bundle).values()))

    def test_the_cli_prints_a_summary_without_the_token(self):
        output = io.StringIO()
        with patch.object(kind_bundle, "prepare", return_value={"bundle": "b", "files": []}) as prepared, \
                redirect_stdout(output):
            self.assertEqual(kind_bundle.main(["prepare", "--context", CONTEXT, "--storefront", STOREFRONT]), 0)
        self.assertEqual(prepared.call_args.kwargs["token_duration"], "2h")
        self.assertEqual(json.loads(output.getvalue())["bundle"], "b")
        with patch.object(kind_bundle, "prepare", side_effect=LabError("not healthy")), redirect_stdout(output):
            self.assertEqual(kind_bundle.main(["prepare", "--context", CONTEXT, "--storefront", STOREFRONT]), 1)


class KindLabTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cluster = FakeKindLab()
        prepare(self.cluster, self.directory)
        self.bundle = self.directory / "bundle"
        self.clock = Clock()

    def lab(self, **options):
        return KindLab.from_bundle(self.bundle, CONTEXT, **{
            "runner": self.cluster.runner, "status": self.cluster.status, "sleep": self.clock.sleep,
            "monotonic": self.clock.monotonic, **options})

    def test_inject_applies_the_catalog_fault_and_waits_until_it_shows(self):
        record = self.lab().inject()
        self.assertEqual(self.cluster.writes, ["inject"])
        self.assertTrue(record["fault_visible"])
        self.assertEqual(record["scenario"], "R1")
        self.assertEqual(record["injection"], {"kind": "set_env", "container": "server", "env": "PORT",
                                               "value": "invalid-port"})
        self.assertEqual(record["health_http_status"], 500)
        self.assertFalse(record["state"]["template_is_baseline"])
        (injected,) = [call for call in self.cluster.calls if "env" in call]
        self.assertEqual(injected[-3:], ["deployment/shippingservice", "--containers=server", "PORT=invalid-port"])

    def test_restore_puts_the_baseline_back_only_when_sentinel_did_not(self):
        lab = self.lab()
        lab.inject()
        record = lab.restore()
        self.assertTrue(record["operator_restore"])
        self.assertTrue(record["healthy"])
        self.assertTrue(record["after"]["template_is_baseline"])
        self.assertEqual(self.cluster.writes, ["inject", "restore"])
        lab.inject()
        self.cluster.rollback("ACT-1", HEALTHY)  # Sentinel recovered it.
        record = lab.restore()
        self.assertEqual((record["operator_restore"], record["healthy"]), (False, True))
        self.assertEqual(self.cluster.writes, ["inject", "restore", "inject"])

    def test_restore_retests_a_moved_resource_version_and_reports_a_target_that_stays_unhealthy(self):
        lab = self.lab()
        lab.inject()
        original = self.cluster.runner
        moved = []

        def racing(command, **options):
            if "patch" in command and not moved:
                moved.append(True)
                self.cluster.annotate("other", "change")  # Someone commits between the read and the patch.
            return original(command, **options)
        lab.runner = racing
        self.cluster.storefront_up = False
        record = lab.restore()
        self.assertTrue(record["operator_restore"])
        self.assertFalse(record["healthy"])  # The template is back but the page never answered 200.
        self.assertEqual(len([call for call in self.cluster.calls if "--type=json" in call]), 2)

    def test_inject_refuses_a_target_that_is_not_at_its_healthy_baseline(self):
        self.cluster.storefront_up = False
        with self.assertRaises(LabError):
            self.lab().inject()
        self.assertEqual(self.cluster.writes, [])

    def test_a_fault_that_never_shows_is_restored_and_stops_the_run(self):
        self.cluster.apply_injection = False
        with self.assertRaises(LabError) as stopped:
            self.lab().inject()
        self.assertIn("did not show", str(stopped.exception))
        self.assertTrue(self.cluster.healthy())

    def failing_gets(self, failures):
        """Wrap the fake kubectl: once the fault was written, the next `failures` gets fail (rc 1)."""
        original, failed = self.cluster.runner, []

        def runner(command, **options):
            if "get" in command and "inject" in self.cluster.writes and len(failed) < failures:
                failed.append(command)
                return subprocess.CompletedProcess(command, 1, "", "the server is currently unable to handle it")
            return original(command, **options)
        return runner

    def test_an_applied_fault_that_never_shows_is_put_back_to_the_baseline(self):
        self.cluster.storefront_up = True  # The template changes, but the page keeps answering 200.
        with self.assertRaises(LabError) as stopped:
            self.lab().inject()
        self.assertEqual(self.cluster.writes, ["inject", "restore"])
        self.assertTrue(self.cluster.healthy())
        self.assertIn("did not show", str(stopped.exception))
        self.assertIn("restored and verified healthy", str(stopped.exception))

    def test_a_fault_that_never_shows_and_an_unhealthy_restore_is_not_reported_as_restored(self):
        writes = self.cluster.writes
        lab = self.lab()
        lab.status = lambda _url: 200 if not writes else None  # After the write nothing answers at all.
        with self.assertRaises(LabError) as stopped:
            lab.inject()
        message = str(stopped.exception)
        self.assertEqual(writes, ["inject", "restore"])
        self.assertNotIn("was restored", message)
        self.assertIn("not at a verified healthy baseline", message)
        self.assertIn(f"kind_bundle restore --context {CONTEXT}", message)

    def test_a_read_that_fails_after_the_injection_still_restores_the_baseline(self):
        lab = self.lab(runner=self.failing_gets(1))
        with self.assertRaises(LabError) as stopped:
            lab.inject()
        self.assertEqual(self.cluster.writes, ["inject", "restore"])
        self.assertTrue(self.cluster.healthy())
        self.assertIn("kubectl get failed", str(stopped.exception))
        self.assertIn("restored and verified healthy", str(stopped.exception))
        self.assertNotIn("unable to handle", str(stopped.exception))  # kubectl's error text is never kept.

    def test_a_restore_that_cannot_read_either_names_the_leftover_fault_and_the_restore_command(self):
        lab = self.lab(runner=self.failing_gets(1000))
        with self.assertRaises(LabError) as stopped:
            lab.inject()
        self.assertEqual(self.cluster.writes, ["inject"])
        self.assertFalse(self.cluster.healthy())  # The fault is still in place, and the message says so.
        self.assertIn("fault may still be in place", str(stopped.exception))
        self.assertIn(f"kind_bundle restore --context {CONTEXT} --bundle", str(stopped.exception))

    def test_a_write_that_times_out_after_applying_is_restored(self):
        original = self.cluster.runner

        def runner(command, **options):
            result = original(command, **options)
            if "env" in command:
                raise subprocess.TimeoutExpired(command, 60)  # Applied, but kubectl never returned.
            return result
        with self.assertRaises(LabError) as stopped:
            self.lab(runner=runner).inject()
        self.assertEqual(self.cluster.writes, ["inject", "restore"])
        self.assertTrue(self.cluster.healthy())
        self.assertIn("kubectl set did not complete (TimeoutExpired)", str(stopped.exception))

    def test_a_reply_that_is_not_a_deployment_is_a_lab_error(self):
        lab = self.lab(runner=lambda command, **_: subprocess.CompletedProcess(command, 0, "not json", ""))
        with self.assertRaises(LabError):
            lab.deployment()
        self.assertEqual(lab.restore_or_record(), {"operator_restore": None, "healthy": False,
                                                   "error": "kubectl get returned no Deployment"})

    def test_a_target_outside_the_allowlisted_lab_is_refused(self):
        fault = kind_bundle.catalog_fault("R1", kind_bundle.lab_scope(CONTEXT))
        sent = len(self.cluster.calls)
        for target in (dict(TARGET, cluster_id="other"), dict(TARGET, namespace="default"),
                       dict(TARGET, name="cartservice")):
            with self.subTest(target=target), self.assertRaises(LabError):
                KindLab(CONTEXT, target, HEALTHY, fault, STOREFRONT, runner=self.cluster.runner)
        self.assertEqual(len(self.cluster.calls), sent)  # Refused before any command.

    def test_the_restore_command_puts_a_leftover_fault_back_even_with_an_expired_token(self):
        options = {"runner": self.cluster.runner, "status": self.cluster.status, "sleep": self.clock.sleep,
                   "monotonic": self.clock.monotonic}
        self.lab().inject()
        lab_json = self.bundle / "lab.json"
        lab_json.write_text(json.dumps(dict(json.loads(lab_json.read_text(encoding="utf-8")),
                                            kubeconfig_expires_at="2026-01-01T00:00:00Z")), encoding="utf-8")
        argv = ["restore", "--context", CONTEXT, "--bundle", str(self.bundle)]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(kind_bundle.main(argv, lab_options=options), 0)
        self.assertEqual(self.cluster.writes, ["inject", "restore"])
        self.assertTrue(self.cluster.healthy())
        printed = json.loads(output.getvalue())
        self.assertEqual((printed["status"], printed["operator_restore"]), ("healthy", True))
        self.assertNotIn(TOKEN, output.getvalue())
        self.cluster.storefront_up = False  # A target that never gets healthy exits 1.
        with redirect_stdout(io.StringIO()):
            self.assertEqual(kind_bundle.main(argv, lab_options=options), 1)
        with redirect_stdout(io.StringIO()):  # No bundle there: refused, nothing sent.
            self.assertEqual(kind_bundle.main(["restore", "--context", CONTEXT, "--bundle",
                                               str(self.directory / "none")], lab_options=options), 1)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as refused:
            kind_bundle.main(["restore", "--context", "kind-sentinel-lab", "--bundle", str(self.bundle)])
        self.assertEqual(refused.exception.code, 2)

    def test_a_bundle_for_another_context_an_old_tool_or_an_expiring_token_is_refused(self):
        with self.assertRaises(LabError):
            KindLab.from_bundle(self.bundle, "kind-sentinel-lab")
        later = datetime.now(UTC) + timedelta(hours=2)
        with self.assertRaises(LabError):
            self.lab(now=lambda: later)
        (self.bundle / "lab.json").unlink()
        with self.assertRaises(LabError):
            self.lab()
        with self.assertRaises(LabError):  # A recreated Deployment is not the bundle's target.
            KindLab(CONTEXT, dict(TARGET, uid="other"), HEALTHY, kind_bundle.catalog_fault(
                "R1", kind_bundle.lab_scope(CONTEXT)), STOREFRONT, runner=self.cluster.runner).deployment()


class FakeTopology:
    """Stands in for the process topology: Sentinel's part of a run is the scenario function below."""

    started = []

    def __init__(self, run_dir, *, mode, context, kind_bundle, **options):
        self.run_dir, self.mode, self.options = Path(run_dir), mode, options
        self.processes, self.tls, self.lease_seconds = {}, False, 4
        self.clock_offsets, self.max_clock_skew = {}, None
        self.observer_failure_seconds = OBSERVER_FAILURE_SECONDS

    def start(self, observer=True):
        FakeTopology.started.append(FakeTopology.cluster.healthy())

    def stop(self):
        FakeTopology.stopped += 1

    def wait_terminal(self, remaining):
        FakeTopology.deadlines.append(remaining)
        return True

    def kind_writes(self):
        return []


def records(duplicate):
    rows = [{"incident_id": "INC-1", "state": "RECOVERED", "active_action_id": "ACT-1"}]
    journal = [{"seq": 1, "incident_id": "INC-1", "action_id": None, "from_state": None, "to_state": "OPEN",
                "cause": "incident", "recorded_at": "2026-11-13T00:00:00Z"}]
    if duplicate:
        rows.append({"incident_id": "INC-2", "state": "REJECTED", "active_action_id": "ACT-2"})
        journal.append({"seq": 2, "incident_id": "INC-2", "action_id": None, "from_state": None, "to_state": "OPEN",
                        "cause": "incident", "recorded_at": "2026-11-13T00:00:04Z"})
    return {"incidents": rows, "transitions": journal, "worker_clocks": [], "cluster_writes": []}


class KindRunTests(unittest.TestCase):
    """run_scenarios --mode kind with the fake lab: each run gets its own fault and a verified restore."""

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cluster = FakeKindLab()
        prepare(self.cluster, self.directory)
        self.bundle = self.directory / "bundle"
        self.clock = Clock()
        self.lab = KindLab.from_bundle(self.bundle, CONTEXT, runner=self.cluster.runner, status=self.cluster.status,
                                       sleep=self.clock.sleep, monotonic=self.clock.monotonic)
        FakeTopology.cluster, FakeTopology.started, FakeTopology.stopped, FakeTopology.deadlines = \
            self.cluster, [], 0, []
        recovers = replace(scenarios.SCENARIOS["S1b"], run=lambda _topology: self.cluster.rollback("ACT-1", HEALTHY))
        seals = replace(scenarios.SCENARIOS["S8"], run=lambda _topology: {"sealed": True})
        self.enterContext(patch.dict(run_scenarios.SCENARIOS, {"S1b": recovers, "S8": seals}))
        self.enterContext(patch.object(run_scenarios, "Topology", FakeTopology))
        self.enterContext(patch.object(run_scenarios.invariants, "collect",
                                       side_effect=lambda topology, **_: records(
                                           topology.run_dir.parent.name.startswith("sentinel-s1b-"))))
        self.enterContext(patch.object(run_scenarios.invariants, "check", return_value=[]))
        self.out = self.directory / "out"

    def run_main(self, *extra):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = run_scenarios.main(["--mode", "kind", "--context", CONTEXT, "--kind-bundle", str(self.bundle),
                                       "--out", str(self.out), *extra], lab=self.lab)
        return code, [json.loads(line) for line in output.getvalue().splitlines()]

    def summaries(self):
        return {path.parent.name: json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(self.out.rglob("summary.json"))}

    def test_every_run_of_every_scenario_gets_a_fresh_fault_and_a_verified_restore(self):
        code, lines = self.run_main("--scenarios", "S1b,S8", "--repeat", "2")
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.cluster.writes, ["inject", "inject", "inject", "restore", "inject", "restore"])
        self.assertEqual(FakeTopology.started, [False] * 4)  # Each topology started on the injected fault.
        self.assertEqual(FakeTopology.stopped, 4)
        self.assertTrue(self.cluster.healthy())
        summaries = list(self.summaries().values())
        self.assertEqual(len(summaries), 4)
        for summary in summaries:
            lab = summary["kind_lab"]
            self.assertTrue(lab["fault"]["fault_visible"])
            self.assertEqual(lab["fault"]["scenario"], "R1")
            self.assertTrue(lab["restore"]["healthy"])
            self.assertEqual(lab["restore"]["operator_restore"], summary["scenario"] == "S8")
            self.assertEqual((summary["deadline_seconds"], summary["deadline_source"]), (180, "kind default"))
            self.assertEqual(summary["observer_failure_window_seconds"], OBSERVER_FAILURE_SECONDS)
        self.assertTrue(all(remaining > 170 for remaining in FakeTopology.deadlines))

    def test_duplicate_incidents_are_counted_in_the_summary(self):
        self.assertEqual(self.run_main("--scenarios", "S1b")[0], 0)
        (summary,) = self.summaries().values()
        self.assertEqual(summary["duplicate_incidents"],
                         {"count": 1, "incidents": [{"incident_id": "INC-2", "state": "REJECTED"}]})
        self.assertEqual(summary["states"], ["RECOVERED", "REJECTED"])
        self.assertEqual(self.run_main("--scenarios", "S8")[0], 0)
        s8 = [item for name, item in self.summaries().items() if name.endswith("S8")][0]
        self.assertEqual(s8["duplicate_incidents"], {"count": 0, "incidents": []})

    def test_the_deadline_option_is_recorded_and_limited_to_kind_mode(self):
        self.assertEqual(self.run_main("--scenarios", "S8", "--deadline", "240")[0], 0)
        (summary,) = self.summaries().values()
        self.assertEqual((summary["deadline_seconds"], summary["deadline_source"]), (240, "--deadline"))
        self.assertGreater(FakeTopology.deadlines[0], 230)
        for argv in (["--deadline", "120"], ["--mode", "kind", "--context", CONTEXT, "--kind-bundle",
                                             str(self.bundle), "--deadline", "5"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                run_scenarios.main(argv, lab=self.lab)
            self.assertEqual(stopped.exception.code, 2)
        with self.assertRaises(ValueError):
            run_scenarios.run_once("S0", deadline=120)
        with self.assertRaises(ValueError):
            run_scenarios.run_once("S0", mode="kind", context=CONTEXT, kind_bundle=self.bundle)

    def test_a_failed_restore_stops_the_invocation_before_the_next_run(self):
        original = self.cluster.set_template

        def stuck(template):  # After the first restore the storefront never answers 200 again.
            original(template)
            if template == HEALTHY:
                self.cluster.storefront_up = False
        self.cluster.set_template = stuck
        code, lines = self.run_main("--scenarios", "S8", "--repeat", "3")
        self.assertEqual(code, 3)
        self.assertIn("did not return", lines[-1]["stopped"])
        self.assertEqual(self.cluster.writes, ["inject", "restore"])
        self.assertEqual(len(self.summaries()), 1)

    def test_a_fault_that_does_not_show_starts_no_topology(self):
        self.cluster.apply_injection = False
        code, lines = self.run_main("--scenarios", "S1b,S8")
        self.assertEqual(code, 3)
        self.assertIn("did not show", lines[-1]["stopped"])
        self.assertEqual(FakeTopology.started, [])
        self.assertFalse(self.out.exists())

    def break_reads_during_the_run(self):
        """From the scenario on, every kubectl get fails: the restore after the run cannot read the target."""
        original, broken = self.lab.runner, []

        def runner(command, **options):
            if broken and "get" in command:
                return subprocess.CompletedProcess(command, 1, "", "")
            return original(command, **options)
        self.lab.runner = runner
        return broken

    def test_a_restore_that_raises_still_writes_the_summary_and_stops_naming_the_restore_command(self):
        broken = self.break_reads_during_the_run()
        seals = replace(scenarios.SCENARIOS["S8"], run=lambda _topology: broken.append(True))
        with patch.dict(run_scenarios.SCENARIOS, {"S8": seals}):
            code, lines = self.run_main("--scenarios", "S8,S1b", "--repeat", "2")
        self.assertEqual(code, 3)
        self.assertIn("did not return", lines[-1]["stopped"])
        self.assertIn(f"kind_bundle restore --context {CONTEXT}", lines[-1]["stopped"])
        self.assertEqual(self.cluster.writes, ["inject"])  # The fault is left, and said to be left.
        (summary,) = self.summaries().values()
        self.assertEqual(summary["kind_lab"]["restore"], {"operator_restore": None, "healthy": False,
                                                          "error": "kubectl get failed"})
        self.assertEqual(FakeTopology.stopped, 1)

    def test_a_run_that_fails_and_a_restore_that_fails_stop_with_the_leftover_state(self):
        broken = self.break_reads_during_the_run()

        def crashes(_topology):
            broken.append(True)
            raise OSError("disk full")  # Not a harness_error: it ends the run.
        seals = replace(scenarios.SCENARIOS["S8"], run=crashes)
        with patch.dict(run_scenarios.SCENARIOS, {"S8": seals}):
            code, lines = self.run_main("--scenarios", "S8")
        self.assertEqual(code, 3)
        self.assertIn("The run stopped (OSError)", lines[-1]["stopped"])
        self.assertIn("fault may still be in place", lines[-1]["stopped"])
        self.assertEqual(self.summaries(), {})
        self.assertEqual(self.cluster.writes, ["inject"])
        broken.clear()
        self.assertTrue(self.lab.restore()["healthy"])  # The operator restores it, as the message says.

        def fails(_topology):
            raise OSError("disk full")
        # With a working restore the run's own error comes through, on a restored baseline.
        with patch.dict(run_scenarios.SCENARIOS, {"S8": replace(seals, run=fails)}), self.assertRaises(OSError):
            self.run_main("--scenarios", "S8")
        self.assertTrue(self.cluster.healthy())
        self.assertEqual(self.cluster.writes, ["inject", "restore", "inject", "restore"])

    def test_file_mode_is_unchanged(self):
        summary = run_scenarios.run_once("S8", out=self.out)
        self.assertEqual(summary["deadline_seconds"], scenarios.SCENARIOS["S8"].deadline_seconds)
        for key in ("kind_lab", "duplicate_incidents", "deadline_source", "observer_failure_window_seconds"):
            self.assertNotIn(key, summary)
        self.assertLessEqual(FakeTopology.deadlines[0], scenarios.SCENARIOS["S8"].deadline_seconds)
        self.assertEqual(self.cluster.writes, [])

    def test_no_credential_reaches_any_written_file(self):
        self.assertEqual(self.run_main("--scenarios", "S1b,S8")[0], 0)
        for path, text in files_text(self.out).items():
            self.assertNotIn(TOKEN, text, path)
            self.assertNotIn(ADMIN_KEY, text, path)

    def test_the_bundle_lab_is_loaded_and_refused_without_lab_json(self):
        (self.bundle / "lab.json").unlink()
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            run_scenarios.main(["--mode", "kind", "--context", CONTEXT, "--kind-bundle", str(self.bundle)])
        self.assertIn("lab.json", errors.getvalue())


class VerifierAttemptsTests(unittest.TestCase):
    """A scenario with max_verifier_attempts matches its expectation only within that bound.

    The bound is what reports a live run whose Verifier went back to many
    deliveries (L-VERIFIER-RETRY-BACKOFF) as not matching. File mode cannot
    exceed it on either cadence, so it is checked here on fake records.
    """

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        FakeTopology.cluster, FakeTopology.started, FakeTopology.stopped, FakeTopology.deadlines = \
            FakeKindLab(), [], 0, []
        self.enterContext(patch.object(run_scenarios, "Topology", FakeTopology))
        self.enterContext(patch.object(run_scenarios.invariants, "check", return_value=[]))
        self.receipts = []
        self.enterContext(patch.object(run_scenarios.invariants, "collect", side_effect=lambda _topology, **_: {
            **records(False), "receipts": list(self.receipts),
            "outbox": [{"id": 1, "topic": "verifier.request", "action_id": "ACT-1", "delivered": 1},
                       {"id": 2, "topic": "dependency.request", "action_id": "ACT-1", "delivered": 1},
                       {"id": 3, "topic": "verifier.request", "action_id": "ACT-2", "delivered": 1}]}))

    def run_with(self, bound, *receipts):
        self.receipts = [{"event_id": event_id, "attempts": attempts} for event_id, attempts in receipts]
        scenario = replace(scenarios.SCENARIOS["S8"], run=lambda _topology: {}, max_verifier_attempts=bound)
        with patch.dict(run_scenarios.SCENARIOS, {"S8": scenario}):
            return run_scenarios.run_once("S8")

    def test_more_verifier_deliveries_than_the_bound_do_not_match_the_expectation(self):
        summary = self.run_with(2, (1, 3), (2, 1))
        self.assertEqual((summary["verifier_attempts"], summary["max_verifier_attempts"]), (3, 2))
        self.assertTrue(summary["passed"])  # Every invariant passed: only the bound fails the run.
        self.assertFalse(summary["matches_expectation"])
        # The most deliveries of any verifier.request command counts, not the first one's.
        summary = self.run_with(2, (1, 1), (3, 3))
        self.assertEqual(summary["verifier_attempts"], 3)
        self.assertFalse(summary["matches_expectation"])

    def test_a_bound_with_no_verifier_command_does_not_match_the_expectation(self):
        summary = self.run_with(2, (2, 1))  # Only the dependency.request was delivered.
        self.assertIsNone(summary["verifier_attempts"])
        self.assertFalse(summary["matches_expectation"])

    def test_deliveries_within_the_bound_match_the_expectation(self):
        for attempts in (1, 2):
            with self.subTest(attempts=attempts):
                summary = self.run_with(2, (1, attempts), (2, 5))  # Other topics do not count.
                self.assertEqual(summary["verifier_attempts"], attempts)
                self.assertTrue(summary["matches_expectation"])

    def test_a_scenario_without_a_bound_is_unaffected(self):
        summary = self.run_with(None, (1, 7))
        self.assertEqual((summary["verifier_attempts"], summary["max_verifier_attempts"]), (7, None))
        self.assertTrue(summary["matches_expectation"])
        summary = self.run_with(None)
        self.assertIsNone(summary["verifier_attempts"])
        self.assertTrue(summary["matches_expectation"])

    def test_s0_and_s2_carry_the_bound(self):
        self.assertEqual({name: scenario.max_verifier_attempts for name, scenario in scenarios.SCENARIOS.items()
                          if scenario.max_verifier_attempts is not None}, {"S0": 2, "S2": 2})


class StorefrontStatusTests(unittest.TestCase):
    def test_a_storefront_that_does_not_answer_http_is_no_status_never_an_error(self):
        from http.client import BadStatusLine, RemoteDisconnected
        from urllib.error import HTTPError, URLError
        for raised, expected in ((BadStatusLine("garbage"), None), (RemoteDisconnected("closed"), None),
                                 (URLError("refused"), None), (HTTPError(STOREFRONT, 503, "down", {}, None), 503)):
            with self.subTest(raised=type(raised).__name__), \
                    patch.object(kind_bundle, "build_opener") as opener:
                opener.return_value.open.side_effect = raised
                self.assertEqual(kind_bundle.http_status(STOREFRONT + "/cart"), expected)


class ObserverWindowTests(unittest.TestCase):
    def test_the_kind_observer_gets_the_quieter_window_whatever_the_bundle_says(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "policy.json").write_text(json.dumps({"detection": {
                "failure_duration_seconds": 2, "max_sample_gap_seconds": 45}}), encoding="utf-8")
            topology = Topology(bundle / "run", mode="kind", tls=False, context=CONTEXT, kind_bundle=bundle)
            self.addCleanup(topology.stop)
            topology.paths["config"].mkdir(parents=True)
            topology.inputs = {"target": TARGET}
            topology.tokens = {"observer-1": "o" * 40}
            command = topology._kind_observer("observer-1", {"delivery": type("Link", (), {"port": 1})()})
            policy_path = Path(command[command.index("--policy") + 1])
            self.assertEqual(policy_path.parent, topology.paths["config"])
            self.assertEqual(json.loads(policy_path.read_text(encoding="utf-8")), {"detection": {
                "failure_duration_seconds": OBSERVER_FAILURE_SECONDS, "max_sample_gap_seconds": 45}})
            for window in (0, 301, 2.5, "15"):
                with self.subTest(window=window), self.assertRaises(ValueError):
                    Topology(bundle / "run", observer_failure_seconds=window)


if __name__ == "__main__":
    unittest.main()
