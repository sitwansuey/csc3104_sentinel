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
import shutil
import socket
import subprocess
import tempfile
import time
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

    def test_a_raw_proxy_read_carries_no_request_timeout_query(self):
        """Found live (P4.01 deploy): --request-timeout made the readyz proxy path /readyz?timeout=30s, which the
        demo API answers 404, so the baseline window could never pass. Other calls keep the request timeout."""
        runner = FakeRunner()
        shell = acceptance.Shell("kind-sentinel-dist", runner=runner)
        shell.kubectl("get", "--raw", "/api/v1/namespaces/w/services/demo-api:8080/proxy/readyz", check=False)
        raw = runner.calls[-1]
        self.assertEqual(raw[:2], ["kubectl", "--context=kind-sentinel-dist"])
        self.assertFalse(any(part.startswith("--request-timeout") for part in raw))
        shell.kubectl("get", "deployment", "demo-api", namespace="w", check=False)
        self.assertIn("--request-timeout=30s", runner.calls[-1])

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
        records["seals"] = [{"action_id": "A0", "producer": "executor-b"}, {"action_id": "A1", "producer": "executor-a"}]
        self.assertEqual(acceptance.scope_records(records, "2026-11-13T10:00:00Z")["seals"],
                         [{"action_id": "A1", "producer": "executor-a"}])  # An earlier run's seal is never this one's.

    def test_the_records_carry_each_accepted_seal_answer_with_its_producer_and_v0(self):
        from services.coordinator.store import SQLiteStore
        with tempfile.TemporaryDirectory() as directory:
            view = acceptance.RunView(directory, {})
            with SQLiteStore(view.paths["coordinator"] / "coordinator.db") as store:
                store.add_incident("I1", {"name": "demo-api"}, "OPEN")
                store.add_action("A1", "I1", {"based_on_resource_version": "1200", "action_id": "A1"}, "READY")
                answer = {"kind": "fence_result", "incident_id": "I1", "action_id": "A1", "epoch": 2,
                          "outcome": "SEALED", "resource_version": "1207", "target": {"name": "demo-api"},
                          "recorded_at": "2026-11-13T10:00:41Z", "schema_version": "1.0"}
                store.add_message('["fence_result","A1","2026-11-13T10:00:41Z"]', json.dumps(answer),
                                  high_water=4, answers=44, producer="executor-b", incident_id="I1")
                store.add_message('["incident","I1",""]', json.dumps({"kind": "incident", "incident_id": "I1"}),
                                  high_water=0, producer="observer-1")
            records = acceptance.invariants.collect(view, terminal_in_time=True, cluster_writes=[])
        self.assertEqual(records["seals"], [{
            "seq": 1, "event_key": '["fence_result","A1","2026-11-13T10:00:41Z"]', "producer": "executor-b",
            "incident_id": "I1", "action_id": "A1", "epoch": 2, "outcome": "SEALED", "resource_version": "1207",
            "recorded_at": "2026-11-13T10:00:41Z", "based_on_resource_version": "1200"}])


