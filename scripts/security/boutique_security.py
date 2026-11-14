"""Full Boutique with inspected shipping and cart traffic in a dedicated Calico kind lab."""

import argparse
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from runtime.security_store import import_inspections
from experiments.boutique.harness.connected import APPLICATIONS, UPSTREAM, UPSTREAM_SHA256, connected_manifest, decoded_resources, image_digest
from experiments.boutique.harness.dashboard import BoutiqueDashboard
from scripts.security.boutique_security_manifest import (GATEWAY, GATEWAY_SERVICES, NAMESPACE, VERIFIER_IDENTITY,
                                                          VERIFIER_PROBE_APP, overlay, protect_policies)
from scripts.security.network_policy import (MATRIX_IMAGE, MATRIX_LABEL, committed_security_policies, graph_from_manifest,
                                             matrix_destinations, matrix_sources, policy_differences, probe_targets,
                                             reviewed_security_policies, run_matrix)
from experiments.boutique.traffic.storefront import StorefrontProbe
from scripts.security import cert_lifecycle
from scripts.security.security_lab import CLIENT, ENVOY, INSPECTOR, ROUTES
from runtime.boutique_observability import emit_observation, service_inventory
from shared import provenance

CLUSTER = "sentinel-boutique-security"
CONTEXT = "kind-" + CLUSTER
CALICO_URL = "https://raw.githubusercontent.com/projectcalico/calico/v3.33.0/manifests/calico.yaml"
CALICO_SHA = "2de8f47595fb9c41b3f47d7b767a1f8e72ecf84057af834738ff12689a234da5"
# The certificate identities cert-manager issues into Secrets (never committed; P5.06).
IDENTITIES = tuple(identity for identity, _ in cert_lifecycle.IDENTITIES)
# The identities a test probe may present: the lab's own, and the refused test identities (P5.06).
PROBE_IDENTITIES = ("client", "proxy-client", VERIFIER_IDENTITY, *cert_lifecycle.NEGATIVE_IDENTITIES,
                    cert_lifecycle.OLD_CA_IDENTITY)
# The grpcurl probe per route: its schema file and the read-only RPC used for
# a valid call. A cart probe with a quantity sends AddItem instead.
PROBE_RPCS = {"shipping": ("shipping.proto", "hipstershop.ShippingService/GetQuote"),
              "cart": ("cart.proto", "hipstershop.CartService/GetCart")}
# A fixed test session id; the lab's cartservice keeps carts in its own redis-cart.
PROBE_USER = "00000000-0000-4000-8000-000000000001"


def command(args, *, timeout=300, required=True):
    result = subprocess.run([str(arg) for arg in args], capture_output=True, text=True, timeout=timeout, check=False)
    if required and result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr[-1800:]}")
    return result


def kube(*args, **kwargs):
    return command(["kubectl", "--context=" + CONTEXT, "--namespace=" + NAMESPACE, *args], **kwargs)


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def current_context():
    result = command(["kubectl", "config", "current-context"], required=False, timeout=30)
    return result.stdout.strip() or None if result.returncode == 0 else None


def restore_context(previous):
    """Put the user's current-context back after kind changed it.

    kind makes a new cluster current on create, and leaves no current-context
    after deleting the current one. This lab always passes --context explicitly.
    """
    if previous == CONTEXT:
        return  # The user had selected this lab; nothing else to restore.
    if previous:
        command(["kubectl", "config", "use-context", previous], required=False, timeout=30)
    else:
        command(["kubectl", "config", "unset", "current-context"], required=False, timeout=30)


def setup_cluster(directory):
    clusters = command(["kind", "get", "clusters"]).stdout.split()
    if CLUSTER not in clusters:
        previous = current_context()
        try:
            command(["kind", "create", "cluster", "--name", CLUSTER, "--image", "kindest/node:v1.37.0",
                     "--config", ROOT / "deploy/kind/kind-security.yaml", "--wait", "0s"])
        finally:
            restore_context(previous)
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


