"""Read-only live health evidence from configured Kubernetes and HTTP sources."""

from copy import deepcopy
from datetime import datetime, timezone
import math
import subprocess
import time
from urllib.parse import quote

from services.verifier import _reachable, inspect_dependencies, verify
from services.verifier_http import EvidenceHTTP, validate_url
from services.guard_common import json_sha256
from services.verifier_traces import stamp, trace_edges, utc_time
from shared.contracts import validate_message


def _services(config):
    if not isinstance(config, dict) or not isinstance(config.get("cluster_id"), str) or not config["cluster_id"]:
        raise ValueError("Configure an independent cluster_id")
    services = config.get("services")
    if not isinstance(services, dict) or not 1 <= len(services) <= 32:
        raise ValueError("Configure between one and 32 services")
    for name, service in services.items():
        if not isinstance(service, dict) or not isinstance(service.get("target"), dict):
            raise ValueError("Each configured service requires a full target")
        target = service["target"]
        fields = {"cluster_id", "namespace", "kind", "name", "uid"}
        if (set(target) != fields or any(not isinstance(value, str) or not value for value in target.values())
                or target["cluster_id"] != config["cluster_id"] or target["kind"] != "Deployment"
                or name != target["name"]):
            raise ValueError("Service keys must match unique configured Deployment identities")
        validate_url(service.get("health_url"))
    return deepcopy(services)


