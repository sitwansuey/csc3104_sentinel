"""Graph-derived default-deny NetworkPolicies for Online Boutique, and their enforcement matrix.

The input is the reviewed caller-to-callee graph of the pinned upstream
manifest (deploy/boutique/connected-graph.json). The harness derives the same
graph at run time (connected.configured_graph and connected.service_ports), and
the security lab refuses to start when the two differ.

policies() turns a list of (caller, callee, Pod port) edges into:

- default-deny-ingress: every Pod in the namespace (podSelector {}) accepts
  nothing unless another policy allows it;
- default-deny-egress: every graph workload may send nothing except DNS to
  kube-dns;
- ingress-<callee>: exactly the callee's callers, on its Pod port, plus the
  Sentinel probe Pods for that callee, plus in-cluster storefront traffic Pods
  for frontend;
- egress-<caller>: exactly the caller's callees, on their Pod ports.

These are L4 rules keyed on the Pod label app. They are not authentication:
anyone who may create a Pod in the namespace can give it a workload's label.
kubectl port-forward enters a Pod through the container runtime's stream, not
the Pod network, so NetworkPolicy does not apply to it.

The matrix part runs a short-lived tester Pod per source identity and tries a
TCP connection to every Service. The cells are judged against the reviewed
policy set, never the run folder's copy, and the cluster's own policies must
be exactly that set. It reports PASS only when every connection the policies
allow works, every other one times out, the design's sampled forbidden edges
are among them, and the storefront journey passed. A forbidden connection that
works or is refused at once, a cell that could not be tested, or a policy set
that differs from the reviewed one gives UNKNOWN, never PASS: on a network
plugin that ignores NetworkPolicy every forbidden edge connects.
"""

import argparse
from collections import defaultdict
import ipaddress
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from experiments.boutique.harness import scenarios, targets
from experiments.boutique.harness.connected import configured_graph, service_ports
from experiments.boutique.harness.scenarios import CONNECTED_SCOPE
from scripts.security.boutique_security_manifest import CALLERS, GATEWAY, NAMESPACE
from scripts.security.security_lab import ROUTES

GRAPH = ROOT / "deploy/boutique/connected-graph.json"
SECURITY_POLICIES = ROOT / "deploy/boutique/security-network-policies.json"
API_VERSION = "networking.k8s.io/v1"
APP = "app"
PROBE_APP = "sentinel-probe"
PROBE_TARGET = "sentinel.io/probe-target"
TRAFFIC_APP = "sentinel-traffic"
INTRUDER_APP = "sentinel-intruder"
MATRIX_LABEL = "sentinel.io/network-matrix"
ENTRY = "frontend"
# The busybox release pinned by the reviewed upstream manifest (its
# loadgenerator init container); its nc gives a plain TCP connect test.
MATRIX_IMAGE = "docker.io/library/busybox@sha256:dc2d74b28e4cf8984fa52af1f39bc7c3d9c73760b41a74d629f5d11b1ab28616"
CONNECT_TIMEOUT = 3
# The design's sampled forbidden edges (E9) as matrix cells. A run judged
# against the wrong policy set (for example without the graph policies) would
# expect these to connect, so the verdict needs each one judged forbidden.
SAMPLED_DENIALS = (("intruder", "redis-cart"), ("app-productcatalogservice", "cartservice"),
                   ("app-paymentservice", "checkoutservice"))
DNS_RULE = {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}
_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_LINE = re.compile(r"^MATRIX ([a-z0-9-]+) (open|closed) (\d+)$")


def _name(value, what):
    if not isinstance(value, str) or len(value) > 63 or not _NAME.fullmatch(value):
        raise ValueError("Invalid " + what + ": " + repr(value))
    return value


def _port(value):
    if type(value) is not int or not 0 < value < 65536:
        raise ValueError("Invalid port: " + repr(value))
    return value


# ----- the reviewed graph -----

def load_graph(path=GRAPH):
    """The reviewed graph file: nodes, Service and Pod ports, and caller -> callee edges."""
    graph = json.loads(Path(path).read_text(encoding="utf-8"))
    if graph.get("edge_direction") != "caller_to_callee":
        raise ValueError("The reviewed graph must list caller -> callee edges")
    return graph


