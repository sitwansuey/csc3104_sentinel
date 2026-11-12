"""Structural checks of the Executor admission policy and its live harness (P3.05).

CEL cannot be evaluated faithfully offline, so nothing here claims the policy
denies or allows a request. These tests check that the manifests parse, that
the policy is bound to the identity and namespace executor-rbac.yaml grants,
that each rule still names the fields it protects, and that every patch the
production adapter sends stays inside what the policy allows. Whether the API
server enforces it is for scripts/validation/test_executor_admission_live.py,
which has run once against a kind API server (claim C-ADMISSION-POLICY-LIVE).
"""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
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
# Every apps/v1 DeploymentSpec field that exists today. The live harness checks the server's list against it.
DEPLOYMENT_SPEC_FIELDS = harness.DEPLOYMENT_SPEC_FIELDS
EPOCH_PATTERN = "'^(0|[1-9][0-9]{0,17})$'"
# SHA-256 of each CEL expression as shipped. The structural tests below cannot see a semantic weakening such as
# ">= int(variables.oldEpoch) - 1" or "true || ...", so any edit to a rule must also change this table on purpose,
# after the live harness has been run against the new text.
PINNED_CONDITIONS = {
    "sentinel-executor-identity": "d788e1dbc55aacdce1cd8b5585899260b871b6148b2ac295c2fde24532a4d34d"}
PINNED_VARIABLES = {
    "oldAnnotations": "2203bf3eec99125a154c99d96503ed1aac813d73b122702153db1c9c5f217e2c",
    "newAnnotations": "8f9dbb977bc9a2730f089ce1c70eb80ec8621ec7f8d62908095779c6777c8fa2",
    "writable": "a89f08bbf022ad6fe3e5fb845a0be1a8fa6fa596a9d4498ca448345ebd6af810",
    "oldEpoch": "af88a727987088ccf385f5ef0a92dad06e7aebc077d4c78b53859492cb6baa7a",
    "newEpoch": "3d02a087e5f46e181aa15f04b4d650c782ce334d055b15e0ab75843ffdb257f1",
    "oldFence": "8188d014d748873f4c0b353520aff7f479f5616b3e3420eb62225d1a6beb58c6",
    "newFence": "1f4c8cabe8836b9c08d95c343aa580d95ad29a9c73146ab9c5e554ed82b0874b",
    "templateChanged": "754b7e8252e2e59b9cf528d221e6f85c3a3f558156d2ae878f4206a238af6ee2"}
