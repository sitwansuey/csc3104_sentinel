"""The sentinel-dist acceptance harness offline (P4.01): refusals, preflight, rendering, chaos, evidence.

Nothing here reaches a cluster: kubectl, docker, helm and kind are a fake
runner that answers from a script and records every command. The live run is
docs/distributed-acceptance.md.
"""
from contextlib import contextmanager
from datetime import UTC, datetime
import io
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from experiments.distributed import kind_acceptance as acceptance
from experiments.distributed import kind_acceptance_live as live
from experiments.distributed import scenarios as file_scenarios
from tests.support.manifest_yaml import load_all

GIB = 1024 ** 3


def done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def node(name, host=None):
    labels = {"kubernetes.io/hostname": name}
    if host:
        labels["sentinel.io/host"] = host
    return {"metadata": {"name": name, "labels": labels}, "status": {"nodeInfo": {"kubeletVersion": "v1.37.0"}}}


NODES = [node("sentinel-dist-control-plane"), node("sentinel-dist-worker", "a"), node("sentinel-dist-worker2", "b"),
         node("sentinel-dist-worker3", "c")]


class FakeRunner:
    """Answers commands from (predicate, answer) rules, first match wins; records every command."""

    def __init__(self, **overrides):
        self.calls = []
        self.answers = {
            "clusters": done("sentinel-dist\nsentinel-boutique-connected\n"),
            "view": done(json.dumps({"clusters": [{"name": "kind-sentinel-dist",
                                                   "cluster": {"server": "https://127.0.0.1:40123"}}]})),
            "nodes": done(json.dumps({"items": NODES})),
            "image": done(acceptance.NODE_IMAGE + "\n"),
            "audit": done(),
            "helm": done(json.dumps([{"name": "chaos-mesh", "chart": "chaos-mesh-" + acceptance.CHAOS_VERSION}])),
            "info": done(json.dumps({"MemTotal": 16 * GIB})),
            "stats": done('{"MemUsage": "2GiB / 15.6GiB"}\n{"MemUsage": "512MiB / 15.6GiB"}\n'),
            "contexts": done("kind-sentinel-dist\nkind-sentinel-boutique-connected\n"),
            "connected": done(""),
        }
        self.answers.update(overrides)

    @staticmethod
    def kind_of(command):
        text = " ".join(command)
        rules = (("kind get clusters", "clusters"), ("config view", "view"), ("get nodes", "nodes"),
                 ("{{.Config.Image}}", "image"), ("test -s", "audit"), ("helm", "helm"), ("docker info", "info"),
                 ("docker stats", "stats"), ("get-contexts", "contexts"),
                 ("--context=kind-sentinel-boutique-connected", "connected"))
        return next((key for pattern, key in rules if pattern in text), None)

    def __call__(self, command, input=None, capture_output=True, text=True, timeout=None, check=False):  # noqa: A002
        self.calls.append(list(command))
        key = self.kind_of(command)
        if key is None:
            return done()
        return self.answers[key]


def which(tool):
    return "/usr/bin/" + tool


