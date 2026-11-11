"""Bounded, namespaced Chaos Mesh resources shared by live demonstrations."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

from experiments.boutique.harness.connected import APPLICATIONS, UPSTREAM, UPSTREAM_SHA256, decoded_resources

ROOT = Path(__file__).resolve().parents[3]
FAULTS = ("cpu", "memory", "pod", "network", "http", "dns")
# DNSChaos copies /etc/resolv.conf with a shell inside the target container. The
# pinned Boutique pods have read-only root filesystems and frontend has no shell,
# so the scenario can never be injected there. Refuse it before any cluster work.
UNSUPPORTED = {"dns": "DNSChaos needs a shell and a writable /etc in the target container; "
                      "the pinned Boutique pods are read-only and frontend has no shell"}
SUPPORTED = tuple(fault for fault in FAULTS if fault not in UNSUPPORTED)
KINDS = ("StressChaos", "PodChaos", "NetworkChaos", "HTTPChaos", "DNSChaos")
CLUSTER = "sentinel-boutique-chaos"
CONTEXT = "kind-" + CLUSTER
CHAOS_VERSION = "2.8.2"
TRAFFIC_IMAGE = "python:3.12-alpine3.23@sha256:3d0ca66b723a0ff2a7a6e424cc2d01443e1b7c75ffcc110145f75d0f4daf6d63"


def validate_scope(context, namespace):
    if not re.fullmatch(r"kind-sentinel-boutique-[a-z0-9-]+", context):
        raise ValueError("Use an explicit kind-sentinel-boutique-* lab context")
    if not re.fullmatch(r"sentinel-boutique(?:-[a-z0-9]+)*", namespace) or len(namespace) > 63:
        raise ValueError("Use a sentinel-boutique* lab namespace")


def scenario(fault, namespace, name, duration, target=None):
    if fault not in FAULTS:
        raise ValueError("Unknown fault")
    if type(duration) is not int or not 5 <= duration <= 3600:
        raise ValueError("Fault duration must be 5..3600 seconds")
    config = json.loads((ROOT / "experiments/boutique/scenarios" / (fault + ".json")).read_text())
    target = target or config["target_service"]
    if target not in APPLICATIONS:
        raise ValueError("Target must be a Boutique service")
    if fault == "http" and target != "frontend":
        raise ValueError("HTTPChaos supports frontend:8080; internal gRPC is not HTTP/1.x")
    if fault == "dns" and target == "redis-cart":
        raise ValueError("Redis does not make the dependency DNS queries used by this scenario")
    spec = deepcopy(config["spec"])
    spec.update(mode="one", duration=f"{duration}s",
                selector={"namespaces": [namespace], "labelSelectors": {"app": target}})
    return {"apiVersion": "chaos-mesh.org/v1alpha1", "kind": config["kind"],
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {"app.kubernetes.io/managed-by": "sentinel-chaos-demo"}}, "spec": spec}


def injected(document):
    status = document.get("status", {})
    conditions = {x.get("type"): x.get("status") for x in status.get("conditions", [])}
    records = status.get("experiment", {}).get("containerRecords", [])
    return (conditions.get("Selected") == "True" and conditions.get("AllInjected") == "True"
            and bool(records) and all(x.get("phase") == "Injected" for x in records))


class Cluster:
    def __init__(self, context, namespace, kubectl="kubectl"):
        validate_scope(context, namespace)
        self.context, self.namespace, self.kubectl = context, namespace, kubectl

    @staticmethod
    def command(args, *, data=None, timeout=60):
        result = subprocess.run(args, input=json.dumps(data) if data is not None else None,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, check=False)
        if result.returncode:
            raise RuntimeError(f"{Path(args[0]).name} {args[1]} failed: {result.stderr[-2000:]}")
        return result.stdout

    def call(self, *args, data=None, timeout=60, namespace=None):
        return self.command([self.kubectl, "--context", self.context, "--namespace",
                             namespace or self.namespace, "--request-timeout=20s", *args],
                            data=data, timeout=timeout)

    def get(self, *args, namespace=None):
        return json.loads(self.call("get", *args, "-o", "json", namespace=namespace))

    def raw(self, path):
        return json.loads(self.call("get", "--raw", path))

    def create(self, document, *, dry_run=False):
        args = ["create", "-f", "-", "-o", "json"]
        if dry_run:
            args.append("--dry-run=server")
        return json.loads(self.call(*args, data=document))

    def remove(self, kind, name):
        # Never strip finalizers: successful deletion waits for controller cleanup.
        self.call("delete", kind, name, "--ignore-not-found=true", "--wait=true", "--timeout=90s", timeout=100)
        remaining = self.call("get", kind, name, "--ignore-not-found=true", "-o", "name")
        if remaining.strip():
            raise RuntimeError(f"Cleanup not confirmed for {kind}/{name}")


def deployments_ready(items):
    indexed = {x["metadata"]["name"]: x for x in items}
    for name in APPLICATIONS:
        obj = indexed.get(name, {})
        desired = obj.get("spec", {}).get("replicas", 1)
        status = obj.get("status", {})
        if (desired < 1 or status.get("observedGeneration", 0) < obj.get("metadata", {}).get("generation", 1)
                or status.get("readyReplicas", 0) < desired or status.get("availableReplicas", 0) < desired):
            return False
    return True


def workload_manifest(resources, namespace):
    items = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace,
              "annotations": {"chaos-mesh.org/inject": "enabled"}}}]
    for resource in resources:
        name = resource.get("metadata", {}).get("name")
        if name in {"frontend-external", "loadgenerator"}:
            continue
        if name not in APPLICATIONS or resource["kind"] not in {"Deployment", "Service", "ServiceAccount"}:
            raise ValueError("Unexpected upstream workload resource")
        resource = deepcopy(resource)
        resource["metadata"]["namespace"] = namespace
        if resource["kind"] == "Deployment":
            resource["spec"]["template"]["spec"]["automountServiceAccountToken"] = False
        if resource["kind"] == "Service" and resource["spec"].get("type", "ClusterIP") != "ClusterIP":
            raise ValueError("Only internal services are allowed")
        items.append(resource)
    names = {x["metadata"]["name"] for x in items if x["kind"] == "Deployment"}
    if names != APPLICATIONS:
        raise ValueError("Incomplete Boutique deployment")
    return {"apiVersion": "v1", "kind": "List", "items": items}


def current_context(cluster):
    try:
        return cluster.command([cluster.kubectl, "config", "current-context"]).strip() or None
    except RuntimeError:
        return None  # kubectl exits non-zero when no current-context is set.


def restore_context(cluster, previous):
    """kind switches the user's current-context on create; put the previous one back.

    The runner itself always passes an explicit --context, so it does not need the switch.
    """
    if previous:
        cluster.command([cluster.kubectl, "config", "use-context", previous])
    else:
        cluster.command([cluster.kubectl, "config", "unset", "current-context"])


def setup(cluster, kind="kind", helm="helm"):
    if (cluster.context, cluster.namespace) != (CONTEXT, CLUSTER):
        raise ValueError("--setup only provisions the dedicated sentinel-boutique-chaos lab")
    for tool in (kind, helm, cluster.kubectl, "docker"):
        if not shutil.which(tool):
            raise RuntimeError(f"Missing {tool}; install it or supply its executable path")
    if cluster.command(["docker", "info", "--format", "{{.OSType}}"], timeout=30).strip() != "linux":
        raise RuntimeError("Docker Desktop must run Linux containers")
    if CLUSTER not in cluster.command([kind, "get", "clusters"]).split():
        previous = current_context(cluster)
        try:
            cluster.command([kind, "create", "cluster", "--name", CLUSTER,
                             "--config", str(ROOT / "deploy/kind/kind.yaml"), "--wait", "120s"], timeout=300)
        finally:
            restore_context(cluster, previous)
    nodes = cluster.get("nodes")["items"]
    if [x["metadata"]["name"] for x in nodes] != [CLUSTER + "-control-plane"]:
        raise RuntimeError("Context does not point to the dedicated single-node kind lab")
    cluster.command([helm, "upgrade", "--install", "chaos-mesh", "chaos-mesh", "--repo",
                     "https://charts.chaos-mesh.org", "--version", CHAOS_VERSION,
                     "--kube-context", cluster.context, "--namespace", "chaos-mesh", "--create-namespace",
                     "--values", str(ROOT / "deploy/chaos-mesh/values.yaml"), "--wait", "--timeout", "10m"], timeout=660)
    if hashlib.sha256(UPSTREAM.read_bytes()).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("Pinned upstream Boutique manifest checksum changed")
    text = cluster.call("create", "--dry-run=client", "--validate=false", "-f", str(UPSTREAM), "-o", "json")
    manifest = workload_manifest(decoded_resources(text), cluster.namespace)
    cluster.call("apply", "-f", "-", data=manifest)
    for name in sorted(APPLICATIONS):
        print(f"Waiting for {name}...", flush=True)
        cluster.call("rollout", "status", "deployment/" + name, "--timeout=600s", timeout=620)