def graph_from_manifest(manifest):
    """The graph the harness derives from a connected manifest, in the reviewed file's shape."""
    derived = configured_graph(manifest)
    return {"nodes": derived["nodes"], "ports": service_ports(manifest), "edges": derived["edges"]}


def same_graph(reviewed, derived):
    return all(reviewed.get(key) == derived.get(key) for key in ("nodes", "ports", "edges"))


def edges(graph):
    """Sorted (caller, callee, Pod port) triples; an edge naming an unknown service raises."""
    nodes = set(graph.get("nodes") or ())
    ports = graph.get("ports") or {}
    if not nodes or set(ports) != nodes:
        raise ValueError("Every graph node needs exactly one reviewed port")
    result = set()
    for edge in graph.get("edges") or ():
        caller, callee = edge.get("from"), edge.get("to")
        for name in (caller, callee):
            if name not in nodes:
                raise ValueError("Graph edge names an unknown service: " + repr(name))
        if caller == callee:
            raise ValueError("A service cannot be its own callee")
        result.add((caller, callee, _port(ports[callee]["target"])))
    return sorted(result)


def probe_targets():
    """The catalog's recovery targets in the connected scope: the Services Sentinel probes."""
    return targets.enabled_targets(CONNECTED_SCOPE)


def connected_probe_targets():
    """probe_targets() plus the pair-only targets a scenario pair repairs on the connected lab (redis-cart, P4.05).

    A pair run probes redis-cart with a Redis PING from a Pod labelled like the
    grpcurl probes, so the connected namespace's graph policies must admit it.
    The security lab never runs a pair and keeps probe_targets().
    """
    paired = {item["target"] for pair_id in scenarios.pair_ids() for item in scenarios.pair(pair_id)["items"]
              if CONNECTED_SCOPE in targets.profile(item["target"]).scopes}
    return tuple(sorted(set(probe_targets()) | paired))


# ----- generation -----

def _selector(app):
    return {"matchLabels": {APP: app}}


def _policy(name, namespace, selector, kind, rules):
    spec = {"podSelector": selector, "policyTypes": [kind]}
    spec["ingress" if kind == "Ingress" else "egress"] = rules
    return {"apiVersion": API_VERSION, "kind": "NetworkPolicy",
            "metadata": {"name": _name(name, "policy name"), "namespace": namespace}, "spec": spec}


def _tcp(port):
    return [{"protocol": "TCP", "port": _port(port)}]


def policies(edge_list, namespace, *, workloads, ports, probes=(), entry=ENTRY, ingress_owned=()):
    """Default-deny plus one allow policy per callee and per caller, sorted by name.

    edge_list holds (caller, callee, Pod port); every workload gets egress
    isolation. A caller may reach one callee on several ports (the lab's
    gateway serves one port per protected route), but never twice on the same
    port. ports maps a workload to its Pod port, for the probe and entry
    rules. Callees in ingress_owned get no ingress policy here: other reviewed
    policies (the lab's protect-* gateway chain) own their ingress.
    """
    _name(namespace, "namespace")
    workloads = sorted({_name(name, "workload") for name in workloads})
    owned = set(ingress_owned)
    callers, callees = defaultdict(set), defaultdict(set)
    for caller, callee, port in edge_list:
        if caller not in workloads or callee not in workloads:
            raise ValueError("Edge names an unknown workload: " + repr((caller, callee)))
        if (callee, port) in callees[caller]:
            raise ValueError("Duplicate edge: " + caller + " -> " + callee)
        callers[callee].add((caller, _port(port)))
        callees[caller].add((callee, port))
    probes = sorted(set(probes) - owned)
    for name in [*probes, entry]:
        if name not in workloads or name not in ports:
            raise ValueError("No reviewed port for " + repr(name))
    result = [
        _policy("default-deny-ingress", namespace, {}, "Ingress", []),
        _policy("default-deny-egress", namespace,
                {"matchExpressions": [{"key": APP, "operator": "In", "values": workloads}]}, "Egress", [DNS_RULE]),
    ]
    for callee in sorted(set(callers) | set(probes) | {entry}):
        if callee in owned:
            continue
        rules = []
        by_port = defaultdict(list)
        for caller, port in callers.get(callee, ()):
            by_port[port].append(caller)
        for port in sorted(by_port):
            rules.append({"from": [{"podSelector": _selector(caller)} for caller in sorted(by_port[port])],
                          "ports": _tcp(port)})
        if callee in probes:
            rules.append({"from": [{"podSelector": {"matchLabels": {APP: PROBE_APP, PROBE_TARGET: callee}}}],
                          "ports": _tcp(ports[callee])})
        if callee == entry:
            rules.append({"from": [{"podSelector": _selector(TRAFFIC_APP)}], "ports": _tcp(ports[callee])})
        result.append(_policy("ingress-" + callee, namespace, _selector(callee), "Ingress", rules))
    for caller in sorted(callees):
        rules = [{"to": [{"podSelector": _selector(callee)}], "ports": _tcp(port)}
                 for callee, port in sorted(callees[caller])]
        result.append(_policy("egress-" + caller, namespace, _selector(caller), "Egress", rules))
    names = [item["metadata"]["name"] for item in result]
    if len(names) != len(set(names)):
        raise ValueError("Generated policy names collide")
    return sorted(result, key=lambda item: item["metadata"]["name"])


