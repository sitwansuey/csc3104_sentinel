"""Full Boutique with inspected shipping traffic in a dedicated Calico kind lab."""

import argparse
import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.security_store import import_inspections
from scripts.boutique_connected import APPLICATIONS, UPSTREAM, UPSTREAM_SHA256, connected_manifest, decoded_resources, image_digest
from scripts.boutique_dashboard import BoutiqueDashboard
from scripts.boutique_security_manifest import NAMESPACE, overlay
from scripts.boutique_storefront import StorefrontProbe
from scripts.security_lab import CLIENT, ENVOY, INSPECTOR, certificates
from runtime.boutique_observability import emit_observation, service_inventory

CLUSTER = "sentinel-boutique-security"
CONTEXT = "kind-" + CLUSTER
CALICO_URL = "https://raw.githubusercontent.com/projectcalico/calico/v3.33.0/manifests/calico.yaml"
CALICO_SHA = "2de8f47595fb9c41b3f47d7b767a1f8e72ecf84057af834738ff12689a234da5"


def command(args, *, timeout=300, required=True):
    result = subprocess.run([str(arg) for arg in args], capture_output=True, text=True, timeout=timeout, check=False)
    if required and result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr[-1800:]}")
    return result


def kube(*args, **kwargs):
    return command(["kubectl", "--context=" + CONTEXT, "--namespace=" + NAMESPACE, *args], **kwargs)


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def setup_cluster(directory):
    clusters = command(["kind", "get", "clusters"]).stdout.split()
    if CLUSTER not in clusters:
        command(["kind", "create", "cluster", "--name", CLUSTER, "--image", "kindest/node:v1.37.0",
                 "--config", ROOT / "deploy/kind/kind-security.yaml", "--wait", "0s"])
    nodes = command(["kind", "get", "nodes", "--name", CLUSTER]).stdout.split()
    if nodes != [CLUSTER + "-control-plane"]:
        raise ValueError("This lab requires its dedicated single node")
    with urlopen(CALICO_URL, timeout=60) as response:
        manifest = response.read(2_000_000)
    if hashlib.sha256(manifest).hexdigest() != CALICO_SHA:
        raise ValueError("Calico release manifest checksum changed")
    path = directory / "calico.yaml"
    path.write_bytes(manifest)
    command(["kubectl", "--context=" + CONTEXT, "apply", "--server-side", "-f", path], timeout=90)
    command(["kubectl", "--context=" + CONTEXT, "--namespace=kube-system", "rollout", "status",
             "daemonset/calico-node", "--timeout=240s"], timeout=260)
    kube("wait", "node/" + nodes[0], "--for=condition=Ready", "--timeout=120s", timeout=130)
    return nodes[0]


def local_manifest(tag, node, repository):
    image = json.loads(command(["docker", "image", "inspect", tag]).stdout)[0]
    command(["kind", "load", "docker-image", tag, "--name", CLUSTER], timeout=180)
    reference = "docker.io/library/" + tag if "/" not in tag else tag
    listing = command(["docker", "exec", node, "ctr", "--namespace", "k8s.io", "images", "list"]).stdout
    rows = [line.split() for line in listing.splitlines() if line.startswith(reference + " ")]
    if len(rows) != 1 or len(rows[0]) < 3 or not re.fullmatch(r"sha256:[a-f0-9]{64}", rows[0][2]):
        raise ValueError("Cannot bind the local image to a node manifest")
    alias = repository + "@" + rows[0][2]
    command(["docker", "exec", node, "ctr", "--namespace", "k8s.io", "images", "tag", "--force", reference, alias])
    return alias, image["Id"]