class EvidenceTests(unittest.TestCase):
    def test_the_results_table_starts_empty_and_fills_from_summaries(self):
        empty = acceptance.results_table([])
        self.assertEqual(empty.count("not yet run"), 12)
        runs = [{"scenario": "S2", "passed": True, "invariants_passed": True, "expectation": {"met": True},
                 "audit": {"max_template_writes_per_action": 1}, "takeover_seconds": 17.2,
                 "coordinator_recovery": None},
                {"scenario": "S2", "passed": False, "invariants_passed": False, "expectation": {"met": True},
                 "audit": {"max_template_writes_per_action": 2}, "takeover_seconds": 19.0,
                 "coordinator_recovery": None},
                {"scenario": "S2", "passed": False, "invariants_passed": True, "expectation": {"met": False},
                 "audit": {"max_template_writes_per_action": 1}, "takeover_seconds": None,
                 "coordinator_recovery": None},
                {"scenario": "S5", "passed": True, "invariants_passed": True, "expectation": {"met": True},
                 "audit": {"max_template_writes_per_action": 1}, "takeover_seconds": None,
                 "coordinator_recovery": {"to_ready": 7.5, "to_first_claim": 9.25, "to_next_transition": 12}}]
        table = acceptance.results_table(runs)
        self.assertIn("| S2 | 3 | 1/3 | 2/3 | 2/3 | 2 | 17.2-19.0 | - / - |", table)
        self.assertIn("| S5 | 1 | 1/1 | 1/1 | 1/1 | 1 | - | 7.5-7.5 / 9.2-9.2 |", table)
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

    def test_a_query_while_the_coordinator_is_down_is_a_transient_read_and_parameters_are_not_spliced(self):
        def handler(command):
            if "get" in command and "pods" in command:
                return done(json.dumps({"items": []}))
            return done()
        lab, _ = self.lab(handler)
        # Never [] (no incident yet): a wait asks again, and a read that stays down fails the run.
        with self.assertRaisesRegex(acceptance.TransientRead, "no running Pod"):
            lab.query("SELECT 1")

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

    def test_the_restore_patch_goes_in_a_private_file_kubectl_can_open_never_on_standard_input(self):
        """kubectl on Windows read --patch-file=- as a file named "-", so no restore happened (13 Nov 2026)."""
        from services.executor.store import fingerprint
        template = {"metadata": {"labels": {"app": "demo-api"}}, "spec": {"containers": [{"name": "demo-api"}]}}
        injected = {"metadata": {"labels": {"app": "demo-api"}},
                    "spec": {"containers": [{"name": "demo-api", "env": [{"name": "STARTUP_FAILURE", "value": "true"}]}]}}
        baseline = {"target": {"uid": "uid-1"}, "pod_template": template, "template_sha256": fingerprint(template)}
        seen = []

        def runner(command, **options):
            if "patch" in command:
                [argument] = [part for part in command if part.startswith("--patch-file")]
                path = Path(argument.split("=", 1)[1])
                seen.append({"stdin": options.get("input"), "parent": path.parent,
                             "patch": json.loads(path.read_text(encoding="utf-8"))})
                return done()
            if "get" in command:
                return done(json.dumps({"metadata": {"uid": "uid-1", "resourceVersion": "77"},
                                        "spec": {"template": injected}}))
            return done()
        with tempfile.TemporaryDirectory() as directory:
            lab = acceptance.Lab(acceptance.Shell("kind-sentinel-dist", runner=runner), directory,
                                 sleep=lambda seconds: None)
            self.assertTrue(lab.restore(baseline)["operator_restore"])
            [call] = seen
            self.assertIsNone(call["stdin"])
            self.assertEqual(call["parent"], Path(directory))
            self.assertEqual(call["patch"], [
                {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
                {"op": "test", "path": "/metadata/resourceVersion", "value": "77"},
                {"op": "replace", "path": "/spec/template", "value": template}])
            self.assertEqual(list(Path(directory).iterdir()), [])  # Removed after the call.

    def test_no_harness_command_reads_a_patch_or_file_from_a_dash_but_apply_and_create(self):
        """kubectl apply and create take -f - as standard input everywhere; patch --patch-file does not."""
        root = Path(acceptance.__file__).resolve().parent
        for path in sorted(root.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn('"--patch-file=-"', text)
                self.assertNotIn('"--patch-file", "-"', text)

    def test_deploy_applies_the_overlay_without_the_workload_it_pinned(self):
        text = (acceptance.MANIFESTS / "kustomization.yaml").read_text(encoding="utf-8")
        overlay = live.overlay_without_workload(text)
        self.assertNotIn("workload.yaml", overlay)
        self.assertEqual(overlay.count("\n"), text.count("\n") - 1)
        for other in ("coordinator.yaml", "planner.yaml", "namespaces.yaml", "rbac.yaml"):
            self.assertIn("- " + other, overlay)
        with self.assertRaises(acceptance.AcceptanceError):
            live.overlay_without_workload(overlay)

    def test_deploy_applies_sentinel_without_the_workload_and_refuses_a_moved_baseline(self):
        template = {"spec": {"containers": [{"name": "demo-api", "image": "demo-api@sha256:" + "a" * 64}]}}

        class Shell:
            def __init__(self, manifests):
                self.manifests, self.calls, self.applied = manifests, [], None

            def kubectl(self, *args, **options):
                self.calls.append(args)
                if args[0] == "apply":
                    self.applied = (self.manifests / "kustomization.yaml").read_text(encoding="utf-8")

        class Lab:
            def __init__(self, live):
                self.live = live

            def target(self):
                return {"spec": {"template": self.live}}
        for current, raises in ((template, False), ({"spec": {"containers": [{"name": "demo-api"}]}}, True)):
            with self.subTest(raises=raises), tempfile.TemporaryDirectory() as directory:
                manifests = Path(directory) / "manifests"
                shutil.copytree(acceptance.MANIFESTS, manifests)
                shell = Shell(manifests)
                if raises:
                    with self.assertRaises(acceptance.AcceptanceError):
                        live.apply_sentinel(shell, Lab(current), manifests, template)
                else:
                    live.apply_sentinel(shell, Lab(current), manifests, template)
                self.assertEqual(shell.calls[0], ("apply", "--kustomize=" + str(manifests)))
                self.assertNotIn("workload.yaml", shell.applied)
                self.assertIn("- coordinator.yaml", shell.applied)
                self.assertEqual(len(shell.calls), 1 + len(acceptance.DEPLOYMENTS))
                # The repository's overlay is untouched: only the run directory's copy changed.
                self.assertIn("- workload.yaml", (acceptance.MANIFESTS / "kustomization.yaml").read_text(
                    encoding="utf-8"))

    def test_teardown_deletes_nothing_through_a_context_that_names_another_cluster(self):
        elsewhere = done(json.dumps({"clusters": [{"name": "kind-sentinel-dist", "cluster": {
            "server": "https://10.0.0.5:6443"}}]}))
        for overrides in ({"view": elsewhere}, {"nodes": done(json.dumps({"items": NODES[:3]}))}):
            with self.subTest(changed=list(overrides)):
                runner = FakeRunner(**overrides)
                with self.assertRaises(acceptance.AcceptanceError):
                    live.teardown(acceptance.Shell("kind-sentinel-dist", runner=runner), delete_cluster=True)
                self.assertFalse([command for command in runner.calls if "delete" in command])

    def test_teardown_removes_only_the_lab_and_deletes_the_cluster_only_when_asked(self):
        runner = FakeRunner()
        shell = acceptance.Shell("kind-sentinel-dist", runner=runner)
        self.assertEqual(live.teardown(shell)["checked"]["cluster"], "kind-sentinel-dist")
        self.assertLess(next(index for index, command in enumerate(runner.calls) if "view" in command),
                        next(index for index, command in enumerate(runner.calls) if "delete" in command))
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


def audit_row(effect, *, pod="sentinel-executor-a-7c8d9e6f5-a1a1a", at="2026-11-13T10:00:31Z", code=200,
              generation=None, action_id="ACT-1", sentinel=True):
    return {"audit_id": "x", "at": at, "verb": "patch",
            "user": acceptance.audit.EXECUTOR_USER if sentinel else "<operator>", "sentinel": sentinel, "pod": pod,
            "node": None, "resource": "deployments", "subresource": None, "namespace": acceptance.WORKLOAD,
            "name": acceptance.TARGET, "code": code, "reason": None, "decision": None, "admission_denied": False,
            "dry_run": False, "effect": effect, "paths": [],
            "marker": {"template_write": "write", "fence": "fence"}.get(effect, "none"), "action_id": action_id,
            "fence_epoch": None, "resource_version": None, "generation": generation}


B_POD = "sentinel-executor-b-5d6e7f8a9-b2b2b"


class ExpectationTests(unittest.TestCase):
    """Each scenario's expectation is checked, and fails on records that the write invariants pass."""

    def check(self, name, states, rows, **observed):
        return acceptance.expectation(name, incidents=[{"incident_id": f"I{index}", "state": state}
                                                       for index, state in enumerate(states)], rows=rows, **observed)

    def write_invariants_pass(self, states, rows):
        records = {"audit": rows, "audit_completeness": [],
                   "cluster_writes": acceptance.audit.template_writes(rows),
                   "incidents": [{"incident_id": f"I{index}", "state": state} for index, state in enumerate(states)],
                   "terminal_in_time": True}
        for check in (acceptance.invariants.i1, acceptance.invariants.i3, acceptance.invariants.i7):
            self.assertTrue(check(records)["passed"], check(records))

    def test_a_recovery_scenario_needs_recovered_and_exactly_one_write(self):
        one = [audit_row("template_write", generation=3)]
        self.assertEqual(self.check("S0", ["RECOVERED"], one), [])
        self.write_invariants_pass(["FAILED"], [])
        self.assertEqual(self.check("S0", ["FAILED"], []),
                         ["no incident RECOVERED (states: FAILED)", "0 Sentinel template write(s), expected exactly 1"])
        self.write_invariants_pass(["RECOVERED", "REJECTED"], [])
        self.assertEqual(self.check("S4", ["RECOVERED", "REJECTED"], []),
                         ["0 Sentinel template write(s), expected exactly 1"])

    def test_takeover_rto_and_the_paused_holder_are_checked(self):
        one = [audit_row("template_write", generation=3)]
        self.assertEqual(self.check("S1b", ["RECOVERED"], one, takeover=None),
                         ["no claim by the other Executor after the fault: takeover not measured"])
        self.assertEqual(self.check("S1c", ["RECOVERED"], one, takeover=4.2), [])
        self.assertEqual(self.check("S5", ["RECOVERED"], one, recovery={"to_ready": None}),
                         ["the Coordinator's return to Ready was not measured"])
        self.assertEqual(self.check("S5", ["RECOVERED"], one, recovery={"to_ready": 6.0}), [])
        self.write_invariants_pass(["RECOVERED"], one)
        self.assertEqual(self.check("S2", ["RECOVERED"], one, stalled="executor-a"),
                         ["the paused holder's Pod wrote the template"])
        self.assertEqual(self.check("S2", ["RECOVERED"], [audit_row("template_write", pod=B_POD)],
                                    stalled="executor-a"), [])

    def test_s3_and_s6_need_every_incident_terminal(self):
        self.assertEqual(self.check("S3", ["VERIFYING"], []), ["not every incident terminal (states: VERIFYING)"])
        self.assertEqual(self.check("S3", ["FAILED"], []), [])
        self.assertEqual(self.check("S6", ["RECOVERED"], [audit_row("template_write")]),
                         ["template writes during the partition not recorded as 0"])
        self.assertEqual(self.check("S6", ["RECOVERED"], [audit_row("template_write")],
                                    notes={"during_partition": {"template_writes": 0}}), [])

    def test_s7_needs_failed_and_no_write(self):
        one = [audit_row("template_write")]
        self.write_invariants_pass(["RECOVERED"], one)
        self.assertEqual(self.check("S7", ["RECOVERED"], one),
                         ["expected FAILED and none RECOVERED (states: RECOVERED)",
                          "1 Sentinel template write(s), expected 0"])
        self.assertEqual(self.check("S7", ["FAILED"], []), [])

    def s8_rows(self):
        return [audit_row("template_write", pod=None, sentinel=False, generation=2, at="2026-11-13T10:00:00Z"),
                audit_row("fence", pod=B_POD, generation=3, at="2026-11-13T10:00:40Z", action_id="ACT-1"),
                audit_row("denied", code=422, at="2026-11-13T10:00:49Z")]

    S8_KEY = '["fence_result","ACT-1","2026-11-13T10:00:41.100000Z"]'

    def s8_records(self, **seal):
        """The Coordinator's records of a seal by executor-b that sent nothing (advanced without evidence)."""
        return {"actions": [{"action_id": "ACT-1", "incident_id": "I0", "state": "FAILED"}],
                "outbox": [{"id": 41, "topic": "executor.request", "action_id": "ACT-1", "delivered": 1},
                           {"id": 44, "topic": "executor.seal", "action_id": "ACT-1", "delivered": 1}],
                "receipts": [{"event_id": 41, "delivery_id": "d41", "owner": "executor-a", "attempts": 1,
                              "finished": 1.0, "result_digest": None},
                             {"event_id": 44, "delivery_id": "d44", "owner": "executor-b", "attempts": 1,
                              "finished": 2.0, "result_digest": "e" * 64}],
                "transitions": [{"seq": 9, "incident_id": "I0", "action_id": "ACT-1", "from_state": "VALIDATING",
                                 "to_state": "FAILED", "cause": self.S8_KEY,
                                 "recorded_at": "2026-11-13T10:00:41.300000Z"}],
                "seals": [dict({"seq": 30, "event_key": self.S8_KEY, "producer": "executor-b", "incident_id": "I0",
                                "action_id": "ACT-1", "epoch": 2, "outcome": "SEALED", "resource_version": "1207",
                                "recorded_at": "2026-11-13T10:00:41.100000Z",
                                "based_on_resource_version": "1200"}, **seal)]}

    def s8(self, rows, records=None, stalled="executor-a", generation_at_end=3, states=("FAILED",)):
        return self.check("S8", list(states), rows, stalled=stalled, generation_at_end=generation_at_end,
                          records=records)

    NO_SEAL = ("no seal by the other Executor: no fence from its Pod in the audit log, and no Coordinator record of "
               "its seal (executor.seal delivery, fence_result SEALED past V0, action FAILED on it)")
    NO_PATCH = ("the holder's Pod sent no PATCH to the target after the seal: its stale PATCH never reached the API "
                "server, so the race S8 is for was not exercised")
    NOT_REFUSED = "no PATCH from the holder's Pod refused (409 or 422) after the seal"

    def test_s8_passes_on_a_fence_by_the_other_executor_then_the_holders_refused_patch(self):
        rows = self.s8_rows()
        self.write_invariants_pass(["FAILED"], rows)
        self.assertEqual(self.s8(rows), [])
        self.assertEqual(acceptance.s8_seal(rows, None, "executor-a"),
                         {"path": "fenced", "sealer": "executor-b", "at": "2026-11-13T10:00:40Z", "generation": 3})
        self.assertEqual(self.s8(rows, self.s8_records()), [])  # The audit's fence decides the path.
        self.assertEqual(acceptance.s8_seal(rows, self.s8_records(), "executor-a")["path"], "fenced")

    def test_s8_passes_on_records_of_a_seal_past_v0_with_nothing_sent_then_the_holders_refused_patch(self):
        for code in (409, 422):
            with self.subTest(code=code):
                rows = [self.s8_rows()[0], audit_row("denied", code=code, at="2026-11-13T10:00:49Z")]
                self.write_invariants_pass(["FAILED"], rows)
                self.assertEqual(self.s8(rows, self.s8_records(), generation_at_end=2), [])
                self.assertEqual(acceptance.s8_seal(rows, self.s8_records(), "executor-a"),
                                 {"path": "advanced_without_evidence", "sealer": "executor-b",
                                  "at": "2026-11-13T10:00:41.300000Z", "generation": None})

    def test_s8_fails_without_proof_of_a_seal_by_the_other_executor(self):
        rows = [self.s8_rows()[0], self.s8_rows()[2]]
        self.write_invariants_pass(["FAILED"], rows)
        self.assertEqual(self.s8(rows, generation_at_end=2), [self.NO_SEAL])
        records = self.s8_records()
        unproven = {
            "an APPLIED seal": dict(self.s8_records(outcome="APPLIED")),
            "an UNSEALED seal": dict(self.s8_records(outcome="UNSEALED")),
            "a seal still at V0": dict(self.s8_records(resource_version="1200")),
            "a seal with no version": dict(self.s8_records(resource_version=None)),
            "a seal answer from a worker that is not an Executor": dict(self.s8_records(producer="guard-1")),
            "no executor.seal delivery": dict(records, outbox=records["outbox"][:1]),
            "a delivery another worker owns": dict(records, receipts=[records["receipts"][0],
                                                                      dict(records["receipts"][1], owner="guard-1")]),
            "a delivery never finished": dict(records, receipts=[records["receipts"][0],
                                                                 dict(records["receipts"][1], finished=None)]),
            "a delivery with no result": dict(records, receipts=[records["receipts"][0],
                                                                 dict(records["receipts"][1], result_digest=None)]),
            "a FAILED caused by something else": dict(records, transitions=[
                dict(records["transitions"][0], cause='["execution_result","ACT-1","2026-11-13T10:00:41Z"]')]),
            "no move to FAILED": dict(records, transitions=[dict(records["transitions"][0], to_state="NEEDS_REVIEW")]),
        }
        for name, changed in unproven.items():
            with self.subTest(case=name):
                self.assertIsNone(acceptance.s8_seal(rows, changed, "executor-a"))
                self.assertIn(self.NO_SEAL, self.s8(rows, changed, generation_at_end=2))

    def test_s8_fails_when_the_holder_sealed_its_own_action(self):
        rows = self.s8_rows()
        by_holder = [rows[0], dict(rows[1], pod="sentinel-executor-a-7c8d9e6f5-a1a1a"), rows[2]]
        self.assertEqual(self.s8(by_holder), ["a fence from the holder's own Pod: the holder sealed", self.NO_SEAL])
        records = self.s8_records(producer="executor-a")
        records["receipts"][1]["owner"] = "executor-a"
        self.assertEqual(self.s8([rows[0], rows[2]], records, generation_at_end=2),
                         ["the holder's own fence_result was accepted: the holder sealed", self.NO_SEAL])
        self.assertIsNone(acceptance.s8_seal([rows[0], rows[2]], records, "executor-a"))

    def test_s8_fails_when_the_holders_stale_patch_never_reached_the_api_server(self):
        rows = self.s8_rows()
        for name, records, changed, generation in (
                ("fenced", None, rows[:2], 3),
                ("advanced_without_evidence", self.s8_records(), rows[:1], 2),
                ("refused only before the seal", None, [rows[0], rows[1], dict(rows[2], at="2026-11-13T10:00:39Z")], 3),
                ("a GET only", None, [*rows[:2], dict(rows[2], verb="get", effect="request", code=200)], 3)):
            with self.subTest(case=name):
                self.write_invariants_pass(["FAILED"], changed)
                self.assertEqual(self.s8(changed, records, generation_at_end=generation), [self.NO_PATCH])

    def test_s8_fails_when_the_holder_wrote_or_its_patch_was_not_refused_by_the_api_server(self):
        rows = self.s8_rows()
        wrote = [rows[0], rows[1], audit_row("template_write", at="2026-11-13T10:00:49Z", generation=4)]
        self.assertEqual(self.s8(wrote, generation_at_end=4), [
            "1 Sentinel template write(s), expected 0", "the holder's Pod changed the target", self.NOT_REFUSED,
            "the target's generation is 4 at the end and 3 after the seal: something changed it after the seal"])
        records_wrote = [rows[0], dict(wrote[2])]
        self.assertIn("the holder's Pod changed the target", self.s8(records_wrote, self.s8_records(),
                                                                       generation_at_end=4))
        forbidden = [rows[0], rows[1], dict(rows[2], code=403)]
        self.assertEqual(self.s8(forbidden), [self.NOT_REFUSED])
        failed = [rows[0], rows[1], dict(rows[2], effect="failed", code=500)]
        self.assertEqual(self.s8(failed), [self.NOT_REFUSED])

    def test_s8_needs_the_target_unchanged_after_the_seal_and_not_recovered(self):
        rows = self.s8_rows()
        self.assertEqual(self.s8(rows, generation_at_end=4),
                         ["the target's generation is 4 at the end and 3 after the seal: something changed it after "
                          "the seal"])
        advanced = [rows[0], rows[2]]
        self.assertEqual(self.s8(advanced, self.s8_records(), generation_at_end=3),
                         ["the target's generation is 3 at the end and 2 after the seal: something changed it after "
                          "the seal"])
        self.assertEqual(self.s8(rows, states=("RECOVERED",)), ["expected not RECOVERED (states: RECOVERED)"])
        self.assertEqual(self.s8(rows, stalled=None), ["no paused holder was recorded"])
        self.assertIsNone(acceptance.s8_seal(rows, self.s8_records(), None))

    def test_s9_needs_every_probe_and_no_write(self):
        self.assertEqual(self.check("S9", [], [], notes={"probes": []}), ["not every denial probe behaved as expected"])
        self.assertEqual(self.check("S9", [], [], notes={"probes": [{"passed": True}, {"passed": False}]}),
                         ["not every denial probe behaved as expected"])
        self.assertEqual(self.check("S9", [], [], notes={"probes": [{"passed": True}]}), [])

    def test_every_scenario_states_what_its_expectation_checks(self):
        for name, scenario in acceptance.SCENARIOS.items():
            with self.subTest(name=name):
                self.assertTrue(acceptance.expectation(name, incidents=[], rows=[]), name)  # Nothing is never met.
                self.assertNotIn("replayed", scenario.expect)


class ClockTests(unittest.TestCase):
    def test_the_offset_is_taken_at_the_midpoint_with_half_the_round_trip_as_error(self):
        before = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
        after = datetime(2026, 11, 13, 10, 0, 0, 200000, tzinfo=UTC)
        offset = acceptance.clock_offset(before, f"{before.timestamp() + 3.1:.3f}\n", after)
        self.assertEqual((offset["offset_seconds"], offset["uncertainty_seconds"]), (3.0, 0.1))
        for text in ("", "Thu Nov 13", None):
            with self.subTest(text=text), self.assertRaises(acceptance.AcceptanceError):
                acceptance.clock_offset(before, text, after)
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.clock_offset(after, "1", before)

    def test_the_window_moves_onto_the_cluster_clock_and_widens_by_every_offset(self):
        offsets = [{"offset_seconds": -2.0, "uncertainty_seconds": 0.1},
                   {"offset_seconds": 1.0, "uncertainty_seconds": 0.2}]
        self.assertEqual(acceptance.control_plane_window("2026-11-13T10:00:00Z", "2026-11-13T10:05:00Z", offsets),
                         ("2026-11-13T09:59:57.900000Z", "2026-11-13T10:05:01.200000Z"))
        self.assertEqual(acceptance.control_plane_window("2026-11-13T10:00:00Z", None, offsets[:1]),
                         ("2026-11-13T09:59:57.900000Z", None))
        self.assertEqual(acceptance.control_plane_window("a", "b", []), ("a", "b"))


class AuditReadTests(unittest.TestCase):
    def lab(self, listing, text="", date="1794563400.5\n"):
        calls = []

        def runner(command, **options):
            calls.append(list(command))
            if "find" in command:
                return done(listing)
            if "cat" in command:
                return done(text)
            if "date" in command:
                return done(date)
            return done()
        clock = iter([datetime(2026, 11, 13, 10, 0, tzinfo=UTC), datetime(2026, 11, 13, 10, 0, 1, tzinfo=UTC)])
        lab = acceptance.Lab(acceptance.Shell("kind-sentinel-dist", runner=runner), tempfile.gettempdir(),
                             clock=lambda: next(clock))
        return lab, calls

    def test_the_rotated_backups_are_read_with_the_live_log_and_the_first_event_is_reported(self):
        fixtures = Path(__file__).resolve().parent / "fixtures" / "distributed_audit"
        listing = "\n".join(["/var/log/kubernetes/kube-apiserver-audit.log",
                             "/var/log/kubernetes/kube-apiserver-audit-2026-11-13T10-00-00.000.log",
                             "/var/log/kubernetes/kube-apiserver-audit-2026-11-13T09-00-00.000.log",
                             "/var/log/kubernetes/kube-apiserver-audit.log; rm -rf /", "/etc/passwd"]) + "\n"
        lab, calls = self.lab(listing, (fixtures / "one-write.jsonl").read_text(encoding="utf-8"))
        found = lab.audit_log("2026-11-13T10:00:30Z", None)
        [cat] = [command for command in calls if "cat" in command]
        self.assertEqual(cat[cat.index("cat") + 1:], [
            "/var/log/kubernetes/kube-apiserver-audit-2026-11-13T09-00-00.000.log",
            "/var/log/kubernetes/kube-apiserver-audit-2026-11-13T10-00-00.000.log",
            "/var/log/kubernetes/kube-apiserver-audit.log"])
        self.assertEqual((found["files"], found["skipped"], found["first_at"]),
                         (3, 1, "2026-11-13T10:00:00.100000Z"))
        self.assertEqual([row["audit_id"][-2:] for row in found["rows"]], ["03", "04", "05", "06", "07", "08", "09"])

    def test_without_the_live_log_nothing_is_read(self):
        lab, calls = self.lab("/var/log/kubernetes/kube-apiserver-audit-2026-11-13T10-00-00.000.log\n")
        with self.assertRaisesRegex(acceptance.AcceptanceError, "audit log is missing"):
            lab.audit_rows("2026-11-13T10:00:00Z", None)
        self.assertFalse([command for command in calls if "cat" in command])

    def test_the_control_plane_clock_is_read_inside_its_container(self):
        lab, calls = self.lab("")
        offset = lab.clock_offset()
        self.assertEqual(calls, [["docker", "exec", "sentinel-dist-control-plane", "date", "+%s.%N"]])
        self.assertEqual(offset["uncertainty_seconds"], 0.5)


class FakeLab:
    """The Lab surface run_one uses, recording the order of operator steps."""

    def __init__(self, rows, *, fail_collect=False, fail_wait=False, generations=(1, 4, 4),
                 first_at="2026-11-13T09:00:00Z", offset=0.0):
        self.rows, self.fail_collect, self.fail_wait, self.steps = rows, fail_collect, fail_wait, []
        self.ticks = iter(range(0, 100000, 5))
        self.generations, self.first_at, self.offset = iter(generations), first_at, offset
        self.window, self.approvals, self.read_outages, self.approval_attempts = None, [], [], []

    def begin_run(self, baseline, policy=None):
        self.steps.append("begin")
        return frozenset({"INC-EARLIER"})

    def settle(self, baseline, *, leftovers=None):
        self.steps.append("settle")
        return {"settled": True, "seconds": 20.0, "target": [], "open": [], "leftovers": leftovers()}

    def clock_offset(self):
        self.steps.append("offset")
        return {"offset_seconds": self.offset, "uncertainty_seconds": 0.01, "at": "2026-11-13T10:00:00Z"}

    def target(self):
        self.steps.append("target")
        return {"metadata": {"uid": "uid-demo", "generation": next(self.generations)}}

    def audit_log(self, since, until):
        self.steps.append("audit")
        self.window = (since, until)
        return {"rows": self.rows, "skipped": 0, "first_at": self.first_at, "files": 2}

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
        if self.fail_wait:
            raise acceptance.AcceptanceError("the Coordinator Pod is not running")
        return True

    def copy_database(self, identity, path, destination):
        if self.fail_collect:
            raise acceptance.AcceptanceError("the Coordinator Pod is not running")
        self.steps.append("copy")
        return False

    def logs(self, identity, since, destination, *, prefix=""):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(prefix, encoding="utf-8")

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
        self.assertEqual(lab.steps[:4], ["offset", "target", "begin", "inject"])
        # The generation is read before the restore; the lab settles before anything is copied; the generation
        # is read again after the settle, and the log is read last.
        self.assertEqual(lab.steps[lab.steps.index("wait"):lab.steps.index("wait") + 4],
                         ["wait", "target", "restore", "settle"])
        self.assertLess(lab.steps.index("settle"), lab.steps.index("copy"))
        self.assertEqual(lab.steps[-4:], ["copy", "target", "offset", "audit"])
        self.assertEqual((written["earlier_incidents"], written["settle"]["settled"]), (1, True))
        self.assertEqual(written["scenario"], "S0")
        self.assertEqual(written["audit_completeness"]["complete"], True)
        self.assertEqual(written["audit_completeness"]["generation"], {"before": 1, "before_restore": 4, "after": 4})
        results = {item["id"]: item["passed"] for item in summary["invariants"]}
        self.assertTrue(results["I7"])
        self.assertEqual(summary["expectation"]["met"], False)  # No incident RECOVERED in the fake Coordinator.
        self.assertIn("no incident RECOVERED (states: none)", summary["expectation"]["failures"])
        self.assertFalse(results["I3"])  # The fake Coordinator recorded no incident: that fails, never passes.
        self.assertFalse(summary["passed"])
        self.assertEqual(summary["audit"]["sentinel_template_writes_per_action"], {"ACT-1": 1})
        self.assertEqual(summary["operator_actions"], [{"operator_action": "inject"}, {"operator_restore": True}])

    def test_the_target_is_restored_even_when_waiting_or_collecting_fails(self):
        for options in ({"fail_collect": True}, {"fail_wait": True}):
            with self.subTest(**options):
                lab = FakeLab(self.rows, **options)
                with tempfile.TemporaryDirectory() as directory, self.assertRaises(acceptance.AcceptanceError):
                    live.run_one(lab, acceptance.SCENARIOS["S0"], 1, self.deployed, {}, self.tokens,
                                 Path(directory))
                self.assertIn("restore", lab.steps)
                self.assertNotIn("copy", lab.steps)

    def test_s6_approves_by_hand_during_the_partition_and_waiting_actions_only_after_it(self):
        """S6 with --stale-replans: a re-plan after the partition waits for approval and still gets its operator."""
        self.assertEqual(acceptance.SCENARIOS["S6"].options["auto_approve"], acceptance.AFTER_FAULT)
        for name, expected in (("S6", ["runner", "approver"]), ("S0", ["approver", "runner"]),
                               ("S9", ["runner"])):
            with self.subTest(scenario=name):
                lab = FakeLab(self.rows)
                with tempfile.TemporaryDirectory() as directory, \
                        patch.object(live, "git_revision", lambda: "abc123"), \
                        patch.object(live, "_operator",
                                     lambda lab, version, stopping, record: lab.steps.append("approver")), \
                        patch.dict(live.RUNNERS, {name: lambda run: (time.sleep(0.2), run.lab.steps.append("runner"))}):
                    live.run_one(lab, acceptance.SCENARIOS[name], 1, self.deployed, {}, self.tokens, Path(directory))
                self.assertEqual([step for step in lab.steps if step in ("runner", "approver")], expected)

    def test_s9_neither_injects_nor_restores(self):
        lab = FakeLab([])
        with tempfile.TemporaryDirectory() as directory, patch.object(live, "git_revision", lambda: "abc123"), \
                patch.dict(live.RUNNERS, {"S9": lambda run: None}):
            summary = live.run_one(lab, acceptance.SCENARIOS["S9"], 1, self.deployed, {}, self.tokens,
                                   Path(directory))
        self.assertNotIn("inject", lab.steps)
        self.assertNotIn("restore", lab.steps)
        self.assertEqual([item["id"] for item in summary["invariants"]], ["I7"])
        self.assertEqual(summary["expectation"]["failures"], ["not every denial probe behaved as expected"])
        self.assertFalse(summary["passed"])  # The stub recorded no probes: that is not a pass.

    def test_s9_with_every_probe_as_expected_passes(self):
        lab = FakeLab([], generations=(4, 4, 4))

        def probes(run):
            run.notes["probes"] = [{"probe": "p", "passed": True}]
        with tempfile.TemporaryDirectory() as directory, patch.object(live, "git_revision", lambda: "abc123"), \
                patch.dict(live.RUNNERS, {"S9": probes}):
            summary = live.run_one(lab, acceptance.SCENARIOS["S9"], 1, self.deployed, {}, self.tokens,
                                   Path(directory))
        self.assertTrue(summary["passed"], summary)

    def test_an_empty_or_short_audit_extract_fails_the_run_instead_of_counting_no_write(self):
        cases = {"empty": FakeLab([], first_at=None), "a generation unexplained": FakeLab(self.rows,
                                                                                          generations=(1, 6, 6)),
                 "rotated": FakeLab(self.rows, first_at="2026-11-13T10:00:00Z")}
        for label, lab in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory, \
                    patch.object(live, "git_revision", lambda: "abc123"):
                summary = live.run_one(lab, acceptance.SCENARIOS["S0"], 1, self.deployed, {}, self.tokens,
                                       Path(directory))
                results = {item["id"]: item for item in summary["invariants"]}
                self.assertFalse(summary["audit_completeness"]["complete"])
                self.assertFalse(results["I7"]["passed"])
                self.assertIn("audit extract incomplete", results["I7"]["detail"])
                self.assertFalse(results["I1"]["passed"])
                self.assertFalse(summary["passed"])

    def test_the_window_and_the_fault_time_move_onto_the_clusters_clock(self):
        lab = FakeLab(self.rows, offset=-2.5)

        def fault(run):
            run.stalled = "executor-a"
            run.mark()
        with tempfile.TemporaryDirectory() as directory, patch.object(live, "git_revision", lambda: "abc123"), \
                patch.dict(live.RUNNERS, {"S1b": fault}):
            summary = live.run_one(lab, acceptance.SCENARIOS["S1b"], 1, self.deployed, {}, self.tokens,
                                   Path(directory))
        self.assertEqual(summary["fault_at"], "2026-11-13T10:00:00Z")
        self.assertEqual(summary["clock"]["fault_at_cluster_clock"], "2026-11-13T09:59:57.500000Z")
        self.assertEqual(lab.window, ("2026-11-13T09:59:56.490000Z", "2026-11-13T09:59:58.510000Z"))
        self.assertIn("no claim by the other Executor after the fault: takeover not measured",
                      summary["expectation"]["failures"])

    def test_run_refuses_a_target_whose_uid_is_not_the_one_deploy_bound(self):
        class Target:
            def __init__(self, uid):
                self.uid = uid

            def target(self):
                return {"metadata": {"uid": self.uid}}
        baseline = {"target": {"uid": "uid-demo"}}
        self.assertEqual(live.check_target(Target("uid-demo"), baseline), "uid-demo")
        for uid, recorded in (("uid-other", baseline), ("uid-demo", {}), (None, baseline)):
            with self.subTest(uid=uid), self.assertRaisesRegex(acceptance.AcceptanceError, "UID differs"):
                live.check_target(Target(uid), recorded)
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "lab.json").write_text(json.dumps(self.deployed), encoding="utf-8")
            (run_dir / "inputs").mkdir()
            (run_dir / "inputs" / "baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
            runner = FakeRunner()
            with patch.object(live, "preflight", lambda *args, **options: {}), \
                    self.assertRaisesRegex(acceptance.AcceptanceError, "UID differs"):
                live.run(acceptance.Shell("kind-sentinel-dist", runner=runner), run_dir, ["S0"], repeat=1,
                         min_free_gib=1, lock=None, lab=Target("uid-recreated"))
            self.assertFalse([command for command in runner.calls if "secret" in command])


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
