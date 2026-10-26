"""Scoped real-cluster helpers for the Verifier acceptance experiment."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from runtime.executor_pipeline import GuardAuthorizer
from scripts.verifier_lab_manifest import COLLECTOR, IMAGE, manifest
from services.coordinator import LocalEventBus, SQLiteStore, approve, handle
from services.executor import ExecutionAdapters, execute
from services.executor_store import LocalTargetCoordinator, SQLiteExecutionLedger, fingerprint
from services.guard import evaluate
from services.guard_preview import _private_file
from services.k8s_executor_adapter import KubectlDeploymentAPI
from services.observer_kubernetes import KubectlReader
from services.verifier_http import EvidenceHTTP
from services.verifier_live import LiveEvidenceCollector

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = "kind-sentinel-lab"


def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class VerifierLab:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.namespace = "verifier-connected-" + uuid4().hex[:8]
        self.created = False
        self.forwards = []
        self.workers = []
        self.credentials = []
        self.urls = {}

    def kube(self, *args, stdin=None, timeout=45):
        result = subprocess.run(["kubectl", "--context", CONTEXT, "-n", self.namespace,
                                 "--request-timeout=30s", *args], input=stdin,
                                capture_output=True, text=True, timeout=timeout)
        require(result.returncode == 0, "Lab Kubernetes command failed: " + result.stderr.strip())
        return result.stdout

    def deployment(self, name):
        return json.loads(self.kube("get", "deployment", name, "-o", "json"))

    def credential(self, identity):
        configuration = json.loads(self.kube("config", "view", "--minify", "--flatten", "--raw", "-o", "json"))
        cluster = configuration["clusters"][0]["cluster"]
        require(cluster["server"].startswith("https://") and "certificate-authority-data" in cluster,
                "Explicit cluster CA is required")
        token = self.kube("create", "token", identity, "--duration=1h").strip()
        context = "sentinel-verifier-lab-" + identity
        config = {"apiVersion": "v1", "kind": "Config",
                  "clusters": [{"name": "sentinel-lab", "cluster": cluster}],
                  "users": [{"name": identity, "user": {"token": token}}],
                  "contexts": [{"name": context, "context": {"cluster": "sentinel-lab", "user": identity,
                                                              "namespace": self.namespace}}],
                  "current-context": context}
        path = self.directory / ("." + identity + "-kubeconfig.json")
        self.credentials.append(path)
        with path.open("x", encoding="utf-8") as stream:
            _private_file(path)
            json.dump(config, stream)
        return context, path

    def forward(self, name):
        pod = json.loads(self.kube("get", "pods", "-l", "app=" + name, "-o", "json"))["items"][0]["metadata"]["name"]
        process = subprocess.Popen(["kubectl", "--context", CONTEXT, "-n", self.namespace,
                                    "port-forward", "pod/" + pod, ":8080", "--address=127.0.0.1"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.forwards.append(process)
        lines = queue.Queue()
        threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
        line = lines.get(timeout=20)
        require("Forwarding from 127.0.0.1:" in line, "Port forwarding did not start")
        port = int(line.split("127.0.0.1:")[1].split(" ")[0])
        return "http://127.0.0.1:" + str(port)

    def permissions(self):
        checks = []
        for identity, context, config, verb, resource, expected in (
            ("verifier", self.verifier_context, self.verifier_config, "get", "deployment/checkout", True),
            ("verifier", self.verifier_context, self.verifier_config, "get", "deployment/payment", True),
            ("verifier", self.verifier_context, self.verifier_config, "patch", "deployment/checkout", False),
            ("verifier", self.verifier_context, self.verifier_config, "get", "secrets", False),
            ("verifier", self.verifier_context, self.verifier_config, "create", "pods/exec", False),
            ("executor", self.executor_context, self.executor_config, "patch", "deployment/checkout", True),
            ("executor", self.executor_context, self.executor_config, "patch", "deployment/payment", False)):
            result = subprocess.run(["kubectl", "--context", context, "--kubeconfig", str(config),
                "-n", self.namespace, "--request-timeout=10s", "auth", "can-i", verb, resource],
                capture_output=True, text=True, timeout=15)
            require(result.stdout.strip() == ("yes" if expected else "no"), "Unexpected live RBAC permission")
            checks.append({"identity": identity, "verb": verb, "resource": resource, "allowed": expected})
        save(self.directory / "rbac-checks.json", checks)

    def request(self, name, path, *, post=False):
        try:
            with urlopen(Request(self.urls[name] + path, method="POST" if post else "GET"), timeout=8) as response:
                return response.status
        except HTTPError as error:
            status = error.code
            error.close()
            return status

    def setup(self):
        self.kube("create", "namespace", self.namespace)
        self.created = True
        self.kube("apply", "-f", "-", stdin=json.dumps(manifest(self.namespace)))
        for name in ("collector", "payment", "checkout"):
            self.kube("rollout", "status", "deployment/" + name, "--timeout=90s", timeout=100)
            self.urls[name] = self.forward(name)
        self.verifier_context, self.verifier_config = self.credential("verifier")
        self.executor_context, self.executor_config = self.credential("executor")
        self.permissions()
        self.worker_env = dict(os.environ, KUBECONFIG=str(self.verifier_config))
        services = {}
        for name in ("checkout", "payment"):
            deployment = self.deployment(name)
            services[name] = {"target": {"cluster_id": "sentinel-lab", "namespace": self.namespace,
                "kind": "Deployment", "name": name, "uid": deployment["metadata"]["uid"]},
                "health_url": self.urls[name] + "/healthz", "trace_service": name}
        self.inventory = {"cluster_id": "sentinel-lab", "services": services, "declared_edges": [],
                          "dependency_inventory_complete": True, "require_traces": True,
                          "max_trace_age_seconds": 300}
        save(self.directory / "inventory.json", self.inventory)
        self.collector = LiveEvidenceCollector(self.inventory, KubectlReader(self.verifier_context,
            runner=lambda cmd, **kw: subprocess.run(cmd, env=self.worker_env, **kw)),
            trace_reader=lambda: EvidenceHTTP(max_bytes=2_000_000).json(self.urls["collector"] + "/traces"))
        require(self.request("checkout", "/request") == 200, "Connected service request failed")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            evidence = self.collector.collect(services["checkout"]["target"])
            save(self.directory / "latest-evidence.json", evidence)
            if ({"from": "checkout", "to": "payment"} in evidence["graph"]["edges"]
                    and not evidence["graph"]["unknowns"]):
                save(self.directory / "initial-evidence.json", evidence)
                save(self.directory / "trace-export.json", EvidenceHTTP(max_bytes=2_000_000).json(
                    self.urls["collector"] + "/traces"))
                return
            time.sleep(1)
        raise RuntimeError("Fresh complete SDK trace edge did not arrive through Collector")

    def prepare_action(self, name):
        folder = self.directory / name
        folder.mkdir()
        current = self.deployment("checkout")
        target = self.inventory["services"]["checkout"]["target"]
        policy = json.loads((ROOT / "samples/policy.json").read_text())
        policy.update(targets=[target], policy_id="verifier-live-lab", policy_version="verifier-live-lab-v1")
        policy["health"] = {"minimum_samples": 3, "stable_window_seconds": 6, "max_sample_gap_seconds": 12,
                            "required_http_status": 200, "required_dependencies": ["payment"],
                            "require_target_identity": True, "require_available_replicas": True,
                            "require_template_match": True, "verification_timeout_seconds": 25}
        baseline = {"source": "operator-reviewed-live-lab", "baseline_id": "BASE-" + uuid4().hex[:10],
                    "target": target, "healthy_observed_at": utc(), "pod_template": current["spec"]["template"],
                    "template_sha256": fingerprint(current["spec"]["template"])}
        proposal = json.loads((ROOT / "samples/messages/proposal.json").read_text())
        proposal.update(target=target, incident_id="INC-" + uuid4().hex[:12], action_id="ACT-" + uuid4().hex[:12],
                        based_on_resource_version=current["metadata"]["resourceVersion"],
                        rationale="Operator-authorized lab restore to an observed healthy template; no Planner is invoked",
                        evidence_refs=["LAB-" + name],
                        parameters={"baseline_id": baseline["baseline_id"], "template_sha256": baseline["template_sha256"]})
        incident = json.loads((ROOT / "samples/messages/incident.json").read_text())
        incident.update(target=target, incident_id=proposal["incident_id"], observed_at=utc(),
                        observed_resource_version=proposal["based_on_resource_version"], observed_generation=current["metadata"]["generation"])
        incident["signals"] = [{"evidence_id": "LAB-" + name, "source": "deployment_status",
            "reason": "OperatorVerificationExperiment", "detail": "Operator-seeded incident for Verifier acceptance, not automatic fault detection", "at": utc()}]
        store = SQLiteStore(folder / "coordinator.sqlite3")
        try:
            bus = LocalEventBus()
            dependencies = self.collector.dependencies(proposal)
            require(dependencies["status"] == "CLEAR", "Pre-execution dependencies must be healthy")
            for message in (incident, proposal, dependencies):
                handle(message, store, bus)
            state = {"cluster_id": "sentinel-lab", "captured_at": utc(), "deployment": current}
            handle(evaluate(proposal, state, policy, baseline, dependencies), store, bus)
            approval = approve(proposal["action_id"], "live-lab-operator", store)
            handle(evaluate(proposal, state, policy, baseline, dependencies, approval), store, bus)
            api = KubectlDeploymentAPI(context=self.executor_context, cluster_id="sentinel-lab",
                                       kubeconfig=self.executor_config, allowed_targets=[target])
            ledger = SQLiteExecutionLedger(folder / "executor.sqlite3")
            try:
                adapters = ExecutionAdapters(ledger, LocalTargetCoordinator(folder / "locks"), api,
                    GuardAuthorizer(policy_reader=lambda p: deepcopy(policy), baseline_reader=lambda p: deepcopy(baseline),
                                    dependency_reader=lambda p: deepcopy(dependencies), approval_reader=lambda p: store.approval(p["action_id"])))
                result = execute(proposal, adapters)
                require(result["status"] == "APPLIED", "Real Executor did not confirm the conditional write: " + result["details"])
                require(execute(proposal, adapters) == result and api.stats["successful_writes"] == 1,
                        "Repeated execution must reuse the same action without another write")
            finally:
                ledger.close()
            handle(result, store, bus)
            require(store.incident(proposal["incident_id"])["state"] == "VERIFYING", "APPLIED cannot mean recovered")
            for filename, value in (("policy.json", policy), ("proposal.json", proposal), ("execution.json", result),
                                    ("baseline.json", baseline), ("dependency-result.json", dependencies), ("api-stats.json", api.stats)):
                save(folder / filename, value)
        finally:
            store.close()
        return folder, proposal

    def fault(self, name, enabled):
        require(self.request(name, "/lab/fail" if enabled else "/lab/restore", post=True) == 200, "Fault control failed")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready = self.deployment(name).get("status", {}).get("readyReplicas", 0)
            if ready == (0 if enabled else 1):
                return
            time.sleep(1)
        raise RuntimeError("Readiness did not reflect the fault")

    def close(self):
        for process in self.workers + self.forwards:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
        for path in self.credentials:
            path.unlink(missing_ok=True)
        if self.created:
            self.kube("delete", "namespace", self.namespace, "--wait=false")