def prepare(directory, candidate):
    directory.mkdir(parents=True, exist_ok=False)
    directory.chmod(0o700)
    node = setup_cluster(directory)
    pki = directory / "pki"
    pki.mkdir(mode=0o700)
    certificates(pki, extra_clients=("frontend-client", "checkoutservice-client"))
    if hashlib.sha256(UPSTREAM.read_bytes()).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("Pinned Boutique manifest checksum changed")
    resources = decoded_resources(kube("create", "--dry-run=client", "--validate=false", "-f", UPSTREAM, "-o", "json").stdout)
    originals = {container["image"] for item in resources if item.get("kind") == "Deployment" and item["metadata"]["name"] in APPLICATIONS
                 for container in item["spec"]["template"]["spec"]["containers"]}
    images = {}
    for original in sorted(originals):
        print("Preparing full application image: " + original, flush=True)
        command(["docker", "pull", original], timeout=600)
        digest = image_digest(original, json.loads(command(["docker", "image", "inspect", original]).stdout))
        command(["docker", "exec", node, "crictl", "pull", digest], timeout=600)
        images[original] = digest
    for image in (ENVOY, CLIENT):
        command(["docker", "pull", image])
        command(["docker", "exec", node, "crictl", "pull", image])
    command(["docker", "build", "-f", ROOT / "deploy/docker/security-inspector.Dockerfile", "-t", INSPECTOR, ROOT])
    inspector, inspector_id = local_manifest(INSPECTOR, node, "sentinel.local/security-inspector")
    base = connected_manifest(resources, images, NAMESPACE)
    if candidate:
        evidence = json.loads(candidate.read_text())
        scan = json.loads((candidate.parent / "scan.json").read_text())
        actual = json.loads(command(["docker", "image", "inspect", evidence["tag"]]).stdout)[0]["Id"]
        if (actual != evidence["image_id"] or scan.get("image") != "local://" + actual
                or scan.get("status") not in {"NO_KNOWN_CVES", "FINDINGS"}
                or scan.get("counts", {}).get("HIGH") != 0 or scan.get("counts", {}).get("CRITICAL") != 0):
            raise ValueError("Candidate identity and passing High/Critical scan gate are required")
        replacement, _ = local_manifest(evidence["tag"], node, "sentinel.local/shipping-candidate")
        for item in base["items"]:
            if item["kind"] == "Deployment" and item["metadata"]["name"] == "shippingservice":
                item["spec"]["template"]["spec"]["containers"][0]["image"] = replacement
        save(directory / "candidate-validation.json", {**evidence, "lab_manifest": replacement, "deployed_only_to": CLUSTER})
    manifest = overlay(base, envoy_image=ENVOY, inspector_image=inspector)
    public = directory / "manifest.json"
    save(public, manifest)
    kube("apply", "-f", public)
    # Identity-specific Secrets never enter the public manifest or source tree.
    for identity in ("gateway", "upstream", "proxy-client", "client", "frontend-client", "checkoutservice-client"):
        secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "security-" + identity, "namespace": NAMESPACE},
                  "type": "Opaque", "data": {name: base64.b64encode((pki / identity / name).read_bytes()).decode()
                                             for name in ("ca.crt", "tls.crt", "tls.key")}}
        path = pki / (identity + ".json")
        save(path, secret)
        path.chmod(0o600)
        kube("apply", "-f", path)
    proto = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "security-test-proto", "namespace": NAMESPACE},
             "data": {"shipping.proto": (ROOT / "experiments/workloads/security/shipping.proto").read_text()}}
    save(directory / "proto.json", proto)
    kube("apply", "-f", directory / "proto.json")
    for name in sorted(APPLICATIONS | {"security-gateway"}):
        kube("rollout", "status", "deployment/" + name, "--timeout=360s", timeout=380)
    deployed = [container["image"] for item in manifest["items"] if item["kind"] == "Deployment"
                for container in item["spec"]["template"]["spec"]["containers"]]
    inventory = [{"image": image} for image in sorted(set(deployed)) if image.startswith(("docker.io/", "us-central1-", "envoyproxy/"))]
    inventory.append({"local_image": inspector_id})
    if candidate:
        inventory.append({"local_image": evidence["image_id"]})
    save(directory / "scan-inventory.json", inventory)
    save(directory / "session.json", {"cluster": CLUSTER, "namespace": NAMESPACE, "context": CONTEXT,
                                       "calico": "3.33.0", "calico_sha256": CALICO_SHA})


