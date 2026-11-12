"""Structural checks of the Executor admission policy and its live harness (P3.05).

CEL cannot be evaluated faithfully offline, so nothing here claims the policy
denies or allows a request. These tests check that the manifests parse, that
the policy is bound to the identity and namespace executor-rbac.yaml grants,
that each rule still names the fields it protects, and that every patch the
production adapter sends stays inside what the policy allows. Whether the API
server enforces it is for scripts/validation/test_executor_admission_live.py,
which has not been run yet.
"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from experiments.validation.demo_guard import load_fixtures
from scripts.validation import test_executor_admission_live as harness
from services.executor.core import ACTION_ANNOTATION
from services.executor.kubernetes_adapter import KubectlDeploymentAPI
from services.executor.reconciliation import FENCE_ANNOTATION, FENCE_EPOCH_ANNOTATION, fence_marker
from services.executor.store import fingerprint
from shared.actions import RESTART_ANNOTATION, restart_template
from tests.support.manifest_yaml import ManifestYamlError, load_all
from tests.test_k8s_executor_adapter import NOW, Runner


ROOT = Path(__file__).resolve().parents[1]
ADMISSION = ROOT / "deploy/sentinel/executor-admission.yaml"
RBAC = ROOT / "deploy/sentinel/executor-rbac.yaml"
# Every apps/v1 DeploymentSpec field. If Kubernetes adds one, add it here and to the policy deliberately.
DEPLOYMENT_SPEC_FIELDS = ("replicas", "selector", "template", "strategy", "minReadySeconds",
                          "revisionHistoryLimit", "progressDeadlineSeconds", "paused")
EPOCH_PATTERN = "'^(0|[1-9][0-9]{0,17})$'"


def documents(path):
    return {document["kind"]: document for document in load_all(path.read_text(encoding="utf-8"))}


class ManifestYamlReaderTests(unittest.TestCase):
    def test_reads_the_subset_used_by_the_manifests(self):
        text = ("# comment\na:\n  b: 1\n  c: \"x: y\"\n  d: 'it''s'\n  e: [\"p\", q]\n"
                "  f: true\nlist:\n- name: one\n  value: |-\n    first\n    second\n- plain\n---\nz: null\n")
        self.assertEqual(load_all(text), [
            {"a": {"b": 1, "c": "x: y", "d": "it's", "e": ["p", "q"], "f": True},
             "list": [{"name": "one", "value": "first\nsecond"}, "plain"]},
            {"z": None}])

    def test_refuses_yaml_it_does_not_understand(self):
        for text in ("a:\n\tb: 1\n", "a: &anchor 1\n", "a: *alias\n", "a: !!str 1\n", "a: {b: 1}\n",
                     "a: >-\n  folded\n", "a: 1\na: 2\n", "a:\n  b: 1\n    c: 2\n", "a: [[1]]\n",
                     "a: b: c\n", " a: 1\n", "a: \"open\n"):
            with self.subTest(text=text), self.assertRaises(ManifestYamlError):
                load_all(text)


class ExecutorAdmissionPolicyTests(unittest.TestCase):
    def setUp(self):
        manifests = documents(ADMISSION)
        self.policy = manifests["ValidatingAdmissionPolicy"]
        self.binding = manifests["ValidatingAdmissionPolicyBinding"]
        self.rbac = documents(RBAC)
        self.spec = self.policy["spec"]
        self.rules = [rule["expression"] for rule in self.spec["validations"]]
        self.variables = {item["name"]: item["expression"] for item in self.spec["variables"]}

    def rule_mentioning(self, *texts):
        matches = [rule for rule in self.rules if all(text in rule for text in texts)]
        self.assertTrue(matches, f"No validation mentions all of {texts}")
        return matches[0]

    def test_policy_and_binding_parse_and_reference_each_other(self):
        self.assertEqual(set(documents(ADMISSION)), {"ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"})
        for document in (self.policy, self.binding):
            self.assertEqual(document["apiVersion"], "admissionregistration.k8s.io/v1")
        self.assertEqual(self.policy["metadata"]["name"], "sentinel-executor-writes")
        self.assertEqual(self.binding["spec"]["policyName"], self.policy["metadata"]["name"])
        self.assertEqual(self.binding["spec"]["validationActions"], ["Deny", "Audit"])
        self.assertEqual(self.spec["failurePolicy"], "Fail")

    def test_policy_matches_only_deployment_updates_and_reads_no_parameters(self):
        self.assertEqual(self.spec["matchConstraints"], {"resourceRules": [
            {"apiGroups": ["apps"], "apiVersions": ["v1"], "operations": ["UPDATE"], "resources": ["deployments"]}]})
        self.assertNotIn("paramKind", self.spec)
        self.assertNotIn("paramRef", self.binding["spec"])

    def test_identity_condition_names_the_service_account_in_executor_rbac(self):
        account = self.rbac["ServiceAccount"]["metadata"]["name"]
        self.assertEqual(self.rbac["RoleBinding"]["subjects"][0]["name"], account)
        [condition] = self.spec["matchConditions"]
        expression = condition["expression"]
        self.assertIn("request.userInfo.username.startsWith('system:serviceaccount:')", expression)
        self.assertIn(f"request.userInfo.username.endsWith(':{account}')", expression)
        self.assertNotIn("||", expression)

    def test_binding_selects_the_namespace_executor_rbac_grants(self):
        namespace = self.rbac["ServiceAccount"]["metadata"]["namespace"]
        self.assertEqual(self.rbac["Role"]["metadata"]["namespace"], namespace)
        self.assertEqual(self.binding["spec"]["matchResources"], {"namespaceSelector": {"matchExpressions": [
            {"key": "kubernetes.io/metadata.name", "operator": "In", "values": [namespace]}]}})
        # The Role names the targets; the policy covers every Deployment the identity updates there.
        self.assertEqual(self.rbac["Role"]["rules"], [{"apiGroups": ["apps"], "resources": ["deployments"],
                                                       "resourceNames": ["demo-api"], "verbs": ["get", "patch"]}])

    def test_every_spec_field_except_the_template_is_held_unchanged(self):
        for field in DEPLOYMENT_SPEC_FIELDS:
            if field == "template":
                continue
            with self.subTest(field=field):
                self.rule_mentioning(f"object.spec.{field} == oldObject.spec.{field}")
        self.assertEqual(self.variables["templateChanged"], "object.spec.template != oldObject.spec.template")
        self.assertFalse(any("spec.template ==" in rule for rule in self.rules))

    def test_each_rule_references_its_field_set(self):
        self.rule_mentioning("object.spec.replicas == oldObject.spec.replicas")
        self.rule_mentioning("object.metadata.labels == oldObject.metadata.labels")
        self.rule_mentioning("object.metadata.finalizers == oldObject.metadata.finalizers",
                             "object.metadata.ownerReferences == oldObject.metadata.ownerReferences")
        self.rule_mentioning("variables.oldAnnotations.all(key, key in variables.newAnnotations)",
                             "variables.newAnnotations.all(key, key in variables.writable")
        self.rule_mentioning("variables.newEpoch.matches(" + EPOCH_PATTERN + ")")
        self.rule_mentioning("int(variables.newEpoch) >= int(variables.oldEpoch)", "variables.oldEpoch == ''")
        self.rule_mentioning("!variables.templateChanged", f"variables.newAnnotations['{ACTION_ANNOTATION}'] !=")
        self.rule_mentioning("!variables.templateChanged", "variables.newFence == variables.oldFence",
                             "variables.newEpoch == variables.oldEpoch")
        self.assertEqual(len(self.rules), 9)

    def test_writable_annotations_are_exactly_the_ones_the_adapter_writes(self):
        writable = set(re.findall(r"'([^']+)'", self.variables["writable"]))
        self.assertEqual(writable, {ACTION_ANNOTATION, FENCE_ANNOTATION, FENCE_EPOCH_ANNOTATION})
        # The restart token is a Pod template annotation, so the template rules cover it, not this list.
        self.assertNotIn(RESTART_ANNOTATION, writable)
        for name, key in (("oldEpoch", FENCE_EPOCH_ANNOTATION), ("newEpoch", FENCE_EPOCH_ANNOTATION),
                          ("oldFence", FENCE_ANNOTATION), ("newFence", FENCE_ANNOTATION)):
            self.assertIn(f"['{key}']", self.variables[name])

    def test_epoch_pattern_accepts_what_a_seal_writes_and_rejects_malformed_values(self):
        pattern = re.compile(EPOCH_PATTERN.strip("'"))
        for epoch in (0, 1, 6, 2**53, 10**18 - 1):
            self.assertIsNotNone(pattern.fullmatch(str(epoch)), epoch)
        for text in ("", "-1", "06", "1.0", " 1", "1e3", "abc", str(10**18)):
            self.assertIsNone(pattern.fullmatch(text), text)

    def test_variables_are_defined_before_use(self):
        defined = []
        for item in self.spec["variables"]:
            for used in re.findall(r"variables\.([A-Za-z]+)", item["expression"]):
                self.assertIn(used, defined, f"{item['name']} uses {used} before it is defined")
            defined.append(item["name"])
        for rule in self.rules:
            for used in re.findall(r"variables\.([A-Za-z]+)", rule):
                self.assertIn(used, defined)

    def test_every_validation_has_a_message_and_a_known_reason(self):
        for rule in self.spec["validations"]:
            with self.subTest(rule=rule["message"]):
                self.assertEqual(set(rule), {"expression", "message", "reason"})
                self.assertIn(rule["reason"], {"Forbidden", "Invalid"})
                self.assertNotIn("\n", rule["message"])


class AdapterStaysInsidePolicyTests(unittest.TestCase):
    """Every JSON Patch path the production adapter sends is one the policy lets the Executor change."""

    ALLOWED_PATHS = {"/metadata/uid", "/metadata/resourceVersion", "/spec/template"} | {
        "/metadata/annotations/" + key.replace("/", "~1")
        for key in (ACTION_ANNOTATION, FENCE_ANNOTATION, FENCE_EPOCH_ANNOTATION)}

    def setUp(self):
        fixtures = load_fixtures()
        self.target = fixtures["proposal"]["target"]
        self.deployment = fixtures["live_state"]["deployment"]
        self.template = fixtures["baseline"]["pod_template"]

    def patches(self, send, *, annotations=True):
        deployment = json.loads(json.dumps(self.deployment))
        if annotations:
            deployment["metadata"].setdefault("annotations", {"example.com/owner": "platform"})
        else:
            deployment["metadata"].pop("annotations", None)
        runner = Runner(deployment)
        api = KubectlDeploymentAPI(context="kind-team", cluster_id=self.target["cluster_id"],
                                   kubeconfig="restricted credentials.json", allowed_targets=[self.target],
                                   runner=runner, clock=lambda: NOW)
        send(api, deployment["metadata"]["resourceVersion"])
        return runner.patches

    def evidence(self, template, action=None):
        evidence = {"action_id": "ACT-LIVE-001", "proposal_sha256": "a" * 64, "target_uid": self.target["uid"],
                    "template_sha256": fingerprint(template), "attempt_id": "b" * 32}
        return dict(evidence, action=action) if action else evidence

    def sends(self):
        restarted = restart_template(self.template, "RST-0123456789abcdef")
        return {
            "rollback": lambda api, version: api.conditional_restore(
                target=self.target, version=version, template=self.template, evidence=self.evidence(self.template)),
            "restart": lambda api, version: api.conditional_restore(
                target=self.target, version=version, template=restarted,
                evidence=self.evidence(restarted, "RESTART")),
            "fence": lambda api, version: api.fence(target=self.target, version=version,
                                                    marker=fence_marker("ACT-LIVE-001", "c" * 32)),
            "seal": lambda api, version: api.fence(target=self.target, version=version,
                                                   marker=fence_marker("ACT-LIVE-001", "d" * 32, 7)),
        }

    def test_rollback_restart_fence_and_seal_patch_only_allowed_paths(self):
        for name, send in self.sends().items():
            for annotations in (True, False):
                with self.subTest(action=name, annotations=annotations):
                    [patch] = self.patches(send, annotations=annotations)
                    for operation in patch:
                        if operation["path"] == "/metadata/annotations":
                            # A whole map is added only when the object had no annotations at all.
                            self.assertFalse(annotations)
                            self.assertEqual(operation["op"], "add")
                            self.assertLessEqual(set(operation["value"]),
                                                 {ACTION_ANNOTATION, FENCE_ANNOTATION, FENCE_EPOCH_ANNOTATION})
                        else:
                            self.assertIn(operation["path"], self.ALLOWED_PATHS)
                        if operation["path"] in ("/metadata/uid", "/metadata/resourceVersion"):
                            self.assertEqual(operation["op"], "test")
                        self.assertNotEqual(operation["op"], "remove")

    def test_template_writes_carry_evidence_and_fences_never_touch_the_template(self):
        for name, send in self.sends().items():
            with self.subTest(action=name):
                [patch] = self.patches(send)
                paths = {operation["path"] for operation in patch}
                writes_template = "/spec/template" in paths
                self.assertEqual(writes_template, name in ("rollback", "restart"))
                if writes_template:
                    self.assertIn("/metadata/annotations/" + ACTION_ANNOTATION.replace("/", "~1"), paths)
                epoch = "/metadata/annotations/" + FENCE_EPOCH_ANNOTATION.replace("/", "~1")
                self.assertEqual(epoch in paths, name == "seal")
                if name == "seal":
                    value = next(operation["value"] for operation in patch if operation["path"] == epoch)
                    self.assertRegex(value, EPOCH_PATTERN.strip("'"))


def quiet_main(argv, runner, **options):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return harness.main(argv, runner=runner, **options)


class FakeKubectl:
    """Scripted kubectl answers; the policy itself is never modelled."""

    def __init__(self, *, minor="37", cluster="kind-sentinel-boutique-connected", enforce=False):
        self.minor, self.cluster, self.enforce = minor, cluster, enforce
        self.calls = []
        deployment = harness.deployment_manifest("ns")
        deployment["metadata"].update(uid="uid-1", resourceVersion="100")
        self.deployment = deployment

    def reply(self, command, code=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(command, code, stdout.encode(), stderr.encode())

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        words = [item for item in command[1:] if not item.startswith("--")]
        if words[:2] == ["config", "view"]:
            context = next(item.split("=", 1)[1] for item in command if item.startswith("--context="))
            return self.reply(command, stdout=json.dumps({
                "contexts": [{"name": context, "context": {"cluster": self.cluster}}],
                "clusters": [{"name": self.cluster, "cluster": {"server": "https://127.0.0.1:6443",
                                                                "certificate-authority-data": "Q0E="}}]}))
        if words[:1] == ["version"]:
            return self.reply(command, stdout=json.dumps({"serverVersion": {"major": "1", "minor": self.minor}}))
        if words[:2] == ["get", "deployment"]:
            return self.reply(command, stdout=json.dumps(self.deployment))
        if words[:2] == ["create", "token"]:
            return self.reply(command, stdout="fake-token-" + "z" * 40 + "\n")
        if words[:1] == ["patch"] and "--dry-run=server" in command:
            if self.enforce:
                return self.reply(command, 1, stderr="deployments.apps \"demo-api\" is forbidden: "
                                  "ValidatingAdmissionPolicy 'x' with binding 'x' denied request")
            return self.reply(command)
        return self.reply(command)

    def verbs(self):
        return [[item for item in command[1:] if not item.startswith("--")][:2] for command in self.calls]


class AdmissionHarnessTests(unittest.TestCase):
    def test_only_the_connected_lab_context_is_accepted_and_sentinel_lab_is_refused(self):
        for context in ("kind-sentinel-lab", "Kind-Sentinel-LAB-copy", "kind-sentinel-wedge", "docker-desktop", " "):
            runner = FakeKubectl()
            with self.subTest(context=context):
                self.assertEqual(quiet_main(["--context", context], runner), 2)
                self.assertEqual(runner.calls, [])
        self.assertEqual(harness.check_context("kind-sentinel-boutique-connected"),
                         "kind-sentinel-boutique-connected")

    def test_a_context_that_points_at_the_lab_cluster_is_refused_before_anything_is_created(self):
        runner = FakeKubectl(cluster="kind-sentinel-lab")
        with tempfile.TemporaryDirectory() as directory:
            code = quiet_main(["--context", "kind-sentinel-boutique-connected", "--run-dir", directory + "/run"],
                              runner)
            summary = json.loads((Path(directory) / "run/admission.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 2)
        self.assertEqual(summary["status"], "REFUSED")
        self.assertEqual(runner.verbs(), [["config", "view"]])

    def test_a_server_older_than_1_30_is_refused(self):
        runner = FakeKubectl(minor="29")
        with tempfile.TemporaryDirectory() as directory:
            code = quiet_main(["--context", "kind-sentinel-boutique-connected", "--run-dir", directory + "/run"],
                              runner)
        self.assertEqual(code, 2)
        self.assertNotIn(["create", "namespace"], runner.verbs())

    def test_a_policy_that_never_takes_effect_fails_and_everything_is_removed(self):
        runner = FakeKubectl(enforce=False)
        with tempfile.TemporaryDirectory() as directory:
            code = quiet_main(["--context", "kind-sentinel-boutique-connected", "--run-dir", directory + "/run"],
                              runner, sleep=lambda seconds: None)
            run = Path(directory) / "run"
            text = (run / "admission.json").read_text(encoding="utf-8")
            credentials_left = (run / ".executor-kubeconfig.json").exists()
        summary = json.loads(text)
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "FAILED")
        self.assertTrue(summary["policy_deleted"] and summary["namespace_deleted"])
        self.assertFalse(credentials_left)
        verbs = runner.verbs()
        for kind in ("validatingadmissionpolicybinding", "validatingadmissionpolicy", "namespace"):
            self.assertIn(["delete", kind], verbs)
        probes = [command for command in runner.calls if "--dry-run=server" in command]
        self.assertEqual(len(probes), harness.POLICY_WAIT_ATTEMPTS)
        self.assertTrue(all(any(item.startswith("--kubeconfig=") for item in probe) for probe in probes))
        for secret in ("fake-token-", "https://127.0.0.1:6443", "Q0E="):
            self.assertNotIn(secret, text)

    def test_rendered_policy_differs_from_the_shipped_one_only_in_names_and_namespace(self):
        shipped = load_all(ADMISSION.read_text(encoding="utf-8"))
        rendered = load_all(harness.render_policy(ADMISSION.read_text(encoding="utf-8"), "sentinel-admission-1a2b3c4d",
                                                  "sentinel-admission-1a2b3c4d-executor-writes"))
        self.assertEqual(rendered[0]["spec"], shipped[0]["spec"])
        self.assertEqual(rendered[0]["metadata"]["name"], "sentinel-admission-1a2b3c4d-executor-writes")
        self.assertEqual(rendered[1]["spec"]["policyName"], rendered[0]["metadata"]["name"])
        self.assertEqual(rendered[1]["spec"]["matchResources"]["namespaceSelector"]["matchExpressions"][0]["values"],
                         ["sentinel-admission-1a2b3c4d"])
        rendered[1]["spec"]["policyName"] = shipped[1]["spec"]["policyName"]
        rendered[1]["spec"]["matchResources"] = shipped[1]["spec"]["matchResources"]
        self.assertEqual(rendered[1]["spec"], shipped[1]["spec"])
        rbac = load_all(harness.render_rbac(RBAC.read_text(encoding="utf-8"), "sentinel-admission-1a2b3c4d"))
        self.assertEqual({document["metadata"]["namespace"] for document in rbac}, {"sentinel-admission-1a2b3c4d"})
        self.assertEqual(rbac[2]["subjects"][0]["namespace"], "sentinel-admission-1a2b3c4d")

    def test_rendering_refuses_a_manifest_whose_names_moved(self):
        text = ADMISSION.read_text(encoding="utf-8")
        for changed in (text.replace("policyName: sentinel-executor-writes", "policyName: other"),
                        text.replace('values: ["sentinel-demo"]', 'values: ["other"]'), text + "\n# sentinel-demo\n"
                        "name: sentinel-executor-writes\n"):
            with self.subTest(), self.assertRaises(RuntimeError):
                harness.render_policy(changed, "ns", "name")

    def test_the_planned_checks_cover_every_required_live_check(self):
        denied = {name for name, _ in harness.denial_patches({"spec": {"containers": [{"name": "app"}]}})}
        for required in ("replicas changed by JSON patch", "label added", "foreign annotation changed",
                         "lower sentinel.io/fence-epoch"):
            self.assertIn(required, denied)
        rbac = dict(harness.RBAC_CHECKS)
        self.assertEqual(rbac["kubectl scale (scale subresource)"][0], "scale")
        self.assertEqual(rbac["get secrets"], ("get", "secrets"))
        self.assertIn("--subresource=exec", rbac["pods/exec create (auth can-i)"])
        self.assertTrue(all("--dry-run=server" in arguments for name, arguments in harness.RBAC_CHECKS
                            if arguments[0] in ("delete", "create")))

    def test_classify_tells_policy_rbac_and_other_failures_apart(self):
        def result(code, stdout="", stderr=""):
            return subprocess.CompletedProcess([], code, stdout.encode(), stderr.encode())
        policy = ("The deployments \"demo-api\" is invalid: : ValidatingAdmissionPolicy 'p-executor-writes' with "
                  "binding 'p-executor-writes' denied request: sentinel.io/fence-epoch may not decrease")
        self.assertEqual(harness.classify(result(1, stderr=policy), "p-executor-writes"), harness.POLICY_DENIED)
        self.assertEqual(harness.classify(result(1, stderr=policy), "another-policy"), harness.OTHER)
        rbac = ('Error from server (Forbidden): secrets is forbidden: User "system:serviceaccount:n:sentinel-executor"'
                ' cannot list resource "secrets" in API group "" in the namespace "n"')
        self.assertEqual(harness.classify(result(1, stderr=rbac), "p"), harness.RBAC_DENIED)
        self.assertEqual(harness.classify(result(1, stdout="no\n"), "p"), harness.RBAC_DENIED)
        self.assertEqual(harness.classify(result(1, stderr="Unable to connect to the server"), "p"), harness.OTHER)
        self.assertEqual(harness.classify(result(0, stdout="yes\n"), "p"), harness.ALLOWED)


if __name__ == "__main__":
    unittest.main()