class LiveEvidenceCollector:
    def __init__(self, config, kubernetes, *, http=None, clock=None, monotonic=None, trace_reader=None):
        self.services = _services(config)
        self.config = deepcopy(config)
        self.kubernetes = kubernetes
        self.http = http or EvidenceHTTP()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.trace_reader = trace_reader
        for key in ("dependency_inventory_complete", "require_traces"):
            if key in config and type(config[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
        for key, default, limit in (("max_collection_seconds", 20, 60), ("max_trace_age_seconds", 300, 86400),
                                    ("max_health_age_seconds", 10, 600)):
            value = config.get(key, default)
            if type(value) is not int or not 0 < value <= limit:
                raise ValueError(f"Invalid {key}")
        edges = config.get("declared_edges", [])
        if not isinstance(edges, list) or any(not isinstance(edge, dict)
                or not isinstance(edge.get("from"), str) or not isinstance(edge.get("to"), str)
                or edge.get("from") not in self.services or edge.get("to") not in self.services for edge in edges):
            raise ValueError("Declared edges must reference configured services")
        self.aliases = {}
        for name, service in self.services.items():
            alias = service.get("trace_service", name)
            if not isinstance(alias, str) or not alias or alias in self.aliases:
                raise ValueError("Trace service aliases must be nonempty and unique")
            self.aliases[alias] = name

    def _snapshot(self, deployment, target):
        if not isinstance(deployment, dict) or deployment.get("kind") != "Deployment":
            raise ValueError("Invalid Deployment response")
        metadata, spec, status = (deployment.get(key) for key in ("metadata", "spec", "status"))
        if not all(isinstance(value, dict) for value in (metadata, spec, status)):
            raise ValueError("Incomplete Deployment response")
        if any(metadata.get(key) != target[key] for key in ("name", "namespace", "uid")):
            raise ValueError("Deployment identity changed")
        generation, version = metadata.get("generation"), metadata.get("resourceVersion")
        desired = spec.get("replicas")
        if (type(generation) is not int or generation < 1 or not isinstance(version, str) or not version
                or type(desired) is not int or desired < 1):
            raise ValueError("Incomplete or scaled-to-zero Deployment state")
        values = [status.get(key, 0) for key in ("observedGeneration", "readyReplicas", "availableReplicas", "updatedReplicas")]
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("Invalid readiness counters")
        observed, ready, available, updated = values
        ready = min(ready, available, updated) if observed >= generation else 0
        return {"generation": generation, "resource_version": version,
                "ready_replicas": ready, "desired_replicas": desired, "available_replicas": available,
                "template_sha256": json_sha256(spec["template"]) if isinstance(spec.get("template"), dict) else None}

    def read_service(self, name):
        """Bind endpoint health to a stable configured Deployment identity."""
        service = self.services[name]
        target = service["target"]
        path = ("/apis/apps/v1/namespaces/" + quote(target["namespace"], safe="")
                + "/deployments/" + quote(target["name"], safe=""))
        result = {"target": deepcopy(target), "http_status": None, "ready_replicas": None,
                  "desired_replicas": None, "known_health": None, "errors": []}
        try:
            first = self._snapshot(self.kubernetes.get(path), target)
            health = self.http.health(service["health_url"])
            final = self._snapshot(self.kubernetes.get(path), target)
            if first["generation"] != final["generation"]:
                raise ValueError("Deployment changed during collection")
            if first["template_sha256"] != final["template_sha256"]:
                raise ValueError("Deployment template changed during collection")
            result.update(final, http_status=health["http_status"])
            result["configuration_stable"] = True
            if health["error"]:
                result["errors"].append(health["error"])
            ready = final["ready_replicas"] >= final["desired_replicas"]
            if not ready or (health["http_status"] is not None and health["http_status"] != 200):
                result["known_health"] = False
            elif health["http_status"] == 200:
                result["known_health"] = True
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            result["errors"].append("Deployment evidence unavailable or inconsistent")
        result["observed_at"] = stamp(utc_time(self.clock()))
        return result

    def _graph(self):
        now = utc_time(self.clock())
        edges = {(edge["from"], edge["to"]) for edge in self.config.get("declared_edges", [])}
        evidence = [{"from": caller, "to": callee, "source": "declared_configuration",
                     "checked_at": stamp(now)} for caller, callee in sorted(edges)]
        unknowns = []
        if not self.config.get("dependency_inventory_complete", False):
            unknowns.append("Declared dependency inventory is not marked complete")
        if self.trace_reader:
            try:
                namespaces = {alias: self.services[name]["target"]["namespace"] for alias, name in self.aliases.items()}
                traces = trace_edges(self.trace_reader(), self.aliases, now=utc_time(self.clock()),
                    max_age_seconds=self.config.get("max_trace_age_seconds", 300), namespaces=namespaces)
                edges.update((edge["from"], edge["to"]) for edge in traces["edges"])
                # Keep one proof per observed edge rather than every request span.
                proofs = {}
                for item in traces["evidence"]:
                    key = (item["from"], item["to"])
                    if key not in proofs or item["observed_at"] > proofs[key]["observed_at"]:
                        proofs[key] = item
                evidence.extend(proofs.values())
                unknowns.extend(traces["unknowns"])
            except (OSError, ValueError, RuntimeError):
                unknowns.append("Configured trace source is unavailable or invalid")
        elif self.config.get("require_traces", False):
            unknowns.append("Required trace source is not configured")
        return {"source": "live_verifier", "edge_direction": "caller_to_callee",
                "nodes": sorted(self.services), "edges": [{"from": left, "to": right} for left, right in sorted(edges)],
                "known_health": {}, "edge_evidence": evidence, "unknowns": sorted(set(unknowns)),
                "captured_at": stamp(now)}

    def collect(self, target):
        """Return one full-identity reading and its bounded dependency evidence."""
        if not isinstance(target, dict) or target.get("name") not in self.services:
            raise ValueError("Target is not in the configured inventory")
        if self.services[target["name"]]["target"] != target:
            raise ValueError("Target does not match the configured identity")
        started = self.monotonic()
        graph = self._graph()
        forward, reverse = {}, {}
        for edge in graph["edges"]:
            forward.setdefault(edge["from"], set()).add(edge["to"])
            reverse.setdefault(edge["to"], set()).add(edge["from"])
        name = target["name"]
        affected = _reachable(name, forward) | _reachable(name, reverse)
        sources = {}
        budget = self.config.get("max_collection_seconds", 20)
        for service in [name, *sorted(affected - {name})]:
            if self.monotonic() - started >= budget:
                graph["unknowns"].append("Collection time budget exceeded")
                graph["known_health"][service] = None
                continue
            sources[service] = self.read_service(service)
            graph["known_health"][service] = sources[service]["known_health"]
        if self.monotonic() - started >= budget:
            graph["unknowns"].append("Collection time budget exceeded")
        now = utc_time(self.clock())
        for service, source in sources.items():
            observed = datetime.fromisoformat(source["observed_at"].replace("Z", "+00:00"))
            if not 0 <= (now - observed).total_seconds() <= self.config.get("max_health_age_seconds", 10):
                graph["known_health"][service] = None
                graph["unknowns"].append(f"Stale or future health evidence: {service}")
        graph["unknowns"] = sorted(set(graph["unknowns"]))
        root = sources.get(name, {})
        dependencies = {service: graph["known_health"].get(service) for service in sorted(affected - {name})}
        sample = {"at": stamp(now), "service": name, "target": deepcopy(target),
                  "http_status": root.get("http_status"), "ready_replicas": root.get("ready_replicas"),
                  "desired_replicas": root.get("desired_replicas"),
                  "available_replicas": root.get("available_replicas"),
                  "template_sha256": root.get("template_sha256"),
                  "configuration_stable": root.get("configuration_stable", False),
                  "dependencies": None if graph["unknowns"] else dependencies,
                  "source": "live_verifier", "dependency_unknowns": graph["unknowns"]}
        sample["dependency_evidence"] = deepcopy(graph["edge_evidence"])
        sample["observations"] = {service: {key: deepcopy(source.get(key)) for key in
            ("target", "observed_at", "resource_version", "generation", "known_health")}
            for service, source in sources.items()}
        return {"sample": sample, "graph": graph, "sources": sources}

    def samples(self, execution):
        """Adapter callable for #20; no worker or persistence is imported here."""
        validate_message(execution)
        if execution["kind"] != "execution_result" or execution["status"] not in {"APPLIED", "NOOP"}:
            raise ValueError("Expected an applied/no-op execution")
        return [self.collect(execution["target"])["sample"]]

    def dependencies(self, proposal):
        validate_message(proposal)
        if proposal["kind"] != "proposal":
            raise ValueError("Expected a proposal")
        evidence = self.collect(proposal["target"])
        result = inspect_dependencies(proposal, evidence["graph"], {"target": proposal["target"]},
            now=datetime.fromisoformat(evidence["sample"]["at"].replace("Z", "+00:00")))
        result["unknowns"] = sorted(set(result["unknowns"] + evidence["graph"]["unknowns"]))
        if result["status"] == "CLEAR" and result["unknowns"]:
            result["status"] = "UNKNOWN"
        validate_message(result)
        return result


def collect_window(collector, execution, policy, *, interval_seconds=None, count=None, sleep=time.sleep):
    """Collect actual timed readings, then apply the existing Verifier rules."""
    verify(execution, [], policy)
    rules = policy["health"]
    if interval_seconds is None:
        interval_seconds = min(rules["max_sample_gap_seconds"] / 2,
                               rules["stable_window_seconds"] / max(1, rules["minimum_samples"] - 1))
    if (type(interval_seconds) not in (float, int) or not math.isfinite(interval_seconds)
            or not 0 < interval_seconds <= min(60, rules["max_sample_gap_seconds"])):
        raise ValueError("Collection interval must be positive and within the policy gap limit")
    if count is None:
        count = max(rules["minimum_samples"], math.ceil(rules["stable_window_seconds"] / interval_seconds) + 1)
    if type(count) is not int or not 1 <= count <= 1000:
        raise ValueError("Collect between one and 1000 samples per explicit window")
    readings = []
    for index in range(count):
        readings.extend(collector.samples(execution))
        if index + 1 < count:
            sleep(interval_seconds)
    return {"samples": readings, "outcome": verify(execution, readings, policy)}
