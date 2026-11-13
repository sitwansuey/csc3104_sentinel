"""P5.05: guarded recovery on the protected template, offline.

The reviewed sidecar policy is derived from the security lab's own overlay;
the Guard reader's RBAC is generated from the protected template; the
Executor's rollback restores the whole protected template, sidecar included,
with one write; the harness probes through the gateway with the Verifier's
identity; and the staged live recipe refuses until its preconditions hold.
Nothing here contacts a cluster.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import lab_lock
from experiments.boutique.harness import protected
from experiments.boutique.harness.protected import (GUARD_READER_RBAC, ProtectedBoutiqueExperiment, guard_reader_rbac,
                                                    preflight, protected_ceilings, protected_policy_constraints, rbac_matrix,
                                                    reviewed_references, template_references)
from experiments.validation.demo_guard import load_fixtures
from runtime.live_executor import build_adapters
from scripts.security import network_policy
from scripts.security.boutique_security_manifest import (GATEWAY_SERVICES, INGRESS_CONTAINERS, NAMESPACE,
                                                         VERIFIER_IDENTITY, VERIFIER_PROBE_APP, overlay,
                                                         reviewed_sidecars, verifier_probe_pod)
from scripts.security.security_lab import CLIENT, ENVOY, ROUTES
from services.executor.core import execute
from services.guard.core import proposal_fingerprint, template_sha256
from services.guard.decisions import validate_policy
from tests.support.executor_fake import FakeDeploymentAPI
from tests.test_security_connected import APPLICATIONS, base, deployment, lab_items

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=UTC)


def lab_manifest():
    return overlay(base(), envoy_image=ENVOY, inspector_image="sentinel.local/security-inspector@sha256:" + "d" * 64)


def protected_template(name="shippingservice"):
    return deepcopy(deployment(lab_manifest(), name)["spec"]["template"])


class ReviewedSidecarTests(unittest.TestCase):
    def test_the_reviewed_entries_are_exactly_the_overlays_ingress_sidecars(self):
        manifest = lab_manifest()
        entries = {entry["target"]: entry for entry in reviewed_sidecars(ENVOY)}
        self.assertEqual(set(entries), {spec["service"] for spec in ROUTES.values()})
        for route, spec in ROUTES.items():
            with self.subTest(route=route):
                pod = deployment(manifest, spec["service"])["spec"]["template"]["spec"]
                sidecar = next(item for item in pod["containers"] if item["name"] == INGRESS_CONTAINERS[route])
                entry = entries[spec["service"]]
                self.assertEqual((entry["name"], entry["image"], entry["command"], entry["args"]),
                                 (sidecar["name"], sidecar["image"], sidecar["command"], sidecar["args"]))
                volumes = {volume["name"]: volume for volume in pod["volumes"]}
                actual = []
                for mount in sidecar["volumeMounts"]:
                    self.assertIs(mount["readOnly"], True)
                    volume = volumes[mount["name"]]
                    source = ({"kind": "Secret", "name": volume["secret"]["secretName"]} if "secret" in volume
                              else {"kind": "ConfigMap", "name": volume["configMap"]["name"]})
                    actual.append({"volume": mount["name"], "mount_path": mount["mountPath"], "source": source})
                self.assertEqual(actual, entry["mounts"])

    def test_the_protected_policy_is_a_valid_live_policy_and_omits_the_inspector_registry(self):
        constraints = protected_policy_constraints(["shippingservice"])
        self.assertEqual(constraints["allowed_image_registries"], ["us-central1-docker.pkg.dev", "docker.io"])
        self.assertEqual([entry["target"] for entry in constraints["allowed_sidecars"]], ["shippingservice"])
        self.assertEqual(constraints["allowed_sidecars"][0]["image"], ENVOY)
        policy = deepcopy(LIVE_POLICY)
        policy["security_constraints"].update(constraints)
        validate_policy(policy, require_live=True)

    def test_the_protected_ceilings_raise_only_what_the_reviewed_sidecar_needs(self):
        from experiments.boutique.harness import targets
        from services.guard.common import quantity
        catalog = targets.profile("shippingservice").ceilings
        raised = protected_ceilings(catalog)
        self.assertEqual(raised["container_limits"], {"cpu": "500m", "memory": "192Mi"})
        self.assertEqual({key: raised[key] for key in raised if key != "container_limits"},
                         {key: catalog[key] for key in catalog if key != "container_limits"})
        for key in ("container_requests", "container_limits", "pod_requests", "rollout_requests"):
            for resource in ("cpu", "memory"):
                self.assertGreaterEqual(quantity(raised[key][resource], resource, key),
                                        quantity(catalog[key][resource], resource, key))
        sidecar = next(item for item in protected_template()["spec"]["containers"] if item["name"] == "shipping-ingress")
        for side, key in (("requests", "container_requests"), ("limits", "container_limits")):
            for resource in ("cpu", "memory"):
                self.assertLessEqual(quantity(sidecar["resources"][side][resource], resource, side),
                                     quantity(raised[key][resource], resource, key))
        # A larger Pod request ceiling is raised to the container ceiling plus the sidecar, never lowered.
        small = deepcopy(catalog)
        small["pod_requests"] = {"cpu": "100m", "memory": "64Mi"}
        self.assertEqual(protected_ceilings(small)["pod_requests"], {"cpu": "120m", "memory": "112Mi"})

    def test_the_envoy_image_is_pinned_by_digest(self):
        self.assertRegex(ENVOY, r"^envoyproxy/envoy@sha256:[0-9a-f]{64}$")
        self.assertRegex(CLIENT, r"^fullstorydev/grpcurl@sha256:[0-9a-f]{64}$")


class GuardReaderTests(unittest.TestCase):
    def test_the_committed_rbac_is_generated_from_the_protected_template(self):
        references = template_references(protected_template(), NAMESPACE)
        self.assertEqual(references, reviewed_references())
        self.assertEqual([(item["kind"], item["name"]) for item in references],
                         [("ConfigMap", "security-upstream"), ("Secret", "security-upstream")])
        self.assertEqual(GUARD_READER_RBAC.read_text(encoding="utf-8").replace("\r\n", "\n"),
                         guard_reader_rbac(NAMESPACE, references))

    def test_the_reader_role_grants_get_on_named_objects_only(self):
        text = guard_reader_rbac(NAMESPACE, reviewed_references())
        role = text.split("kind: Role\n", 1)[1].split("---", 1)[0]
        self.assertEqual(role.count("verbs: [\"get\"]"), 2)
        self.assertEqual(role.count("resourceNames:"), 2)
        for word in ("list", "watch", "patch", "update", "delete", "create", "deployments", "pods", "*"):
            self.assertNotIn(word, role.replace("resourceNames", ""))
        self.assertIn("automountServiceAccountToken: false", text)
        self.assertNotRegex(text, r"(?im)^\s*token:|BEGIN |tls\.key")

    def test_the_executor_role_never_names_a_secret(self):
        executor = (ROOT / "deploy/sentinel/executor-rbac.yaml").read_text(encoding="utf-8")
        self.assertNotIn("secrets", executor)
        self.assertNotIn("configmaps", executor)

    def test_a_reference_outside_the_namespace_or_kinds_is_refused(self):
        good = reviewed_references()
        for references in ([], [dict(good[0], namespace="other")], [dict(good[0], kind="Pod")],
                           [dict(good[0], name="Bad_Name")], [dict(good[0], extra=True)]):
            with self.subTest(references=references), self.assertRaises(ValueError):
                guard_reader_rbac(NAMESPACE, references)

    def test_an_unverifiable_reference_form_is_refused(self):
        template = protected_template()
        template["spec"]["volumes"].append({"name": "external", "csi": {"driver": "example"}})
        with self.assertRaises(ValueError):
            template_references(template, NAMESPACE)

    def test_the_can_i_matrix_denies_every_executor_read_of_configuration(self):
        rows = rbac_matrix(NAMESPACE, ["shippingservice"], reviewed_references())
        executor = [row for row in rows if row["identity"] == "executor"]
        reader = [row for row in rows if row["identity"] == "reader"]
        self.assertEqual({row["resource"] for row in executor if row["allowed"]}, {"deployment/shippingservice"})
        self.assertTrue(all(not row["allowed"] for row in executor
                            if row["resource"].startswith(("secret", "configmap"))))
        self.assertIn({"identity": "executor", "verb": "get", "resource": "secret/security-upstream", "allowed": False},
                      executor)
        self.assertEqual({row["resource"] for row in reader if row["allowed"]},
                         {"secret/security-upstream", "configmap/security-upstream"})
        self.assertTrue({"secret/security-gateway", "secrets", "deployment/shippingservice"}
                        <= {row["resource"] for row in reader if not row["allowed"]})
        self.assertEqual({row["verb"] for row in reader if row["allowed"]}, {"get"})
        with self.assertRaises(ValueError):
            rbac_matrix(NAMESPACE, ["shippingservice"], reviewed_references(), other_secret="security-upstream")


class AuthenticatedProbeTests(unittest.TestCase):
    def pod(self, **changes):
        values = dict(image=CLIENT, route="shipping", method="hipstershop.ShippingService/GetQuote",
                      request={"items": [{"productId": "demo-item", "quantity": 1}]}, schema="shipping.proto")
        values.update(changes)
        return verifier_probe_pod("shipping-probe-x", **values)

    def test_the_probe_presents_the_verifier_certificate_to_the_gateway_only(self):
        pod = self.pod()
        args = pod["spec"]["containers"][0]["args"]
        self.assertNotIn("-plaintext", args)
        for flag, value in (("-cert", "/client/tls.crt"), ("-key", "/client/tls.key"), ("-cacert", "/client/ca.crt"),
                            ("-servername", "gateway.internal")):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertEqual(args[-2:], [GATEWAY_SERVICES["shipping"] + ":8443", "hipstershop.ShippingService/GetQuote"])
        self.assertEqual(pod["metadata"]["labels"], {"app": VERIFIER_PROBE_APP})
        secret = next(volume for volume in pod["spec"]["volumes"] if volume["name"] == "client")
        self.assertEqual(secret["secret"]["secretName"], "security-" + VERIFIER_IDENTITY)
        self.assertTrue(all(mount["readOnly"] for mount in pod["spec"]["containers"][0]["volumeMounts"]))
        self.assertIs(pod["spec"]["automountServiceAccountToken"], False)

    def test_an_unpinned_client_or_unknown_route_is_refused(self):
        for changes in ({"image": "fullstorydev/grpcurl:v1.9.3"}, {"route": "payment"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.pod(**changes)

    def test_the_probe_pod_reaches_only_the_gateways_route_ports(self):
        items = lab_items()
        graph = network_policy.load_graph()
        ports = {name: {value["target"]} for name, value in graph["ports"].items()}
        ports["shippingservice"] |= {8444}
        ports["cartservice"] |= {8446}
        ports["security-gateway"] = {8443, 8445}
        source = {"namespace": NAMESPACE, "labels": self.pod()["metadata"]["labels"]}
        reached = {(name, port) for name, numbers in ports.items() for port in numbers
                   if network_policy.allowed(items, source, {"namespace": NAMESPACE, "labels": {"app": name}}, port)}
        self.assertEqual(reached, {("security-gateway", 8443), ("security-gateway", 8445)})
        self.assertEqual(set(APPLICATIONS) & {name for name, _ in reached}, set())


LIVE_POLICY = {
    "policy_id": "protected-test", "policy_version": "protected-test-1", "profile": "live",
    "targets": [], "allowed_actions": ["ROLLBACK"], "require_operator_approval": True,
    "unknown_dependency_result": "ESCALATE", "decision_ttl_seconds": 20, "max_approval_lifetime_seconds": 60,
    "max_state_age_seconds": 30, "max_dependency_age_seconds": 30,
    "baseline_constraints": {"max_age_seconds": 300, "min_healthy_duration_seconds": 30,
                             "trusted_sources": ["synthetic-verifier"], "require_health_evidence": True,
                             "require_compatible_references": True, "require_image_digests": True},
    "security_constraints": {"deny_privileged": True, "deny_host_access": True, "forbid_service_account_change": True,
                             "allowed_image_registries": ["docker.io"], "allowed_service_accounts": ["default"]},
    "resource_constraints": {"require_requests_and_limits": True,
                             "max_container_requests": {"cpu": "100m", "memory": "64Mi"},
                             "max_container_limits": {"cpu": "500m", "memory": "256Mi"}},
    "workload_constraints": {"max_pod_requests": {"cpu": "100m", "memory": "128Mi"},
                             "max_rollout_requests": {"cpu": "1", "memory": "1Gi"},
                             "max_replicas": 3, "max_surge_replicas": 1},
}


class ProtectedRollbackTests(unittest.TestCase):
    """The real Guard and Executor restore the whole protected template, sidecar included, with one write."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        fixtures = load_fixtures()
        self.proposal, self.baseline, self.snapshot = fixtures["proposal"], fixtures["baseline"], fixtures["live_state"]
        self.dependencies = fixtures["dependencies"]
        target = self.proposal["target"]
        self.policy = deepcopy(LIVE_POLICY)
        self.policy["targets"] = [deepcopy(target)]
        entry = dict(reviewed_sidecars(ENVOY)[0], target=target["name"])
        self.policy["security_constraints"]["allowed_sidecars"] = [entry]
        shipping = protected_template()["spec"]
        sidecar = next(item for item in shipping["containers"] if item["name"] == entry["name"])
        for template in (self.baseline["pod_template"], self.snapshot["deployment"]["spec"]["template"]):
            spec = template["spec"]
            spec["containers"][0]["image"] = "demo/sentinel@sha256:" + "a" * 64
            spec["containers"].append(deepcopy(sidecar))
            spec["volumes"] = deepcopy(shipping["volumes"])
        self.references = [{"kind": kind, "namespace": target["namespace"], "name": "security-upstream",
                            "uid": kind + "-uid", "resource_version": "5"} for kind in ("ConfigMap", "Secret")]
        self.baseline["reference_versions"] = deepcopy(self.references)
        self.baseline["health_evidence"] = {
            "source": "synthetic-verifier", "evidence_id": "synthetic-window", "status": "HEALTHY",
            "target": deepcopy(target), "template_sha256": "pending", "window_start": "2026-10-10T11:58:30Z",
            "window_end": self.baseline["healthy_observed_at"]}
        self.cluster = FakeDeploymentAPI(self.snapshot, clock=lambda: NOW)
        patch("services.executor.kubernetes_adapter.KubectlDeploymentAPI", return_value=self.cluster).start()
        self.reader = patch("runtime.live_executor.read_reference_versions",
                            side_effect=lambda *args, **kwargs: deepcopy(self.references)).start()
        patch("runtime.live_executor._utc_now", return_value=NOW).start()
        self.addCleanup(patch.stopall)
        (self.directory / "executor.kubeconfig").write_text("executor", encoding="utf-8")
        (self.directory / "reader.kubeconfig").write_text("reader", encoding="utf-8")

    def run_execute(self, *, reader=True):
        digest = template_sha256(self.baseline["pod_template"])
        self.baseline["template_sha256"] = self.baseline["health_evidence"]["template_sha256"] = digest
        self.proposal["parameters"]["template_sha256"] = digest
        approval = {"action_id": self.proposal["action_id"], "proposal_sha256": proposal_fingerprint(self.proposal),
                    "policy_version": self.policy["policy_version"], "operator_id": "synthetic-operator",
                    "decision": "APPROVED", "approved_at": (NOW - timedelta(seconds=5)).isoformat(),
                    "expires_at": (NOW + timedelta(seconds=20)).isoformat()}
        files = dict(policy=self.policy, baseline=self.baseline, dependencies=self.dependencies, approval=approval,
                     allowed_targets=[self.proposal["target"]])
        for name, value in files.items():
            (self.directory / (name + ".json")).write_text(json.dumps(value), encoding="utf-8")
        adapters = build_adapters(
            context="restricted-lab", cluster_id="sentinel-lab", kubeconfig=self.directory / "executor.kubeconfig",
            allowed_targets=self.directory / "allowed_targets.json", ledger=self.directory / "executor.db",
            locks=self.directory / "locks", **{key: self.directory / (key + ".json")
                                               for key in ("policy", "baseline", "dependencies", "approval")},
            **({"reference_kubeconfig": self.directory / "reader.kubeconfig"} if reader else {}))
        self.addCleanup(adapters.ledger.close)
        return execute(self.proposal, adapters)

    def test_one_write_restores_the_protected_template_byte_for_byte(self):
        result = self.run_execute()
        self.assertEqual(result["status"], "APPLIED", result)
        self.assertEqual(len(self.cluster.writes), 1)
        written = self.cluster.snapshot["deployment"]["spec"]["template"]
        self.assertEqual(template_sha256(written), self.baseline["template_sha256"])
        self.assertEqual(written["spec"]["containers"][1], self.baseline["pod_template"]["spec"]["containers"][1])
        for call in self.reader.call_args_list:
            self.assertEqual(call.kwargs["kubeconfig"], self.directory / "reader.kubeconfig")

    def test_an_extra_container_or_another_envoy_digest_writes_nothing(self):
        for change in (
            lambda spec: spec["containers"].append({"name": "debug", "image": "demo/debug@sha256:" + "b" * 64}),
            lambda spec: spec["containers"][1].update(image="envoyproxy/envoy@sha256:" + "f" * 64),
            lambda spec: spec["volumes"].append({"name": "node", "hostPath": {"path": "/"}}),
        ):
            with self.subTest(change=change):
                self.setUp()
                change(self.baseline["pod_template"]["spec"])
                result = self.run_execute()
                self.assertEqual(result["status"], "FAILED")
                self.assertEqual(self.cluster.write_attempts, [])

    def test_without_the_reader_the_protected_rollback_escalates_and_writes_nothing(self):
        result = self.run_execute(reader=False)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("BASELINE_REFERENCE_EVIDENCE_MISSING", result["details"])
        self.assertEqual(self.cluster.write_attempts, [])
        self.reader.assert_not_called()