def prepare(directory, candidate, *, profile="lab"):
    # P5.06: refuse before any cluster command while a cert-manager pin is unresolved.
    pins = cert_lifecycle.load_pins()
    problems = cert_lifecycle.unresolved_pins(pins)
    if problems:
        raise ValueError("cert-manager pins are unresolved; run scripts/security/cert_lifecycle.py pins: "
                         + "; ".join(problems))
    directory.mkdir(parents=True, exist_ok=False)
    directory.chmod(0o700)
    node = setup_cluster(directory)
    pki = directory / "pki"
    pki.mkdir(mode=0o700)
    if hashlib.sha256(UPSTREAM.read_bytes()).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("Pinned Boutique manifest checksum changed")
    resources = decoded_resources(kube("create", "--dry-run=client", "--validate=false", "-f", UPSTREAM, "-o", "json").stdout)
    originals = {container["image"] for item in resources if item.get("kind") == "Deployment" and item["metadata"]["name"] in APPLICATIONS
                 for container in item["spec"]["template"]["spec"]["containers"]}
    # Refuse before any pull when the wiring is not the reviewed graph or the
    # generated policies differ from deploy/boutique/security-network-policies.json.
    policies = reviewed_security_policies(graph_from_manifest(connected_manifest(
        resources, {image: image for image in originals}, NAMESPACE)))
    # The lab CA that seeds cert-manager's issuer (its key stays in this private folder), then the
    # pinned cert-manager chart and images (P5.06).
    ca = cert_lifecycle.make_ca(pki, "ca-1", command=command)
    cert_lifecycle.install(directory, node, command, pins=pins)
    images = {}
    for original in sorted(originals):
        print("Preparing full application image: " + original, flush=True)
        command(["docker", "pull", original], timeout=600)
        digest = image_digest(original, json.loads(command(["docker", "image", "inspect", original]).stdout))
        command(["docker", "exec", node, "crictl", "pull", digest], timeout=600)
        images[original] = digest
    for image in (ENVOY, CLIENT, MATRIX_IMAGE):
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
    manifest["items"].extend(policies)
    save(directory / "network-policies.json", {"apiVersion": "v1", "kind": "List", "items": policies})
    public = directory / "manifest.json"
    save(public, manifest)
    kube("apply", "-f", public)
    # cert-manager issues every identity's Secret from the lab CA; keys never enter the
    # public manifest or source tree, and Envoy reloads renewed files without a restart.
    cert_lifecycle.issue(directory, ca, command, profile=profile)
    proto = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "security-test-proto", "namespace": NAMESPACE},
             "data": {name: (ROOT / "experiments/workloads/security" / name).read_text()
                      for name in sorted({schema for schema, _ in PROBE_RPCS.values()})}}
    save(directory / "proto.json", proto)
    kube("apply", "-f", directory / "proto.json")
    for name in sorted(APPLICATIONS | {"security-gateway"}):
        kube("rollout", "status", "deployment/" + name, "--timeout=360s", timeout=380)
    deployed = [container["image"] for item in manifest["items"] if item["kind"] == "Deployment"
                for container in item["spec"]["template"]["spec"]["containers"]]
    inventory = [{"image": image} for image in sorted(set(deployed)) if image.startswith(("docker.io/", "us-central1-", "envoyproxy/"))]
    inventory.append({"image": MATRIX_IMAGE})
    inventory.append({"local_image": inspector_id})
    if candidate:
        inventory.append({"local_image": evidence["image_id"]})
    save(directory / "scan-inventory.json", inventory)
    save(directory / "session.json", {"cluster": CLUSTER, "namespace": NAMESPACE, "context": CONTEXT,
                                       "calico": "3.33.0", "calico_sha256": CALICO_SHA,
                                       "certificates": {"issuer": "cert-manager", "version": pins["version"],
                                                        "profile": profile, "ca": ca.name}})


def probe_call(route="shipping", quantity=None):
    """(schema file, RPC, request JSON) for one route's grpcurl probe."""
    schema, method = PROBE_RPCS[route]
    if route == "shipping":
        return schema, method, {"items": [{"productId": "OLJCESPC7Z", "quantity": 1 if quantity is None else quantity}]}
    if quantity is None:
        return schema, method, {"userId": PROBE_USER}
    return schema, "hipstershop.CartService/AddItem", {"userId": PROBE_USER,
                                                       "item": {"productId": "OLJCESPC7Z", "quantity": quantity}}


def probe(directory, *, route="shipping", quantity=None, target=None, secure=True, certificate=True, identity="client"):
    if identity not in PROBE_IDENTITIES:
        raise ValueError("Only the lab's test identities may be used")
    schema, method, request = probe_call(route, quantity)
    target = target or f"{GATEWAY_SERVICES[route]}:{ROUTES[route]['gateway_port']}"
    name = "security-probe-" + uuid4().hex[:8]
    args = ["-max-time", "5", "-import-path", "/proto", "-proto", schema]
    if secure:
        args += ["-cacert", "/client/ca.crt", "-servername", "gateway.internal"]
        if certificate:
            args += ["-cert", "/client/tls.crt", "-key", "/client/tls.key"]
    else:
        args += ["-plaintext"]
    args += ["-d", json.dumps(request), target, method]
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
    try:
        result = kube("logs", "deployment/security-gateway", "-c", "inspector", "--tail=500",
                      required=False, timeout=8)
    except subprocess.TimeoutExpired:
        return  # kubectl waits for a Pod while the gateway has none; try again next poll.
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


