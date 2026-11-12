"""Opt-in live check of the Executor admission policy in a disposable namespace.

It installs deploy/sentinel/executor-admission.yaml under a run-specific name,
with a binding for one new namespace only, and the Role from
deploy/sentinel/executor-rbac.yaml in that namespace. A zero-replica
Deployment named demo-api is the target. Every check then runs with a
short-lived token for the sentinel-executor ServiceAccount, through a
kubeconfig that holds only that token:

  denied by the policy   replicas by JSON patch, a label change, a changed or
                         added foreign annotation, a removed sentinel
                         annotation, a Pod template change without new
                         evidence, a seal that also changes the template, a
                         lower sentinel.io/fence-epoch and a malformed one
  denied by RBAC         kubectl scale (the scale subresource), get secrets,
                         delete and create of a Deployment, and pods/exec
                         (kubectl auth can-i for create and get)
  allowed                the real ROLLBACK and RESTART patches and a seal at
                         a higher and then an equal epoch, all sent by the
                         production adapter (KubectlDeploymentAPI), and the
                         Executor's own fence without an epoch

Every denial must leave the resourceVersion unchanged. The summary records each
command's exit code and the server's message.

Safety rules: --context is required and must be kind-sentinel-boutique-connected.
Any context or cluster naming sentinel-lab is refused. The namespace, policy and
binding are created by this run and deleted on exit, also after a failure. The
summary has no token, kubeconfig, server address or local path. It is never run
by the unit tests, and it has not yet been run against a cluster.
"""

import argparse
from datetime import datetime, UTC
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validation.test_executor_wedge_live import sanitise, utc  # noqa: E402
from services.executor.core import ConditionalWriteConflict  # noqa: E402
from services.executor.kubernetes_adapter import KubectlDeploymentAPI, LiveAdapterError, WriteOutcomeUnknown  # noqa: E402
from services.executor.reconciliation import FENCE_ANNOTATION, FENCE_EPOCH_ANNOTATION, fence_marker  # noqa: E402
from services.executor.store import fingerprint  # noqa: E402
from services.guard.preview import _private_file  # noqa: E402
from shared import log  # noqa: E402
from shared.actions import restart_template  # noqa: E402


ALLOWED_CONTEXTS = frozenset({"kind-sentinel-boutique-connected"})
PROTECTED = "sentinel-lab"
POLICY_MANIFEST = ROOT / "deploy/sentinel/executor-admission.yaml"
RBAC_MANIFEST = ROOT / "deploy/sentinel/executor-rbac.yaml"
POLICY = "sentinel-executor-writes"
SHIPPED_NAMESPACE = "sentinel-demo"
SERVICE_ACCOUNT = "sentinel-executor"
NAME = "demo-api"
RESTRICTED_CONTEXT = "sentinel-admission-executor"
FOREIGN_ANNOTATION = "example.com/owner"
MANAGED_BY = "sentinel-admission-harness"
MINIMUM_MINOR = 30  # admissionregistration.k8s.io/v1 ValidatingAdmissionPolicy
POLICY_WAIT_ATTEMPTS = 30
LOG = log.get_logger("validation")
POLICY_DENIED, RBAC_DENIED, ALLOWED, OTHER = "DENIED_BY_POLICY", "DENIED_BY_RBAC", "ALLOWED", "OTHER_ERROR"


class Refused(ValueError):
    """The harness will not touch this cluster."""


def check_context(context):
    """Refuse anything but the one named disposable-namespace cluster."""
    if not isinstance(context, str) or not context.strip():
        raise Refused("An explicit --context is required")
    if PROTECTED in context.lower():
        raise Refused("The protected sentinel-lab cluster is never used")
    if context not in ALLOWED_CONTEXTS:
        raise Refused("Context is not in the admission harness allowlist: " + ", ".join(sorted(ALLOWED_CONTEXTS)))
    return context


def _replace_exactly(text, old, new, count):
    if text.count(old) != count:
        raise RuntimeError(f"The manifest no longer has {count} occurrence(s) of {old!r}; update the harness")
    return text.replace(old, new)


def render_policy(text, namespace, name):
    """The shipped policy and binding, renamed to name and bound to namespace only; nothing else changes."""
    text = _replace_exactly(text, "name: " + POLICY, "name: " + name, 2)
    text = _replace_exactly(text, "policyName: " + POLICY, "policyName: " + name, 1)
    return _replace_exactly(text, f'values: ["{SHIPPED_NAMESPACE}"]', f'values: ["{namespace}"]', 1)