PINNED_VALIDATIONS = {
    "The Sentinel Executor may not change spec.replicas":
        "8cacfd79faeab567e5497ae3ec0c2145e9aec23340213a55a2cc0c2057df2b95",
    "The Sentinel Executor may change no spec field other than spec.template":
        "fa76c39f0763c89d4f944576913247a83a6a6a894a57e02175031712a58d4359",
    "The Sentinel Executor may not change metadata.labels":
        "990771f33819df0386827ca9ba0893cfb79ca6b0290273991bb9800ff4a9ecee",
    "The Sentinel Executor may not change metadata.finalizers or metadata.ownerReferences":
        "ed31c1b9d8159c751e39db1ace479ff0f9c5cfa987821a000559dadcd3f92078",
    "The Sentinel Executor may remove no annotation and may change only sentinel.io/execution, sentinel.io/fence "
    "and sentinel.io/fence-epoch": "1524e914896000c66786831f3334101f383125f0a7f2326f4a4aed62701a4a0a",
    "sentinel.io/fence-epoch must be a decimal integer":
        "58631c5cc46d508d782cd664dbaec57b283cd90606e393fe79880d2a4fa56483",
    "sentinel.io/fence-epoch may not decrease": "ea2984e2e877db57a3e2bb6f5636b1e86c4ca39a0626c15ff5b048c6518c2f34",
    "A Pod template change by the Sentinel Executor must write new sentinel.io/execution evidence":
        "609cc1f3575bd0062f0facbce66441274397271c0ed5e064f6e51c2c858d5ba6",
    "A fence or seal by the Sentinel Executor may not change the Pod template":
        "4545d477cee60700f5be3d32168899e21f1812cd51ffd70963a6e2d481f60e35"}


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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

    def rule_with_message(self, message):
        [rule] = [rule["expression"] for rule in self.spec["validations"] if message in rule["message"]]
        return rule

    def test_every_cel_expression_is_pinned_to_its_reviewed_text(self):
        def pinned(found, expected, kind):
            self.assertEqual(set(found), set(expected), f"{kind} were added, removed or renamed")
            for key, expression in found.items():
                self.assertEqual(sha256(expression), expected[key],
                                 f"The {kind[:-1]} {key!r} changed; review it, run the live harness, then re-pin:\n"
                                 + expression)
        pinned({item["name"]: item["expression"] for item in self.spec["matchConditions"]}, PINNED_CONDITIONS,
               "conditions")
        pinned(self.variables, PINNED_VARIABLES, "variables")
        pinned({rule["message"]: rule["expression"] for rule in self.spec["validations"]}, PINNED_VALIDATIONS,
               "validations")

    def test_the_pin_catches_a_semantic_weakening_the_substring_checks_miss(self):
        weakened = {
            "may not decrease": ("int(variables.newEpoch) >= int(variables.oldEpoch)",
                                 "int(variables.newEpoch) >= int(variables.oldEpoch) - 1"),
            "may not change the Pod template": ("!variables.templateChanged ||", "true || !variables.templateChanged ||"),
            "must be a decimal integer": ("variables.newEpoch.matches(" + EPOCH_PATTERN + ")",
                                          "variables.newEpoch.matches('^[0-9]+$')")}
        for message, (old, new) in weakened.items():
            with self.subTest(message=message):
                expression = self.rule_with_message(message)
                self.assertIn(old, expression)
                self.assertNotEqual(sha256(expression.replace(old, new, 1)),
                                    next(value for key, value in PINNED_VALIDATIONS.items() if message in key))

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

    def test_each_epoch_rule_holds_the_strict_pattern_itself(self):
        # The order rule also holds the pattern, so finding it anywhere would let a loose format rule pass:
        # with no stored epoch the order rule is skipped, and only the format rule refuses a first "06".
        strict = "variables.newEpoch.matches(" + EPOCH_PATTERN + ")"
        self.assertIn(strict, self.rule_with_message("must be a decimal integer"))
        order = self.rule_with_message("may not decrease")
        self.assertIn(strict, order)
        self.assertIn("variables.oldEpoch.matches(" + EPOCH_PATTERN + ")", order)
        self.assertEqual(sum(rule.count(".matches(") for rule in self.rules), 3)
        self.assertEqual(sum(rule.count(EPOCH_PATTERN) for rule in self.rules), 3)

    def test_the_harness_rule_messages_each_name_exactly_one_validation(self):
        messages = [rule["message"] for rule in self.spec["validations"]]
        for expected in harness.RULE_MESSAGES:
            with self.subTest(rule=expected):
                self.assertEqual(sum(expected in message for message in messages), 1)

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
        # The adapter accepts any non-negative epoch, so the manifest states the policy's narrower bound.
        self.assertIn("a decimal integer below 10^18", ADMISSION.read_text(encoding="utf-8"))

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


def canonical(operations):
    return json.dumps(operations, sort_keys=True)


def apply_json_patch(document, operations):
    """The few JSON Patch operations the harness and the adapter send, for the fake's stored object."""
    document = json.loads(json.dumps(document))
    for operation in operations:
        parts = [part.replace("~1", "/").replace("~0", "~") for part in operation["path"].split("/")[1:]]
        parent = document
        for part in parts[:-1]:
            parent = parent[part]
        if operation["op"] == "test":
            if parent.get(parts[-1]) != operation["value"]:
                raise ValueError("test failed")
        elif operation["op"] in ("add", "replace"):
            parent[parts[-1]] = operation["value"]
        else:
            del parent[parts[-1]]
    return document


