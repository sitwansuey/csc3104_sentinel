"""Opt-in shippingservice experiment; real recovery and Role 6 verification.

Uses a dedicated kind cluster. --agent enables real RCA and model planning.
"""

import argparse
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from experiments.recovery.live import Experiment, crash_worker, now, require, save, utc
from experiments.boutique.harness import scenarios
from runtime.approval_session import MODES, operator_identity, require_attended
from runtime.observer_pipeline import LocalCoordinatorSink, ObserverStore
from runtime.verifier_pipeline import process_verifier_requests
from services.verifier.core import inspect_dependencies
from services.verifier.store import VerificationStore, observation_seconds
from services.executor.store import fingerprint
from services.observer.kubernetes import CollectionError, KubernetesEvidenceCollector, KubectlReader
from shared import log
from shared.provenance import tool_versions

LOG = log.get_logger("experiment")

# A Deployment read through kubectl can fail transiently (non-zero exit, timeout, bad JSON).
READ_ERRORS = (RuntimeError, ValueError, OSError, subprocess.SubprocessError)


SHIPPING_IMAGE = "us-central1-docker.pkg.dev/online-boutique-ci/microservices-demo/shippingservice:v0.10.7"
CLIENT_IMAGE = "fullstorydev/grpcurl:v1.9.3"


def prepared_environment(path):
    """Only the host bootstrap can supply reviewed image/scope configuration."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    require(set(data) == {"cluster_id", "context", "node_name", "shipping", "client"}, "Invalid prepared environment")
    require(data["cluster_id"] == "sentinel-boutique-package"
            and data["context"] == "kind-sentinel-boutique-package"
            and data["node_name"] == "sentinel-boutique-package-control-plane", "Wrong packaged demo scope")
    for key, image in (("shipping", SHIPPING_IMAGE), ("client", CLIENT_IMAGE)):
        repository = image.rsplit(":", 1)[0]
        if key == "client":
            repository = "docker.io/" + repository
        require(isinstance(data[key], str) and re.fullmatch(re.escape(repository) + r"@sha256:[a-f0-9]{64}", data[key]),
                "Prepared image must be the selected repository's immutable manifest")
    return data


def command(arguments, timeout=300):
    result = subprocess.run(arguments, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout, check=False)
    require(result.returncode == 0, "Command failed: " + " ".join(arguments[:3]) + "\n" + result.stderr[-1500:])
    return result.stdout


def repository_digest(image, inspection):
    """Accept the selected repository's manifest digest, never the config ID."""
    repository = image.rsplit(":", 1)[0]
    values = (value.removeprefix("docker.io/") if repository.startswith("fullstorydev/") else value
              for item in inspection for value in item.get("RepoDigests", []))
    matches = {value for value in values
               if re.fullmatch(re.escape(repository) + r"@sha256:[a-f0-9]{64}", value)}
    require(len(matches) == 1, "Cannot resolve an unambiguous manifest digest for " + repository)
    return matches.pop()


def shipping_manifest(image):
    """Small lab deployment of the actual upstream application, not a substitute."""
    namespace = BoutiqueExperiment.namespace
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}},
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": "shippingservice", "namespace": namespace}, "automountServiceAccountToken": False},
        {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": "shippingservice", "namespace": namespace},
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "minReadySeconds": 2,
                  "selector": {"matchLabels": {"app": "shippingservice"}},
                  "template": {"metadata": {"labels": {"app": "shippingservice"}}, "spec": {
                      "serviceAccountName": "shippingservice", "automountServiceAccountToken": False,
                      "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                                          "seccompProfile": {"type": "RuntimeDefault"}},
                      "containers": [{"name": "server", "image": image, "imagePullPolicy": "Never",
                          "ports": [{"containerPort": 50051}],
                          "env": [{"name": "PORT", "value": "50051"},
                                  {"name": "DISABLE_PROFILER", "value": "1"},
                                  {"name": "DISABLE_TRACING", "value": "1"}],
                          "resources": {"requests": {"cpu": "100m", "memory": "64Mi"},
                                        "limits": {"cpu": "200m", "memory": "128Mi"}},
                          "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                              "capabilities": {"drop": ["ALL"]}},
                          "readinessProbe": {"grpc": {"port": 50051}, "periodSeconds": 3},
                          "livenessProbe": {"grpc": {"port": 50051}, "periodSeconds": 5,
                                            "initialDelaySeconds": 5}}]}}}},
        {"apiVersion": "v1", "kind": "Service",
         "metadata": {"name": "shippingservice", "namespace": namespace},
         "spec": {"selector": {"app": "shippingservice"},
                  "ports": [{"name": "grpc", "port": 50051, "targetPort": 50051}]}}
    ]}


def validate_quote(response):
    cost = response.get("costUsd", {})
    require(cost.get("currencyCode") == "USD", "Shipping request did not return a USD quote")
    units, nanos = int(cost.get("units", 0)), int(cost.get("nanos", 0))
    require(units >= 0 and 0 <= nanos < 1_000_000_000 and units + nanos > 0,
            "Shipping quote must be positive and valid")
    return cost


