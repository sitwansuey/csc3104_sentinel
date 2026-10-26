"""Opt-in shippingservice experiment; real recovery, manual planning/verification.

Uses a dedicated kind cluster. Reuses the demo's execution scenarios, not its
workload assumptions. No LLM, Chaos Mesh or whole-storefront recovery is claimed.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.test_executor_live import Experiment, crash_worker, now, require, save, utc
from runtime.observer_pipeline import LocalCoordinatorSink, ObserverStore
from services.observer_kubernetes import CollectionError, KubernetesEvidenceCollector, KubectlReader


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
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=False)
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
    entrypoint = Path(__file__).resolve()
    health_source = "sentinel-boutique-lab-grpc"
    health_description = "gRPC quote and replica health"
    allowed_registries = ["us-central1-docker.pkg.dev"]
    service_account = "shippingservice"
    dashboard = None
    pause_before_injection = False
    prepared = None

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
                            dependencies="operator-attested isolated service; no dependency discovery",
                            verification="operator measurements; Role 6 pending")
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
        text = (ROOT / "infra/executor-rbac.yaml").read_text(encoding="utf-8")
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

    def quote(self):
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
                    validate_quote(response)
                    return response
                time.sleep(1)
            raise RuntimeError("Shipping RPC probe timed out")
        finally:
            self.kubectl("delete", "pod", name, "--ignore-not-found", "--wait=false", allow_failure=True)

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
        # Invalid PORT is a real application startup failure, not a fake API.
        self.kubectl("set", "env", "deployment/" + self.name, "--containers=server", "PORT=invalid-port")
        self.event(1, "fault", "Operator injected PORT=invalid-port into shippingservice", target=self.target)

    def fault(self, label):
        if self.pause_before_injection:
            input("Baseline ready. Press Enter to inject the " + label + " bad rollout (Ctrl+C aborts): ")
        self.inject_fault()
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
                return incident
            index += 1
            time.sleep(2)
        raise RuntimeError("Role 2 did not detect the Boutique rollout failure")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--prepared-environment", type=Path, help="Trusted host-prepared packaged cluster/images; no Docker access in runtime")
    parser.add_argument("--dashboard", action="store_true", help="Serve the real UI and collect live cluster telemetry")
    parser.add_argument("--dashboard-port", type=int, default=8765, help="Loopback UI port; 0 selects a free port")
    parser.add_argument("--dashboard-bind", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--exit-after-test", action="store_true", help="Stop the dashboard after validation instead of waiting for Ctrl+C")
    parser.add_argument("--pause-before-injection", action="store_true", help="Wait for Enter before each controlled bad rollout")
    parser.add_argument("--crash-worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.crash_worker:
        return crash_worker(args.crash_worker)
    if not 0 <= args.dashboard_port <= 65535:
        parser.error("--dashboard-port must be between 0 and 65535")
    if args.exit_after_test and not args.dashboard:
        parser.error("--exit-after-test requires --dashboard")
    directory = args.run_dir or ROOT / "runs" / ("boutique-live-" + now().strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:6])
    experiment = BoutiqueExperiment(directory)
    if args.prepared_environment:
        experiment.prepared = prepared_environment(args.prepared_environment)
        experiment.cluster_id = experiment.prepared["cluster_id"]
        experiment.context = experiment.prepared["context"]
    experiment.pause_before_injection = args.pause_before_injection
    print("Boutique evidence: " + str(experiment.directory), flush=True)
    dashboard = None
    # Docker stop sends SIGTERM. Reuse the normal interruption/finally cleanup.
    def stop_requested(_signum, _frame):
        raise KeyboardInterrupt
    previous_term = signal.signal(signal.SIGTERM, stop_requested)
    try:
        if args.dashboard:
            from scripts.boutique_dashboard import BoutiqueDashboard
            dashboard = BoutiqueDashboard(experiment.directory, context=experiment.context,
                cluster_id=experiment.cluster_id, namespace=experiment.namespace, port=args.dashboard_port, bind=args.dashboard_bind)
            experiment.dashboard = dashboard
            dashboard.start()
        experiment.run()
        if dashboard:
            experiment.summary["dashboard"] = dashboard.verify()
            print("PASS: live dashboard HTTP, Guard reports, execution records and cluster telemetry", flush=True)
            if not args.exit_after_test:
                print("Tests complete. Dashboard stays live for inspection; press Ctrl+C to stop it.", flush=True)
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
    except KeyboardInterrupt:
        experiment.summary.update(status="INTERRUPTED")
        return 130
    except Exception as exc:
        experiment.summary.update(status="FAILED", integration_error=type(exc).__name__)
        print("FAIL: Boutique experiment: " + str(exc), file=sys.stderr)
        return 1
    finally:
        try:
            if dashboard:
                dashboard.close()
                save(experiment.directory / "dashboard-cleanup.json", {"server_stopped": True, "collector_stopped": True})
        finally:
            experiment.store.close()
            experiment.credentials.unlink(missing_ok=True)
            save(experiment.directory / "summary.json", experiment.summary)
            signal.signal(signal.SIGTERM, previous_term)
    print("PASS: live Boutique restricted rollback and crash reconciliation; Coordinator remains VERIFYING", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
