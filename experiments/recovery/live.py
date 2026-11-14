"""Opt-in real-cluster executor experiment, restricted to the named kind lab.

An administrator prepares the lab, injects faults and measures HTTP health. All
executor reads/patches use a separate, short-lived ServiceAccount kubeconfig.
Planner proposals and single-app dependency attestations are operator inputs;
this is not an implementation of Planner or Verifier. No relative undo is used.
"""

import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from runtime.approval_session import (
    MODES, LocalApprovalAuthority, approver_for, executor_client_from_environment, operator_identity, require_attended,
)
from runtime.executor_pipeline import process_executor_requests
from runtime.guard_pipeline import process_guard_requests
from runtime.image_scans import read_image_scans
from runtime.live_executor import build_adapters
from services.coordinator.core import LocalEventBus, SQLiteStore, handle
from services.executor.core import ACTION_ANNOTATION, execute, reconcile
from services.executor.store import ActionConflict, SQLiteExecutionLedger, fingerprint
from services.guard.core import proposal_fingerprint
from services.guard.preview import _private_file
from services.guard.scale import autoscalers_for
from services.executor.kubernetes_adapter import KubectlDeploymentAPI, WriteOutcomeUnknown
from services.observer.kubernetes_adapter import deployment_to_observation
from services.observer.core import Observer
from shared import log, provenance

LOG = log.get_logger("recovery")


CONTEXT = "kind-sentinel-lab"
NAMESPACE = "sentinel-demo"
NAME = "demo-api"
RESTRICTED_CONTEXT = "sentinel-executor-lab"
CVE_GATE_RULES = ROOT / "deploy/security/cve-gate.json"  # the reviewed P5.02 gate block
# Guard resource ceilings for the generic lab workload. Boutique targets take theirs from the catalog.
LAB_CEILINGS = {"container_requests": {"cpu": "100m", "memory": "64Mi"},
                "container_limits": {"cpu": "500m", "memory": "128Mi"},
                "pod_requests": {"cpu": "250m", "memory": "128Mi"},
                "rollout_requests": {"cpu": "1", "memory": "512Mi"},
                "max_replicas": 2, "max_surge_replicas": 1}


def now():
    return datetime.now(timezone.utc)


def utc(value=None):
    return (now() if value is None else value).isoformat().replace("+00:00", "Z")


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def policy_ceilings(ceilings):
    """The Guard policy's resource and workload constraints for one target's ceilings."""
    return {"resource_constraints": {"require_requests_and_limits": True,
                                     "max_container_requests": deepcopy(ceilings["container_requests"]),
                                     "max_container_limits": deepcopy(ceilings["container_limits"])},
            "workload_constraints": {"max_pod_requests": deepcopy(ceilings["pod_requests"]),
                                     "max_rollout_requests": deepcopy(ceilings["rollout_requests"]),
                                     "max_replicas": ceilings["max_replicas"],
                                     "max_surge_replicas": ceilings["max_surge_replicas"]}}


def loaded_image_digest(inspection, listing):
    """Resolve the manifest behind the named image, never its config ID.

    kind/containerd can expose repoDigests under import-generated names. In that
    case the exact tag's containerd target is the authoritative manifest digest.
    """
    for item in inspection["status"].get("repoDigests", []):
        if re.fullmatch(r"(?:docker.io/library/)?sentinel-demo@sha256:[a-f0-9]{64}", item):
            return item if item.startswith("docker.io/") else "docker.io/library/" + item
    matches = []
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] == "docker.io/library/sentinel-demo:0.1.0":
            require(re.fullmatch(r"sha256:[a-f0-9]{64}", fields[2]), "Invalid named image manifest digest")
            matches.append("docker.io/library/sentinel-demo@" + fields[2])
    require(len(matches) == 1, "Could not unambiguously resolve the loaded lab image's manifest digest")
    return matches[0]