def security_run(folder, *, passed=True, verifier=True, cluster=protected.CLUSTER):
    run = Path(folder) / "security-run"
    run.mkdir()
    (run / "session.json").write_text(json.dumps({"cluster": cluster, "namespace": NAMESPACE,
                                                  "context": "kind-" + cluster}), encoding="utf-8")
    validation = {"passed": passed}
    if verifier:
        validation["verifier_identity_quote"] = {"succeeded": True}
    (run / "validation.json").write_text(json.dumps(validation), encoding="utf-8")
    return run


class PreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.lock = lab_lock.HeavyLabLock(self.folder / "lock", holder="test")

    def test_a_ready_lab_passes(self):
        self.assertEqual(preflight(security_run(self.folder), stage="baseline", lock=self.lock), [])

    def test_each_missing_precondition_refuses(self):
        cases = (
            (dict(passed=False), "validation has not passed"),
            (dict(verifier=False), "predates the Verifier identity"),
            (dict(cluster="sentinel-boutique-connected"), "not the sentinel-boutique-security lab"),
        )
        for index, (changes, reason) in enumerate(cases):
            with self.subTest(reason=reason):
                folder = self.folder / str(index)
                folder.mkdir()
                problems = preflight(security_run(folder, **changes), stage="full", lock=self.lock)
                self.assertTrue(any(reason in problem for problem in problems), problems)
        self.assertTrue(preflight(self.folder / "absent", stage="full", lock=self.lock))
        self.assertTrue(any("stage" in problem for problem in preflight(security_run(self.folder), stage="later",
                                                                         lock=self.lock)))

    def test_another_heavy_run_holding_the_lock_refuses(self):
        other = lab_lock.HeavyLabLock(self.folder / "lock", holder="P4.01 acceptance", alive=lambda pid: True)
        other.acquire()
        self.addCleanup(other.release)
        problems = preflight(security_run(self.folder), stage="baseline",
                             lock=lab_lock.HeavyLabLock(self.folder / "lock", alive=lambda pid: True))
        self.assertTrue(any(problem.startswith("heavy-lab lock") for problem in problems), problems)

    def test_a_stale_reader_rbac_or_an_unpinned_image_refuses(self):
        stale = self.folder / "guard-reader-rbac.yaml"
        stale.write_text(GUARD_READER_RBAC.read_text(encoding="utf-8").replace("security-upstream", "security-gateway"),
                         encoding="utf-8")
        with patch.object(protected, "GUARD_READER_RBAC", stale):
            problems = preflight(security_run(self.folder), stage="baseline", lock=self.lock)
        self.assertTrue(any("guard-reader-rbac.yaml differs" in problem for problem in problems), problems)
        with patch.object(protected, "ENVOY", "envoyproxy/envoy:v1.40.2"):
            problems = preflight(self.folder / "security-run", stage="baseline", lock=self.lock)
        self.assertTrue(any("pinned by digest" in problem for problem in problems), problems)

    def test_a_todo_pin_left_in_a_reviewed_file_refuses(self):
        marked = self.folder / "guard-reader-rbac.yaml"
        marked.write_text(GUARD_READER_RBAC.read_text(encoding="utf-8") + "# TODO-PIN\n", encoding="utf-8")
        with patch.object(protected, "GUARD_READER_RBAC", marked):
            problems = preflight(security_run(self.folder), stage="baseline", lock=self.lock)
        self.assertTrue(any("TODO-PIN" in problem for problem in problems), problems)

    def test_the_catalog_must_offer_the_protected_scope(self):
        from experiments.boutique.harness.scenarios import load
        catalog = deepcopy(load())
        del catalog["scopes"]["protected"]
        problems = preflight(security_run(self.folder), stage="baseline", catalog=catalog, lock=self.lock)
        self.assertIn("the catalog has no protected scope", problems)

    def test_the_recipe_script_refuses_and_plans_without_cluster_commands(self):
        from scripts.security import protected_recovery
        out, err = io.StringIO(), io.StringIO()
        with patch("subprocess.run", side_effect=AssertionError("cluster command attempted")), \
                patch("sys.stdout", out), patch("sys.stderr", err):
            self.assertEqual(protected_recovery.main(["preflight", "--security-run", str(self.folder / "absent")]), 1)
            self.assertEqual(protected_recovery.main(["rbac", "--check"]), 0)
            self.assertEqual(protected_recovery.main(["plan", "--security-run", "lab", "--run-dir", "run"]), 0)
        self.assertIn("Refused: --security-run", err.getvalue())
        plan = json.loads(out.getvalue().split("\n", 1)[1])
        self.assertEqual(list(plan), ["offline", "lab", "baseline", "full", "standalone"])
        self.assertIn("--protected-stage", plan["baseline"][0])
        self.assertEqual(plan["full"][0][plan["full"][0].index("--protected-stage") + 1], "full")


class ProtectedExperimentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        with patch("shared.provenance.collect", return_value={"source_revision": "test"}):
            self.experiment = ProtectedBoutiqueExperiment(self.folder / "run", security_run=security_run(self.folder))
        self.addCleanup(self.experiment.store.close)
        self.experiment.select_target("shippingservice")
        self.experiment.client_image = CLIENT
        self.experiment.target = {"cluster_id": protected.CLUSTER, "namespace": NAMESPACE, "kind": "Deployment",
                                  "name": "shippingservice", "uid": "uid-1"}

    def test_the_lab_scope_and_registries_are_the_security_labs(self):
        experiment = self.experiment
        self.assertEqual((experiment.cluster_id, experiment.context, experiment.namespace),
                         ("sentinel-boutique-security", "kind-sentinel-boutique-security", "sentinel-boutique-security"))
        self.assertEqual(experiment.scope(), "protected")
        self.assertEqual(experiment.executor_names(), ["shippingservice"])
        self.assertEqual(experiment.allowed_registries, ["us-central1-docker.pkg.dev", "docker.io"])

    def test_the_health_probe_is_the_authenticated_gateway_call(self):
        pod = self.experiment.probe_pod("shipping-probe-1", "hipstershop.ShippingService/GetQuote",
                                        {"items": [{"productId": "demo-item", "quantity": 1}]})
        self.assertEqual(pod["metadata"]["labels"], {"app": VERIFIER_PROBE_APP})
        self.assertIn("-cert", pod["spec"]["containers"][0]["args"])
        self.assertNotIn("-plaintext", pod["spec"]["containers"][0]["args"])
        with self.assertRaises(RuntimeError):
            self.experiment.probe_pod("p", "hipstershop.CartService/GetCart", {})

    def test_the_policy_carries_the_reviewed_sidecar_and_is_a_valid_live_policy(self):
        policy = self.experiment.executor_policy()
        validate_policy(policy, require_live=True)
        security = policy["security_constraints"]
        self.assertEqual([entry["name"] for entry in security["allowed_sidecars"]], ["shipping-ingress"])
        self.assertNotIn("sentinel.local", security["allowed_image_registries"])
        self.assertEqual(policy["resource_constraints"]["max_container_limits"], {"cpu": "500m", "memory": "192Mi"})

    def test_the_executor_adapters_get_the_reader_and_never_its_own_kubeconfig_for_references(self):
        folder = self.experiment.directory / "case"
        folder.mkdir()
        with patch("experiments.recovery.live.build_adapters") as build:
            self.experiment.adapters(folder)
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["reference_kubeconfig"], self.experiment.reader_credentials)
        self.assertEqual(kwargs["reference_context"], "sentinel-guard-reader-boutique-security")
        self.assertNotEqual(kwargs["reference_kubeconfig"], kwargs["kubeconfig"])
        self.assertEqual(self.experiment.reader_arguments()[:1], ["--reference-kubeconfig"])

    def test_the_coordinator_guard_state_reads_references_with_the_reader(self):
        template = protected_template()
        self.experiment.baseline = {"pod_template": template}
        deployment_state = {"metadata": {"name": "shippingservice"}, "spec": {"template": template}}
        with patch.object(ProtectedBoutiqueExperiment, "deployment", return_value=deployment_state), \
                patch.object(protected, "read_reference_versions", return_value=["versions"]) as read:
            state = self.experiment.guard_state({"action": "ROLLBACK"})
        self.assertEqual(state["reference_versions"], ["versions"])
        self.assertEqual(read.call_args.kwargs["kubeconfig"], self.experiment.reader_credentials)
        self.assertEqual(read.call_args.kwargs["context"], "sentinel-guard-reader-boutique-security")
        self.assertEqual(sorted(item["kind"] for item in read.call_args.args[1]),
                         ["ConfigMap", "ConfigMap", "Secret", "Secret"])

    def test_preparation_refuses_before_any_cluster_command_when_preflight_fails(self):
        (self.experiment.security_run / "validation.json").write_text(json.dumps({"passed": False}), encoding="utf-8")
        with patch("subprocess.run", side_effect=AssertionError("cluster command attempted")), \
                self.assertRaisesRegex(RuntimeError, "Protected preflight refused"):
            self.experiment.prepare_image()

    def test_a_baseline_stage_injects_no_fault(self):
        calls = []
        experiment = self.experiment
        experiment.stage = "baseline"
        with patch.object(ProtectedBoutiqueExperiment, "prepare", lambda self: calls.append("prepare")), \
                patch.object(ProtectedBoutiqueExperiment, "permissions", lambda self: calls.append("permissions")), \
                patch.object(ProtectedBoutiqueExperiment, "fault", side_effect=AssertionError("fault injected")):
            experiment.run()
        self.assertEqual(calls, ["prepare", "permissions"])
        summary = json.loads((experiment.directory / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual((summary["status"], summary["protected_stage"]), ("PASS", "baseline"))
        self.assertIs(summary["private_reader_credentials_removed"], True)

    def test_a_bypass_that_succeeds_at_any_time_point_fails_the_run(self):
        from scripts.security import boutique_security as lab
        pods = json.dumps({"items": [{"status": {"podIP": "10.0.0.5"}}]})
        empty = json.dumps({"items": []})

        def kubectl(*args, **kwargs):
            if args[:3] == ("get", "pods", "-l"):
                return type("R", (), {"stdout": empty if "security-gateway" in args[3] else pods})()
            return type("R", (), {"stdout": ""})()
        for leak in ("shippingservice:50051", "10.0.0.5:50051", None):
            with self.subTest(leak=leak):
                def probe(directory, leak=leak, **kwargs):
                    succeeded = kwargs.get("target") == leak if leak else kwargs.get("identity") == VERIFIER_IDENTITY
                    return {"succeeded": succeeded, "output": ""}
                with patch.object(self.experiment, "kubectl", side_effect=kubectl), \
                        patch.object(lab, "probe", side_effect=probe):
                    with self.assertRaisesRegex(RuntimeError, "bypass of the protected route succeeded during"):
                        self.experiment.bypass_checks("during")
        with patch.object(self.experiment, "kubectl", side_effect=kubectl), \
                patch.object(lab, "probe", return_value={"succeeded": False, "output": ""}), \
                patch("sys.stdout", io.StringIO()):
            self.experiment.bypass_checks("before")
        self.assertEqual(self.experiment.summary["bypass_checks"]["before"],
                         {"direct_service_bypass": True, "direct_pod_ip_bypass": True, "gateway_outage": True,
                          "outage_direct_bypass": True})

    def test_the_recovery_is_one_write_with_bypass_probes_after(self):
        calls = []
        experiment = self.experiment
        with patch.object(ProtectedBoutiqueExperiment, "negative_cases", lambda self, incident: calls.append("negative")), \
                patch.object(ProtectedBoutiqueExperiment, "positive", lambda self, incident: calls.append("positive")), \
                patch.object(ProtectedBoutiqueExperiment, "bypass_checks", lambda self, stage: calls.append(stage)), \
                patch.object(ProtectedBoutiqueExperiment, "conditional_race", side_effect=AssertionError("race")), \
                patch.object(ProtectedBoutiqueExperiment, "crash_restart", side_effect=AssertionError("second write")), \
                patch("scripts.security.boutique_security.collect_inspections"):
            experiment.recover_incident({"incident_id": "INC-1"})
            experiment.after_recovery_scenarios()
        self.assertEqual(calls, ["negative", "positive", "after"])


class ProtectedOptionTests(unittest.TestCase):
    def main(self, *arguments):
        from experiments.boutique.harness import live
        errors = io.StringIO()
        with patch("sys.argv", ["test_boutique_live.py", *arguments]), patch("sys.stderr", errors), \
                patch("subprocess.run", side_effect=AssertionError("cluster command attempted")):
            with self.assertRaises(SystemExit) as stopped:
                live.main()
        self.assertEqual(stopped.exception.code, 2)
        return errors.getvalue()

    def test_protected_options_are_checked_before_any_cluster_command(self):
        self.assertIn("--protected requires --security-run", self.main("--protected"))
        self.assertIn("require --protected", self.main("--security-run", "x"))
        self.assertIn("require --protected", self.main("--protected-stage", "baseline"))
        from experiments.boutique.harness.scenarios import pair_ids
        for extra in (["--connected"], ["--agent"], ["--dashboard"], ["--guard-rejection"], ["--pair", pair_ids()[0]]):
            with self.subTest(extra=extra):
                self.assertIn("--protected runs its own staged recovery",
                              self.main("--protected", "--security-run", "x", *extra))
        self.assertIn("shippingservice from scenario R1 only",
                      self.main("--protected", "--security-run", "x", "--target", "cartservice"))

    def test_a_protected_run_refuses_while_another_heavy_run_holds_the_lock(self):
        from experiments.boutique.harness import live
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lock"
            other = lab_lock.HeavyLabLock(path, holder="P4.01 acceptance", alive=lambda pid: True)
            other.acquire()
            try:
                errors = io.StringIO()
                with patch("experiments.lab_lock.default_path", return_value=path), \
                        patch("experiments.lab_lock.pid_alive", return_value=True), \
                        patch("sys.argv", ["test_boutique_live.py", "--protected", "--security-run", folder,
                                           "--approval", "auto-lab", "--run-dir", str(Path(folder) / "run")]), \
                        patch("sys.stderr", errors), \
                        patch("subprocess.run", side_effect=AssertionError("cluster command attempted")):
                    self.assertEqual(live.main(), 2)
            finally:
                other.release()
        self.assertIn("Refused: Another heavy lab run holds", errors.getvalue())
        self.assertFalse((Path(folder) / "run").exists())

    def test_a_protected_run_holds_the_lock_and_builds_the_protected_experiment(self):
        from experiments.boutique.harness import live
        seen = []

        def stop(experiment):
            seen.append(experiment)
            raise RuntimeError("stop before the protected preflight")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lock"
            with patch("experiments.lab_lock.default_path", return_value=path), \
                    patch("sys.argv", ["test_boutique_live.py", "--protected", "--security-run", folder,
                                       "--protected-stage", "baseline", "--approval", "auto-lab",
                                       "--run-dir", str(Path(folder) / "run")]), \
                    patch("sys.stderr", io.StringIO()), patch("sys.stdout", io.StringIO()), \
                    patch("subprocess.run", side_effect=AssertionError("cluster command attempted")), \
                    patch("shared.provenance.collect", return_value={"source_revision": "test"}), \
                    patch.object(live, "tool_versions", return_value={}), \
                    patch.object(ProtectedBoutiqueExperiment, "run", autospec=True, side_effect=stop):
                self.assertEqual(live.main(), 1)
                self.assertFalse(path.exists())
        [experiment] = seen
        self.assertEqual((experiment.stage, experiment.name, experiment.held_lock.holder),
                         ("baseline", "shippingservice", "P5.05 protected recovery R1"))


if __name__ == "__main__":
    unittest.main()