def render_rbac(text, namespace):
    """The shipped ServiceAccount, Role and RoleBinding, moved to namespace."""
    return _replace_exactly(text, "namespace: " + SHIPPED_NAMESPACE, "namespace: " + namespace, 4)


def deployment_manifest(namespace):
    """Zero replicas: no Pod or image pull is needed, only the API object."""
    labels = {"app": NAME}
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": NAME, "namespace": namespace, "labels": labels,
                     "annotations": {FOREIGN_ANNOTATION: "platform"}},
        "spec": {"replicas": 0, "selector": {"matchLabels": labels},
                 "template": {"metadata": {"labels": labels},
                              "spec": {"automountServiceAccountToken": False,
                                       "containers": [{"name": "app", "image": "registry.k8s.io/pause:3.9",
                                                       "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                                                                     "limits": {"cpu": "50m", "memory": "32Mi"}}}]}}},
    }


def _pointer(key):
    return "/metadata/annotations/" + key.replace("~", "~0").replace("/", "~1")


def denial_patches(baseline_template):
    """Each request the policy must deny, as (check name, JSON Patch). Sent after the first seal."""
    probe_template = json.loads(json.dumps(baseline_template))
    probe_template.setdefault("metadata", {}).setdefault("annotations", {})["sentinel.io/admission-probe"] = "1"
    fence = json.dumps({"action_id": "ACT-ADMISSION-PROBE", "nonce": "f" * 32, "epoch": 9}, separators=(",", ":"))
    return [
        ("replicas changed by JSON patch", [{"op": "replace", "path": "/spec/replicas", "value": 1}]),
        ("label added", [{"op": "add", "path": "/metadata/labels/sentinel-probe", "value": "1"}]),
        ("foreign annotation changed", [{"op": "replace", "path": _pointer(FOREIGN_ANNOTATION), "value": "attacker"}]),
        ("foreign annotation added", [{"op": "add", "path": _pointer("example.com/added"), "value": "1"}]),
        ("sentinel fence annotation removed", [{"op": "remove", "path": _pointer(FENCE_ANNOTATION)}]),
        ("Pod template changed without new evidence",
         [{"op": "replace", "path": "/spec/template", "value": probe_template}]),
        ("seal that also changes the Pod template",
         [{"op": "replace", "path": "/spec/template", "value": probe_template},
          {"op": "replace", "path": _pointer(FENCE_ANNOTATION), "value": fence},
          {"op": "replace", "path": _pointer(FENCE_EPOCH_ANNOTATION), "value": "9"}]),
        ("lower sentinel.io/fence-epoch", [{"op": "replace", "path": _pointer(FENCE_EPOCH_ANNOTATION), "value": "4"}]),
        ("malformed sentinel.io/fence-epoch",
         [{"op": "replace", "path": _pointer(FENCE_EPOCH_ANNOTATION), "value": "06"}]),
    ]


RBAC_CHECKS = (
    ("kubectl scale (scale subresource)", ("scale", "deployment", NAME, "--replicas=1")),
    ("get secrets", ("get", "secrets")),
    ("delete the Deployment", ("delete", "deployment", NAME, "--dry-run=server")),
    ("create a Deployment", ("create", "deployment", "sentinel-admission-probe",
                             "--image=registry.k8s.io/pause:3.9", "--dry-run=server")),
    ("pods/exec create (auth can-i)", ("auth", "can-i", "create", "pods", "--subresource=exec")),
    ("pods/exec get (auth can-i)", ("auth", "can-i", "get", "pods", "--subresource=exec")),
)


def classify(result, policy_name):
    """ALLOWED, DENIED_BY_POLICY, DENIED_BY_RBAC or OTHER_ERROR, from one kubectl result."""
    stdout, stderr = (_text(result.stdout), _text(result.stderr))
    if result.returncode == 0:
        return ALLOWED
    if "ValidatingAdmissionPolicy" in stderr and f"'{policy_name}'" in stderr:
        return POLICY_DENIED
    if ("forbidden" in stderr.lower() and "cannot" in stderr) or stdout.strip().startswith("no"):
        return RBAC_DENIED
    return OTHER


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


class AdmissionHarness:
    def __init__(self, context, run_dir, *, runner=subprocess.run, kubectl="kubectl", sleep=time.sleep):
        self.context = check_context(context)
        self.runner, self.kubectl_path, self.sleep = runner, kubectl, sleep
        self.directory = Path(run_dir).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.namespace = "sentinel-admission-" + uuid4().hex[:8]
        self.policy_name = self.namespace + "-executor-writes"
        self.credentials = self.directory / ".executor-kubeconfig.json"
        self.secrets = []
        self.namespace_created = self.policy_created = False
        self.last_result = None
        self.summary = {"context": context, "namespace": self.namespace, "policy": self.policy_name,
                        "started_at": utc(), "status": "RUNNING", "checks": [],
                        "scope": ("one disposable namespace on a local kind cluster; one API server on one host; "
                                  "the shipped policy under a run-specific name and binding")}

    # kubectl -------------------------------------------------------------

    def kubectl(self, *arguments, namespaced=True, executor=False, allow_failure=False, timeout=60):
        command = [self.kubectl_path]
        if executor:
            command += ["--kubeconfig=" + str(self.credentials), "--context=" + RESTRICTED_CONTEXT]
        else:
            command.append("--context=" + self.context)
        command.append(f"--request-timeout={max(10, timeout - 10)}s")
        if namespaced:
            command.append("--namespace=" + self.namespace)
        result = self.runner(command + list(arguments), capture_output=True, check=False, timeout=timeout)
        if result.returncode and not allow_failure:
            raise RuntimeError("kubectl " + arguments[0] + " failed")
        return result

    def deployment(self):
        return json.loads(_text(self.kubectl("get", "deployment", NAME, "--output=json").stdout))

    def apply_file(self, path):
        self.kubectl("apply", "-f", str(path))

    def write(self, label, text):
        path = self.directory / label
        path.write_text(text, encoding="utf-8")
        return path

    # Setup ---------------------------------------------------------------

    def preflight(self):
        """Confirm the selected context's cluster and its version; keep its CA and server in memory only."""
        stdout = self.kubectl("config", "view", "--minify", "--raw", "--output=json", namespaced=False).stdout
        configuration = json.loads(_text(stdout))
        contexts, clusters = configuration.get("contexts", []), configuration.get("clusters", [])
        if len(contexts) != 1 or contexts[0].get("name") != self.context or len(clusters) != 1:
            raise Refused("The selected context could not be confirmed")
        if PROTECTED in str(contexts[0].get("context", {}).get("cluster", "")).lower() or \
                PROTECTED in str(clusters[0].get("name", "")).lower():
            raise Refused("The selected context points at the protected sentinel-lab cluster")
        cluster = clusters[0]["cluster"]
        if not str(cluster.get("server", "")).startswith("https://") or "certificate-authority-data" not in cluster:
            raise Refused("Restricted credentials need the cluster's HTTPS server and CA data")
        self.secrets += [cluster["server"], cluster["certificate-authority-data"]]
        version = json.loads(_text(self.kubectl("version", "--output=json", namespaced=False).stdout))
        server = version.get("serverVersion", {})
        minor = int("".join(character for character in str(server.get("minor", "")) if character.isdigit()) or 0)
        self.summary["server_version"] = f"{server.get('major')}.{server.get('minor')}"
        if str(server.get("major")) != "1" or minor < MINIMUM_MINOR:
            raise Refused("ValidatingAdmissionPolicy v1 needs Kubernetes 1.30 or later")
        return cluster

    def prepare(self, cluster):
        # Set first: a create that fails or times out on the client may still have succeeded on the server.
        self.namespace_created = True
        self.kubectl("create", "namespace", self.namespace, namespaced=False)
        self.kubectl("label", "namespace", self.namespace, "app.kubernetes.io/managed-by=" + MANAGED_BY,
                     namespaced=False)
        self.apply_file(self.write("deployment.json", json.dumps(deployment_manifest(self.namespace))))
        self.apply_file(self.write("executor-rbac.yaml",
                                   render_rbac(RBAC_MANIFEST.read_text(encoding="utf-8"), self.namespace)))
        healthy = self.deployment()
        template = healthy["spec"]["template"]
        self.target = {"cluster_id": self.context, "namespace": self.namespace, "kind": "Deployment",
                       "name": NAME, "uid": healthy["metadata"]["uid"]}
        self.baseline = template
        self.original = {"replicas": healthy["spec"].get("replicas"), "labels": healthy["metadata"].get("labels")}
        token = _text(self.kubectl("create", "token", SERVICE_ACCOUNT, "--duration=10m").stdout).strip()
        if not token:
            raise RuntimeError("No ServiceAccount token was issued")
        self.secrets.append(token)
        config = {"apiVersion": "v1", "kind": "Config",
                  "clusters": [{"name": "admission", "cluster": {
                      "server": cluster["server"],
                      "certificate-authority-data": cluster["certificate-authority-data"]}}],
                  "users": [{"name": "executor", "user": {"token": token}}],
                  "contexts": [{"name": RESTRICTED_CONTEXT, "context": {"cluster": "admission", "user": "executor",
                                                                        "namespace": self.namespace}}],
                  "current-context": RESTRICTED_CONTEXT}
        with self.credentials.open("x", encoding="utf-8") as stream:
            _private_file(self.credentials)
            json.dump(config, stream)
        self.policy_created = True  # Set first, as for the namespace.
        self.apply_file(self.write("executor-admission.yaml",
                                   render_policy(POLICY_MANIFEST.read_text(encoding="utf-8"),
                                                 self.namespace, self.policy_name)))
        if not self.await_policy():
            raise RuntimeError("The admission policy was not enforced in time")

    def await_policy(self):
        """Policies take effect asynchronously: probe with a server-side dry run until it is denied."""
        probe = json.dumps([{"op": "add", "path": "/metadata/labels/sentinel-probe", "value": "1"}])
        for attempt in range(POLICY_WAIT_ATTEMPTS):
            path = self.write("probe.json", probe)
            result = self.kubectl("patch", "deployment", NAME, "--type=json", "--patch-file", str(path),
                                  "--dry-run=server", executor=True, allow_failure=True)
            if classify(result, self.policy_name) == POLICY_DENIED:
                self.summary["policy_enforced_after_probes"] = attempt + 1
                return True
            self.sleep(1)
        return False

    # Checks --------------------------------------------------------------

    def record(self, name, expected, result, before, after, **details):
        outcome = classify(result, self.policy_name)
        unchanged = before == after
        passed = outcome == expected and (expected == ALLOWED or unchanged)
        self.summary["checks"].append({
            "name": name, "expected": expected, "outcome": outcome, "exit_code": result.returncode,
            "stdout": _text(result.stdout).strip()[:400] if outcome != ALLOWED else "",
            "message": _text(result.stderr).strip()[:600], "resource_version_unchanged": unchanged,
            "passed": passed, **details})
        print(("PASS: " if passed else "FAIL: ") + name, flush=True)
        return passed

    def version(self):
        return self.deployment()["metadata"]["resourceVersion"]

    def denied_patch(self, name, operations):
        before = self.version()
        path = self.write("check.json", json.dumps(operations))
        result = self.kubectl("patch", "deployment", NAME, "--type=json", "--patch-file", str(path),
                              executor=True, allow_failure=True)
        self.record(name, POLICY_DENIED, result, before, self.version())

    def denied_by_rbac(self, name, arguments):
        before = self.version()
        result = self.kubectl(*arguments, executor=True, allow_failure=True)
        self.record(name, RBAC_DENIED, result, before, self.version())

    def recording_runner(self, command, **kwargs):
        self.last_result = self.runner(command, **kwargs)
        return self.last_result

    def adapter(self):
        return KubectlDeploymentAPI(context=RESTRICTED_CONTEXT, cluster_id=self.context,
                                    kubeconfig=self.credentials, allowed_targets=[self.target],
                                    runner=self.recording_runner, kubectl=self.kubectl_path)

    def allowed(self, name, send):
        """Run one production-adapter write; it must commit."""
        before = self.version()
        self.last_result = None
        try:
            send(self.adapter(), before)
            error = None
        except (WriteOutcomeUnknown, ConditionalWriteConflict, LiveAdapterError) as exc:
            error = type(exc).__name__
        result = self.last_result or subprocess.CompletedProcess([], 1, b"", b"no command was sent")
        if error is not None and result.returncode == 0:
            result = subprocess.CompletedProcess(result.args, 1, result.stdout, b"response did not confirm the write")
        return self.record(name, ALLOWED, result, before, self.version(), adapter_error=error)

    def evidence(self, template, action=None):
        evidence = {"action_id": "ACT-ADMISSION-" + uuid4().hex[:12], "proposal_sha256": uuid4().hex * 2,
                    "target_uid": self.target["uid"], "template_sha256": fingerprint(template),
                    "attempt_id": uuid4().hex}
        if action:
            evidence["action"] = action
        return evidence

    def rollback(self, api, version):
        api.conditional_restore(target=self.target, version=version, template=self.baseline,
                                evidence=self.evidence(self.baseline))

    def restart(self, api, version):
        template = restart_template(self.baseline, "RST-" + uuid4().hex[:16])
        api.conditional_restore(target=self.target, version=version, template=template,
                                evidence=self.evidence(template, "RESTART"))

    def sealer(self, epoch):
        def seal(api, version):
            api.fence(target=self.target, version=version,
                      marker=fence_marker("ACT-ADMISSION-SEAL-" + uuid4().hex[:8], uuid4().hex, epoch))
        return seal

    def run_checks(self):
        # An operator edit to the Pod template, made with administrator credentials, for the rollback to undo.
        edit = json.dumps({"spec": {"template": {"metadata": {"annotations": {"sentinel.io/admission-fault": "1"}}}}})
        self.kubectl("patch", "deployment", NAME, "--type=merge", "--patch", edit)
        self.allowed("real ROLLBACK patch (production adapter)", self.rollback)
        self.allowed("real RESTART patch (production adapter)", self.restart)
        self.allowed("seal at epoch 5 (production adapter, metadata only)", self.sealer(5))
        for name, operations in denial_patches(self.baseline):
            self.denied_patch(name, operations)
        for name, arguments in RBAC_CHECKS:
            self.denied_by_rbac(name, arguments)
        self.allowed("seal at a higher epoch, 6", self.sealer(6))
        self.allowed("seal at an equal epoch, 6", self.sealer(6))
        self.allowed("Executor fence without an epoch", self.sealer(None))
        final = self.deployment()
        annotations = final["metadata"].get("annotations", {})
        truth = {"replicas_unchanged": final["spec"].get("replicas") == self.original["replicas"],
                 "labels_unchanged": final["metadata"].get("labels") == self.original["labels"],
                 "foreign_annotation_unchanged": annotations.get(FOREIGN_ANNOTATION) == "platform",
                 "fence_epoch": annotations.get(FENCE_EPOCH_ANNOTATION)}
        self.summary["final_state"] = truth
        self.summary["checks"].append({"name": "final state", "passed": all(
            truth[key] for key in ("replicas_unchanged", "labels_unchanged", "foreign_annotation_unchanged"))
            and truth["fence_epoch"] == "6"})

    def remove_policy(self):
        """Delete the binding, then the policy; both are cluster-scoped and outlive the namespace."""
        results = [self.kubectl("delete", kind, self.policy_name, "--ignore-not-found", namespaced=False,
                                allow_failure=True).returncode
                   for kind in ("validatingadmissionpolicybinding", "validatingadmissionpolicy")]
        self.summary["policy_deleted"] = not any(results)

    def cleanup(self):
        self.credentials.unlink(missing_ok=True)
        self.summary["private_credentials_removed"] = not self.credentials.exists()
        if self.policy_created:
            self.remove_policy()
        if self.namespace_created:
            result = self.kubectl("delete", "namespace", self.namespace, "--wait=false", "--ignore-not-found",
                                  namespaced=False, allow_failure=True)
            self.summary["namespace_deleted"] = result.returncode == 0

    def run(self):
        try:
            cluster = self.preflight()
            self.prepare(cluster)
            self.run_checks()
            passed = bool(self.summary["checks"]) and all(check["passed"] for check in self.summary["checks"])
            self.summary["status"] = "PASS" if passed else "FAIL"
        except Refused:
            self.summary["status"] = "REFUSED"
            raise
        except BaseException as exc:
            self.summary.update(status="FAILED", error_type=type(exc).__name__)
            raise
        finally:
            try:
                self.cleanup()
            finally:
                self.summary["finished_at"] = utc()
                self.write_summary()
        return self.summary

    def write_summary(self):
        clean = sanitise(self.summary, secrets=self.secrets, paths=[self.directory, Path.home(), ROOT])
        text = json.dumps(clean, indent=2, sort_keys=True) + "\n"
        if any(secret and secret in text for secret in self.secrets):
            raise RuntimeError("Summary still contains a secret; not written")
        (self.directory / "admission.json").write_text(text, encoding="utf-8")
        return clean


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--context", required=True, help="kubeconfig context: " + ", ".join(sorted(ALLOWED_CONTEXTS)))
    parser.add_argument("--run-dir", type=Path, help="new directory for the summary")
    return parser.parse_args(argv)


def main(argv=None, *, runner=subprocess.run, sleep=time.sleep):
    args = parse_args(argv)
    try:
        check_context(args.context)
    except Refused as exc:
        print("Refused: " + str(exc), file=sys.stderr)
        return 2
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = args.run_dir or ROOT / "data/sentinel/runs" / f"executor-admission-{stamp}"
    harness = AdmissionHarness(args.context, directory, runner=runner, sleep=sleep)
    try:
        summary = harness.run()
    except Refused as exc:
        print("Refused: " + str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - any harness error ends the run as FAILED with exit code 1
        log.failure(LOG, "validation.admission.failed", exc)
        print("Admission harness FAILED: " + type(exc).__name__, file=sys.stderr)
        return 1
    print("Summary: " + str(harness.directory / "admission.json"), flush=True)
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