class Experiment:
    # The experiment supplies workload configuration; production adapters stay generic.
    context = CONTEXT
    namespace = NAMESPACE
    name = NAME
    cluster_id = "sentinel-lab"
    restricted_context = RESTRICTED_CONTEXT
    entrypoint = Path(__file__).resolve()
    health_source = "sentinel-live-lab-http"
    health_description = "HTTP and replica health"
    allowed_registries = ["docker.io"]
    service_account = "default"
    rbac_path = ROOT / "deploy/sentinel/executor-rbac.yaml"
    ceilings = LAB_CEILINGS
    publish_dependency_results = False
    health_outcome_description = " after executor rollback; Role 6 outcome remains pending"
    approval_mode = "interactive"
    operator = None  # Interactive operator identity; lab-operator when unset.
    # P5.02: a directory of scan summaries turns on the CVE gate (deploy/security/cve-gate.json) in this run's
    # policy; the Guard and the Executor then read their image_scans from it. None leaves the gate off.
    image_scans = None
    cve_exceptions = None

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.credentials = self.directory / ".executor-kubeconfig.json"
        self.summary = {"mode": "LIVE Kubernetes; restricted executor credentials",
                        "started_at": utc(), "checks": [], "status": "RUNNING",
                        "scope": "single-node kind, local coordination; no Planner/Verifier implementation",
                        "provenance": provenance.collect(ROOT), "timeline": []}
        self.baseline = None
        self.target = None
        self.store = SQLiteStore(self.directory / "coordinator.sqlite")
        self.bus = LocalEventBus()
        self.authority = self.approver = None
        self.configure_approval(self.approval_mode, self.operator)

    def configure_approval(self, mode, operator=None):
        """Choose the approver before the run; nothing starts until a target exists."""
        identity = operator_identity(mode, operator)
        require(self.authority is None, "Approval mode cannot change after the authority started")
        self.approval_mode, self.operator = mode, operator
        self.summary["operator_approval"] = {"mode": mode, "identity": None if mode == "reject" else identity,
                                             "via": "approval_http"}

    def approval_not_requested(self, reason):
        """Record that this scenario asks for no approval; the authority can then never start."""
        require(self.authority is None, "Approval mode cannot change after the authority started")
        self.approval_mode = self.operator = None
        self.summary["operator_approval"] = {"mode": "not-requested", "identity": None, "via": None,
                                             "human": False, "decided_by": reason}

    def approval_authority(self):
        """Start this run's approval authority once the measured target identity is known."""
        if self.authority is None:
            require(self.approval_mode in MODES, "This scenario requests no approval")
            require(self.target is not None, "The approval authority needs the measured target identity")
            authority = LocalApprovalAuthority(self.directory, self.directory / "coordinator.sqlite", self.target,
                                               operator=operator_identity(self.approval_mode, self.operator))
            authority.start()
            self.authority = authority
            self.approver = approver_for(self.approval_mode, authority, relay=getattr(self, "approval_relay", None))
            self.summary["operator_approval"] = self.approver.describe()
        return self.authority

    def request_approval(self, proposal, decision):
        """Ask this run's approver. An approval is only ever created through approval_http."""
        self.approval_authority()
        try:
            return self.approver(proposal, decision)
        finally:
            self.summary["operator_approval"] = self.approver.describe()

    def close_approval_authority(self):
        if self.authority is not None:
            self.authority.close()
            self.summary["operator_approval"]["authority_stopped"] = not self.authority.running()

    def kubectl(self, *arguments, restricted=False, timeout=40, allow_failure=False):
        command = ["kubectl", "--context=" + (self.restricted_context if restricted else self.context),
                   "--namespace=" + self.namespace, f"--request-timeout={max(10, timeout - 5)}s"]
        if restricted:
            command.append("--kubeconfig=" + str(self.credentials))
        result = subprocess.run(command + list(arguments), capture_output=True,
                                text=True, timeout=timeout, check=False)
        if not allow_failure:
            require(result.returncode == 0, "kubectl operation failed: " + arguments[0])
        return result

    def deployment(self):
        return json.loads(self.kubectl("get", "deployment", self.name, "-o", "json").stdout)

    def guard_state(self, proposal):
        """The Guard's trusted live state: the Deployment, and for a RESTART the restart history.

        The history comes from this run's Executor ledger (executor.sqlite), the
        same source the Executor's own Guard check reads under its target lock.
        Without it the Guard cannot check the restart budget and escalates the
        RESTART as RESTART_BUDGET_UNKNOWN.
        """
        state = {"cluster_id": self.cluster_id, "captured_at": utc(), "deployment": self.deployment()}
        if proposal.get("action") == "RESTART":
            with SQLiteExecutionLedger(self.directory / "executor.sqlite") as ledger:
                state["restart_history"] = ledger.restart_history(proposal["target"])
        if proposal.get("action") == "SCALE":
            # P5.07: the HorizontalPodAutoscalers that scale this Deployment, read like the Executor's own check.
            listing = json.loads(self.kubectl("get", "horizontalpodautoscalers.autoscaling", "-o", "json").stdout)
            state["autoscalers"] = autoscalers_for(listing, self.name)
        if self.image_scans is not None:
            state["image_scans"] = self.read_image_scans(state["deployment"])
        return state

    def read_image_scans(self, deployment):
        """image_scans for the baseline's and the current template's images, as the Executor's reader builds it."""
        images = set()
        for template in (self.baseline["pod_template"], deployment["spec"]["template"]):
            for group in ("containers", "initContainers"):
                images.update(container["image"] for container in template["spec"].get(group, []))
        return read_image_scans(self.image_scans, sorted(images), self.cve_exceptions)

    def phase(self, name, scenario=None):
        """Record a lifecycle step so evidence timings are measured, not typed."""
        self.summary.setdefault("timeline", []).append({"phase": name, "at": utc(), "scenario": scenario})

    def check(self, name, **details):
        self.summary["checks"].append({"name": name, "passed": True, "at": utc(), **details})
        print("PASS: " + name, flush=True)

    def prepare_image(self):
        deployment = self.deployment()
        require(deployment["metadata"]["name"] == self.name, "Unexpected lab Deployment")
        # Resolve the actual node image manifest, not Docker's image-config hash.
        inspection = subprocess.run(
            ["docker", "exec", "sentinel-lab-control-plane", "crictl", "inspecti", "sentinel-demo:0.1.0"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        require(inspection.returncode == 0, "The lab image must already be loaded into kind")
        listing = subprocess.run(
            ["docker", "exec", "sentinel-lab-control-plane", "ctr", "--namespace", "k8s.io", "images", "list"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        require(listing.returncode == 0, "Could not inspect the named node image's manifest")
        image = loaded_image_digest(json.loads(inspection.stdout), listing.stdout)
        # Register the same local bytes under their immutable reference. This
        # needs no registry push/pull and preserves imagePullPolicy: Never.
        alias = subprocess.run(
            ["docker", "exec", "sentinel-lab-control-plane", "ctr", "--namespace", "k8s.io", "images", "tag", "--force",
             "docker.io/library/sentinel-demo:0.1.0", image], capture_output=True, text=True, timeout=30, check=False,
        )
        require(alias.returncode == 0, "Could not register the local immutable image reference")
        self.kubectl("set", "image", "deployment/" + self.name, "demo-api=" + image)
        self.kubectl("set", "env", "deployment/" + self.name, "--containers=demo-api", "STARTUP_FAILURE=false")
        self.kubectl("rollout", "status", "deployment/" + self.name, "--timeout=120s", timeout=160)
        return image

    def prepare(self):
        image = self.prepare_image()
        deployment = self.deployment()
        self.target = {"cluster_id": self.cluster_id, "namespace": self.namespace,
                       "kind": "Deployment", "name": self.name, "uid": deployment["metadata"]["uid"]}
        save(self.directory / "allowed-targets.json", self.execution_scope())
        template = deployment["spec"]["template"]
        samples = self.health_window(template)
        self.baseline = self.measured_baseline(template, samples)
        save(self.directory / "baseline.json", self.baseline)
        # The Executor can also read baselines keyed by target name; it refuses one for another target.
        save(self.directory / "baselines" / (self.name + ".json"), self.baseline)
        save(self.directory / "baseline-health.json", samples)
        policy = self.executor_policy()
        save(self.directory / "policy.json", policy)
        self.policy = policy
        self.issue_restricted_credentials()
        self.check("healthy baseline captured with real service samples and immutable image", image=image)

    def measured_baseline(self, template, samples):
        """The healthy baseline of self.target: its template and the health window that measured it."""
        return {"baseline_id": "BASE-LIVE-" + uuid4().hex[:12], "target": self.target,
                "healthy_observed_at": samples[-1]["at"], "pod_template": template,
                "template_sha256": fingerprint(template), "reference_versions": [],
                "health_evidence": {"source": self.health_source,
                    "evidence_id": "HEALTH-" + uuid4().hex, "status": "HEALTHY",
                    "target": self.target, "template_sha256": fingerprint(template),
                    "window_start": samples[0]["at"], "window_end": samples[-1]["at"]}}

    def executor_policy(self):
        """The live Guard and Executor policy for self.target alone, with its ceilings and registries."""
        return {"policy_id": "executor-live-lab", "policy_version": "executor-live-lab-1",
                  "profile": "live", "targets": [self.target], "allowed_actions": ["ROLLBACK"],
                  "require_operator_approval": True, "unknown_dependency_result": "ESCALATE",
                  "max_state_age_seconds": 30, "max_dependency_age_seconds": 300,
                  "decision_ttl_seconds": 20, "max_approval_lifetime_seconds": 300,
                  "require_server_dry_run": False,
                  "baseline_constraints": {"max_age_seconds": 1800, "min_healthy_duration_seconds": 10,
                      "trusted_sources": [self.health_source], "require_health_evidence": True,
                      "require_compatible_references": True, "require_image_digests": True},
                  "security_constraints": {"deny_privileged": True, "deny_host_access": True,
                      "allowed_image_registries": self.allowed_registries, "allowed_service_accounts": [self.service_account],
                      "forbid_service_account_change": True, **self.cve_gate()},
                  **policy_ceilings(self.ceilings)}

    def cve_gate(self):
        """{"image_vulnerability_gate": reviewed rules} when this run has a scan directory, else {}."""
        if self.image_scans is None:
            return {}
        return {"image_vulnerability_gate": json.loads(CVE_GATE_RULES.read_text(encoding="utf-8"))}

    def issue_restricted_credentials(self):
        """Apply the executor Role and write the private one-hour executor kubeconfig (never logged)."""
        self.kubectl("apply", "-f", str(self.rbac_path))
        configuration = json.loads(self.kubectl("config", "view", "--minify", "--flatten", "--raw", "-o", "json").stdout)
        cluster = configuration["clusters"][0]["cluster"]
        require("certificate-authority-data" in cluster and cluster["server"].startswith("https://"),
                "Restricted credentials require the selected cluster's CA and HTTPS server")
        token = self.kubectl("create", "token", "sentinel-executor", "--duration=1h").stdout.strip()
        private_config = {"apiVersion": "v1", "kind": "Config",
                          "clusters": [{"name": self.cluster_id, "cluster": {
                              "server": cluster["server"], "certificate-authority-data": cluster["certificate-authority-data"]}}],
                          "users": [{"name": "executor", "user": {"token": token}}],
                          "contexts": [{"name": self.restricted_context, "context": {
                              "cluster": self.cluster_id, "user": "executor", "namespace": self.namespace}}],
                          "current-context": self.restricted_context}
        with self.credentials.open("x", encoding="utf-8") as stream:
            _private_file(self.credentials)
            json.dump(private_config, stream)

    def execution_scope(self):
        """The Executor's independent target allowlist (allowed-targets.json); only this target by default."""
        return [self.target]

    def executor_names(self):
        """Deployment names the restricted executor Role may get and patch."""
        return [self.name]

    def denied_permissions(self):
        return [("delete", "deployment/" + self.name), ("get", "secrets"),
                ("create", "pods/exec"), ("patch", "deployment/outside-executor-scope")]

    def baseline_inputs(self):
        """How the Executor reads the measured baseline: one file here; Boutique uses a per-target directory."""
        return {"baseline": self.directory / "baseline.json"}

    def baseline_arguments(self):
        return [argument for key, path in self.baseline_inputs().items() if path is not None
                for argument in ("--" + key, str(path))]

    def health_window(self, expected_template):
        samples = []
        for index in range(3):
            deployment = self.deployment()
            require(deployment["spec"]["template"] == expected_template, "Healthy template changed during measurement")
            require(deployment.get("status", {}).get("readyReplicas") == 1
                    and deployment["status"].get("availableReplicas") == 1, "Application is not ready")
            result = self.kubectl("exec", "deployment/" + self.name, "--", "python3", "-c",
                "import urllib.request; r=urllib.request.urlopen('http://demo-api:8080/healthz',timeout=5); "
                "assert r.status==200; print(r.read().decode())")
            require(json.loads(result.stdout).get("status") == "ok", "HTTP health response was not healthy")
            samples.append({"at": utc(), "http_status": 200, "ready_replicas": 1,
                            "resource_version": deployment["metadata"]["resourceVersion"]})
            if index < 2:
                time.sleep(5)
        return samples

    def permissions(self):
        allowed = [(verb, "deployment/" + name) for name in self.executor_names() for verb in ("get", "patch")]
        denied = self.denied_permissions()
        results = []
        for verb, resource in allowed + denied:
            result = self.kubectl("auth", "can-i", verb, resource, restricted=True, allow_failure=True)
            expected = (verb, resource) in allowed
            require(result.stdout.strip() == ("yes" if expected else "no"), "RBAC permission did not match the restricted role")
            results.append({"verb": verb, "resource": resource, "allowed": expected})
        denial = self.kubectl("patch", "deployment/outside-executor-scope", "--type=merge",
                             "-p", '{"metadata":{"annotations":{"sentinel-test":"denied"}}}',
                             restricted=True, allow_failure=True)
        require(denial.returncode != 0 and "forbidden" in denial.stderr.lower(),
                "Out-of-scope write was not denied by Kubernetes RBAC")
        save(self.directory / "rbac-checks.json", results)
        self.check("real ServiceAccount permits only selected Deployment get/patch; forbidden write denied")

    def inject_fault(self):
        self.kubectl("set", "env", "deployment/" + self.name, "--containers=demo-api", "STARTUP_FAILURE=true")

    def fault(self, label):
        self.inject_fault()
        self.phase("fault_injected", label)
        deadline = time.monotonic() + 130
        while time.monotonic() < deadline:
            pods = json.loads(self.kubectl("get", "pods", "-l", "app=" + self.name, "-o", "json").stdout)
            crashes = [status for pod in pods["items"] for status in pod.get("status", {}).get("containerStatuses", [])
                       if status.get("state", {}).get("waiting", {}).get("reason") in {"CrashLoopBackOff", "RunContainerError", "CreateContainerError"}]
            if crashes:
                save(self.directory / (label + "-fault-pods.json"), pods)
                break
            time.sleep(2)
        else:
            raise RuntimeError("Injected fault did not reach a container startup failure")
        observer = Observer({"detection": {"failure_duration_seconds": 5}})
        for _ in range(5):
            observation = deployment_to_observation(self.deployment(), cluster_id=self.cluster_id, observed_at=now())
            incident = observer.observe(observation)
            if incident:
                save(self.directory / (label + "-incident.json"), incident)
                self.phase("incident_observed", label)
                handle(incident, self.store, self.bus)
                return incident
            time.sleep(5)
        raise RuntimeError("Live Observer did not report sustained unavailability")

    def dependency_result(self, proposal):
        """Default generic lab attestation; Boutique overrides with Role 6."""
        return {"schema_version": "1.0", "kind": "dependency_result", "incident_id": proposal["incident_id"],
                "action_id": proposal["action_id"], "target": self.target, "status": "CLEAR",
                "affected_services": [self.name], "blockers": [], "unknowns": [], "checked_at": utc()}

    def complete_recovery(self, folder, proposal, execution):
        """Generic lab keeps its original health-only path; Boutique verifies."""
        self.kubectl("rollout", "status", "deployment/" + self.name, "--timeout=120s", timeout=160)
        save(folder / "post-health.json", self.health_window(self.baseline["pod_template"]))

    def build_proposal(self, incident, folder, rationale=None):
        return {"schema_version": "1.0", "kind": "proposal", "incident_id": incident["incident_id"],
                    "action_id": "ACT-LIVE-" + uuid4().hex, "target": self.target, "action": "ROLLBACK",
                    "parameters": {"baseline_id": self.baseline["baseline_id"], "template_sha256": self.baseline["template_sha256"]},
                    "based_on_resource_version": self.deployment()["metadata"]["resourceVersion"],
                    "rationale": rationale or "Operator-authored live lab proposal: restore measured healthy template",
                    "evidence_refs": [incident["incident_id"], self.baseline["health_evidence"]["evidence_id"]]}

    def case(self, label, incident, *, coordinator=False, approval=True, rationale=None):
        folder = self.directory / label
        folder.mkdir()
        proposal = self.build_proposal(incident, folder, rationale)
        dependencies = self.dependency_result(proposal)
        record = {"action_id": proposal["action_id"], "proposal_sha256": proposal_fingerprint(proposal),
                  "policy_version": self.policy["policy_version"], "operator_id": "authorized-live-lab-operator",
                  "decision": "APPROVED", "approved_at": utc(), "expires_at": utc(now() + timedelta(seconds=180))}
        save(folder / "proposal.json", proposal)
        self.phase("proposal_saved", label)
        save(folder / "dependencies.json", dependencies)
        # This fixed record serves only the file-backed negative cases; it never reaches the Coordinator.
        save(folder / "approval.json", record if approval and not coordinator else None)
        if coordinator:
            handle(proposal, self.store, self.bus)
            if self.publish_dependency_results:
                handle(dependencies, self.store, self.bus)
            readers = dict(state_reader=self.guard_state, policy_reader=lambda p: self.policy,
                           baseline_reader=lambda p: self.baseline, dependency_reader=lambda p: dependencies)
            first = process_guard_requests(self.store, self.bus, **readers)[0]
            require(first["verdict"] == "ESCALATE" and first["reason_codes"] == ["OPERATOR_APPROVAL_REQUIRED"],
                    "Guard must require approval before live execution")
            record = self.request_approval(proposal, first)
            save(folder / "approval.json", record)
            require(record is not None, "Operator did not approve " + proposal["action_id"] + "; no Executor write attempted")
            decision = process_guard_requests(self.store, self.bus, **readers)[0]
            save(folder / "initial-guard.json", decision)
            self.phase("guard_decision", label)
            require(decision["verdict"] == "ALLOW", "Initial Guard did not allow the approved lab proposal")
        return folder, proposal

    def adapters(self, folder, *, coordinator=False, reconcile_only=False):
        return build_adapters(context=self.restricted_context, cluster_id=self.cluster_id, kubeconfig=self.credentials,
            allowed_targets=self.directory / "allowed-targets.json", ledger=self.directory / "executor.sqlite",
            locks=self.directory / "locks", policy=self.directory / "policy.json", **self.baseline_inputs(),
            dependencies=folder / "dependencies.json", approval=folder / "approval.json", server_dry_run=True,
            approval_reader=self.approval_authority().executor_client() if coordinator else None,
            reconcile_only=reconcile_only, image_scans=self.image_scans, cve_exceptions=self.cve_exceptions,
            **self.reader_inputs())

    def reader_inputs(self):
        """The Guard-reader identity for ConfigMap and Secret versions (P5.05); none here, so references escalate."""
        return {}

    def reader_arguments(self):
        """reader_inputs() as live_executor command-line arguments."""
        return []

    def negative_cases(self, incident):
        original = self.deployment()["spec"]["template"]
        for label in ("missing-approval", "hard-policy-rejection", "stale-state"):
            folder, proposal = self.case(label, incident, approval=label != "missing-approval")
            adapters = self.adapters(folder)
            try:
                if label == "hard-policy-rejection":
                    policy = deepcopy(self.policy)
                    policy["allowed_actions"] = []
                    save(self.directory / "policy.json", policy)
                if label == "stale-state":
                    self.kubectl("annotate", "deployment/" + self.name, "sentinel.io/stale-probe=" + uuid4().hex, "--overwrite")
                result = execute(proposal, adapters)
                save(folder / "execution.json", result)
                require(result["status"] in {"FAILED", "CONFLICT"}, "Rejected input unexpectedly executed")
                require(adapters.cluster.stats["write_attempts"] == 0, "Rejected input attempted a Kubernetes write")
                require(self.deployment()["spec"]["template"] == original, "Rejected input changed the template")
                self.check(label + " makes zero executor writes", status=result["status"])
            finally:
                adapters.ledger.close()
                save(self.directory / "policy.json", self.policy)

    def conditional_race(self, incident):
        folder, proposal = self.case("server-cas-race", incident)
        before = self.deployment()
        def race(command, **kwargs):
            if "patch" in command:
                self.kubectl("annotate", "deployment/" + self.name, "sentinel.io/race=" + uuid4().hex, "--overwrite")
            return subprocess.run(command, **kwargs)
        api = KubectlDeploymentAPI(context=self.restricted_context, cluster_id=self.cluster_id, kubeconfig=self.credentials,
                                   allowed_targets=[self.target], runner=race)
        evidence = {"action_id": proposal["action_id"], "proposal_sha256": proposal_fingerprint(proposal),
                    "target_uid": self.target["uid"], "template_sha256": self.baseline["template_sha256"], "attempt_id": uuid4().hex}
        try:
            api.conditional_restore(target=self.target, version=before["metadata"]["resourceVersion"],
                                    template=self.baseline["pod_template"], evidence=evidence)
        except WriteOutcomeUnknown:
            pass
        else:
            raise RuntimeError("Server accepted a patch with an obsolete resourceVersion")
        after = self.deployment()
        require(after["spec"]["template"] == before["spec"]["template"], "Stale server patch altered the template")
        require(after["metadata"].get("annotations", {}).get(ACTION_ANNOTATION) ==
                before["metadata"].get("annotations", {}).get(ACTION_ANNOTATION), "Stale server patch altered action evidence")
        save(folder / "result.json", {"patch_attempts": api.stats["write_attempts"], "template_unchanged": True,
                                     "note": "Transport classifies server errors conservatively; core would reconcile, not retry."})
        self.check("real API rejects JSON Patch after resourceVersion changes", patch_attempts=1)

    def positive(self, incident):
        folder, proposal = self.case("approved-rollback", incident, coordinator=True)
        adapters = self.adapters(folder, coordinator=True)
        try:
            command = [sys.executable, "-m", "runtime.live_executor", "--proposal", str(folder / "proposal.json"),
                "--allowed-targets", str(self.directory / "allowed-targets.json"), "--kubeconfig", str(self.credentials),
                "--context", self.restricted_context, "--cluster-id", self.cluster_id, "--ledger", str(self.directory / "executor.sqlite"),
                "--locks", str(self.directory / "locks"), "--policy", str(self.directory / "policy.json"),
                *self.baseline_arguments(), "--dependencies", str(folder / "dependencies.json"),
                "--approval", str(folder / "approval.json"), *self.reader_arguments()]
            with adapters.coordination.hold(self.target):
                rival = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
            require(rival.returncode == 1 and json.loads(rival.stderr)["error"]["code"] == "TARGET_BUSY",
                    "A competing process was not excluded")
            self.check("competing real worker is excluded before mutation")
            results = process_executor_requests(self.store, self.bus, adapters=adapters)
            require(len(results) == 1 and results[0]["status"] == "APPLIED", "Live executor did not apply the healthy template")
            result = results[0]
            require(adapters.cluster.stats["write_attempts"] == 1, "Expected exactly one live mutation")
            require(execute(proposal, adapters) == result, "Duplicate did not return the identical saved result")
            changed = dict(proposal, rationale="changed intent under reused action ID")
            try:
                execute(changed, adapters)
            except ActionConflict:
                pass
            else:
                raise RuntimeError("Changed intent under an existing ID was accepted")
            require(adapters.cluster.stats["write_attempts"] == 1, "Duplicate or changed ID repeated a mutation")
            require(self.store.incident(incident["incident_id"])["state"] == "VERIFYING", "APPLIED must only route to VERIFYING")
            save(folder / "execution.json", result)
            self.phase("execution_recorded", folder.name)
            save(folder / "transport-stats.json", adapters.cluster.stats)
            self.check("approved real rollback, duplicate replay and changed-ID rejection", status="APPLIED", writes=1,
                       coordinator_state="VERIFYING")
        finally:
            adapters.ledger.close()
        self.complete_recovery(folder, proposal, result)
        self.check(self.health_description + self.health_outcome_description)

    def crash_restart(self):
        incident = self.fault("crash")
        folder, proposal = self.case("crash-after-write", incident, coordinator=True)
        configuration = {"directory": str(self.directory), "folder": str(folder), "credentials": str(self.credentials),
                         "cluster_id": self.cluster_id, "context": self.restricted_context,
                         **{key: str(path) for key, path in self.baseline_inputs().items()
                            if key == "baselines" and path is not None},
                         # The CVE gate's inputs (P5.02): without them the worker's own Guard re-check of a run whose
                         # policy has the gate on has no scans, fails closed and never reaches its write.
                         **{key: str(path) for key, path in (("image_scans", self.image_scans),
                                                             ("cve_exceptions", self.cve_exceptions))
                            if path is not None}}
        save(folder / "worker.json", configuration)
        # The Executor token reaches the worker only through its environment, never argv or worker.json.
        worker = subprocess.run([sys.executable, str(self.entrypoint), "--crash-worker", str(folder / "worker.json")],
                                cwd=ROOT, capture_output=True, text=True, timeout=60,
                                env={**os.environ, **self.approval_authority().executor_environment()})
        require(worker.returncode == 73, "Crash worker did not stop at the intended post-write boundary")
        with SQLiteExecutionLedger(self.directory / "executor.sqlite") as ledger:
            require(ledger.get(proposal["action_id"])["phase"] == "WRITE_STARTED", "Crash completed the ledger unexpectedly")
        require(fingerprint(self.deployment()["spec"]["template"]) == self.baseline["template_sha256"],
                "Crash worker did not actually restore Kubernetes before exiting")
        adapters = self.adapters(folder, coordinator=True, reconcile_only=True)
        try:
            results = process_executor_requests(self.store, self.bus, adapters=adapters)
            require(len(results) == 1 and results[0]["status"] == "APPLIED", "Restart did not reconcile the real committed write")
            require(adapters.cluster.stats["write_attempts"] == 0, "Restart issued another mutation")
            receipt = json.loads((folder / "write-receipt.json").read_text(encoding="utf-8"))
            require(receipt["stats"]["write_attempts"] == 1, "Crash worker issued multiple mutations")
            save(folder / "reconciliation.json", results[0])
            save(folder / "restart-transport-stats.json", adapters.cluster.stats)
            self.check("actual worker process exits after live patch; restart reconciles without resubmission",
                       worker_exit=73, original_writes=1, restart_writes=0, coordinator_state="VERIFYING")
        finally:
            adapters.ledger.close()
        self.complete_recovery(folder, proposal, results[0])

    def after_recovery_scenarios(self):
        """Run optional workload-specific checks before credentials are removed."""

    def recover_incident(self, incident):
        self.negative_cases(incident)
        self.conditional_race(incident)
        self.positive(incident)
        self.crash_restart()

    def run(self):
        try:
            self.prepare()
            self.permissions()
            incident = self.fault("primary")
            self.recover_incident(incident)
            self.after_recovery_scenarios()
            save(self.directory / "final-deployment.json", self.deployment())
            self.summary["status"] = "PASS"
        except BaseException as exc:
            self.summary.update(status="FAILED", error_type=type(exc).__name__, error=str(exc)[:300])
            self.emergency_restore()
            raise
        finally:
            self.credentials.unlink(missing_ok=True)
            self.close_approval_authority()
            self.store.close()
            self.summary.update(finished_at=utc(), private_executor_credentials_removed=not self.credentials.exists())
            save(self.directory / "summary.json", self.summary)

    def emergency_restore(self):
        """Remove the injected fault as the lab administrator; never counted as Sentinel recovery.

        Records operator_emergency_restore and returns whether the patch was applied.
        """
        if self.baseline is None:
            return False
        try:
            deployment = self.deployment()
            patch = [{"op": "test", "path": "/metadata/uid", "value": self.target["uid"]},
                     {"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]},
                     {"op": "replace", "path": "/spec/template", "value": self.baseline["pod_template"]}]
            path = self.directory / "emergency-cleanup-patch.json"
            save(path, patch)
            self.kubectl("patch", "deployment", self.name, "--type=json", "--patch-file", str(path))
            self.summary["operator_emergency_restore"] = True
        except Exception as restore_error:  # noqa: BLE001 - recorded; a failed run re-raises its original error
            log.failure(LOG, "recovery.emergency_restore.failed", restore_error, level=log.ERROR)
            self.summary["operator_emergency_restore"] = False
        return self.summary["operator_emergency_restore"]


def crash_worker(configuration):
    """Test-only child process: terminate AFTER a real patch, before ledger finish.

    A scenario-pair run names its target's own policy file in "policy"; other
    runs use the run's one policy.json.
    """
    config = json.loads(Path(configuration).read_text(encoding="utf-8"))
    directory, folder = Path(config["directory"]), Path(config["folder"])
    adapters = build_adapters(context=config.get("context", RESTRICTED_CONTEXT),
        cluster_id=config.get("cluster_id", "sentinel-lab"), kubeconfig=config["credentials"],
        allowed_targets=directory / "allowed-targets.json", ledger=directory / "executor.sqlite", locks=directory / "locks",
        policy=Path(config.get("policy", directory / "policy.json")), dependencies=folder / "dependencies.json",
        **({"baselines": config["baselines"]} if "baselines" in config else
           {"baseline": directory / "baseline.json"}),
        approval_reader=executor_client_from_environment(), server_dry_run=True,
        image_scans=Path(config["image_scans"]) if "image_scans" in config else None,
        cve_exceptions=Path(config["cve_exceptions"]) if "cve_exceptions" in config else None)
    actual = adapters.cluster

    class CrashAfterWrite:
        def read(self, target):
            return actual.read(target)

        def conditional_restore(self, **kwargs):
            applied = actual.conditional_restore(**kwargs)
            save(folder / "write-receipt.json", {"resource_version": applied["deployment"]["metadata"]["resourceVersion"],
                                                 "stats": actual.stats})
            os._exit(73)

    adapters = replace(adapters, cluster=CrashAfterWrite())
    execute(json.loads((folder / "proposal.json").read_text(encoding="utf-8")), adapters)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--crash-worker", help=argparse.SUPPRESS)
    parser.add_argument("--approval", choices=[mode for mode in MODES if mode != "ui"], default="interactive",
                        help="interactive: a person types each action ID (needs a terminal); auto-lab: explicit "
                             "opt-in for unattended lab runs, recorded as automated approval; reject: refuse every approval")
    parser.add_argument("--operator", help="Interactive operator identity (default lab-operator)")
    args = parser.parse_args()
    if args.crash_worker:
        return crash_worker(args.crash_worker)
    try:
        operator_identity(args.approval, args.operator)
        require_attended(args.approval)
    except ValueError as error:
        parser.error(str(error))
    directory = args.run_dir or ROOT / "data/sentinel/runs" / ("executor-live-" + now().strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:6])
    experiment = Experiment(directory)
    experiment.configure_approval(args.approval, args.operator)
    experiment.summary["provenance"]["tools"] = provenance.tool_versions(("kubectl", "kind", "docker"))
    print("Live executor evidence: " + str(experiment.directory), flush=True)
    try:
        experiment.run()
    except Exception as exc:  # noqa: BLE001 - top-level boundary: report the failure and exit 1
        log.failure(LOG, "recovery.experiment.failed", exc, level=log.ERROR)
        print("Live executor test FAILED: " + str(exc), file=sys.stderr)
        return 1
    print("PASS: live restricted executor and crash reconciliation; Coordinator remains VERIFYING", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