def network_matrix(directory, journey_passed):
    """Every tester identity against every Service, judged against the reviewed NetworkPolicies.

    The expected cells come from the protect-* chain and the committed
    graph-derived set, never from the run folder. The run folder's
    manifest.json (what prepare applied) and the cluster's live policies must
    both be exactly that set, or the verdict cannot be PASS: an older run
    folder without the graph policies would otherwise expect every forbidden
    edge to connect.
    """
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    reviewed = protect_policies() + committed_security_policies()
    applied = [item for item in manifest["items"] if item["kind"] == "NetworkPolicy"]
    sources = matrix_sources(APPLICATIONS | {GATEWAY[0]}, probe_targets(), extra=("security-test", VERIFIER_PROBE_APP))
    return run_matrix(lambda *args: command(["kubectl", "--context=" + CONTEXT, *args], required=False, timeout=60),
                      directory, namespace=NAMESPACE, items=reviewed, sources=sources,
                      destinations=matrix_destinations(manifest), journey_passed=journey_passed,
                      policy_drift=policy_differences(reviewed, applied, "manifest.json"))


def workload(app):
    """A label selector for a workload's own Pods, excluding matrix testers that carry its label."""
    return "app=" + app + ",!" + MATRIX_LABEL


# The probes that must succeed, and those that must fail, for a passing run.
ALLOWED_PROBES = ("valid_mtls_quote", "valid_mtls_cart", "verifier_identity_quote")
DENIED_PROBES = ("invalid_quantity", "missing_client_certificate", "wrong_client_identity", "direct_service_bypass",
                 "direct_pod_ip_bypass", "direct_egress_bypass", "proxy_outage", "outage_direct_bypass",
                 "cart_invalid_quantity", "cart_missing_client_certificate", "cart_wrong_client_identity",
                 "cart_direct_service_bypass", "cart_direct_pod_ip_bypass", "cart_direct_egress_bypass",
                 "cart_proxy_outage", "cart_outage_direct_bypass")