def graph_policies(graph, namespace, *, probes=None):
    """The plain graph-derived set for one namespace, with Sentinel probe allowances (pair targets included)."""
    ports = {name: value["target"] for name, value in graph["ports"].items()}
    return policies(edges(graph), namespace, workloads=graph["nodes"], ports=ports,
                    probes=connected_probe_targets() if probes is None else probes)


def security_lab_policies(graph, *, probes=None):
    """The security lab's set: callers reach each protected service only through the mTLS gateway.

    For every protected route (security_lab.ROUTES: shipping and, from P5.04,
    cart) the lab rewires frontend and checkoutservice to their loopback
    sidecars, which dial the gateway on the route's port (8443 shipping, 8445
    cart); the gateway dials the service's own TLS sidecar (8444 shipping,
    8446 cart). Ingress to the protected services and the gateway stays with
    the overlay's protect-* policies, so a graph policy can never reopen a
    plaintext bypass. Sentinel probes of shippingservice and cartservice are
    therefore refused in the lab; they need the authenticated Verifier probe
    of P5.05.
    """
    protected = {spec["service"]: (route, spec) for route, spec in ROUTES.items()}
    rewritten = []
    for caller, callee, port in edges(graph):
        if callee in protected:
            route, spec = protected[callee]
            if caller not in CALLERS:
                raise ValueError("Unreviewed " + route + " caller: " + caller)
            rewritten.append((caller, GATEWAY[0], spec["gateway_port"]))
        else:
            rewritten.append((caller, callee, port))
    rewritten.extend((GATEWAY[0], service, spec["upstream_port"]) for service, (_, spec) in sorted(protected.items()))
    ports = {name: value["target"] for name, value in graph["ports"].items()}
    return policies(rewritten, NAMESPACE, workloads=[*graph["nodes"], GATEWAY[0]], ports=ports,
                    probes=probe_targets() if probes is None else probes,
                    ingress_owned=(*sorted(protected), GATEWAY[0]))


def render(items):
    """Byte-stable JSON for a policy set, as a kubectl-appliable List."""
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2, sort_keys=True) + "\n"


def reviewed_security_policies(derived_graph, *, graph_path=None, policies_path=None):
    """The lab policies, only when the live graph is the reviewed one and regeneration matches the file."""
    if not same_graph(load_graph(graph_path or GRAPH), derived_graph):
        raise ValueError("The Boutique dependency graph differs from the reviewed graph file; review it first")
    return committed_security_policies(graph_path=graph_path, policies_path=policies_path)


def committed_security_policies(*, graph_path=None, policies_path=None):
    """The committed lab policies, only when regenerating them from the reviewed graph gives the same file."""
    items = security_lab_policies(load_graph(graph_path or GRAPH))
    if render(items) != Path(policies_path or SECURITY_POLICIES).read_text(encoding="utf-8"):
        raise ValueError("Generated NetworkPolicies differ from the reviewed file; run network_policy.py --write and review")
    return items


def _spec(item):
    # The API server omits empty rule lists, so a stored default-deny has no
    # "ingress" key while the generated one has "ingress": [].
    spec = dict(item.get("spec") or {})
    for key in ("ingress", "egress"):
        spec[key] = spec.get(key) or []
    return spec


