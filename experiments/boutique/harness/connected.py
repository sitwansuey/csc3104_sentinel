"""Full Boutique deployment with scoped, catalog-driven recovery and checkout evidence.

Topology comes from the pinned upstream manifest, not discovery from traces.
Readiness informs pre-action dependency assessment; real shopping requests are
mandatory post-action evidence. The recovery targets, their reviewed spec
overrides and the storefront impact a fault must show come from the scenario
catalog (experiments/boutique/catalog.json). Executor permissions name exactly
the catalog targets enabled in the connected scope (shippingservice and
cartservice), never the whole namespace.

Verification has two scopes. A single-target run keeps the shop scope: every
other application must be ready and a real storefront checkout must pass in
every reading. A scenario-pair run (P4.05, multi_fault.py) uses the target
scope: a target is judged by its own probe, readiness and template and by the
readiness of the services it calls (verification_dependencies), never by its
callers or by an unrelated broken service, so the other fault cannot mark a
correct repair NOT_RECOVERED. The storefront is still recorded in every
reading, and the pair judges it separately as a ShopOutcome.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import time
from urllib.request import urlopen

from experiments.boutique.traffic.storefront import StorefrontProbe
from experiments.boutique.harness import targets
from experiments.boutique.harness.live import BoutiqueExperiment, CLIENT_IMAGE, command, shipping_manifest
from experiments.boutique.harness.scenarios import CONNECTED_SCOPE
from experiments.recovery.live import now, require, save, utc
from services.executor.store import fingerprint
from services.observer.kubernetes import KubernetesEvidenceCollector, KubectlReader
from services.verifier.core import inspect_dependencies
from runtime.boutique_observability import emit_observation, service_inventory


UPSTREAM = Path(__file__).resolve().parents[3] / "deploy/boutique/upstream.yaml"
UPSTREAM_SHA256 = "ae3c20181c051048e6a5cae51fa530681affa4043ba60ae95d4f39e1262f2744"
APPLICATIONS = {"frontend", "checkoutservice", "productcatalogservice", "currencyservice", "cartservice",
                "redis-cart", "shippingservice", "paymentservice", "emailservice", "adservice", "recommendationservice"}


def decoded_resources(output):
    """kubectl create emits one JSON object per YAML document, not always a List."""
    decoder, offset, result = json.JSONDecoder(), 0, []
    while offset < len(output):
        while offset < len(output) and output[offset].isspace():
            offset += 1
        if offset == len(output):
            break
        item, offset = decoder.raw_decode(output, offset)
        if not isinstance(item, dict):
            raise ValueError("Expected Kubernetes JSON objects")
        result.extend(item["items"] if item.get("kind") == "List" else [item])
    return result


def canonical_repository(repository):
    first = repository.split("/")[0]
    if "." in first or ":" in first:
        return repository
    return "docker.io/" + (repository if "/" in repository else "library/" + repository)


def image_digest(image, records):
    repository = canonical_repository(image.rsplit(":", 1)[0])
    matches = set()
    for record in records:
        for reference in record.get("RepoDigests", []):
            name, separator, digest = reference.partition("@")
            if separator and canonical_repository(name) == repository and re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                matches.add(repository + "@" + digest)
    require(len(matches) == 1, "Cannot resolve the selected workload image manifest")
    return matches.pop()


def connected_manifest(resources, images, namespace):
    """Preserve upstream application wiring; remove external exposure/loadgen.

    Each catalog recovery target gets its reviewed spec: shipping keeps its
    isolated lab manifest's spec, and other targets get their catalog overrides
    (strategy Recreate, so a bad rollout replaces the serving Pod).
    """
    items = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}]
    deployments = set()
    profiles = {name: targets.profile(name) for name in targets.enabled_targets(CONNECTED_SCOPE)}
    for source in resources:
        item = deepcopy(source)
        kind, name = item.get("kind"), item.get("metadata", {}).get("name")
        if kind not in {"Deployment", "Service", "ServiceAccount"}:
            raise ValueError("Unexpected upstream resource kind")
        if name in {"loadgenerator", "frontend-external"}:
            continue
        require(name in APPLICATIONS, "Unreviewed upstream component")
        item["metadata"]["namespace"] = namespace
        if kind == "Deployment":
            deployments.add(name)
            pod = item["spec"]["template"]["spec"]
            pod["automountServiceAccountToken"] = False
            for container in pod["containers"]:
                container["image"] = images[container["image"]]
                container["imagePullPolicy"] = "Never"
            if name in profiles and profiles[name].isolated_lab:
                reviewed = shipping_manifest(pod["containers"][0]["image"])["items"][2]
                item["spec"] = reviewed["spec"]
            elif name in profiles:
                item["spec"] = targets.apply_spec_overrides(item["spec"], profiles[name])
        elif kind == "ServiceAccount":
            item["automountServiceAccountToken"] = False
        else:
            require(item["spec"].get("type", "ClusterIP") == "ClusterIP", "Connected services must stay internal")
        items.append(item)
    require(deployments == APPLICATIONS, "The full reviewed Boutique application set is incomplete")
    return {"apiVersion": "v1", "kind": "List", "items": items}


def configured_graph(manifest):
    edges = set()
    for item in manifest["items"]:
        if item["kind"] != "Deployment":
            continue
        caller = item["metadata"]["name"]
        for container in item["spec"]["template"]["spec"]["containers"]:
            for entry in container.get("env", []):
                value = entry.get("value", "")
                callee = value.split(":", 1)[0]
                if callee in APPLICATIONS and callee != caller:
                    edges.add((caller, callee))
    return {"edge_direction": "caller_to_callee", "nodes": sorted(APPLICATIONS), "known_health": {},
            "edges": [{"from": caller, "to": callee} for caller, callee in sorted(edges)]}


def verification_dependencies(graph, name):
    """The direct callees of name in the reviewed caller-to-callee graph, sorted.

    A target-scope repair is judged by the target and what it calls, never by
    its callers or by an unrelated broken service.
    """
    if not isinstance(graph, dict) or graph.get("edge_direction") != "caller_to_callee":
        raise ValueError("The dependency graph must declare caller_to_callee edges")
    return sorted({edge["to"] for edge in graph.get("edges", []) if edge.get("from") == name and edge.get("to") != name})


def readiness(deployments, target=None):
    """Readiness of every application except the recovery target (the catalog default when None)."""
    result = {}
    indexed = {item["metadata"]["name"]: item for item in deployments}
    for name in sorted(APPLICATIONS - {target or targets.default_target()}):
        item = indexed.get(name)
        if item is None:
            result[name] = None
            continue
        desired, status = item.get("spec", {}).get("replicas", 1), item.get("status", {})
        observed, generation = status.get("observedGeneration"), item["metadata"].get("generation")
        if (any(type(value) is not int for value in (desired, observed, generation,
                status.get("readyReplicas", 0), status.get("availableReplicas", 0)))):
            result[name] = None
            continue
        result[name] = (type(desired) is int and desired > 0
                        and type(observed) is int and type(generation) is int and observed >= generation
                        and status.get("readyReplicas", 0) >= desired and status.get("availableReplicas", 0) >= desired)
    return result


class ConnectedBoutiqueExperiment(BoutiqueExperiment):
    cluster_id = "sentinel-boutique-connected"
    context = "kind-sentinel-boutique-connected"
    namespace = "sentinel-boutique-connected"
    restricted_context = "sentinel-executor-boutique-connected"
    health_source = "sentinel-boutique-connected-checkout"
    # "shop" (single-target runs) or "target" (scenario pairs); see the module docstring.
    verification_scope = "shop"

    def __init__(self, directory, *, storefront_port=8080):
        super().__init__(directory)
        if not 1 <= storefront_port <= 65535:
            raise ValueError("Invalid storefront port")
        self.storefront_url = "http://127.0.0.1:" + str(storefront_port)
        self.storefront = StorefrontProbe(self.storefront_url)
        self.forward = None
        self.forward_log = None

    def prepare_image(self):
        require(not self.prepared, "Connected mode currently uses host setup, not the old packaged image")
        nodes = command(["kind", "get", "nodes", "--name", self.cluster_id]).split()
        require(nodes == [self.cluster_id + "-control-plane"], "Connected experiment requires its dedicated single-node lab")
        require(hashlib.sha256(UPSTREAM.read_bytes()).hexdigest() == UPSTREAM_SHA256, "Upstream manifest checksum changed")
        resources = decoded_resources(self.kubectl("create", "--dry-run=client", "--validate=false", "-f", str(UPSTREAM),
                                                   "-o", "json", timeout=90).stdout)
        selected = [item for item in resources if item.get("kind") == "Deployment"
                    and item["metadata"]["name"] in APPLICATIONS]
        originals = {container["image"] for item in selected for container in item["spec"]["template"]["spec"]["containers"]}
        images = {}
        for original in sorted(originals | {CLIENT_IMAGE}):
            print("Preparing connected image: " + original, flush=True)
            command(["docker", "pull", original], timeout=600)
            digest = image_digest(original, json.loads(command(["docker", "image", "inspect", original])))
            command(["docker", "exec", nodes[0], "crictl", "pull", digest], timeout=600)
            images[original] = digest
        manifest = connected_manifest(resources, images, self.namespace)
        save(self.directory / "connected-manifest.json", manifest)
        save(self.directory / "images.json", images)
        self.graph = configured_graph(manifest)
        save(self.directory / "dependency-graph.json", self.graph)
        self.kubectl("apply", "-f", str(self.directory / "connected-manifest.json"), timeout=100)
        for name in sorted(APPLICATIONS):
            self.kubectl("rollout", "status", "deployment/" + name, "--timeout=600s", timeout=650)
        self.client_image = images[CLIENT_IMAGE]
        self.install_probe_schema()
        self.observer_collector = KubernetesEvidenceCollector(KubectlReader(self.context), namespace=self.namespace,
            name=self.name, cluster_id=self.cluster_id, collect_logs=True)
        self.write_executor_rbac()
        self._start_storefront()
        label = self.profile.label
        self.summary.update(workload="Full Online Boutique v0.10.7 with synthetic checkout traffic",
            scope=f"Dedicated single-node cluster; {label}-only recovery; harness planning",
            dependencies="Configured upstream address graph; live component readiness; no trace discovery",
            verification=f"Actual {label} gRPC, full storefront cart/checkout and dependency readiness")
        return next(item["spec"]["template"]["spec"]["containers"][0]["image"]
                    for item in manifest["items"] if item["kind"] == "Deployment" and item["metadata"]["name"] == self.name)

    def _start_storefront(self):
        port = self.storefront_url.rsplit(":", 1)[1]
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", int(port)))
        self.forward_log = (self.directory / "storefront-forward.log").open("w", encoding="utf-8")
        self.forward = subprocess.Popen(["kubectl", "--context=" + self.context, "--namespace=" + self.namespace,
            "port-forward", "--address=127.0.0.1", "service/frontend", port + ":80"],
            stdout=self.forward_log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.forward.poll() is not None:
                raise RuntimeError("Storefront forwarding failed; check whether its local port is already used")
            try:
                if "Forwarding from 127.0.0.1:" + port not in (self.directory / "storefront-forward.log").read_text():
                    time.sleep(.2)
                    continue
                with urlopen(self.storefront_url + "/_healthz", timeout=2) as response:
                    if response.status == 200:
                        print("Online Boutique storefront: " + self.storefront_url, flush=True)
                        return
            except OSError:
                time.sleep(.3)
        raise RuntimeError("Storefront forwarding did not become available")

    def required_dependencies(self):
        """The dependency names every verification reading must cover, for this run's verification scope."""
        if self.verification_scope == "target":
            return verification_dependencies(self.graph, self.name)
        return sorted(APPLICATIONS - {self.name}) + ["storefront-checkout"]

    def dependency_readings(self, deployments, journey):
        """One verification reading's dependencies: what required_dependencies names, measured now."""
        ready = readiness(deployments, self.name)
        if self.verification_scope == "target":
            return {name: ready.get(name) for name in self.required_dependencies()}
        return {**ready, "storefront-checkout": journey["passed"]}

    def prepare(self):
        super().prepare()
        self.policy["health"]["required_dependencies"] = self.required_dependencies()
        self.policy["health"]["max_sample_gap_seconds"] = 90
        save(self.directory / "policy.json", self.policy)

    def dependency_result(self, proposal):
        deployments = json.loads(self.kubectl("get", "deployments", "-o", "json").stdout)["items"]
        target = next(item for item in deployments if item["metadata"]["name"] == self.name)
        state = {"cluster_id": self.cluster_id, "captured_at": utc(), "deployment": target,
                 "known_health": readiness(deployments, self.name)}
        save(self.directory / "dependency-readiness.json", state)
        return inspect_dependencies(proposal, self.graph, state, now=now())

    def application_inventory(self):
        # Called by the dashboard thread: no Coordinator connection or mutation.
        response = self.kubectl("get", "deployments", "-o", "json", timeout=8, allow_failure=True)
        deployments = json.loads(response.stdout)["items"] if response.returncode == 0 else None
        return service_inventory(deployments, APPLICATIONS, recovery_targets={self.name})

    def record_shopping(self, stage, journey, *, incident_id=None, action_id=None):
        emit_observation(self.directory / "telemetry.sqlite3", cluster_id=self.cluster_id,
                         namespace=self.namespace, stage=stage, journey=journey,
                         incident_id=incident_id, action_id=action_id)

    def health_window(self, expected_template):
        samples = super().health_window(expected_template)
        collected = []
        for sample in samples:
            sample["storefront"] = self.storefront.journey()
            self.record_shopping("baseline", sample["storefront"])
            collected.append(sample["storefront"])
            save(self.directory / "baseline-storefront.json", collected)
            require(sample["storefront"]["passed"] is True, "Healthy baseline requires actual cart and checkout success")
        return samples

    def fault(self, label):
        incident = super().fault(label)
        if incident is None:  # A scored NO_INCIDENT that the scenario's label allows.
            return None
        impact = self.storefront.journey()
        self.record_shopping(label + "-fault", impact, incident_id=incident["incident_id"])
        save(self.directory / (label + "-storefront-impact.json"), impact)
        # The catalog pre-registers which storefront steps the target's fault breaks.
        require(targets.impact_matches(self.profile, impact),
                "Storefront impact differs from the catalog prediction: " + json.dumps(
                    self.profile.fault_storefront_impact, sort_keys=True))
        self.check(f"Connected {targets.impact_phrase(self.profile)} while {self.profile.label} configuration is broken",
                   scenario=label, cart_status=impact["cart_status"], checkout_status=impact["checkout_status"])
        return incident

    def verification_samples(self, execution):
        for sample in super().verification_samples(execution):
            journey = self.storefront.journey()
            self.record_shopping("verification", journey, incident_id=execution["incident_id"], action_id=execution["action_id"])
            deployments = json.loads(self.kubectl("get", "deployments", "-o", "json").stdout)["items"]
            target = next(item for item in deployments if item["metadata"]["name"] == self.name)
            metadata, status = target["metadata"], target.get("status", {})
            sample["dependencies"] = self.dependency_readings(deployments, journey)
            sample["storefront"] = journey
            sample["configuration_stable"] = (sample["configuration_stable"]
                and metadata["uid"] == execution["target"]["uid"]
                and fingerprint(target["spec"]["template"]) == sample["template_sha256"]
                and status.get("observedGeneration", 0) >= metadata["generation"])
            sample.update(at=utc(), ready_replicas=status.get("readyReplicas", 0),
                          available_replicas=status.get("availableReplicas", 0),
                          desired_replicas=target["spec"].get("replicas", 1),
                          resource_version=metadata.get("resourceVersion"))
            yield sample

    def close(self):
        if self.forward is not None:
            self.forward.terminate()
            try:
                self.forward.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.forward.kill()
                self.forward.wait(timeout=5)
        if self.forward_log:
            self.forward_log.close()
        save(self.directory / "storefront-cleanup.json", {"port_forward_stopped": self.forward is None or self.forward.poll() is not None})