class BoutiqueExperiment(Experiment):
    cluster_id = "sentinel-boutique-lab"
    context = "kind-sentinel-boutique-lab"
    namespace = "sentinel-boutique"
    name = "shippingservice"
    restricted_context = "sentinel-executor-boutique"
    entrypoint = ROOT / "scripts/test_boutique_live.py"
    health_source = "sentinel-boutique-lab-grpc"
    health_description = "gRPC quote and replica health"
    allowed_registries = ["us-central1-docker.pkg.dev"]
    service_account = "shippingservice"
    dashboard = None
    lab_control = None
    approval_relay = None  # Dashboard approval relay; set only for --approval ui.
    pause_before_injection = False
    prepared = None
    publish_dependency_results = True
    health_outcome_description = " after executor rollback; Role 6 published RECOVERED"
    # The catalog scenario this run injects (experiments/boutique/catalog.json). R1 is the original PORT fault.
    scenario_id = "R1"
    # The image this run removed from the kind node (scenario N1), until it is pulled back.
    evicted_image = None

    @property
    def scenario(self):
        if getattr(self, "_scenario", None) is None or self._scenario["id"] != self.scenario_id:
            self._scenario = scenarios.scenario(self.scenario_id)
        return self._scenario

    def after_recovery_scenarios(self):
        """Finish the default walkthrough by refusing a redundant third repair."""
        if self.provider_script is None:
            from experiments.boutique.harness.rejection import run_unnecessary_rollback
            run_unnecessary_rollback(self)

    def close(self):
        """Workload-specific child processes may override this teardown hook."""

    def request_approval(self, proposal, decision):
        if self.approval_mode in {"interactive", "ui"}:
            self.event(4, "status", "Guard needs operator approval: " + (
                "type the action ID at the harness terminal" if self.approval_mode == "interactive" else
                "type the action ID in the dashboard approval panel and approve"),
                       target=proposal["target"], incident_id=proposal["incident_id"], action_id=proposal["action_id"])
        record = super().request_approval(proposal, decision)
        self.event(4, "status", ("Approval recorded through the approval API by " + record["operator_id"]
                                 + (" (automated lab approval)" if self.approval_mode == "auto-lab" else ""))
                   if record else "Approval refused; no Executor write", target=proposal["target"],
                   incident_id=proposal["incident_id"], action_id=proposal["action_id"])
        return record

    provider_script = None

    def adapters(self, folder, *, coordinator=False, reconcile_only=False):
        adapters = super().adapters(folder, coordinator=coordinator, reconcile_only=reconcile_only)
        if not reconcile_only:
            # Initial routing evidence may age while the action waits. The
            # Executor obtains a new Role 6 finding under its target lock.
            adapters.authorizer.dependency_reader = self.dependency_result
        return adapters

    def recover_incident(self, incident):
        if self.provider_script is None:
            return super().recover_incident(incident)
        from scripts.boutique_provider import run_provider_case
        return run_provider_case(self, incident)

    def prepare(self):
        super().prepare()
        self.policy["health"] = {
            "protocol": "grpc", "required_grpc_status": 0,
            "minimum_samples": 3, "stable_window_seconds": 10,
            "max_sample_gap_seconds": 45, "required_dependencies": [],
            "require_target_identity": True, "require_template_match": True,
            "require_available_replicas": True,
            # Callers' gRPC reconnect backoff can outlive Pod readiness; sustained health is still required afterwards.
            "convergence_grace_seconds": 150, "verification_timeout_seconds": 300,
        }
        save(self.directory / "policy.json", self.policy)

    def dependency_result(self, proposal):
        # Reviewed single-service topology, not automatic dependency discovery.
        graph = json.loads((ROOT / "deploy/boutique/dependencies.json").read_text(encoding="utf-8"))
        state = {"cluster_id": self.cluster_id, "captured_at": utc(), "deployment": self.deployment()}
        result = inspect_dependencies(proposal, graph, state, now=now())
        save(self.directory / "dependency-graph.json", graph)
        self.event(6, "status", "Role 6 assessed the isolated shipping dependency graph: " + result["status"],
                   target=proposal["target"], incident_id=proposal["incident_id"], action_id=proposal["action_id"])
        return result

    def verification_samples(self, execution):
        """Collect protocol-native evidence; collection failures stay unknown.

        The durable worker closes this reader as soon as the ledger freezes an
        outcome. Its own limit only ensures one reading is offered after the fixed
        deadline, so a pass that collects without raising ends with exactly one
        outcome. A probe that cannot report is missing evidence, never a known
        failure: a target that never answers ends UNKNOWN at that deadline, not
        NOT_RECOVERED after the grace. A failed cluster read skips that reading;
        the Verifier's maximum sample gap still bounds the hole it leaves. Three
        consecutive failed reads raise and leave the saved job pending for a later pass.
        """
        limit = time.monotonic() + observation_seconds(self.policy["health"]) + 5
        index = failures = 0

        def skip(error):
            nonlocal failures
            failures += 1
            self.event(6, "error", "Role 6 skipped a verification reading; cluster read failed: "
                       + type(error).__name__, target=execution["target"],
                       incident_id=execution["incident_id"], action_id=execution["action_id"])
            if failures >= 3:
                raise error
            time.sleep(5)

        while True:
            last = time.monotonic() >= limit  # Decided before collecting this reading.
            try:
                before = self.deployment()
            except READ_ERRORS as error:
                skip(error)
                continue
            sample = {"service": self.name, "protocol": "grpc", "dependencies": {},
                      "grpc_status": None, "application_check_passed": None}
            try:
                response = self.quote_response()
                sample.update(grpc_status=0, quote=response)
                try:
                    validate_quote(response)
                except (AttributeError, TypeError, ValueError, RuntimeError):
                    sample["application_check_passed"] = False
                else:
                    sample["application_check_passed"] = True
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                # Do not turn a timeout or unavailable probe into an invented gRPC status.
                sample["collection_error"] = type(error).__name__
            try:
                after = self.deployment()
            except READ_ERRORS as error:
                skip(error)
                continue
            failures = 0
            metadata, status = after["metadata"], after.get("status", {})
            sample.update(
                at=utc(), target={"cluster_id": self.cluster_id, "namespace": self.namespace,
                                  "kind": "Deployment", "name": self.name, "uid": metadata["uid"]},
                ready_replicas=status.get("readyReplicas", 0),
                available_replicas=status.get("availableReplicas", 0),
                desired_replicas=after["spec"].get("replicas", 1),
                template_sha256=fingerprint(after["spec"]["template"]),
                resource_version=metadata["resourceVersion"],
                configuration_stable=(before["metadata"]["uid"] == metadata["uid"]
                                      and before["spec"]["template"] == after["spec"]["template"]
                                      and status.get("observedGeneration", 0) >= metadata["generation"]),
            )
            self.event(6, "status", "Role 6 collected shipping verification sample " + str(index + 1),
                       target=execution["target"], incident_id=execution["incident_id"], action_id=execution["action_id"])
            try:
                self.observe("role6-" + execution["action_id"] + "-" + str(index))
            except CollectionError:
                self.event(2, "error", "Dashboard observation unavailable during Role 6 measurement")
            yield sample
            if last:
                return
            index += 1
            time.sleep(5)

    def complete_recovery(self, folder, proposal, execution, *, clock=None):
        # Readiness waiting is bounded and never itself establishes recovery.
        self.kubectl("rollout", "status", "deployment/" + self.name, "--timeout=120s",
                     timeout=160, allow_failure=True)

        # The sampler runs until the ledger freezes one outcome, not for a fixed count.
        def collect(current):
            return self.verification_samples(current)

        ledger = VerificationStore(self.directory / "verifier.sqlite3")
        try:
            outcomes = process_verifier_requests(self.store, self.bus, ledger=ledger, sample_reader=collect,
                                                 policy_reader=lambda current: self.policy, clock=clock)
            job = ledger.get(execution["action_id"])
            if job:
                save(folder / "post-health.json", job["samples"])
        finally:
            ledger.close()
        require(len(outcomes) == 1 and outcomes[0]["action_id"] == proposal["action_id"],
                "Expected one Role 6 outcome for the current recovery")
        outcome = outcomes[0]
        save(folder / "outcome.json", outcome)
        self.phase("outcome_recorded", folder.name)
        disclosed = next((check["detail"] for check in outcome["checks"]
                          if check["name"] == "convergence" and check["detail"].startswith("Excluded ")), None)
        failed = ", ".join(check["name"] for check in outcome["checks"] if not check["passed"])
        self.event(6, "status", "Role 6 published " + outcome["status"], target=proposal["target"],
                   incident_id=proposal["incident_id"], action_id=proposal["action_id"])
        if disclosed:
            self.event(6, "status", "Role 6 disclosed convergence: " + disclosed, target=proposal["target"],
                       incident_id=proposal["incident_id"], action_id=proposal["action_id"])
        require(outcome["status"] == "RECOVERED", "Role 6 could not verify shipping recovery: " + outcome["status"]
                + (" (failed: " + failed + ")" if failed else ""))
        require(self.store.incident(proposal["incident_id"])["state"] == "RECOVERED",
                "Coordinator did not close the verified incident")
        self.check("Role 6 verified gRPC, readiness and template; Coordinator RECOVERED",
                   action_id=proposal["action_id"], outcome="RECOVERED",
                   **({"convergence": disclosed} if disclosed else {}))
        if self.lab_control:
            self.lab_control.complete_scenario("RECOVERED", incident_id=proposal["incident_id"],
                                               action_id=proposal["action_id"])

    def event(self, component, event_type, summary, **details):
        if self.dashboard:
            self.dashboard.event(component, event_type, summary, **details)

    def check(self, name, **details):
        super().check(name, **details)
        self.event(1, "status", "Live harness assertion passed: " + name)

    def load_image(self, image):
        print("Preparing image: " + image, flush=True)
        command(["docker", "pull", image], timeout=600)
        digest = repository_digest(image, json.loads(command(["docker", "image", "inspect", image])))
        # Pull the immutable manifest through the node runtime. Docker Desktop
        # exports can omit other architectures referenced by a multiarch index;
        # kind's --all-platforms importer then fails on those absent layers.
        destination = "docker.io/" + digest if image == CLIENT_IMAGE else digest
        command(["docker", "exec", self.cluster_id + "-control-plane", "crictl", "pull", destination], timeout=600)
        return destination

    def prepare_image(self):
        if self.prepared:
            node = json.loads(self.kubectl("get", "node", self.prepared["node_name"], "-o", "json").stdout)
            require(node["metadata"]["name"] == self.prepared["node_name"], "Wrong prepared cluster node")
        else:
            nodes = command(["kind", "get", "nodes", "--name", self.cluster_id]).split()
            require(nodes == [self.cluster_id + "-control-plane"], "Requires the dedicated single-node Boutique lab")
        self.summary.update(workload="Online Boutique shippingservice v0.10.7",
                            planning="operator-authored proposal; no model call",
                            dependencies="Role 6 evaluates reviewed isolated-service graph against live identity; no automatic discovery",
                            verification="Role 6 consumes real gRPC, readiness and template evidence and publishes outcomes",
                            scope="single-node kind; Roles 1/2/4/5/6 integrated; Planner proposal supplied by harness")
        image = self.prepared["shipping"] if self.prepared else self.load_image(SHIPPING_IMAGE)
        self.client_image = self.prepared["client"] if self.prepared else self.load_image(CLIENT_IMAGE)
        save(self.directory / "images.json", {"shipping": image, "client": self.client_image})
        manifest = self.directory / "shippingservice.json"
        save(manifest, shipping_manifest(image))
        self.kubectl("apply", "-f", str(manifest))
        self.kubectl("rollout", "status", "deployment/" + self.name, "--timeout=120s", timeout=160)
        self.observer_collector = KubernetesEvidenceCollector(
            KubectlReader(self.context), namespace=self.namespace, name=self.name,
            cluster_id=self.cluster_id, collect_logs=True)
        # Reuse the same narrow Role semantics, adapting only this lab's scope.
        text = (ROOT / "deploy/sentinel/executor-rbac.yaml").read_text(encoding="utf-8")
        self.rbac_path = self.directory / "executor-rbac.yaml"
        self.rbac_path.write_text(text.replace("sentinel-demo", self.namespace).replace("demo-api", self.name), encoding="utf-8")
        return image

    def observe(self, label):
        """Use Role 2's full collector, durable checkpoint/outbox and Role 1 sink."""
        observation = self.observer_collector.collect()
        policy = {"detection": {"failure_duration_seconds": 5, "max_sample_gap_seconds": 45}}
        with ObserverStore(self.directory / "observer.sqlite", policy, self.observer_collector.target) as store:
            incident = store.observe(observation)
            delivery = store.flush(LocalCoordinatorSink(self.directory / "coordinator.sqlite"))
            require(delivery["error"] is None, "Observer could not deliver its persisted incident")
            observer_status = store.status()
        save(self.directory / (label + "-observation.json"), observation)
        save(self.directory / "observer-status.json", observer_status)
        deployment = observation["deployment"]
        self.event(2, "observation", "Role 2 collected actual Deployment, owned Pods, warnings and bounded logs",
                   target={**self.observer_collector.target, "uid": deployment["metadata"]["uid"]},
                   metrics={"desired": deployment["spec"].get("replicas", 1),
                            "ready": deployment.get("status", {}).get("readyReplicas", 0),
                            "available": deployment.get("status", {}).get("availableReplicas", 0)})
        if incident:
            save(self.directory / (label + "-incident.json"), incident)
            self.event(2, "status", "Role 2 incident durably delivered to Coordinator", target=incident["target"],
                       incident_id=incident["incident_id"])
        return incident

    def quote_response(self):
        """Independent unprivileged client calls the service through cluster DNS."""
        name = "shipping-probe-" + uuid4().hex[:10]
        request = {"items": [{"productId": "demo-item", "quantity": 1}]}
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": self.namespace},
               "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": 45,
                        "automountServiceAccountToken": False,
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000,
                                            "seccompProfile": {"type": "RuntimeDefault"}},
                        "containers": [{"name": "probe", "image": self.client_image, "imagePullPolicy": "Never",
                            "args": ["-plaintext", "-max-time", "5", "-d", json.dumps(request),
                                     "shippingservice:50051", "hipstershop.ShippingService/GetQuote"],
                            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                                          "limits": {"cpu": "100m", "memory": "64Mi"}},
                            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                "capabilities": {"drop": ["ALL"]}}}]}}
        path = self.directory / (name + ".json")
        save(path, pod)
        try:
            self.kubectl("create", "-f", str(path))
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = json.loads(self.kubectl("get", "pod", name, "-o", "json").stdout)
                phase = status.get("status", {}).get("phase")
                if phase in {"Succeeded", "Failed"}:
                    logs = self.kubectl("logs", name, allow_failure=True)
                    require(phase == "Succeeded" and logs.returncode == 0, "Shipping RPC failed: " + logs.stdout[-500:])
                    response = json.loads(logs.stdout)
                    return response
                time.sleep(1)
            raise RuntimeError("Shipping RPC probe timed out")
        finally:
            self.kubectl("delete", "pod", name, "--ignore-not-found", "--wait=false", allow_failure=True)

    def quote(self):
        response = self.quote_response()
        validate_quote(response)
        return response

    def health_window(self, expected_template):
        samples = []
        for index in range(3):
            deployment = self.deployment()
            require(deployment["spec"]["template"] == expected_template, "Template changed during health measurement")
            status = deployment.get("status", {})
            require(status.get("readyReplicas") == 1 and status.get("availableReplicas") == 1,
                    "Shipping service is not ready/available")
            response = self.quote()
            after = self.deployment()
            require(after["metadata"]["uid"] == deployment["metadata"]["uid"]
                    and after["spec"]["template"] == expected_template, "Target changed during RPC")
            samples.append({"at": utc(), "ready_replicas": 1, "quote": response,
                            "resource_version": after["metadata"]["resourceVersion"]})
            self.observe("healthy-" + str(index))
            if index < 2:
                time.sleep(5)
        return samples

    def inject_fault(self):
        """Inject the catalog scenario's fault, only inside one of its catalog scopes."""
        item = self.scenario
        scope = scenarios.require_scope(item, cluster_id=self.cluster_id, context=self.context,
                                        namespace=self.namespace)
        injection, profile = item["injection"], item["target_profile"]
        require(item["target"] == self.name, "The scenario targets another workload")
        if injection["kind"] == "set_env":
            # Invalid PORT (R1) is a real application startup failure, not a fake API.
            self.kubectl(*scenarios.set_env_argv(self.name, injection["container"], injection["env"],
                                                 injection["value"]))
            message = f"Operator injected {injection['env']}={injection['value']} into {self.name}"
        elif injection["kind"] == "set_image_digest":
            # R2: a rollout to a well-formed digest that is not on the node. imagePullPolicy
            # stays Never, so kubelet reports ErrImageNeverPull and never contacts a registry.
            image = scenarios.pinned_image(profile["image_repository"], injection["digest"])
            self.kubectl(*scenarios.set_image_argv(self.name, injection["container"], image,
                                                   profile["image_repository"]))
            message = f"Operator rolled {self.name} out to an image digest that is not on the node"
        else:
            self.evict_node_image(scope, item)
            message = (f"Operator removed the {self.name} image from the kind node and deleted its Pod; "
                       "the Deployment template is unchanged")
        # When the API server accepted the bad rollout. Latency reports measure
        # detection from here; older runs only have the first failing snapshot.
        # The first injection is kept; later scenarios are in the timeline.
        self.summary.setdefault("fault_injected_at", utc())
        self.event(1, "fault", message, target=self.target)

    def evict_node_image(self, scope, item):
        """N1: remove the target's image from the kind node, then delete its Pod; the template stays.

        Refuses unless the live template equals the measured baseline, the
        image is the catalog repository pinned by digest, and no other Pod
        references that digest. The removal is confirmed before the Pod is
        deleted, so a failed removal leaves the running Pod in place.
        """
        repository = item["target_profile"]["image_repository"]
        template = self.deployment()["spec"]["template"]
        require(self.baseline is not None and template == self.baseline["pod_template"],
                "Node image eviction needs the live template to equal the healthy baseline")
        image = scenarios.container_image(template, item["injection"]["container"])
        digest = scenarios.image_digest(image, repository)
        pods = json.loads(self.kubectl("get", "pods", "--all-namespaces", "-o", "json").stdout)
        users = scenarios.other_image_users(pods, digest, namespace=self.namespace, owner=self.name)
        require(not users, "Node image eviction refused; other Pods use the image: " + ", ".join(users[:5]))
        self.evicted_image = image
        command(scenarios.node_argv(scope, "rmi", image, repository), timeout=120)
        check = subprocess.run(scenarios.node_argv(scope, "inspecti", image, repository), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
        require(check.returncode != 0, "The image is still on the node after removal; no Pod was deleted")
        self.kubectl("delete", "pod", "-l", "app=" + self.name, "--wait=false")
        self.summary["node_image_evicted"] = {"node": scope["node"], "image": image}

    def emergency_restore(self):
        """Also pull a removed node image back (N1), the same way setup loads it, and replace the Pod."""
        restored = super().emergency_restore()
        if self.evicted_image is None:
            return restored
        try:
            scope = scenarios.require_scope(self.scenario, cluster_id=self.cluster_id, context=self.context,
                                            namespace=self.namespace)
            command(scenarios.node_argv(scope, "pull", self.evicted_image,
                                        self.scenario["target_profile"]["image_repository"]), timeout=600)
            # kubelet retries ErrImageNeverPull only after its back-off; a new Pod starts at once.
            self.kubectl("delete", "pod", "-l", "app=" + self.name, "--wait=false")
            self.evicted_image = None
            self.summary["node_image_restored"] = True
        except Exception as restore_error:  # noqa: BLE001 - recorded; a failed run re-raises its original error
            log.failure(LOG, "boutique.node_image_restore.failed", restore_error, level=log.ERROR)
            self.summary["node_image_restored"] = False
            return False
        return restored

    def fault(self, label):
        if self.lab_control:
            print("Baseline ready. Inject the " + label + " scenario from the dashboard.", flush=True)
            self.lab_control.wait_for_injection(label)
        elif self.pause_before_injection:
            input("Baseline ready. Press Enter to inject the " + label + " bad rollout (Ctrl+C aborts): ")
        self.inject_fault()
        self.phase("fault_injected", label)
        deadline = time.monotonic() + 130
        index = 0
        consecutive_errors = 0
        while time.monotonic() < deadline:
            try:
                incident = self.observe(label + "-" + str(index))
                consecutive_errors = 0
            except CollectionError as error:
                # A rollout can change while multiple objects are being read.
                # Role 2 correctly rejects that sample; reset its timer and retry
                # bounded read-only collection, never the recovery mutation.
                consecutive_errors += 1
                policy = {"detection": {"failure_duration_seconds": 5, "max_sample_gap_seconds": 45}}
                with ObserverStore(self.directory / "observer.sqlite", policy, self.observer_collector.target) as store:
                    store.collection_failed(error)
                self.event(2, "error", "Role 2 rejected an inconsistent rollout sample; retrying collection")
                if consecutive_errors >= 3:
                    raise
                incident = None
            if incident:
                # Keep the existing evidence filenames used by the original lab.
                save(self.directory / (label + "-incident.json"), incident)
                self.phase("incident_observed", label)
                return incident
            index += 1
            time.sleep(2)
        # A scored run records NO_INCIDENT first. Only a scenario whose label allows
        # no incident continues; for every current scenario this stays a failure.
        if not self.no_incident(label):
            raise RuntimeError("Role 2 did not detect the Boutique rollout failure")
        return None

    def no_incident(self, label):
        """Record that no incident appeared; True only when the scenario's label allows that."""
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--agent", action="store_true", help="Use live RCA and model planning for one guarded recovery")
    parser.add_argument("--agent-config", type=Path, help="JSON overrides for the live agent's Ollama settings")
    parser.add_argument("--expect", choices=["recovered", "escalated"],
                        help="Agent mode: the outcome that passes (default: the scenario's pre-registered one; "
                             "recovered for R1 and R2). 'escalated' passes only when planning ends "
                             "in a saved ESCALATE and NEEDS_REVIEW; the administrator then removes the fault")
    catalog_ids = scenarios.scenario_ids()
    parser.add_argument("--scenario", choices=catalog_ids, default="R1",
                        help="Catalog scenario to inject (experiments/boutique/catalog.json). R1 (default) is the "
                             "invalid PORT; R2 and N1 need --agent --connected and are scored against their "
                             "pre-registered expected outcome")
    parser.add_argument("--prepared-environment", type=Path, help="Trusted host-prepared packaged cluster/images; no Docker access in runtime")
    parser.add_argument("--dashboard", action="store_true", help="Serve the real UI and collect live cluster telemetry")
    parser.add_argument("--dashboard-port", type=int, default=8765, help="Loopback UI port; 0 selects a free port")
    parser.add_argument("--dashboard-bind", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--exit-after-test", action="store_true", help="Stop the dashboard after validation instead of waiting for Ctrl+C")
    parser.add_argument("--pause-before-injection", action="store_true",
                        help="Wait for Enter before each bad rollout and the final unnecessary rollback proposal")
    parser.add_argument("--ui-controls", action="store_true",
                        help="Opt in to local dashboard buttons for the fixed lab injection sequence")
    parser.add_argument("--connected", action="store_true", help="Deploy the full connected Boutique storefront")
    parser.add_argument("--storefront-port", type=int, default=8080, help="Loopback storefront port in connected mode")
    parser.add_argument("--guard-rejection", action="store_true",
                        help="Run only the final unnecessary-rollback rejection scenario, skipping both recovery rounds")
    parser.add_argument("--crash-worker", help=argparse.SUPPRESS)
    parser.add_argument("--provider-script", type=Path, help="Opt in to a trusted Planner provider adapter (JSON stdin/stdout)")
    parser.add_argument("--provider-policy", type=Path, help="Trusted policy override for provider recovery")
    parser.add_argument("--approval", choices=MODES, default="interactive",
                        help="interactive (default): a person types each action ID at this terminal; auto-lab: explicit "
                             "opt-in for unattended lab runs, recorded as automated approval; reject: refuse every "
                             "approval; ui: type the action ID and approve in the dashboard (needs --ui-controls)")
    parser.add_argument("--operator", help="Operator identity for interactive or ui approval "
                                           "(default lab-operator; dashboard-operator for ui)")
    args = parser.parse_args()
    if args.agent_config and not args.agent:
        parser.error("--agent-config requires --agent")
    if args.expect == "escalated" and not args.agent:
        parser.error("--expect escalated requires --agent")
    scenario = scenarios.scenario(args.scenario)
    if args.scenario != "R1":
        # The new scenarios are scored agent decisions on the connected lab, outside the fixed UI sequence.
        if not (args.agent and args.connected):
            parser.error("--scenario " + args.scenario + " requires --agent --connected")
        if args.ui_controls:
            parser.error("--ui-controls runs the fixed three-step lab sequence; use the default scenario R1")
    expect = args.expect or scenarios.expect(scenario)
    if args.agent and args.expect and args.expect != scenarios.expect(scenario) and args.scenario != "R1":
        parser.error(f"--expect {args.expect} contradicts scenario {args.scenario}'s pre-registered outcome "
                     f"({scenario['expected']['terminal_state']})")
    if args.agent and args.guard_rejection:
        parser.error("--agent and --guard-rejection are separate scenarios")
    if args.agent and args.provider_script:
        parser.error("--agent and --provider-script are separate planning modes")
    if args.provider_script and not args.provider_script.is_file():
        parser.error("--provider-script must identify an existing adapter")
    if args.provider_script and args.dashboard:
        parser.error("The dashboard acceptance sequence uses the original operator lab mode")
    if args.provider_script and args.guard_rejection:
        parser.error("Provider recovery and the harness-only rejection scenario are separate modes")
    if args.provider_policy and not args.provider_script:
        parser.error("--provider-policy requires --provider-script")
    if args.crash_worker:
        return crash_worker(args.crash_worker)
    try:
        operator_identity(args.approval, args.operator)
        # The rejection scenario and an expected agent escalation never ask for approval.
        if not (args.guard_rejection or (args.agent and expect == "escalated")):
            require_attended(args.approval)
    except ValueError as error:
        parser.error(str(error))
    if not 0 <= args.dashboard_port <= 65535:
        parser.error("--dashboard-port must be between 0 and 65535")
    if args.exit_after_test and not args.dashboard:
        parser.error("--exit-after-test requires --dashboard")
    if args.ui_controls and (not args.dashboard or args.pause_before_injection):
        parser.error("--ui-controls requires --dashboard and replaces --pause-before-injection")
    if args.approval == "ui" and not args.ui_controls:
        parser.error("--approval ui requires --dashboard --ui-controls: the approval routes use the lab session token")
    if args.connected and args.prepared_environment:
        parser.error("Connected mode requires its host lab; the old packaged bootstrap is single-service")
    if not 1 <= args.storefront_port <= 65535 or (args.connected and args.dashboard and args.storefront_port == args.dashboard_port):
        parser.error("Storefront port must be valid and separate from the dashboard port")
    directory = args.run_dir or ROOT / "data/experimentation/runs" / ("boutique-live-" + now().strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:6])
    if args.connected:
        from experiments.boutique.harness.connected import ConnectedBoutiqueExperiment
        experiment_type = ConnectedBoutiqueExperiment
    else:
        experiment_type = BoutiqueExperiment
    if args.agent:
        from experiments.boutique.harness.agent_live import AgentPlanningMixin
        experiment_type = type("AgentBoutiqueExperiment", (AgentPlanningMixin, experiment_type), {})
    experiment = experiment_type(directory, **({"storefront_port": args.storefront_port} if args.connected else {}))
    if args.agent:
        experiment.agent_config = args.agent_config
        experiment.expect = expect
        # An explicit --expect that differs from the label is a planning-failure check (R1 only):
        # the score is still recorded, but it does not decide whether the run passes.
        experiment.scenario_gates_pass = args.expect in (None, scenarios.expect(scenario))
    experiment.scenario_id = args.scenario
    if not args.guard_rejection:  # The rejection-only check injects no fault.
        experiment.summary["scenario"] = {"id": scenario["id"], "name": scenario["name"],
                                          "class": scenario["class"], "labels": scenario["labels"]["status"]}
    experiment.summary["provenance"]["tools"] = tool_versions(
        ("kubectl", "kind", "docker", "helm") + (("ollama",) if args.agent else ()))
    experiment.provider_script = args.provider_script.resolve() if args.provider_script else None
    experiment.provider_policy = args.provider_policy
    experiment.configure_approval(args.approval, args.operator)
    if args.guard_rejection:
        experiment.approval_not_requested(
            "no approval was requested: the Guard-only refusal check uses a fixed file-backed lab record "
            "that never reaches the Coordinator and proves a refusal only")
    if args.expect == "escalated":
        experiment.approval_not_requested(
            "no approval was requested: --expect escalated passes only when agent planning ends in a saved "
            "ESCALATE and NEEDS_REVIEW, which never reaches Guard approval; a proposed repair is refused unasked")
    elif args.agent and expect == "escalated":
        experiment.approval_not_requested(
            f"no approval was requested: scenario {args.scenario}'s pre-registered correct outcome is a saved "
            "ESCALATE and NEEDS_REVIEW, which never reaches Guard approval; a proposed repair is refused unasked")
    if args.prepared_environment:
        experiment.prepared = prepared_environment(args.prepared_environment)
        experiment.cluster_id = experiment.prepared["cluster_id"]
        experiment.context = experiment.prepared["context"]
    experiment.pause_before_injection = args.pause_before_injection
    if args.ui_controls:
        from runtime.lab_control import LabControl
        experiment.lab_control = LabControl(directory, cluster_id=experiment.cluster_id, context=experiment.context,
            namespace=experiment.namespace, deployment=experiment.name, rejection_only=args.guard_rejection)
    if args.approval == "ui" and not args.guard_rejection:
        from runtime.approval_relay import ApprovalRelay
        experiment.approval_relay = ApprovalRelay()
    print("Boutique evidence: " + str(experiment.directory), flush=True)
    dashboard = None
    # Docker stop sends SIGTERM. Reuse the normal interruption/finally cleanup.
    def stop_requested(_signum, _frame):
        raise KeyboardInterrupt
    previous_term = signal.signal(signal.SIGTERM, stop_requested)
    try:
        if args.dashboard:
            from experiments.boutique.harness.dashboard import BoutiqueDashboard
            dashboard = BoutiqueDashboard(experiment.directory, context=experiment.context,
                cluster_id=experiment.cluster_id, namespace=experiment.namespace, port=args.dashboard_port,
                bind=args.dashboard_bind, lab_control=experiment.lab_control,
                approval_relay=experiment.approval_relay,
                application_collector=getattr(experiment, "application_inventory", None))
            experiment.dashboard = dashboard
            dashboard.start()
        if args.guard_rejection:
            from experiments.boutique.harness.rejection import run_unnecessary_rollback
            experiment.prepare()
            experiment.permissions()
            run_unnecessary_rollback(experiment)
            experiment.summary.update(status="PASS", finished_at=utc())
            experiment.credentials.unlink(missing_ok=True)
            experiment.summary["private_executor_credentials_removed"] = not experiment.credentials.exists()
            save(experiment.directory / "summary.json", experiment.summary)
        else:
            experiment.run()
        if dashboard:
            identity = experiment.summary["operator_approval"]["identity"]
            experiment.summary["dashboard"] = (dashboard.verify_agent(expect, operator_id=identity) if args.agent else
                                                dashboard.verify_rejection() if args.guard_rejection else
                                                dashboard.verify(operator_id=identity))
            print("PASS: live rejection dashboard, Guard findings and zero-write evidence"
                  if args.guard_rejection else
                  "PASS: live dashboard HTTP, Guard reports, execution records and cluster telemetry", flush=True)
            if experiment.lab_control:
                experiment.lab_control.finish("COMPLETED")
            if not args.exit_after_test:
                print("Tests complete. Dashboard stays live for inspection; press Ctrl+C to stop it.", flush=True)
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
    except KeyboardInterrupt:
        experiment.summary.update(status="INTERRUPTED")
        if experiment.lab_control:
            experiment.lab_control.finish("STOPPED")
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level boundary: record FAILED, stop the lab and exit 1
        log.failure(LOG, "experiment.failed", exc, level=log.ERROR)
        experiment.summary.update(status="FAILED", integration_error=type(exc).__name__)
        if experiment.lab_control:
            experiment.lab_control.finish("FAILED")
        print("FAIL: Boutique experiment: " + str(exc), file=sys.stderr)
        return 1
    finally:
        try:
            if dashboard:
                dashboard.close()
                save(experiment.directory / "dashboard-cleanup.json", {"server_stopped": True, "collector_stopped": True})
        finally:
            try:
                experiment.close()
            finally:
                experiment.close_approval_authority()
                experiment.store.close()
                experiment.credentials.unlink(missing_ok=True)
                save(experiment.directory / "summary.json", experiment.summary)
                signal.signal(signal.SIGTERM, previous_term)
    print("PASS: agent planning ended in a saved ESCALATE; incident NEEDS_REVIEW with no repair"
          if args.agent and expect == "escalated" else
          "PASS: live agent diagnosed the fault, proposed rollback, and Coordinator verified RECOVERED"
          if args.agent else
          "PASS: configured provider recovery completed with an independent Verifier outcome"
          if args.provider_script else
          "PASS: Guard rejected unnecessary rollback; service unchanged and zero Executor patch attempts"
          if args.guard_rejection else
          "PASS: two Boutique recoveries verified; final unnecessary rollback REJECTED with zero Executor patch attempts", flush=True)
    graded = experiment.summary.get("scenario_result")
    if graded:
        print(f"Scenario {graded['scenario']} ({graded['name']}): decision {graded['actual']}, scored {graded['score']}; "
              f"labels {graded['labels']['status']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