def policy_differences(expected, found, where):
    """Why the NetworkPolicies found differ from the expected set: missing, extra or changed, by name."""
    want = {item["metadata"]["name"]: _spec(item) for item in expected}
    have, reasons = {}, []
    for item in found:
        name = item.get("metadata", {}).get("name")
        if name in have:
            reasons.append(where + " lists the NetworkPolicy " + repr(name) + " twice")
        have[name] = _spec(item)
    for label, names in (("lacks reviewed", set(want) - set(have)), ("has unreviewed", set(have) - set(want)),
                         ("has changed", {name for name in set(want) & set(have) if want[name] != have[name]})):
        if names:
            reasons.append(where + " " + label + " NetworkPolicies: " + ", ".join(sorted(map(str, names))))
    return reasons


# ----- a NetworkPolicy evaluator for the subset generated here -----

def _matches(selector, labels):
    if not isinstance(selector, dict):
        raise ValueError("Selector must be an object")
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expression in selector.get("matchExpressions") or ():
        key, operator, values = expression["key"], expression["operator"], expression.get("values") or ()
        if operator == "In":
            ok = key in labels and labels[key] in values
        elif operator == "NotIn":
            ok = labels.get(key) not in values
        elif operator == "Exists":
            ok = key in labels
        elif operator == "DoesNotExist":
            ok = key not in labels
        else:
            raise ValueError("Unknown selector operator: " + repr(operator))
        if not ok:
            return False
    return True


def _peer(peer, pod, policy_namespace):
    if "ipBlock" in peer:
        raise ValueError("ipBlock peers are not evaluated")
    namespace_selector, pod_selector = peer.get("namespaceSelector"), peer.get("podSelector")
    if namespace_selector is None:
        in_namespace = pod["namespace"] == policy_namespace
    else:
        in_namespace = _matches(namespace_selector, {"kubernetes.io/metadata.name": pod["namespace"]})
    return in_namespace and (pod_selector is None or _matches(pod_selector, pod["labels"]))


def _port_ok(rule, port, protocol):
    ports = rule.get("ports")
    if not ports:
        return True
    for entry in ports:
        if not isinstance(entry.get("port", 0), int):
            raise ValueError("Named ports are not evaluated")
        if entry.get("protocol", "TCP") == protocol and entry.get("port") in (None, port):
            return True
    return False


def _direction(items, pod, other, port, protocol, kind):
    key, peers = ("ingress", "from") if kind == "Ingress" else ("egress", "to")
    selecting = [item for item in items if item["metadata"]["namespace"] == pod["namespace"]
                 and kind in item["spec"]["policyTypes"] and _matches(item["spec"]["podSelector"], pod["labels"])]
    if not selecting:
        return True  # not isolated in this direction
    for item in selecting:
        for rule in item["spec"].get(key) or ():
            listed = rule.get(peers)
            # Kubernetes treats a missing or empty peer list as "every peer".
            if (not listed or any(_peer(peer, other, item["metadata"]["namespace"]) for peer in listed)) \
                    and _port_ok(rule, port, protocol):
                return True
    return False


def allowed(items, source, destination, port, protocol="TCP"):
    """Whether these policies let source open a connection to destination's Pod port.

    source and destination are {"namespace", "labels"}. Both the source's
    egress and the destination's ingress must allow it.
    """
    return (_direction(items, source, destination, port, protocol, "Egress")
            and _direction(items, destination, source, port, protocol, "Ingress"))


# ----- the enforcement matrix -----

def matrix_sources(workloads, probes, *, extra=()):
    """Tester identities: an intruder with no reviewed role, storefront traffic, each workload, each probe."""
    sources = [{"id": "intruder", "labels": {APP: INTRUDER_APP}},
               {"id": "traffic", "labels": {APP: TRAFFIC_APP}}]
    sources += [{"id": "app-" + name, "labels": {APP: name}} for name in sorted(set(workloads) | set(extra))]
    sources += [{"id": "probe-" + name, "labels": {APP: PROBE_APP, PROBE_TARGET: name}} for name in sorted(set(probes))]
    for source in sources:
        _name(source["id"], "source id")
    return sources