def validate(directory, storefront):
    # passed stays false in every partial save; only the final verdict sets it.
    summary = {"provenance": provenance.collect(ROOT, tools=("kubectl", "kind", "docker"), captured_by="security-lab"),
               "passed": False}
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
    summary["network_matrix"] = network_matrix(directory, summary["shopping_and_checkout"]["passed"])["verdict"]
    save(directory / "validation.json", summary)
    summary["valid_mtls_quote"] = probe(directory)
    # P5.05: the Verifier's client identity is accepted at the gateway (its SAN is allowlisted there).
    summary["verifier_identity_quote"] = probe(directory, identity=VERIFIER_IDENTITY)
    summary["invalid_quantity"] = probe(directory, quantity=101)
    summary["missing_client_certificate"] = probe(directory, certificate=False)
    summary["wrong_client_identity"] = probe(directory, identity="proxy-client")
    summary["direct_service_bypass"] = probe(directory, target="shippingservice:50051", secure=False)
    # Matrix testers carry workload labels; never mistake a leftover one for the workload.
    shipping = json.loads(kube("get", "pods", "-l", workload("shippingservice"), "-o", "json").stdout)["items"][0]
    summary["direct_pod_ip_bypass"] = probe(directory, target=shipping["status"]["podIP"] + ":50051", secure=False)
    frontend = json.loads(kube("get", "pods", "-l", workload("frontend"), "-o", "json").stdout)["items"][0]
    summary["direct_egress_bypass"] = probe(directory, target=frontend["status"]["podIP"] + ":50052", secure=False)
    # The cart route (P5.04): the same checks through its own gateway port.
    summary["valid_mtls_cart"] = probe(directory, route="cart")
    summary["cart_invalid_quantity"] = probe(directory, route="cart", quantity=11)
    summary["cart_missing_client_certificate"] = probe(directory, route="cart", certificate=False)
    summary["cart_wrong_client_identity"] = probe(directory, route="cart", identity="proxy-client")
    summary["cart_direct_service_bypass"] = probe(directory, route="cart", target="cartservice:7070", secure=False)
    cart = json.loads(kube("get", "pods", "-l", workload("cartservice"), "-o", "json").stdout)["items"][0]
    summary["cart_direct_pod_ip_bypass"] = probe(directory, route="cart", target=cart["status"]["podIP"] + ":7070",
                                                 secure=False)
    summary["cart_direct_egress_bypass"] = probe(directory, route="cart", target=frontend["status"]["podIP"] + ":50053",
                                                 secure=False)
    collect_inspections(directory)
    kube("scale", "deployment/security-gateway", "--replicas=0")
    try:
        deadline = time.monotonic() + 60
        while json.loads(kube("get", "pods", "-l", workload(GATEWAY[0]), "-o", "json").stdout)["items"]:
            if time.monotonic() > deadline:
                raise RuntimeError("Proxy did not stop for the failure test")
            time.sleep(.5)
        summary["proxy_outage"] = probe(directory)
        summary["outage_direct_bypass"] = probe(directory, target="shippingservice:50051", secure=False)
        summary["cart_proxy_outage"] = probe(directory, route="cart")
        summary["cart_outage_direct_bypass"] = probe(directory, route="cart", target="cartservice:7070", secure=False)
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
    expected = all(summary[key]["succeeded"] for key in ALLOWED_PROBES) and all(
        not summary[key]["succeeded"] for key in DENIED_PROBES)
    summary["passed"] = bool(expected and summary["shopping_after_proxy_restore"]["passed"] is True and status == 200
                             and summary["network_matrix"]["status"] == "PASS")
    save(directory / "validation.json", summary)
    collect_inspections(directory)
    if not summary["passed"]:
        raise RuntimeError("Security validation failed; inspect validation.json")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "test", "serve", "stop", "probe"))
    parser.add_argument("--directory", type=Path, default=ROOT / "data/sentinel/runs/boutique-security")
    parser.add_argument("--candidate", type=Path, help="Reviewed candidate.json with adjacent passing scan.json")
    parser.add_argument("--storefront-port", type=int, default=8081)
    parser.add_argument("--dashboard-port", type=int, default=8767)
    parser.add_argument("--exit-after-test", action="store_true")
    parser.add_argument("--route", choices=sorted(ROUTES), default="shipping",
                        help="probe: the protected route to call through the gateway")
    parser.add_argument("--quantity", type=int, help="probe: send this item quantity (cart: AddItem instead of GetCart)")
    parser.add_argument("--identity", choices=PROBE_IDENTITIES, default="client",
                        help="probe: the client certificate to present (P5.06 negatives must be refused)")
    parser.add_argument("--profile", choices=sorted(cert_lifecycle.PROFILES), default="lab",
                        help="start: certificate lifetime (rotation renews every ten minutes)")
    args = parser.parse_args(argv)
    directory = args.directory.resolve()
    if args.action == "probe":
        # One authenticated call through the gateway from a short-lived test Pod;
        # exit status 0 only when the call succeeded.
        result = probe(directory, route=args.route, quantity=args.quantity, identity=args.identity)
        print(json.dumps({"route": args.route, "quantity": args.quantity, "identity": args.identity,
                          "succeeded": result["succeeded"]}), flush=True)
        return 0 if result["succeeded"] else 1
    if args.action == "stop":
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "stop-ui.request").touch()
        if (directory / "scans").exists():
            (directory / "scans/stop.request").touch()
        previous = current_context()
        try:
            command(["kind", "delete", "cluster", "--name", CLUSTER])
        finally:
            restore_context(previous)
        return
    if args.action == "start":
        prepare(directory, args.candidate, profile=args.profile)
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
            print("PASS: full Boutique shipping and cart traffic inspected; direct bypass and proxy outage blocked; "
                  "NetworkPolicy matrix passed", flush=True)
        else:
            print("Serving recorded validation and current read-only observations; no faults injected.", flush=True)
        if args.exit_after_test:
            return
        dashboard = BoutiqueDashboard(directory, context=CONTEXT, cluster_id=CLUSTER, namespace=NAMESPACE,
                                      port=args.dashboard_port, application_collector=inventory)
        dashboard.start()
        print(f"Storefront {storefront.base_url}; dashboard http://127.0.0.1:{args.dashboard_port}/#security", flush=True)
        while not (directory / "stop-ui.request").exists():
            try:
                collect_inspections(directory)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                # Collection is best-effort evidence; keep the dashboard serving.
                print("Inspection collection skipped: " + type(error).__name__, flush=True)
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
    sys.exit(main())
