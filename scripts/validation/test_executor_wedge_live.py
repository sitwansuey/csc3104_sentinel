"""Opt-in live Executor wedge harness for a disposable kind cluster.

Scenarios (--scenario):

  rv-race          Another writer annotates the Deployment just before the
                   Executor's patch reaches the API server. The server rejects
                   the patch's resourceVersion test and kubectl exits 1, so the
                   Executor cannot tell rejection from a lost response. It must
                   still release the workload (SUPERSEDED, basis
                   ADVANCED_WITHOUT_EVIDENCE), and a new proposal must then be
                   admitted and APPLIED.
  delete-recreate  The Executor process stops after journaling its write and
                   before sending it. An administrator deletes and recreates
                   the Deployment (new UID). Reconcile must end SUPERSEDED with
                   basis TARGET_REPLACED and no patch, and a new proposal for
                   the new UID must be admitted and APPLIED.

Safety rules: --context is required and must be in ALLOWED_CONTEXTS. Any
context or cluster naming sentinel-lab is refused. The harness creates its own
namespace with a zero-replica Deployment, makes every Executor read and patch
with a short-lived ServiceAccount token scoped to that one Deployment, and
deletes the namespace on exit, also after a failure. Guard is not under test:
the harness authorizes its own proposals. The summary it writes has no token,
kubeconfig, server address or local path. It is never run by the unit tests.
"""

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.executor.core import ACTION_ANNOTATION, ExecutionAdapters, GuardCheck, execute, reconcile
from services.executor.kubernetes_adapter import KubectlDeploymentAPI
from services.executor.store import LocalTargetCoordinator, SQLiteExecutionLedger, TargetBusy, fingerprint
from services.guard.preview import _private_file
from shared import log


ALLOWED_CONTEXTS = frozenset({"kind-sentinel-boutique-connected", "kind-sentinel-wedge"})
PROTECTED = "sentinel-lab"
SCENARIOS = ("rv-race", "delete-recreate")
RESTRICTED_CONTEXT = "sentinel-wedge-executor"
LOG = log.get_logger("validation")
SERVICE_ACCOUNT = "sentinel-wedge-executor"
NAME = "wedge-target"
RECONCILE_PASSES = 5
_SECRET_KEYS = frozenset({"token", "kubeconfig", "server", "certificate-authority-data", "client-key-data",
                          "client-certificate-data", "password"})
EXPECTED = {"rv-race": ("SUPERSEDED", "ADVANCED_WITHOUT_EVIDENCE"),
            "delete-recreate": ("SUPERSEDED", "TARGET_REPLACED")}


class Refused(ValueError):
    """The harness will not touch this cluster."""


class StopBeforeSend(BaseException):
    """Simulated process death after the write intent is journaled."""


def check_context(context):
    """Refuse anything but an allowlisted, explicitly named disposable cluster."""
    if not isinstance(context, str) or not context.strip():
        raise Refused("An explicit --context is required")
    if PROTECTED in context.lower():
        raise Refused("The protected sentinel-lab cluster is never used")
    if context not in ALLOWED_CONTEXTS:
        raise Refused("Context is not in the wedge harness allowlist: " + ", ".join(sorted(ALLOWED_CONTEXTS)))
    return context