def matrix_destinations(manifest):
    """Every Service with an app selector and one numeric port, as a matrix destination."""
    result = []
    for item in manifest["items"]:
        if item["kind"] != "Service":
            continue
        selector = item.get("spec", {}).get("selector") or {}
        ports = item["spec"].get("ports") or []
        if set(selector) != {APP} or len(ports) != 1:
            raise ValueError("Matrix destinations need an app selector and one port: " + item["metadata"]["name"])
        result.append({"id": _name(item["metadata"]["name"], "Service"), "labels": dict(selector),
                       "service_port": _port(ports[0]["port"]),
                       "target_port": _port(ports[0].get("targetPort", ports[0]["port"]))})
    return sorted(result, key=lambda entry: entry["id"])


def matrix_plan(items, namespace, sources, destinations):
    """One row per (source, destination) with the verdict the policies give."""
    rows = []
    for source in sources:
        for destination in destinations:
            permitted = allowed(items, {"namespace": namespace, "labels": source["labels"]},
                                {"namespace": namespace, "labels": destination["labels"]}, destination["target_port"])
            rows.append({"source": source["id"], "destination": destination["id"],
                         "port": destination["service_port"], "target_port": destination["target_port"],
                         "expected": "allow" if permitted else "deny", "observed": "untested", "seconds": None})
    return rows


def _timed_out(row):
    # A dropped SYN waits out the connect timeout; an instant refusal does not.
    return row["observed"] == "closed" and (row.get("seconds") or 0) >= CONNECT_TIMEOUT - 1


def evaluate(rows, *, journey_passed, policy_drift=(), required_denials=SAMPLED_DENIALS):
    """PASS, FAIL or UNKNOWN for a completed matrix.

    FAIL: an allowed edge was refused, or the storefront journey failed (the
    policies are too tight). UNKNOWN: a forbidden edge connected (enforcement
    was not shown), a forbidden edge was refused at once instead of timing out
    (a missing listener, not a policy drop), a cell was not tested, the
    journey result is missing, the matrix has no allowed or no forbidden cell,
    a sampled forbidden edge is missing or judged allowed, or the policies
    applied differ from the reviewed set (policy_drift). PASS otherwise.
    """
    over_tight = [row for row in rows if row["expected"] == "allow" and row["observed"] == "closed"]
    leaked = [row for row in rows if row["expected"] == "deny" and row["observed"] == "open"]
    refused = [row for row in rows if row["expected"] == "deny" and row["observed"] == "closed" and not _timed_out(row)]
    untested = [row for row in rows if row["observed"] not in {"open", "closed"}]
    cells = {(row["source"], row["destination"]): row["expected"] for row in rows}
    unjudged = [source + "->" + destination for source, destination in required_denials
                if cells.get((source, destination)) != "deny"]
    reasons = []
    if over_tight:
        reasons.append("allowed edges refused: " + ", ".join(row["source"] + "->" + row["destination"] for row in over_tight))
    if journey_passed is False:
        reasons.append("storefront journey failed")
    if leaked:
        reasons.append("forbidden edges connected: " + ", ".join(row["source"] + "->" + row["destination"] for row in leaked))
    if refused:
        reasons.append("forbidden edges refused at once, not dropped: "
                       + ", ".join(row["source"] + "->" + row["destination"] for row in refused))
    if untested:
        reasons.append(f"{len(untested)} cells not tested")
    if journey_passed is not True and journey_passed is not False:
        reasons.append("storefront journey not recorded")
    if not any(row["expected"] == "allow" for row in rows) or not any(row["expected"] == "deny" for row in rows):
        reasons.append("the matrix needs allowed and forbidden cells")
    if unjudged:
        reasons.append("sampled forbidden edges missing or judged allowed: " + ", ".join(unjudged))
    reasons.extend(policy_drift)
    if over_tight or journey_passed is False:
        status = "FAIL"
    elif reasons:
        status = "UNKNOWN"
    else:
        status = "PASS"
    allow = [row for row in rows if row["expected"] == "allow"]
    deny = [row for row in rows if row["expected"] == "deny"]
    return {"status": status, "reasons": reasons, "journey_passed": journey_passed,
            "allowed": {"cells": len(allow), "open": sum(row["observed"] == "open" for row in allow)},
            "forbidden": {"cells": len(deny), "closed": sum(row["observed"] == "closed" for row in deny),
                          "timed_out": sum(_timed_out(row) for row in deny)},
            "untested": len(untested)}