@contextmanager
def socket_listener():
    """A loopback TCP listener for the probe helper's allowed case."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        yield server.getsockname()[1]


class ContextTests(unittest.TestCase):
    def test_only_kind_sentinel_dist_is_accepted_and_sentinel_lab_is_refused_first(self):
        self.assertEqual(acceptance.check_context("kind-sentinel-dist"), "kind-sentinel-dist")
        for context in ("kind-sentinel-lab", "sentinel-lab", "kind-sentinel-dist-sentinel-lab",
                        "kind-sentinel-boutique-connected", "kind-sentinel-dist2", "", None):
            with self.subTest(context=context), self.assertRaises(acceptance.AcceptanceError):
                acceptance.check_context(context)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "never targets sentinel-lab"):
            acceptance.check_context("kind-sentinel-lab")

    def test_a_refused_context_runs_nothing(self):
        runner = FakeRunner()
        for argv in (["preflight", "--context", "kind-sentinel-lab"],
                     ["run", "--context", "kind-sentinel-boutique-connected", "--run-dir", "x"],
                     ["teardown", "--context", "kind-sentinel-lab", "--delete-cluster"]):
            with self.subTest(argv=argv), patch("subprocess.run", runner), \
                    patch("sys.stderr", io.StringIO()) as stderr:
                self.assertEqual(acceptance.main(argv), 2)
                self.assertIn("Refused", stderr.getvalue())
        self.assertEqual(runner.calls, [])

    def test_every_command_names_the_lab_context_and_never_the_protected_cluster(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory, patch("shutil.which", which):
            acceptance.preflight(acceptance.Shell("kind-sentinel-dist", runner=runner),
                                 lock=acceptance.LabLock(Path(directory) / "lock"))
        for command in runner.calls:
            with self.subTest(command=command):
                self.assertFalse(any("sentinel-lab" in part for part in command))
                if command[0] == "kubectl":
                    context = command[1]
                    self.assertIn(context, {"--context=kind-sentinel-dist",
                                            "--context=kind-sentinel-boutique-connected"})
                    if context.endswith("boutique-connected"):
                        self.assertEqual(command[3], "get")  # The P4.03 lab is only read.
                if command[0] == "helm":
                    self.assertEqual(command[1:3], ["--kube-context", "kind-sentinel-dist"])
        self.assertFalse(any("use-context" in part for command in runner.calls for part in command))


class PreflightTests(unittest.TestCase):
    def preflight(self, runner, lock=None, **options):
        with tempfile.TemporaryDirectory() as directory, patch("shutil.which", which):
            lock = lock or acceptance.LabLock(Path(directory) / "lock")
            return acceptance.preflight(acceptance.Shell("kind-sentinel-dist", runner=runner), lock=lock, **options)

    def test_a_ready_lab_passes_and_reports_what_it_checked(self):
        checks = self.preflight(FakeRunner())
        self.assertEqual(checks["nodes"]["hosts"], {"a": "sentinel-dist-worker", "b": "sentinel-dist-worker2",
                                                    "c": "sentinel-dist-worker3"})
        self.assertEqual(checks["docker_memory"]["free_gib"], 13.5)
        self.assertEqual((checks["chaos_mesh"], checks["p4_03_running"]), ("2.8.2", False))

    def test_each_unready_lab_is_refused(self):
        stale = [dict(NODES[0]), *NODES[1:3]]
        relabelled = [NODES[0], NODES[1], NODES[2], node("sentinel-dist-worker3", "a")]
        foreign = [node("other-control-plane"), *NODES[1:]]
        nodes = "four sentinel-dist nodes with hosts a, b and c"
        cases = [
            ("does not exist", {"clusters": done("sentinel-boutique-connected\n")}),
            ("does not point", {"view": done(json.dumps({"clusters": [{"name": "kind-other", "cluster": {
                "server": "https://127.0.0.1:1"}}]}))}),
            ("does not point", {"view": done(json.dumps({"clusters": [{"name": "kind-sentinel-dist", "cluster": {
                "server": "https://10.0.0.5:6443"}}]}))}),
            (nodes, {"nodes": done(json.dumps({"items": stale}))}),
            (nodes, {"nodes": done(json.dumps({"items": relabelled}))}),
            (nodes, {"nodes": done(json.dumps({"items": foreign}))}),
            ("pinned node image", {"image": done("kindest/node:v1.37.0\n")}),
            ("audit log is missing", {"audit": done(code=1)}),
            ("Chaos Mesh chart", {"helm": done(json.dumps([{"name": "chaos-mesh", "chart": "chaos-mesh-2.7.0"}]))}),
            ("GiB of Docker memory", {"stats": done('{"MemUsage": "14GiB / 15.6GiB"}\n')}),
            ("P4.03 may be running", {"connected": done("podchaos.chaos-mesh.org/pod-failure\n")}),
            ("Could not tell", {"connected": done(code=1, stderr="Unable to connect to the server")}),
        ]
        for message, overrides in cases:
            with self.subTest(message=message, changed=list(overrides)), \
                    self.assertRaises(acceptance.AcceptanceError) as caught:
                self.preflight(FakeRunner(**overrides))
            self.assertIn(message, str(caught.exception))

    def test_chaos_mesh_absent_from_the_connected_lab_is_not_p4_03(self):
        missing = done(code=1, stderr='error: the server doesn\'t have a resource type "podchaos"')
        self.assertFalse(self.preflight(FakeRunner(connected=missing))["p4_03_running"])
        alone = FakeRunner(contexts=done("kind-sentinel-dist\n"))
        self.preflight(alone)
        self.assertFalse(any("--context=kind-sentinel-boutique-connected" in command for command in alone.calls))

    def test_before_create_needs_no_cluster_yet_and_more_memory(self):
        runner = FakeRunner(clusters=done("sentinel-boutique-connected\n"))
        checks = self.preflight(runner, before_create=True, min_free_gib=acceptance.CREATE_FREE_GIB)
        self.assertEqual((checks["cluster"], checks["docker_memory"]["required_free_gib"]), ("absent", 6.0))
        self.assertFalse(any("nodes" in command or "helm" in command for command in runner.calls))
        with self.assertRaisesRegex(acceptance.AcceptanceError, "already exists"):
            self.preflight(FakeRunner(), before_create=True)
        tight = FakeRunner(clusters=done(""), stats=done('{"MemUsage": "11GiB / 15.6GiB"}\n'))
        with self.assertRaisesRegex(acceptance.AcceptanceError, "6.0 GiB is needed"):
            self.preflight(tight, before_create=True, min_free_gib=acceptance.CREATE_FREE_GIB)
        lab_lock = acceptance.LabLock
        with tempfile.TemporaryDirectory() as directory, patch("subprocess.run", FakeRunner(clusters=done(""))), \
                patch("shutil.which", which), patch("sys.stdout", io.StringIO()) as stdout, \
                patch.object(acceptance, "LabLock", lambda: lab_lock(Path(directory) / "lock")):
            self.assertEqual(acceptance.main(["preflight", "--context", "kind-sentinel-dist", "--before-create"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["docker_memory"]["required_free_gib"], 6.0)

    def test_a_missing_tool_is_refused(self):
        with patch("shutil.which", lambda tool: None if tool == "helm" else "/usr/bin/" + tool), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "Missing helm"):
            acceptance.preflight(acceptance.Shell("kind-sentinel-dist", runner=FakeRunner()))

    def test_the_shared_lock_refuses_a_second_heavy_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            other = acceptance.LabLock(path, holder="P4.03 boutique chaos")
            with other:
                with self.assertRaisesRegex(acceptance.AcceptanceError, "P4.03 boutique chaos"):
                    self.preflight(FakeRunner(), lock=acceptance.LabLock(path))
                with self.assertRaises(acceptance.AcceptanceError), acceptance.LabLock(path):
                    pass
            self.assertFalse(path.exists())
            with acceptance.LabLock(path) as mine:  # The run's own lock does not refuse its own preflight.
                self.preflight(FakeRunner(), lock=mine)

    def test_memory_sizes_and_headroom(self):
        self.assertEqual(acceptance.parse_size("1.5GiB"), int(1.5 * GIB))
        self.assertEqual(acceptance.parse_size("512MB"), 512 * 1000 ** 2)
        self.assertEqual(acceptance.parse_size("0B"), 0)
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.parse_size("lots")
        self.assertEqual(acceptance.memory_headroom(4 * GIB, ['{"MemUsage": "5GiB / 4GiB"}', ""]),
                         (4 * GIB, 5 * GIB, 0))


class RenderTests(unittest.TestCase):
    def test_the_api_server_policy_gets_the_observed_addresses_and_nothing_else(self):
        text = (acceptance.MANIFESTS / "networkpolicy.yaml").read_text(encoding="utf-8")
        rendered = acceptance.render_api_server_policy(text, "10.96.0.1", "172.18.0.4")
        [policy] = [doc for doc in load_all(rendered) if doc["metadata"]["name"] == "sentinel-dist-api-server"]
        self.assertEqual([rule["to"][0]["ipBlock"]["cidr"] for rule in policy["spec"]["egress"]],
                         ["10.96.0.1/32", "172.18.0.4/32"])
        self.assertEqual(len(rendered.splitlines()), len(text.splitlines()))
        for service, endpoint in (("10.96.0.256", "172.18.0.4"), ("10.96.0.1/8", "172.18.0.4"), ("", "1.2.3.4")):
            with self.subTest(service=service), self.assertRaises(acceptance.AcceptanceError):
                acceptance.render_api_server_policy(text, service, endpoint)
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.render_api_server_policy(rendered, "10.96.0.1", "172.18.0.4")  # Placeholders already gone.

    def test_the_admission_policy_is_bound_to_the_workload_namespace_with_its_rules_unchanged(self):
        shipped = acceptance.ADMISSION.read_text(encoding="utf-8")
        rendered = acceptance.render_admission(shipped)
        before, after = load_all(shipped), load_all(rendered)
        self.assertEqual(before[0], after[0])  # Every CEL expression stays the pinned, reviewed text.
        values = after[1]["spec"]["matchResources"]["namespaceSelector"]["matchExpressions"][0]["values"]
        self.assertEqual(values, ["sentinel-dist-workload"])
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.render_admission(rendered)

    def test_each_identity_gets_its_own_fresh_secret_and_secrets_are_never_applied(self):
        values = acceptance.credentials()
        documents = acceptance.secret_documents(values)
        self.assertEqual({doc["metadata"]["name"] for doc in documents},
                         {f"sentinel-dist-{name}" for name in [*acceptance.IDENTITIES, "operator-1"]})
        tokens = [value for keys in values.values() for value in keys.values()]
        self.assertEqual(len(tokens), len(set(tokens)))
        self.assertTrue(all(len(token) >= 32 for token in tokens))
        self.assertEqual(set(values["planner-1"]), {"delivery-token"})
        self.assertEqual(set(values["executor-a"]), {"delivery-token", "approval-token"})
        self.assertEqual(set(values["operator-1"]), {"approval-token"})
        runner = FakeRunner()
        shell = acceptance.Shell("kind-sentinel-dist", runner=runner)
        with self.assertRaises(acceptance.AcceptanceError):
            shell.apply(documents[0])
        shell.create(documents[0])
        command = runner.calls[-1]
        self.assertEqual(command[-2:], ["create", "--filename=-"])
        self.assertFalse(any(token in " ".join(command) for token in tokens))

    def test_the_certificate_names_the_coordinator_service_and_loopback(self):
        names = acceptance.certificate_names()
        self.assertIn("DNS:sentinel-coordinator.sentinel-dist.svc", names)
        self.assertIn("IP:127.0.0.1", names)
        from experiments.distributed.topology import make_certificates
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            make_certificates(directory, openssl="unused", names=("DNS:bad name",))

    def test_target_inputs_hold_no_credential_and_bind_the_exact_target(self):
        target = {"cluster_id": "sentinel-dist", "namespace": "sentinel-dist-workload", "kind": "Deployment",
                  "name": "demo-api", "uid": "uid-1"}
        template = {"metadata": {"labels": {"app": "demo-api"}}, "spec": {"containers": [{"name": "demo-api"}]}}
        window = [{"at": "2026-11-13T10:00:00Z"}, {"at": "2026-11-13T10:00:30Z"}]
        files = acceptance.target_inputs(target, template, window)
        self.assertEqual(set(files), {"baseline.json", "policy.json", "allowed-targets.json",
                                      "verifier-inventory.json", "observer-policy.json"})
        self.assertEqual(files["allowed-targets.json"], [target])
        self.assertTrue(files["policy.json"]["baseline_constraints"]["require_image_digests"])
        self.assertEqual(files["verifier-inventory.json"]["services"]["demo-api"]["health_url"],
                         "http://demo-api.sentinel-dist-workload.svc:8080/readyz")
        permissions = acceptance.approval_permissions(target)
        self.assertEqual(permissions["operator-1"][0]["operations"], ["create", "read", "revoke", "history"])
        self.assertEqual({item[0]["operations"][0] for name, item in permissions.items() if name != "operator-1"},
                         {"read"})
        from scripts.evidence.sanitise import check_text
        check_text(json.dumps(files), "inputs")


class ChaosTests(unittest.TestCase):
    def test_each_object_is_namespaced_labelled_and_bounded(self):
        samples = acceptance.plan()["chaos_samples"]
        self.assertEqual({item["kind"] for item in samples}, {"PodChaos", "NetworkChaos", "TimeChaos"})
        for item in samples:
            with self.subTest(name=item["metadata"]["name"]):
                self.assertEqual(item["apiVersion"], "chaos-mesh.org/v1alpha1")
                self.assertEqual(item["metadata"]["namespace"], "sentinel-dist")
                self.assertEqual(item["metadata"]["labels"], acceptance.CHAOS_LABEL)
                self.assertEqual(item["spec"]["selector"]["namespaces"], ["sentinel-dist"])

    def test_partition_delay_and_skew_have_the_documented_shape(self):
        cut = acceptance.partition("p", "sentinel-executor-a", ["sentinel-coordinator"], 60, direction="from")
        self.assertEqual((cut["spec"]["action"], cut["spec"]["direction"], cut["spec"]["duration"]),
                         ("partition", "from", "60s"))
        self.assertEqual(cut["spec"]["target"]["selector"]["expressionSelectors"][0]["values"],
                         ["sentinel-coordinator"])
        delay = acceptance.delay_to_api_server("d", "sentinel-executor-a", "172.18.0.4", 60)
        self.assertEqual((delay["spec"]["delay"]["latency"], delay["spec"]["externalTargets"]),
                         ("8000ms", ["172.18.0.4"]))
        skew = acceptance.time_skew("t", ["sentinel-executor-a", "sentinel-executor-b"], -300, 60)
        self.assertEqual((skew["spec"]["timeOffset"], skew["spec"]["clockIds"], skew["spec"]["containerNames"]),
                         ("-300s", ["CLOCK_REALTIME"], ["worker"]))

    def test_chaos_outside_sentinel_pods_or_bounds_is_refused(self):
        refused = [lambda: acceptance.pod_kill("k", "demo-api"),
                   lambda: acceptance.pod_kill("k", "kube-apiserver"),
                   lambda: acceptance.pod_failure("k", "sentinel-executor-a", 3),
                   lambda: acceptance.pod_failure("k", "sentinel-executor-a", 3600),
                   lambda: acceptance.partition("p", "sentinel-executor-a", [], 60),
                   lambda: acceptance.partition("p", "sentinel-executor-a", ["coredns"], 60),
                   lambda: acceptance.partition("p", "sentinel-executor-a", ["sentinel-coordinator"], 60, "sideways"),
                   lambda: acceptance.delay_to_api_server("d", "sentinel-executor-a", "1.2.3.4", 60, latency_ms=10000),
                   lambda: acceptance.time_skew("t", ["sentinel-executor-a"], 0, 60),
                   lambda: acceptance.time_skew("t", ["sentinel-executor-a"], 7200, 60),
                   lambda: acceptance.chaos("StressChaos", "s", {}),
                   lambda: acceptance.chaos("PodChaos", "Bad_Name", {})]
        for index, build in enumerate(refused):
            with self.subTest(index=index), self.assertRaises(acceptance.AcceptanceError):
                build()


class ScenarioTests(unittest.TestCase):
    def test_the_matrix_is_s0_to_s9_and_maps_onto_the_file_mode_catalogue(self):
        self.assertEqual(acceptance.select("all"), ["S0", "S1a", "S1b", "S1c", "S2", "S3", "S4", "S5", "S6", "S7",
                                                    "S8", "S9"])
        self.assertEqual(set(acceptance.SCENARIOS), set(live.RUNNERS))
        for name, scenario in acceptance.SCENARIOS.items():
            with self.subTest(name=name):
                if name == "S9":
                    self.assertEqual(scenario.checks, ("I7",))
                else:
                    self.assertIn(scenario.catalogue, file_scenarios.SCENARIOS)
                    self.assertIn("I7", scenario.checks)
        self.assertEqual(acceptance.select(["S8", "S0"]), ["S0", "S8"])
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.select(["S10"])

    def test_takeover_latency_counts_only_the_other_executors_claim_after_the_fault(self):
        access = [
            {"ts": "2026-11-13T10:00:01Z", "identity": "executor-b", "operation": "claim", "outcome": "claimed",
             "http_status": 200},  # Before the fault.
            {"ts": "2026-11-13T10:00:12Z", "identity": "executor-a", "operation": "claim", "outcome": "claimed",
             "http_status": 200},  # The stalled one.
            {"ts": "2026-11-13T10:00:14Z", "identity": "executor-b", "operation": "claim", "outcome": "idle",
             "http_status": 200},
            {"ts": "2026-11-13T10:00:21.5Z", "identity": "executor-b", "operation": "claim", "outcome": "claimed",
             "http_status": 200}]
        self.assertEqual(acceptance.takeover_seconds(access, "2026-11-13T10:00:05Z", "executor-a"), 16.5)
        self.assertIsNone(acceptance.takeover_seconds(access[:3], "2026-11-13T10:00:05Z", "executor-a"))

    def test_coordinator_recovery_measures_ready_first_claim_and_next_transition(self):
        access = [{"ts": "2026-11-13T10:00:00Z", "operation": "claim", "http_status": 200},
                  {"ts": "2026-11-13T10:00:09Z", "operation": "claim", "http_status": 503},
                  {"ts": "2026-11-13T10:00:11Z", "operation": "claim", "http_status": 200}]
        transitions = [{"recorded_at": "2026-11-13T09:59:00Z"}, {"recorded_at": "2026-11-13T10:00:15Z"}]
        self.assertEqual(acceptance.coordinator_recovery("2026-11-13T10:00:02Z", "2026-11-13T10:00:08Z", access,
                                                         transitions),
                         {"to_ready": 6.0, "to_first_claim": 9.0, "to_next_transition": 13.0})
        self.assertEqual(acceptance.coordinator_recovery("2026-11-13T10:00:20Z", None, access, transitions),
                         {"to_ready": None, "to_first_claim": None, "to_next_transition": None})


class ScopeTests(unittest.TestCase):
    def test_only_the_incidents_this_run_opened_are_checked(self):
        records = {
            "transitions": [
                {"seq": 1, "incident_id": "old", "action_id": None, "from_state": None, "to_state": "OPEN",
                 "recorded_at": "2026-11-13T09:00:00Z"},
                {"seq": 2, "incident_id": "old", "action_id": "A0", "from_state": "OPEN", "to_state": "FAILED",
                 "recorded_at": "2026-11-13T09:01:00Z"},
                {"seq": 3, "incident_id": "new", "action_id": None, "from_state": None, "to_state": "OPEN",
                 "recorded_at": "2026-11-13T10:00:05Z"}],
            "incidents": [{"incident_id": "old", "state": "FAILED"}, {"incident_id": "new", "state": "OPEN"}],
            "actions": [{"action_id": "A0", "incident_id": "old"}, {"action_id": "A1", "incident_id": "new"}],
            "outbox": [{"id": 1, "action_id": "A0"}, {"id": 2, "action_id": "A1"}],
            "revocations": [{"action_id": "A0", "revoked_at": "2026-11-13T09:00:30Z"}],
            "receipts": [{"event_id": 1}]}
        scoped = acceptance.scope_records(records, "2026-11-13T10:00:00Z")
        self.assertEqual([row["incident_id"] for row in scoped["incidents"]], ["new"])
        self.assertEqual([row["action_id"] for row in scoped["actions"]], ["A1"])
        self.assertEqual([row["seq"] for row in scoped["transitions"]], [3])
        self.assertEqual([row["id"] for row in scoped["outbox"]], [2])
        self.assertEqual((scoped["revocations"], scoped["receipts"]), ([], [{"event_id": 1}]))
        self.assertFalse(acceptance.invariants.i3(scoped)["passed"])  # The new incident is still OPEN.
        self.assertFalse(acceptance.invariants.i3(acceptance.scope_records(records, "2026-11-13T11:00:00Z"))[
            "passed"])  # No incident in this run is a failure, not a pass on the old one.


class EvidenceTests(unittest.TestCase):
    def test_the_results_table_starts_empty_and_fills_from_summaries(self):
        empty = acceptance.results_table([])
        self.assertEqual(empty.count("not yet run"), 12)
        runs = [{"scenario": "S2", "passed": True, "audit": {"max_template_writes_per_action": 1},
                 "takeover_seconds": 17.2, "coordinator_recovery": None},
                {"scenario": "S2", "passed": False, "audit": {"max_template_writes_per_action": 2},
                 "takeover_seconds": 19.0, "coordinator_recovery": None},
                {"scenario": "S5", "passed": True, "audit": {"max_template_writes_per_action": 1},
                 "takeover_seconds": None,
                 "coordinator_recovery": {"to_ready": 7.5, "to_first_claim": 9.25, "to_next_transition": 12}}]
        table = acceptance.results_table(runs)
        self.assertIn("| S2 | 2 | 1/2 | 2 | 17.2-19.0 | - / - |", table)
        self.assertIn("| S5 | 1 | 1/1 | 1 | - | 7.5-7.5 / 9.2-9.2 |", table)
        self.assertEqual(table.count("not yet run"), 10)

    def test_the_documented_table_is_the_empty_one(self):
        document = (acceptance.ROOT / "docs/distributed-acceptance.md").read_text(encoding="utf-8")
        self.assertIn(acceptance.results_table([]), document.replace("\r\n", "\n"))

    def test_evidence_with_a_secret_name_token_or_jwt_is_refused_and_nothing_is_written(self):
        tokens = {"executor-a": {"delivery-token": "x" * 43}}
        forbidden = live.forbidden_strings(tokens)
        self.assertIn("sentinel-dist-executor-a", forbidden)
        self.assertIn("x" * 43, forbidden)
        jwt = "eyJ" + "hbGciOiJSUzI1NiJ9" + "." + "eyJzdWIiOiJzeXN0ZW06c2EifQ" + "." + "c2lnbmF0dXJlLXZhbHVl"
        for leak in ("sentinel-dist-operator-1", "x" * 43, jwt):
            with self.subTest(leak=leak[:12]), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory) / "run"
                with self.assertRaises(acceptance.audit.AuditError):
                    live.write_evidence(folder, {"summary.json": {"ok": True},
                                                 "timeline.jsonl": json.dumps({"line": leak}) + "\n"}, forbidden)
                self.assertFalse(folder.exists())
        with tempfile.TemporaryDirectory() as directory:
            written = live.write_evidence(Path(directory) / "run", {"summary.json": {"ok": True}}, forbidden)
            self.assertEqual(written, ["summary.json"])

    def test_a_denial_probe_is_classified_by_the_api_servers_answer(self):
        self.assertEqual(live._outcome(done(code=1, stderr='Error from server (Forbidden): secrets "x" is forbidden')),
                         "denied")
        self.assertEqual(live._outcome(done(code=1, stderr="ValidatingAdmissionPolicy 'p' with binding 'p' denied "
                                                           "request: The Sentinel Executor may not change spec.replicas")),
                         "denied")
        self.assertEqual(live._outcome(done(code=1, stderr="Unable to connect to the server")), "error")
        self.assertEqual(live._outcome(done("deployment.apps/demo-api patched")), "allowed")

    def test_rbac_checks_stop_at_the_first_unexpected_answer(self):
        runner = FakeRunner()
        answers = iter(["yes\n", "yes\n", "yes\n"])  # The third, delete, must be "no".
        original = runner.__call__

        def answer(command, **options):
            if "can-i" in command:
                runner.calls.append(list(command))
                return done(next(answers))
            return original(command, **options)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "sentinel-executor may delete"):
            live.rbac_checks(acceptance.Shell("kind-sentinel-dist", runner=answer))
        impersonated = [part for command in runner.calls for part in command if part.startswith("--as=")]
        self.assertEqual(set(impersonated), {"--as=system:serviceaccount:sentinel-dist:sentinel-executor"})


class LabTests(unittest.TestCase):
    def lab(self, handler):
        calls = []

        def runner(command, **options):
            calls.append(list(command))
            return handler(command)
        shell = acceptance.Shell("kind-sentinel-dist", runner=runner)
        times = iter(range(10000))
        return acceptance.Lab(shell, tempfile.gettempdir(), sleep=lambda seconds: None,
                              monotonic=lambda: next(times)), calls

    @staticmethod
    def pods(command):
        selector = next(part for part in command if part.startswith("--selector=app="))
        app = selector.split("=", 2)[2]
        return done(json.dumps({"items": [{"metadata": {"name": app + "-pod"}, "spec": {"nodeName": "n"},
                                           "status": {"phase": "Running"}}]}))

    def test_the_first_executor_to_reach_a_point_wins_and_the_other_is_disarmed(self):
        def handler(command):
            if "get" in command and "pods" in command:
                return self.pods(command)
            if "exec" in command and "reached" in command:
                pod = command[command.index("exec") + 1]
                return done("executor-b\n" if pod == "sentinel-executor-b-pod" else "")
            return done()
        lab, calls = self.lab(handler)
        lab.arm(acceptance.EXECUTORS, "executor.before_patch", "block")
        self.assertEqual(lab.first_reached(acceptance.EXECUTORS, "executor.before_patch"), "executor-b")
        disarmed = [command for command in calls if "disarm" in command]
        self.assertEqual([command[command.index("exec") + 1] for command in disarmed], ["sentinel-executor-a-pod"])
        armed = [command for command in calls if "arm" in command]
        self.assertEqual(len(armed), 2)
        for command in calls:
            if "exec" in command:
                self.assertEqual(command[command.index("--") + 1:command.index("--") + 3],
                                 ["python", "-c"])  # No shell: the images have none.

    def test_a_query_while_the_coordinator_is_down_is_empty_and_parameters_are_not_spliced(self):
        def handler(command):
            if "get" in command and "pods" in command:
                return done(json.dumps({"items": []}))
            return done()
        lab, _ = self.lab(handler)
        self.assertEqual(lab.query("SELECT 1"), [])

        def up(command):
            if "get" in command and "pods" in command:
                return self.pods(command)
            return done(json.dumps([{"proposal": "{}"}]))
        lab, calls = self.lab(up)
        lab.query("SELECT proposal FROM actions WHERE action_id=?", ("A'; DROP TABLE x; --",))
        [command] = [command for command in calls if "exec" in command]
        self.assertEqual(command[-2:], ["SELECT proposal FROM actions WHERE action_id=?", '["A\'; DROP TABLE x; --"]'])

    def test_the_in_pod_helpers_run_on_plain_python(self):
        import sqlite3
        import sys

        def python(script, *args):
            return subprocess.run([sys.executable, "-c", script, *args], capture_output=True, check=True,
                                  timeout=60)
        with tempfile.TemporaryDirectory() as directory:
            faults = Path(directory)
            helper = acceptance.FAULT_HELPER.replace("'/faults'", repr(str(faults)))
            (faults / "executor.before_patch.reached").write_text("executor-a")
            (faults / "executor.before_patch.release").write_text("go")
            python(helper, "arm", "executor.before_patch", "block")
            self.assertEqual(sorted(path.name for path in faults.iterdir()), ["executor.before_patch.action"])
            self.assertEqual(python(helper, "reached", "executor.before_patch").stdout.strip(), b"")
            python(helper, "disarm", "executor.before_patch")
            python(helper, "release", "executor.before_patch")
            self.assertEqual(sorted(path.name for path in faults.iterdir()), ["executor.before_patch.release"])
            database = faults / "c.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE actions(action_id TEXT, proposal TEXT)")
                connection.execute("INSERT INTO actions VALUES ('A1', '{}'), ('A2', '[]')")
            connection.close()
            rows = json.loads(python(acceptance.QUERY_HELPER, str(database),
                                     "SELECT proposal FROM actions WHERE action_id=?", '["A2"]').stdout)
            self.assertEqual(rows, [{"proposal": "[]"}])
            copy = faults / "copy.sqlite3"
            copy.write_bytes(python(acceptance.BACKUP_HELPER, str(database)).stdout)
            with sqlite3.connect(copy) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM actions").fetchone()[0], 2)
            connection.close()
            self.assertEqual(python(acceptance.BACKUP_HELPER, str(faults / "absent.db")).stdout, b"")
        with socket_listener() as port:
            self.assertEqual(python(acceptance.PROBE_HELPER, "127.0.0.1", str(port), "3").stdout.strip(), b"open")

    def test_only_sentinel_dist_worker_nodes_can_be_paused(self):
        lab, calls = self.lab(lambda command: done())
        for name in ("sentinel-dist-control-plane", "sentinel-lab-worker", "kind-worker"):
            with self.subTest(name=name), self.assertRaises(acceptance.AcceptanceError):
                lab.pause_node(name)
        lab.pause_node("sentinel-dist-worker2")
        lab.unpause_node("sentinel-dist-worker2")
        self.assertEqual(calls, [["docker", "pause", "sentinel-dist-worker2"], ["docker", "unpause",
                                                                                "sentinel-dist-worker2"]])

    def test_teardown_removes_only_the_lab_and_deletes_the_cluster_only_when_asked(self):
        runner = FakeRunner()
        shell = acceptance.Shell("kind-sentinel-dist", runner=runner)
        live.teardown(shell)
        self.assertFalse(any(command[0] == "kind" for command in runner.calls))
        [namespaces] = [command for command in runner.calls if "namespace" in command]
        self.assertEqual(namespaces[namespaces.index("namespace") + 1:namespaces.index("namespace") + 3],
                         ["sentinel-dist", "sentinel-dist-workload"])
        live.teardown(shell, delete_cluster=True)
        self.assertEqual([command for command in runner.calls if command[0] == "kind"],
                         [["kind", "delete", "cluster", "--name", "sentinel-dist"]])

    def test_the_run_view_feeds_the_invariants_from_copied_files(self):
        with tempfile.TemporaryDirectory() as directory:
            view = acceptance.RunView(directory, {"executor-a": {"delivery-token": "t" * 43}})
            records = acceptance.invariants.collect(view, terminal_in_time=True, cluster_writes=[])
            self.assertEqual(records["incidents"], [])
            self.assertFalse(acceptance.invariants.i3(records)["passed"])  # No incident read is a failure.
            self.assertEqual(records["secret_scan"]["hits"], [])


class FakeLab:
    """The Lab surface run_one uses, recording the order of operator steps."""

    def __init__(self, rows, *, fail_collect=False):
        self.rows, self.fail_collect, self.steps = rows, fail_collect, []
        self.ticks = iter(range(0, 100000, 5))

    def clock(self):
        return datetime(2026, 11, 13, 10, 0, tzinfo=UTC)

    def monotonic(self):
        return next(self.ticks)

    def inject(self):
        self.steps.append("inject")
        return {"operator_action": "inject"}

    def approve_pending(self, policy_version):
        return None

    def wait_terminal(self, timeout):
        self.steps.append("wait")
        return True

    def copy_database(self, identity, path, destination):
        if self.fail_collect:
            raise acceptance.AcceptanceError("the Coordinator Pod is not running")
        self.steps.append("copy")
        return False

    def logs(self, identity, since, destination, *, prefix=""):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(prefix, encoding="utf-8")

    def audit_rows(self, since, until):
        self.steps.append("audit")
        return self.rows, 0

    def restore(self, baseline):
        self.steps.append("restore")
        return {"operator_restore": True}


class RunOneTests(unittest.TestCase):
    def setUp(self):
        from experiments.distributed import audit
        events, _ = audit.parse((Path(__file__).resolve().parent / "fixtures" / "distributed_audit" /
                                 "one-write.jsonl").read_text(encoding="utf-8"))
        self.rows = audit.extract(events)
        self.deployed = {"policy_version": "v1", "versions": {"node_image": acceptance.NODE_IMAGE},
                         "api_server": {"endpoint": "172.18.0.4"}}
        self.tokens = {"executor-a": {"delivery-token": "q" * 43}}

    def test_a_run_injects_waits_collects_restores_and_writes_checked_evidence(self):
        lab = FakeLab(self.rows)
        with tempfile.TemporaryDirectory() as directory, patch.object(live, "git_revision", lambda: "abc123"):
            summary = live.run_one(lab, acceptance.SCENARIOS["S0"], 1, self.deployed, {}, self.tokens,
                                   Path(directory))
            [folder] = list((Path(directory) / "2026-11-13").iterdir())
            self.assertEqual(sorted(path.name for path in folder.iterdir()),
                             ["audit-extract.jsonl", "records.json", "summary.json", "timeline.jsonl",
                              "versions.json"])
            self.assertFalse((Path(directory) / "copied" / "S0-r1").exists())
            written = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(lab.steps[0], "inject")
        self.assertLess(lab.steps.index("audit"), lab.steps.index("restore"))
        self.assertEqual(written["scenario"], "S0")
        results = {item["id"]: item["passed"] for item in summary["invariants"]}
        self.assertTrue(results["I7"])
        self.assertFalse(results["I3"])  # The fake Coordinator recorded no incident: that fails, never passes.
        self.assertFalse(summary["passed"])
        self.assertEqual(summary["audit"]["sentinel_template_writes_per_action"], {"ACT-1": 1})
        self.assertEqual(summary["operator_actions"], [{"operator_action": "inject"}, {"operator_restore": True}])

    def test_the_target_is_restored_even_when_collecting_fails(self):
        lab = FakeLab(self.rows, fail_collect=True)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(acceptance.AcceptanceError):
            live.run_one(lab, acceptance.SCENARIOS["S0"], 1, self.deployed, {}, self.tokens, Path(directory))
        self.assertEqual(lab.steps[-1], "restore")

    def test_s9_neither_injects_nor_restores(self):
        lab = FakeLab([])
        with tempfile.TemporaryDirectory() as directory, patch.object(live, "git_revision", lambda: "abc123"), \
                patch.dict(live.RUNNERS, {"S9": lambda run: None}):
            summary = live.run_one(lab, acceptance.SCENARIOS["S9"], 1, self.deployed, {}, self.tokens,
                                   Path(directory))
        self.assertNotIn("inject", lab.steps)
        self.assertNotIn("restore", lab.steps)
        self.assertEqual([item["id"] for item in summary["invariants"]], ["I7"])
        self.assertTrue(summary["passed"])


class CommandLineTests(unittest.TestCase):
    def test_plan_is_offline_and_lists_every_scenario(self):
        with patch("subprocess.run", side_effect=AssertionError("no command")), \
                patch("sys.stdout", io.StringIO()) as stdout:
            self.assertEqual(acceptance.main(["plan"]), 0)
        plan = json.loads(stdout.getvalue())
        self.assertEqual([item["name"] for item in plan["scenarios"]], list(acceptance.ORDER))
        self.assertEqual(plan["chaos_mesh"], "2.8.2")

    def test_repeat_is_bounded_before_any_cluster_work(self):
        runner = FakeRunner()
        with patch("subprocess.run", runner), patch("sys.stderr", io.StringIO()):
            self.assertEqual(acceptance.main(["run", "--context", "kind-sentinel-dist", "--run-dir", "x",
                                              "--repeat", "0"]), 2)
        self.assertEqual(runner.calls, [])

    def test_run_before_deploy_is_refused(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(acceptance.AcceptanceError,
                                                                                "Run deploy first"):
            live.run(acceptance.Shell("kind-sentinel-dist", runner=FakeRunner()), directory, ["S0"], repeat=1,
                     min_free_gib=1, lock=None)

    def test_stamps_are_utc(self):
        self.assertEqual(acceptance.stamp(datetime(2026, 11, 13, 10, 0, tzinfo=UTC)), "2026-11-13T10:00:00Z")


if __name__ == "__main__":
    unittest.main()