class FakeKubectl:
    """Scripted kubectl answers; the policy itself is never modelled.

    enforce makes the harness's dry-run probe come back denied by the run's own
    policy. deny maps a JSON Patch from the Executor identity to the policy
    message the test scripts for it, or None to let it through; rbac makes the
    RBAC checks come back forbidden. Anything let through is applied to the
    stored Deployment.
    """

    def __init__(self, *, minor="37", cluster="kind-sentinel-boutique-connected", enforce=False,
                 deny=lambda operations: None, rbac=False, spec_fields=harness.DEPLOYMENT_SPEC_FIELDS):
        self.minor, self.cluster, self.enforce, self.deny, self.rbac = minor, cluster, enforce, deny, rbac
        self.spec_fields = spec_fields
        self.calls = []
        deployment = harness.deployment_manifest("ns")
        deployment["metadata"].update(uid="uid-1", resourceVersion="100", generation=1)
        deployment["status"] = {"observedGeneration": 1}
        self.deployment = deployment

    def reply(self, command, code=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(command, code, stdout.encode(), stderr.encode())

    def policy_denial(self, command, message):
        namespace = next(item.split("=", 1)[1] for item in command if item.startswith("--namespace="))
        policy = namespace + "-executor-writes"
        return self.reply(command, 1, stderr=f"The deployments \"demo-api\" is invalid: : ValidatingAdmissionPolicy "
                                             f"'{policy}' with binding '{policy}' denied request: {message}")

    def patch(self, command):
        path = command[command.index("--patch-file") + 1]
        operations = json.loads(Path(path).read_text(encoding="utf-8"))
        if "--dry-run=server" in command:
            return self.policy_denial(command, harness.LABELS_RULE) if self.enforce else self.reply(command)
        message = self.deny(operations)
        if message is not None:
            return self.policy_denial(command, message)
        try:
            updated = apply_json_patch(self.deployment, operations)
        except (KeyError, ValueError):
            return self.reply(command, 1, stderr="The request is invalid")
        updated["metadata"]["resourceVersion"] = str(int(self.deployment["metadata"]["resourceVersion"]) + 1)
        self.deployment = updated
        return self.reply(command, stdout=json.dumps(updated) if "--output=json" in command else "patched")

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        words = [item for item in command[1:] if not item.startswith("--")]
        executor = any(item.startswith("--kubeconfig=") for item in command)
        if words[:2] == ["config", "view"]:
            context = next(item.split("=", 1)[1] for item in command if item.startswith("--context="))
            return self.reply(command, stdout=json.dumps({
                "contexts": [{"name": context, "context": {"cluster": self.cluster}}],
                "clusters": [{"name": self.cluster, "cluster": {"server": "https://127.0.0.1:6443",
                                                                "certificate-authority-data": "Q0E="}}]}))
        if words[:1] == ["version"]:
            return self.reply(command, stdout=json.dumps({"serverVersion": {"major": "1", "minor": self.minor}}))
        if words[:2] == ["create", "namespace"]:
            self.deployment["metadata"]["namespace"] = words[2]
        if words[:2] == ["get", "/openapi/v3/apis/apps/v1"]:
            return self.reply(command, stdout=json.dumps({"components": {"schemas": {
                harness.DEPLOYMENT_SPEC_SCHEMA: {"properties": {field: {} for field in self.spec_fields}}}}}))
        if words[:2] == ["get", "deployment"]:
            return self.reply(command, stdout=json.dumps(self.deployment))
        if words[:2] == ["create", "token"]:
            return self.reply(command, stdout="fake-token-" + "z" * 40 + "\n")
        if executor and words[:1] == ["patch"]:
            return self.patch(command)
        if executor and self.rbac:
            if words[:2] == ["auth", "can-i"]:
                return self.reply(command, 1, stdout="no\n")
            return self.reply(command, 1, stderr='Error from server (Forbidden): User "system:serviceaccount:'
                                                 'n:sentinel-executor" cannot do that')
        return self.reply(command)

    def verbs(self):
        return [[item for item in command[1:] if not item.startswith("--")][:2] for command in self.calls]


def scripted_denials(rules=None):
    """A deny function answering each planned denial with its expected rule (or with rules[name])."""
    baseline = harness.deployment_manifest("ns")["spec"]["template"]
    planned = {canonical(operations): (rules or {}).get(name, rule)
               for name, operations, rule in [harness.FIRST_EPOCH_CHECK, *harness.denial_patches(baseline)]}

    def deny(operations):
        if canonical(operations) in planned:
            return planned[canonical(operations)]
        if any(operation["path"].endswith("fence-epoch") and operation.get("value") == "1"
               for operation in operations):
            return (rules or {}).get("fresh store", harness.EPOCH_ORDER_RULE)
        return None
    return deny


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
        for document in rendered:
            # Labelled so an operator can find both cluster-scoped objects after a killed run.
            self.assertEqual(document["metadata"]["labels"], {"app.kubernetes.io/managed-by": harness.MANAGED_BY})
            self.assertNotIn("labels", shipped[rendered.index(document)]["metadata"])
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
        planned = {name: (operations, rule) for name, operations, rule
                   in harness.denial_patches({"spec": {"containers": [{"name": "app"}]}})}
        for required, rule in (("replicas changed by JSON patch", harness.REPLICAS_RULE),
                               ("another spec field (paused) changed", harness.SPEC_RULE),
                               ("label added", harness.LABELS_RULE),
                               ("foreign annotation changed", harness.ANNOTATIONS_RULE),
                               ("Pod template changed without new evidence", harness.EVIDENCE_RULE),
                               ("seal that also changes the Pod template", harness.SEAL_RULE),
                               ("lower sentinel.io/fence-epoch", harness.EPOCH_ORDER_RULE),
                               ("malformed sentinel.io/fence-epoch", harness.EPOCH_FORMAT_RULE)):
            self.assertEqual(planned[required][1], rule, required)
        # Every rule the harness names is expected by at least one check, so each has a live witness.
        expected = {rule for _, rule in planned.values()} | {harness.FIRST_EPOCH_CHECK[2]}
        self.assertEqual(expected, set(harness.RULE_MESSAGES))
        rbac = dict(harness.RBAC_CHECKS)
        self.assertEqual(rbac["kubectl scale (scale subresource)"][0], "scale")
        self.assertEqual(rbac["get secrets"], ("get", "secrets"))
        self.assertIn("--subresource=exec", rbac["pods/exec create: auth can-i answers no"])
        self.assertTrue(all("--dry-run=server" in arguments for name, arguments in harness.RBAC_CHECKS
                            if arguments[0] in ("delete", "create")))

    def test_the_seal_and_format_checks_can_only_be_denied_by_their_own_rule(self):
        planned = {name: operations for name, operations, _ in harness.denial_patches({"spec": {}})}
        seal = {operation["path"]: operation for operation in planned["seal that also changes the Pod template"]}
        # New evidence, so the evidence rule passes; a higher epoch, so the order rule passes.
        self.assertEqual(seal["/metadata/annotations/sentinel.io~1execution"]["op"], "add")
        self.assertIn("/spec/template", seal)
        self.assertEqual(seal["/metadata/annotations/sentinel.io~1fence-epoch"]["value"], "9")
        # Sent before any seal, so no epoch is stored and the order rule is skipped.
        name, operations, rule = harness.FIRST_EPOCH_CHECK
        self.assertEqual(operations, [{"op": "add", "path": "/metadata/annotations/sentinel.io~1fence-epoch",
                                       "value": "06"}])
        self.assertEqual(rule, harness.EPOCH_FORMAT_RULE)


class AdmissionVerdictTests(unittest.TestCase):
    """The verdict path that would produce the evidence, against scripted servers."""

    def run_harness(self, runner):
        with tempfile.TemporaryDirectory() as directory:
            code = quiet_main(["--context", "kind-sentinel-boutique-connected", "--run-dir", directory + "/run"],
                              runner, sleep=lambda seconds: None)
            summary = json.loads((Path(directory) / "run/admission.json").read_text(encoding="utf-8"))
        return code, summary, {check["name"]: check for check in summary["checks"]}

    def test_a_server_that_allows_every_request_fails_every_denial_check(self):
        code, summary, checks = self.run_harness(FakeKubectl(enforce=True))
        self.assertEqual((code, summary["status"]), (1, "FAIL"))
        denials = [check for check in checks.values() if check.get("expected") in (harness.POLICY_DENIED,
                                                                                  harness.RBAC_DENIED)]
        self.assertEqual(len(denials), len(harness.denial_patches({})) + 1 + 1 + len(harness.RBAC_CHECKS))
        for check in denials:
            with self.subTest(check=check["name"]):
                self.assertFalse(check["passed"])
        self.assertEqual(checks["replicas changed by JSON patch"]["outcome"], harness.ALLOWED)
        self.assertFalse(checks["replicas changed by JSON patch"]["object_unchanged"])
        self.assertTrue(checks["real ROLLBACK patch (production adapter)"]["passed"])
        self.assertFalse(checks["final state"]["passed"])

    def test_a_server_that_denies_each_request_by_its_rule_passes(self):
        code, summary, checks = self.run_harness(FakeKubectl(enforce=True, deny=scripted_denials(), rbac=True))
        self.assertEqual((code, summary["status"]), (0, "PASS"), [name for name, check in checks.items()
                                                                  if not check["passed"]])
        fresh = checks["seal at epoch 1 from a fresh Coordinator store (production adapter)"]
        self.assertEqual((fresh["outcome"], fresh["adapter_error"]), (harness.POLICY_DENIED, "WriteOutcomeUnknown"))
        self.assertIn(harness.EPOCH_ORDER_RULE, fresh["message"])
        self.assertEqual(summary["final_state"]["fence_epoch"], "6")
        self.assertEqual(checks["pods/exec create: auth can-i answers no"]["outcome"], harness.RBAC_DENIED)

    def test_a_denial_by_another_rule_fails_the_check(self):
        runner = FakeKubectl(enforce=True, rbac=True, deny=scripted_denials({
            "seal that also changes the Pod template": harness.EVIDENCE_RULE,
            "malformed sentinel.io/fence-epoch with no stored epoch": harness.EPOCH_ORDER_RULE}))
        code, summary, checks = self.run_harness(runner)
        self.assertEqual((code, summary["status"]), (1, "FAIL"))
        failed = {name for name, check in checks.items() if not check["passed"]}
        self.assertEqual(failed, {"seal that also changes the Pod template",
                                  "malformed sentinel.io/fence-epoch with no stored epoch"})
        self.assertFalse(checks["seal that also changes the Pod template"]["rule_named"])

    def test_a_fresh_store_seal_that_commits_fails_the_check(self):
        deny = scripted_denials({"fresh store": None})
        code, summary, checks = self.run_harness(FakeKubectl(enforce=True, rbac=True, deny=deny))
        self.assertEqual(code, 1)
        fresh = checks["seal at epoch 1 from a fresh Coordinator store (production adapter)"]
        self.assertEqual((fresh["outcome"], fresh["passed"]), (harness.ALLOWED, False))

    def test_a_server_deploymentspec_field_the_policy_does_not_name_fails(self):
        runner = FakeKubectl(enforce=True, deny=scripted_denials(), rbac=True,
                             spec_fields=harness.DEPLOYMENT_SPEC_FIELDS + ("futureField",))
        code, summary, checks = self.run_harness(runner)
        self.assertEqual(code, 1)
        check = checks["server DeploymentSpec has only the fields the policy names"]
        self.assertFalse(check["passed"])
        self.assertIn("futureField", check["server_fields"])

    def test_a_deployment_that_never_settles_ends_the_run_before_any_check_is_sent(self):
        runner = FakeKubectl(enforce=True, deny=scripted_denials(), rbac=True)
        runner.deployment["status"]["observedGeneration"] = 0
        code, summary, _ = self.run_harness(runner)
        self.assertEqual((code, summary["status"], summary["error_type"]), (1, "FAILED", "RuntimeError"))
        self.assertFalse([command for command in runner.calls if any(item.startswith("--kubeconfig=")
                                                                      for item in command)
                          and "patch" in command and "--dry-run=server" not in command])
        self.assertTrue(summary["policy_deleted"] and summary["namespace_deleted"])

    def test_a_denial_passes_only_with_its_rule_no_change_and_every_extra_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            checker = harness.AdmissionHarness("kind-sentinel-boutique-connected", Path(directory) / "run",
                                               runner=FakeKubectl())
        deployment = FakeKubectl().deployment
        changed = json.loads(json.dumps(deployment))
        changed["spec"]["replicas"] = 1
        denial = subprocess.CompletedProcess([], 1, b"", (f"ValidatingAdmissionPolicy '{checker.policy_name}' "
                                                          f"denied request: {harness.EPOCH_ORDER_RULE}").encode())
        with redirect_stdout(io.StringIO()):
            self.assertTrue(checker.record("a", harness.POLICY_DENIED, denial, deployment, deployment,
                                           rule=harness.EPOCH_ORDER_RULE))
            self.assertFalse(checker.record("b", harness.POLICY_DENIED, denial, deployment, deployment,
                                            rule=harness.EPOCH_ORDER_RULE, required=False))
            self.assertFalse(checker.record("c", harness.POLICY_DENIED, denial, deployment, changed,
                                            rule=harness.EPOCH_ORDER_RULE))
            self.assertFalse(checker.record("d", harness.POLICY_DENIED, denial, deployment, deployment,
                                            rule=harness.SEAL_RULE))
        self.assertEqual([check["passed"] for check in checker.summary["checks"]], [True, False, False, False])

    def test_settled_needs_the_observed_generation_and_observable_ignores_the_controller_revision(self):
        deployment = {"metadata": {"generation": 2, "annotations": {"a": "1"}}, "spec": {}, "status": {}}
        self.assertFalse(harness.settled(deployment))
        deployment["status"]["observedGeneration"] = 1
        self.assertFalse(harness.settled(deployment))
        deployment["status"]["observedGeneration"] = 2
        self.assertTrue(harness.settled(deployment))
        self.assertFalse(harness.settled({"metadata": {}, "status": {}}))
        revised = json.loads(json.dumps(deployment))
        revised["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "3"
        revised["metadata"]["resourceVersion"] = "999"
        self.assertEqual(harness.observable(revised), harness.observable(deployment))
        revised["metadata"]["annotations"]["a"] = "2"
        self.assertNotEqual(harness.observable(revised), harness.observable(deployment))

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