def tester_script(checks):
    """A busybox sh script: one TCP connect per (id, address, port), one MATRIX line each."""
    lines = ["command -v nc >/dev/null 2>&1 || exit 3",
             'check() { s=$(date +%s); if nc -z -w ' + str(CONNECT_TIMEOUT) + ' "$2" "$3" >/dev/null 2>&1; '
             'then r=open; else r=closed; fi; echo "MATRIX $1 $r $(( $(date +%s) - s ))"; }']
    for identifier, address, port in checks:
        lines.append(f"check {_name(identifier, 'check id')} {ipaddress.ip_address(address)} {_port(port)}")
    lines.append("exit 0")
    return "\n".join(lines) + "\n"


def tester_pod(name, namespace, run_id, source, checks, *, image=MATRIX_IMAGE):
    """An unprivileged, short-lived Pod carrying the source's labels.

    The readiness probe always fails, so a tester that carries a workload's
    app label never becomes an endpoint of that workload's Service.
    """
    deadline = 30 + (CONNECT_TIMEOUT + 1) * len(checks)
    labels = {**source["labels"], MATRIX_LABEL: _name(run_id, "run id")}
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": _name(name, "Pod name"), "namespace": namespace, "labels": labels},
            "spec": {"automountServiceAccountToken": False, "restartPolicy": "Never",
                     "activeDeadlineSeconds": deadline, "terminationGracePeriodSeconds": 1,
                     "securityContext": {"runAsUser": 65534, "runAsGroup": 65534, "runAsNonRoot": True,
                                         "seccompProfile": {"type": "RuntimeDefault"}},
                     "containers": [{"name": "tester", "image": image, "imagePullPolicy": "Never",
                                     "command": ["sh", "-c", tester_script(checks)],
                                     "readinessProbe": {"exec": {"command": ["false"]}, "periodSeconds": 30,
                                                        "failureThreshold": 1},
                                     "resources": {"requests": {"cpu": "5m", "memory": "8Mi"},
                                                   "limits": {"cpu": "50m", "memory": "32Mi"}},
                                     "securityContext": {"allowPrivilegeEscalation": False,
                                                         "readOnlyRootFilesystem": True,
                                                         "capabilities": {"drop": ["ALL"]}}}]}}


def parse_output(text, identifiers):
    """{id: (open|closed, seconds)} for ids reported exactly once; others stay untested."""
    seen = defaultdict(list)
    for line in text.splitlines():
        match = _LINE.match(line.strip())
        if match and match.group(1) in identifiers:
            seen[match.group(1)].append((match.group(2), int(match.group(3))))
    return {key: values[0] for key, values in seen.items() if len(values) == 1}


def _json(result):
    if result.returncode:
        raise RuntimeError("kubectl failed: " + (result.stderr or "")[-500:])
    return json.loads(result.stdout)