def probe(directory, *, quantity=1, target="security-gateway:8443", secure=True, certificate=True, identity="client"):
    if identity not in {"client", "proxy-client"}:
        raise ValueError("Only the lab's test identities may be used")
    name = "security-probe-" + uuid4().hex[:8]
    args = ["-max-time", "5", "-import-path", "/proto", "-proto", "shipping.proto"]
    if secure:
        args += ["-cacert", "/client/ca.crt", "-servername", "gateway.internal"]
        if certificate:
            args += ["-cert", "/client/tls.crt", "-key", "/client/tls.key"]
    else:
        args += ["-plaintext"]
    args += ["-d", json.dumps({"items": [{"productId": "OLJCESPC7Z", "quantity": quantity}]}), target,
             "hipstershop.ShippingService/GetQuote"]
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NAMESPACE, "labels": {"app": "security-test"}},
           "spec": {"automountServiceAccountToken": False, "restartPolicy": "Never", "activeDeadlineSeconds": 45,
                    "securityContext": {"runAsUser": 10001, "runAsNonRoot": True, "fsGroup": 10001,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "volumes": [{"name": "client", "secret": {"secretName": "security-" + identity, "defaultMode": 288}},
                                {"name": "proto", "configMap": {"name": "security-test-proto"}}],
                    "containers": [{"name": "client", "image": CLIENT, "imagePullPolicy": "Never", "args": args,
                        "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                        "volumeMounts": [{"name": "client", "mountPath": "/client", "readOnly": True}, {"name": "proto", "mountPath": "/proto", "readOnly": True}]}]}}
    path = directory / (name + ".json")
    save(path, pod)
    try:
        kube("create", "-f", path)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = json.loads(kube("get", "pod", name, "-o", "json").stdout)
            phase = state.get("status", {}).get("phase")
            if phase in {"Succeeded", "Failed"}:
                log = kube("logs", name, required=False).stdout
                return {"succeeded": phase == "Succeeded", "output": log[-3000:]}
            time.sleep(.5)
        raise RuntimeError("Security probe did not finish")
    finally:
        kube("delete", "pod", name, "--ignore-not-found", "--wait=false", required=False)


def collect_inspections(directory):
    result = kube("logs", "deployment/security-gateway", "-c", "inspector", "--tail=500", required=False, timeout=8)
    if result.returncode:
        return
    events = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("kind") == "inspection":
            events.append(event)
    import_inspections(directory / "telemetry.sqlite3", events)


def inventory():
    data = json.loads(kube("get", "deployments", "-o", "json", timeout=8).stdout)
    return service_inventory(data["items"], APPLICATIONS)


def validate(directory, storefront):
    summary = {}
    def journey_window(key):
        attempts = []
        for _ in range(5):
            attempts.append(storefront.journey())
            summary[key + "_attempts"] = attempts
            save(directory / "validation.json", summary)
            if attempts[-1]["passed"] is True:
                break
            time.sleep(3)
        return attempts[-1]
    summary["shopping_and_checkout"] = journey_window("baseline")
    if summary["shopping_and_checkout"]["passed"] is not True:
        raise RuntimeError("Connected shopping/checkout failed")
    emit_observation(directory / "telemetry.sqlite3", cluster_id=CLUSTER, namespace=NAMESPACE,
                     stage="baseline", journey=summary["shopping_and_checkout"])
    summary["valid_mtls_quote"] = probe(directory)
    summary["invalid_quantity"] = probe(directory, quantity=101)
    summary["missing_client_certificate"] = probe(directory, certificate=False)
    summary["wrong_client_identity"] = probe(directory, identity="proxy-client")
    summary["direct_service_bypass"] = probe(directory, target="shippingservice:50051", secure=False)
    shipping = json.loads(kube("get", "pods", "-l", "app=shippingservice", "-o", "json").stdout)["items"][0]
    summary["direct_pod_ip_bypass"] = probe(directory, target=shipping["status"]["podIP"] + ":50051", secure=False)
    frontend = json.loads(kube("get", "pods", "-l", "app=frontend", "-o", "json").stdout)["items"][0]
    summary["direct_egress_bypass"] = probe(directory, target=frontend["status"]["podIP"] + ":50052", secure=False)
    collect_inspections(directory)
    kube("scale", "deployment/security-gateway", "--replicas=0")
    try:
        deadline = time.monotonic() + 60
        while json.loads(kube("get", "pods", "-l", "app=security-gateway", "-o", "json").stdout)["items"]:
            if time.monotonic() > deadline:
                raise RuntimeError("Proxy did not stop for the failure test")
            time.sleep(.5)
        summary["proxy_outage"] = probe(directory)
        summary["outage_direct_bypass"] = probe(directory, target="shippingservice:50051", secure=False)
    finally:
        kube("scale", "deployment/security-gateway", "--replicas=1")
        kube("rollout", "status", "deployment/security-gateway", "--timeout=120s", timeout=140)
    summary["shopping_after_proxy_restore"] = journey_window("restore")
    # Fresh empty carts must also work after checkout empties the session cart.
    from http.cookiejar import CookieJar
    from urllib.request import build_opener, HTTPCookieProcessor
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    status, _, error = storefront._request(opener, "/cart")
    summary["empty_cart"] = {"status": status, "error": error}
    expected = summary["valid_mtls_quote"]["succeeded"] and all(not summary[key]["succeeded"] for key in (
        "invalid_quantity", "missing_client_certificate", "wrong_client_identity", "direct_service_bypass", "direct_pod_ip_bypass",
        "direct_egress_bypass", "proxy_outage", "outage_direct_bypass"))
    summary["passed"] = bool(expected and summary["shopping_after_proxy_restore"]["passed"] is True and status == 200)
    save(directory / "validation.json", summary)
    collect_inspections(directory)
    if not summary["passed"]:
        raise RuntimeError("Security validation failed; inspect validation.json")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "test", "serve", "stop"))
    parser.add_argument("--directory", type=Path, default=ROOT / "data/sentinel/runs/boutique-security")
    parser.add_argument("--candidate", type=Path, help="Reviewed candidate.json with adjacent passing scan.json")
    parser.add_argument("--storefront-port", type=int, default=8081)
    parser.add_argument("--dashboard-port", type=int, default=8767)
    parser.add_argument("--exit-after-test", action="store_true")
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.action == "stop":
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "stop-ui.request").touch()
        if (directory / "scans").exists():
            (directory / "scans/stop.request").touch()
        command(["kind", "delete", "cluster", "--name", CLUSTER])
        return
    if args.action == "start":
        prepare(directory, args.candidate)
    if args.action == "serve":
        if not json.loads((directory / "validation.json").read_text()).get("passed"):
            raise ValueError("Run the live security tests before serving this run")
    for port in (args.storefront_port, args.dashboard_port):
        with socket.socket() as check:
            check.bind(("127.0.0.1", port))
    (directory / "stop-ui.request").unlink(missing_ok=True)
    log = (directory / "storefront-forward.log").open("a", encoding="utf-8")
    forward = subprocess.Popen(["kubectl", "--context=" + CONTEXT, "--namespace=" + NAMESPACE, "port-forward",
        "--address=127.0.0.1", "service/frontend", f"{args.storefront_port}:80"], stdout=log, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    dashboard = None
    save(directory / "ui-process.json", {"pid": os.getpid(), "forward_pid": forward.pid, "context": CONTEXT,
                                         "storefront_port": args.storefront_port, "dashboard_port": args.dashboard_port})
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        storefront = StorefrontProbe(f"http://127.0.0.1:{args.storefront_port}")
        deadline = time.monotonic() + 30
        while True:
            try:
                with urlopen(storefront.base_url + "/_healthz", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() > deadline or forward.poll() is not None:
                raise RuntimeError("Storefront forward did not become ready")
            time.sleep(.3)
        if args.action != "serve":
            validate(directory, storefront)
            print("PASS: full Boutique shipping traffic inspected; direct bypass and proxy outage blocked", flush=True)
        else:
            print("Serving recorded validation and current read-only observations; no faults injected.", flush=True)
        if args.exit_after_test:
            return
        dashboard = BoutiqueDashboard(directory, context=CONTEXT, cluster_id=CLUSTER, namespace=NAMESPACE,
                                      port=args.dashboard_port, application_collector=inventory)
        dashboard.start()
        print(f"Storefront {storefront.base_url}; dashboard http://127.0.0.1:{args.dashboard_port}/#security", flush=True)
        while not (directory / "stop-ui.request").exists():
            collect_inspections(directory)
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        if dashboard:
            dashboard.close()
        forward.terminate()
        try:
            forward.wait(timeout=5)
        except subprocess.TimeoutExpired:
            forward.kill()
        log.close()
        save(directory / "ui-process.json", {"pid": os.getpid(), "stopped": True, "context": CONTEXT})


if __name__ == "__main__":
    main()