def utc(moment=None):
    return (moment or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def sanitise(value, *, secrets=(), paths=()):
    """Drop secret-bearing keys and blank out secrets and local paths in strings."""
    if isinstance(value, dict):
        return {key: sanitise(item, secrets=secrets, paths=paths) for key, item in value.items()
                if str(key).lower() not in _SECRET_KEYS}
    if isinstance(value, (list, tuple)):
        return [sanitise(item, secrets=secrets, paths=paths) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        for path in paths:
            if path and len(Path(path).parts) > 1:  # Never a bare root such as "/" or "C:\".
                value = value.replace(str(path), "<path>")
        return value
    return value


def manifests(namespace):
    """Zero replicas: no Pod or image pull is needed, only the API object."""
    labels = {"app": NAME}
    deployment = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": NAME, "namespace": namespace, "labels": labels},
        "spec": {"replicas": 0, "selector": {"matchLabels": labels},
                 "template": {"metadata": {"labels": labels},
                              "spec": {"automountServiceAccountToken": False,
                                       "containers": [{"name": "app", "image": "registry.k8s.io/pause:3.9",
                                                       "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                                                                     "limits": {"cpu": "50m", "memory": "32Mi"}}}]}}},
    }
    rbac = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "automountServiceAccountToken": False,
         "metadata": {"name": SERVICE_ACCOUNT, "namespace": namespace}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
         "metadata": {"name": SERVICE_ACCOUNT, "namespace": namespace},
         "rules": [{"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": [NAME],
                    "verbs": ["get", "patch"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
         "metadata": {"name": SERVICE_ACCOUNT, "namespace": namespace},
         "subjects": [{"kind": "ServiceAccount", "name": SERVICE_ACCOUNT, "namespace": namespace}],
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": SERVICE_ACCOUNT}},
    ]
    return deployment, rbac


class HarnessAuthorizer:
    """Guard is not under test here: allow exactly the harness's own proposals."""

    def __init__(self, baseline):
        self.baseline = baseline

    def validate(self, proposal, snapshot, *, now):
        decision = {"schema_version": "1.0", "kind": "guard_result", "incident_id": proposal["incident_id"],
                    "action_id": proposal["action_id"], "target": proposal["target"], "verdict": "ALLOW",
                    "reason_codes": ["VALIDATION_PASSED"], "policy_version": "wedge-harness",
                    "validated_resource_version": snapshot["deployment"]["metadata"]["resourceVersion"],
                    "checked_at": utc(now)}
        baseline = dict(self.baseline, target=proposal["target"])
        return GuardCheck(decision, baseline, {})

    def assert_usable(self, check, *, now):
        return None


class WedgeHarness:
    def __init__(self, context, scenario, run_dir, *, runner=subprocess.run, kubectl="kubectl"):
        self.context = check_context(context)
        if scenario not in SCENARIOS:
            raise Refused("Unknown scenario")
        self.scenario, self.runner, self.kubectl_path = scenario, runner, kubectl
        self.directory = Path(run_dir).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.namespace = "sentinel-wedge-" + uuid4().hex[:8]
        self.cluster_id = context
        self.credentials = self.directory / ".executor-kubeconfig.json"
        self.secrets = []
        self.namespace_created = False
        self.patch_processes = 0
        self.summary = {"scenario": scenario, "context": context, "namespace": self.namespace,
                        "started_at": utc(), "status": "RUNNING", "checks": [], "results": [],
                        "scope": ("one disposable namespace on a local kind cluster; containers on one host, "
                                  "not separate machines; Guard not under test")}

    # kubectl -------------------------------------------------------------

    def kubectl(self, *arguments, namespaced=True, allow_failure=False, timeout=60):
        command = [self.kubectl_path, "--context=" + self.context, f"--request-timeout={max(10, timeout - 10)}s"]
        if namespaced:
            command.append("--namespace=" + self.namespace)
        result = self.runner(command + list(arguments), capture_output=True, check=False, timeout=timeout)
        if result.returncode and not allow_failure:
            raise RuntimeError("kubectl " + arguments[0] + " failed")
        return result

    def deployment(self):
        stdout = self.kubectl("get", "deployment", NAME, "--output=json").stdout
        return json.loads(stdout.decode("utf-8") if isinstance(stdout, bytes) else stdout)

    def apply(self, documents, label):
        path = self.directory / (label + ".json")
        path.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": documents}), encoding="utf-8")
        self.kubectl("apply", "-f", str(path))

    def check(self, name, passed, **details):
        self.summary["checks"].append({"name": name, "passed": bool(passed), **details})
        print(("PASS: " if passed else "FAIL: ") + name, flush=True)

    def ground_truth(self, label):
        deployment = self.deployment()
        metadata = deployment["metadata"]
        evidence = (metadata.get("annotations") or {}).get(ACTION_ANNOTATION)
        record = {"label": label, "resource_version": metadata["resourceVersion"],
                  "generation": metadata.get("generation"), "uid_is_original": metadata["uid"] == self.original_uid,
                  "evidence_action_id": json.loads(evidence)["action_id"] if evidence else None,
                  "field_managers": sorted({entry.get("manager", "") for entry in metadata.get("managedFields", [])}),
                  "template_is_baseline": (fingerprint(deployment["spec"]["template"])
                                           == self.baseline["template_sha256"])}
        self.summary.setdefault("ground_truth", []).append(record)
        return deployment

    # Setup -----------------------------------------------------------------

    def preflight(self):
        """Confirm the selected context's cluster, and keep its CA and server in memory only."""
        stdout = self.kubectl("config", "view", "--minify", "--raw", "--output=json", namespaced=False).stdout
        configuration = json.loads(stdout.decode("utf-8") if isinstance(stdout, bytes) else stdout)
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
        return cluster

    def prepare(self, cluster):
        # Set first: a create that fails or times out on the client may still have succeeded on the server.
        self.namespace_created = True
        self.kubectl("create", "namespace", self.namespace, namespaced=False)
        self.kubectl("label", "namespace", self.namespace, "app.kubernetes.io/managed-by=sentinel-wedge-harness",
                     namespaced=False)
        self.deployment_manifest, rbac = manifests(self.namespace)
        self.apply([self.deployment_manifest] + rbac, "manifests")
        healthy = self.deployment()
        self.original_uid = healthy["metadata"]["uid"]
        template = healthy["spec"]["template"]
        self.baseline = {"baseline_id": "BASE-WEDGE-" + uuid4().hex[:12], "pod_template": template,
                         "template_sha256": fingerprint(template)}
        self.fault()
        token = self.kubectl("create", "token", SERVICE_ACCOUNT, "--duration=10m").stdout
        token = (token.decode("utf-8") if isinstance(token, bytes) else token).strip()
        if not token:
            raise RuntimeError("No ServiceAccount token was issued")
        self.secrets.append(token)
        config = {"apiVersion": "v1", "kind": "Config",
                  "clusters": [{"name": "wedge", "cluster": {
                      "server": cluster["server"],
                      "certificate-authority-data": cluster["certificate-authority-data"]}}],
                  "users": [{"name": "executor", "user": {"token": token}}],
                  "contexts": [{"name": RESTRICTED_CONTEXT, "context": {"cluster": "wedge", "user": "executor",
                                                                        "namespace": self.namespace}}],
                  "current-context": RESTRICTED_CONTEXT}
        with self.credentials.open("x", encoding="utf-8") as stream:
            _private_file(self.credentials)
            json.dump(config, stream)

    def fault(self):
        """An operator edit to the Pod template that the rollback will undo."""
        edit = {"spec": {"template": {"metadata": {"annotations": {"sentinel.io/wedge-fault": "1"}}}}}
        self.kubectl("patch", "deployment", NAME, "--type=merge", "--patch", json.dumps(edit))

    def proposal(self, label):
        deployment = self.deployment()
        target = {"cluster_id": self.cluster_id, "namespace": self.namespace, "kind": "Deployment",
                  "name": NAME, "uid": deployment["metadata"]["uid"]}
        return {"schema_version": "1.0", "kind": "proposal", "incident_id": "INC-WEDGE-" + label,
                "action_id": "ACT-WEDGE-" + label + "-" + uuid4().hex[:12], "target": target, "action": "ROLLBACK",
                "parameters": {"baseline_id": self.baseline["baseline_id"],
                               "template_sha256": self.baseline["template_sha256"]},
                "based_on_resource_version": deployment["metadata"]["resourceVersion"],
                "rationale": "Wedge harness proposal: restore the template captured at setup.",
                "evidence_refs": ["wedge-harness"]}

    def adapters(self, target, runner):
        api = KubectlDeploymentAPI(context=RESTRICTED_CONTEXT, cluster_id=self.cluster_id,
                                   kubeconfig=self.credentials, allowed_targets=[target], runner=runner,
                                   kubectl=self.kubectl_path)
        return ExecutionAdapters(ledger=self.ledger, coordination=self.locks, cluster=api,
                                 authorizer=HarnessAuthorizer(self.baseline))

    def record(self, label, result, adapters, patches_before):
        """One summary row, counted only from the adapters and kubectl patches of this step.

        result is None when the process stopped before execute() returned. adapters is
        every ExecutionAdapters the step built (one per reconcile pass).
        """
        result = result or {"status": None}
        details = result.get("details", "")
        basis = details[len("[basis="):details.index("]")] if details.startswith("[basis=") else None
        self.summary["results"].append({
            "label": label, "status": result["status"], "basis": basis, "passes": len(adapters),
            "write_attempts": sum(item.cluster.stats["write_attempts"] for item in adapters),
            "successful_writes": sum(item.cluster.stats["successful_writes"] for item in adapters),
            "patch_processes": self.patch_processes - patches_before})
        return result["status"], basis

    def counted(self, command, **kwargs):
        if "patch" in command:
            self.patch_processes += 1
        return self.runner(command, **kwargs)

    # Scenarios -------------------------------------------------------------

    def scenario_runner(self):
        if self.scenario == "rv-race":
            def race(command, **kwargs):
                if "patch" in command:
                    # Another writer moves the version between the Executor's pre-read and its patch.
                    self.kubectl("annotate", "deployment", NAME, "sentinel.io/test-bump=" + uuid4().hex[:8],
                                 "--overwrite")
                return self.counted(command, **kwargs)
            return race

        def stop(command, **kwargs):
            if "patch" in command:
                raise StopBeforeSend()
            return self.counted(command, **kwargs)
        return stop

    def run_scenario(self):
        first = self.proposal("first")
        adapters = self.adapters(first["target"], self.scenario_runner())
        started = datetime.now(timezone.utc)
        try:
            self.record("execute", execute(first, adapters), [adapters], 0)
        except StopBeforeSend:
            # The adapter counts the attempt before it starts kubectl, so write_attempts is 1 here
            # while patch_processes stays 0: the patch never left this process.
            self.record("execute (stopped before send)", None, [adapters], 0)
            self.check("process stopped after journaling, before sending",
                       self.ledger.get(first["action_id"])["phase"] == "WRITE_STARTED")
            self.kubectl("delete", "deployment", NAME, "--wait=true")
            self.apply([self.deployment_manifest], "recreate")
            self.fault()
        patches_before = self.patch_processes
        passes = [self.adapters(first["target"], self.counted) for _ in range(RECONCILE_PASSES)]
        reconciled = [reconcile(first, pass_adapters) for pass_adapters in passes]
        self.summary["reconcile_pass_statuses"] = [item["status"] for item in reconciled]
        status, basis = self.record("reconcile", reconciled[-1], passes, patches_before)
        self.summary["time_to_resolution_seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 3)
        expected = EXPECTED[self.scenario]
        self.check("first action resolves as " + "/".join(expected), (status, basis) == expected,
                   observed=[status, basis])
        self.check("ledger releases the workload", self.ledger.get(first["action_id"])["phase"] == "DONE")
        self.summary["patch_processes_for_first_action"] = self.patch_processes
        if self.scenario == "delete-recreate":
            self.check("no patch reached kubectl for the first action", self.patch_processes == 0)
        self.ground_truth("after first action")
        second = self.proposal("second")
        try:
            self.ledger.prepare(second)
            admitted = True
        except TargetBusy:
            admitted = False
        self.check("a new proposal for the workload is admitted", admitted)
        if admitted:
            adapters = self.adapters(second["target"], self.counted)
            patches_before = self.patch_processes
            status, _ = self.record("second execute", execute(second, adapters), [adapters], patches_before)
            self.check("the new proposal is APPLIED", status == "APPLIED")
            deployment = self.ground_truth("after second action")
            self.check("the baseline template is restored",
                       fingerprint(deployment["spec"]["template"]) == self.baseline["template_sha256"])

    def cleanup(self):
        self.credentials.unlink(missing_ok=True)
        self.summary["private_credentials_removed"] = not self.credentials.exists()
        if self.namespace_created:
            result = self.kubectl("delete", "namespace", self.namespace, "--wait=false", "--ignore-not-found",
                                  namespaced=False, allow_failure=True)
            self.summary["namespace_deleted"] = result.returncode == 0

    def run(self):
        self.ledger = SQLiteExecutionLedger(self.directory / "executor.sqlite")
        self.locks = LocalTargetCoordinator(self.directory / "locks")
        try:
            cluster = self.preflight()
            self.prepare(cluster)
            self.run_scenario()
            passed = all(check["passed"] for check in self.summary["checks"])
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
                self.ledger.close()
                self.summary["finished_at"] = utc()
                self.write_summary()
        return self.summary

    def write_summary(self):
        clean = sanitise(self.summary, secrets=self.secrets,
                         paths=[self.directory, Path.home(), ROOT])
        text = json.dumps(clean, indent=2, sort_keys=True) + "\n"
        if any(secret and secret in text for secret in self.secrets):
            raise RuntimeError("Summary still contains a secret; not written")
        (self.directory / (self.scenario + ".json")).write_text(text, encoding="utf-8")
        return clean


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--context", required=True, help="kubeconfig context: " + ", ".join(sorted(ALLOWED_CONTEXTS)))
    parser.add_argument("--scenario", required=True, choices=SCENARIOS)
    parser.add_argument("--run-dir", type=Path, help="new directory for the ledger and summary")
    return parser.parse_args(argv)


def main(argv=None, *, runner=subprocess.run):
    args = parse_args(argv)
    try:
        check_context(args.context)
    except Refused as exc:
        print("Refused: " + str(exc), file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = args.run_dir or ROOT / "data/sentinel/runs" / f"executor-wedge-{stamp}-{args.scenario}"
    harness = WedgeHarness(args.context, args.scenario, directory, runner=runner)
    try:
        summary = harness.run()
    except Refused as exc:
        print("Refused: " + str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - any harness error ends the run as FAILED with exit code 1
        log.failure(LOG, "validation.wedge.failed", exc)
        print("Wedge harness FAILED: " + type(exc).__name__, file=sys.stderr)
        return 1
    print("Summary: " + str(harness.directory / (args.scenario + ".json")), flush=True)
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