def run_matrix(kubectl, directory, *, namespace, items, sources, destinations, journey_passed, policy_drift=(),
               image=MATRIX_IMAGE, run_id=None, clock=time.monotonic, sleep=time.sleep):
    """Run the matrix live and write network-matrix.json.

    items is the reviewed policy set the cells are judged against. The
    cluster's own NetworkPolicies in the namespace must be exactly that set;
    any difference, or a failed read, joins policy_drift and keeps the verdict
    from PASS. kubectl(*args) runs kubectl with the lab context and returns a
    CompletedProcess without raising. Each tester first connects to kube-dns
    on TCP 53, which no policy here blocks; a tester whose control connection
    fails, that could not be created or that did not finish proves nothing
    about its refusals, so its cells stay untested.
    """
    run_id = run_id or uuid4().hex[:10]
    rows = matrix_plan(items, namespace, sources, destinations)
    drift = list(policy_drift)
    live = kubectl("--namespace=" + namespace, "get", "networkpolicies", "-o", "json")
    try:
        drift += policy_differences(items, _json(live)["items"], "the cluster")
    except (RuntimeError, ValueError, KeyError, TypeError, AttributeError):
        drift.append("the cluster's NetworkPolicies could not be read")
    services = _json(kubectl("--namespace=" + namespace, "get", "services", "-o", "json"))["items"]
    addresses = {item["metadata"]["name"]: item["spec"].get("clusterIP") for item in services}
    dns = _json(kubectl("--namespace=kube-system", "get", "service", "kube-dns", "-o", "json"))
    control = ("control", dns["spec"]["clusterIP"], 53)
    folder = Path(directory) / "network-matrix-pods"
    folder.mkdir(exist_ok=True)
    pods, deadlines = {}, []
    for source in sources:
        checks = [control] + [(destination["id"], addresses[destination["id"]], destination["service_port"])
                              for destination in destinations if _routable(addresses.get(destination["id"]))]
        name = "netmatrix-" + source["id"] + "-" + run_id[:6]
        pod = tester_pod(name, namespace, run_id, source, checks, image=image)
        (folder / (name + ".json")).write_text(json.dumps(pod, indent=2) + "\n", encoding="utf-8")
        pods[name] = source["id"]
        deadlines.append(pod["spec"]["activeDeadlineSeconds"])
    outputs, phases, created = {}, {}, set()
    try:
        for name in sorted(pods):
            if kubectl("--namespace=" + namespace, "create", "-f", str(folder / (name + ".json"))).returncode == 0:
                created.add(name)
        deadline = clock() + 60 + max(deadlines, default=0)
        while created - set(phases) and clock() < deadline:
            listing = kubectl("--namespace=" + namespace, "get", "pods", "-l", MATRIX_LABEL + "=" + run_id, "-o", "json")
            for pod in (json.loads(listing.stdout)["items"] if listing.returncode == 0 else ()):
                name, phase = pod["metadata"]["name"], pod.get("status", {}).get("phase")
                if name in created and name not in phases and phase in {"Succeeded", "Failed"}:
                    phases[name] = phase
                    logs = kubectl("--namespace=" + namespace, "logs", name)
                    outputs[name] = logs.stdout if logs.returncode == 0 else ""
            if created - set(phases):
                sleep(1)
    finally:
        kubectl("--namespace=" + namespace, "delete", "pods", "-l", MATRIX_LABEL + "=" + run_id,
                "--ignore-not-found", "--wait=false")
    identifiers = {"control"} | {destination["id"] for destination in destinations}
    by_source = {}
    for name, source in pods.items():
        observed = parse_output(outputs.get(name, ""), identifiers)
        trusted = phases.get(name) == "Succeeded" and observed.get("control", ("closed", 0))[0] == "open"
        by_source[source] = {"pod": name, "created": name in created, "phase": phases.get(name),
                             "control": observed.get("control", ("untested", None))[0],
                             "trusted": trusted, "observed": observed if trusted else {}}
    for row in rows:
        seen = by_source[row["source"]]["observed"].get(row["destination"])
        if seen:
            row["observed"], row["seconds"] = seen
    testers = [{"source": source, **{key: value for key, value in by_source[source].items() if key != "observed"}}
               for source in sorted(by_source)]
    result = {"run_id": run_id, "namespace": namespace, "image": image, "connect_timeout_seconds": CONNECT_TIMEOUT,
              "policy_drift": drift, "testers": testers, "rows": rows,
              "verdict": evaluate(rows, journey_passed=journey_passed, policy_drift=drift)}
    (Path(directory) / "network-matrix.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def _routable(address):
    try:
        ipaddress.ip_address(address)
    except ValueError:
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Check or regenerate the reviewed security-lab NetworkPolicies.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="Fail when the committed file is stale")
    action.add_argument("--write", action="store_true", help="Regenerate the committed file from the reviewed graph")
    args = parser.parse_args()
    text = render(security_lab_policies(load_graph()))
    if args.write:
        SECURITY_POLICIES.write_text(text, encoding="utf-8", newline="\n")
        print("Wrote " + SECURITY_POLICIES.relative_to(ROOT).as_posix())
        return 0
    if SECURITY_POLICIES.read_text(encoding="utf-8") != text:
        print("Stale: " + SECURITY_POLICIES.relative_to(ROOT).as_posix() + "; run with --write and review", file=sys.stderr)
        return 1
    print("Current: " + SECURITY_POLICIES.relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__":
    from scripts.security import network_policy as _canonical
    raise SystemExit(_canonical.main())
