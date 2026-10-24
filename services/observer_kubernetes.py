"""Read-only Kubernetes transports and ownership-correlated Observer evidence."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import ssl
import subprocess
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, HTTPSHandler, HTTPRedirectHandler, build_opener

from services.k8s_observer_adapter import deployment_to_observation
from services.observer import validate_observation


class CollectionError(RuntimeError):
    """A complete, identity-consistent Kubernetes sample could not be read."""


def api_path(path, parameters=None):
    if not path.startswith(("/api/", "/apis/")) or ".." in path:
        raise ValueError("Expected a Kubernetes resource API path")
    return path + ("?" + urlencode(parameters) if parameters else "")


class KubectlReader:
    def __init__(self, context, *, kubectl="kubectl", runner=subprocess.run):
        if not context:
            raise ValueError("An explicit Kubernetes context is required")
        self.context, self.kubectl, self.runner = context, kubectl, runner

    def get(self, path, parameters=None, *, text=False):
        command = [self.kubectl, "--context", self.context, "--request-timeout=5s",
                   "get", "--raw", api_path(path, parameters)]
        result = self.runner(command, capture_output=True, text=True, timeout=8)
        if result.returncode:
            raise CollectionError(result.stderr.strip() or "Kubernetes read failed")
        if len(result.stdout.encode("utf-8")) > 2_000_000:
            raise CollectionError("Kubernetes response exceeded the evidence limit")
        return result.stdout if text else json.loads(result.stdout)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise CollectionError("Kubernetes API redirects are not accepted")


class InClusterReader:
    """HTTPS with verified cluster CA and a projected token reloaded per request."""
    def __init__(self, *, environ=None, credentials="/var/run/secrets/kubernetes.io/serviceaccount",
                 opener=None, ssl_context=None):
        environ = os.environ if environ is None else environ
        host = environ.get("KUBERNETES_SERVICE_HOST")
        port = environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        if not host or not port.isdigit():
            raise ValueError("In-cluster Kubernetes host/HTTPS port are required")
        if ":" in host:
            host = "[" + host.strip("[]") + "]"
        self.base = f"https://{host}:{port}"
        self.credentials = Path(credentials)
        self.context = ssl_context or ssl.create_default_context(cafile=str(self.credentials / "ca.crt"))
        self.opener = opener or build_opener(HTTPSHandler(context=self.context), NoRedirect()).open

    def get(self, path, parameters=None, *, text=False):
        token = (self.credentials / "token").read_text(encoding="utf-8").strip()
        if not token:
            raise CollectionError("Projected service-account token is empty")
        request = Request(self.base + api_path(path, parameters),
                          headers={"Authorization": "Bearer " + token})
        with self.opener(request, timeout=5) as response:
            body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise CollectionError("Kubernetes response exceeded the evidence limit")
        decoded = body.decode("utf-8")
        return decoded if text else json.loads(decoded)


def owned_by(resource, kind, uid):
    return any(owner.get("kind") == kind and owner.get("uid") == uid
               and owner.get("controller") is True
               for owner in resource.get("metadata", {}).get("ownerReferences", []))


def selector_text(selector):
    terms = [f"{key}={value}" for key, value in sorted(selector.get("matchLabels", {}).items())]
    for expression in selector.get("matchExpressions", []):
        key, operator = expression["key"], expression["operator"]
        values = expression.get("values", [])
        if operator in ("In", "NotIn") and values:
            terms.append(f"{key} {'in' if operator == 'In' else 'notin'} ({','.join(values)})")
        elif operator == "Exists":
            terms.append(key)
        elif operator == "DoesNotExist":
            terms.append("!" + key)
        else:
            raise CollectionError("Unsupported or invalid Deployment selector")
    if not terms:
        raise CollectionError("Deployment has no usable selector")
    return ",".join(terms)


class KubernetesEvidenceCollector:
    def __init__(self, reader, *, namespace, name, cluster_id, collect_logs=False,
                 max_pods=20, max_log_reads=4, clock=None):
        if not all(isinstance(value, str) and value.strip() for value in (namespace, name, cluster_id)):
            raise ValueError("Namespace, Deployment and cluster ID are required")
        self.reader, self.namespace, self.name, self.cluster_id = reader, namespace, name, cluster_id
        self.collect_logs = collect_logs
        self.max_pods, self.max_log_reads = max_pods, max_log_reads
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.deadline = None

    def _get(self, path, parameters=None, *, text=False):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise CollectionError("Evidence collection exceeded its 20-second request budget")
        return self.reader.get(path, parameters, text=text)

    @property
    def target(self):
        return {"cluster_id": self.cluster_id, "namespace": self.namespace,
                "kind": "Deployment", "name": self.name}

    def _list(self, path, parameters=None):
        params = {**(parameters or {}), "limit": 200}
        items = []
        for _ in range(3):
            page = self._get(path, params)
            items.extend(page["items"])
            continuation = page.get("metadata", {}).get("continue")
            if not continuation:
                return items
            params["continue"] = continuation
        raise CollectionError("Evidence list exceeded the bounded pagination budget")

    def collect(self):
        self.deadline = time.monotonic() + 20
        ns = quote(self.namespace, safe="")
        apps = f"/apis/apps/v1/namespaces/{ns}"
        core = f"/api/v1/namespaces/{ns}"
        deployment_path = f"{apps}/deployments/{quote(self.name, safe='')}"
        deployment = self._get(deployment_path)
        captured_at = self.clock()
        observation = deployment_to_observation(deployment, cluster_id=self.cluster_id, observed_at=captured_at)
        uid = deployment["metadata"]["uid"]
        selector = {"labelSelector": selector_text(deployment["spec"]["selector"])}
        replica_sets = [rs for rs in self._list(apps + "/replicasets", selector)
                        if owned_by(rs, "Deployment", uid)]
        revision = deployment["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision")
        current_sets = [rs for rs in replica_sets
                        if revision and rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision") == revision]
        current_uids = {rs["metadata"]["uid"] for rs in current_sets}
        pods = [pod for pod in self._list(core + "/pods", selector)
                if any(owned_by(pod, "ReplicaSet", rs_uid) for rs_uid in current_uids)
                and not pod["metadata"].get("deletionTimestamp")]
        if len(pods) > self.max_pods:
            raise CollectionError("Owned Pod count exceeds configured evidence budget")
        # API objects may contain environment variables; retain only diagnostic fields.
        observation["pods"] = [{
            "metadata": {key: pod["metadata"][key] for key in ("name", "uid")},
            "status": {
                "phase": pod.get("status", {}).get("phase", "Unknown"),
                "containerStatuses": deepcopy(pod.get("status", {}).get("containerStatuses", [])),
                "initContainerStatuses": deepcopy(pod.get("status", {}).get("initContainerStatuses", [])),
                "conditions": deepcopy(pod.get("status", {}).get("conditions", [])),
            },
        } for pod in pods]
        evidence_uids = {uid, *current_uids, *(pod["metadata"]["uid"] for pod in pods)}
        recent_events = []
        for event in self._list(core + "/events", {"fieldSelector": "type=Warning"}):
            if event.get("involvedObject", {}).get("uid") not in evidence_uids:
                continue
            at = event.get("lastTimestamp") or event.get("eventTime") or event.get("metadata", {}).get("creationTimestamp")
            if not at or event.get("type") != "Warning":
                continue
            parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
            if not 0 <= (captured_at - parsed).total_seconds() <= 300:
                continue
            recent_events.append({"reason": event.get("reason") or "KubernetesWarning",
                                  "type": "Warning", "message": event.get("message") or "Kubernetes warning",
                                  "lastTimestamp": parsed.astimezone(timezone.utc).isoformat()})
        recent_events.sort(key=lambda event: event["lastTimestamp"])
        observation["events"].extend(recent_events[-50:])
        warnings = []
        log_reads = 0
        if self.collect_logs:
            for pod in pods:
                statuses = pod.get("status", {})
                for container in statuses.get("initContainerStatuses", []) + statuses.get("containerStatuses", []):
                    if container.get("ready") or log_reads >= self.max_log_reads:
                        continue
                    log_reads += 1
                    previous = container.get("restartCount", 0) > 0
                    params = {"container": container["name"], "tailLines": 20, "limitBytes": 4096,
                              "sinceSeconds": 300, "timestamps": "true", "previous": str(previous).lower()}
                    try:
                        raw = self._get(core + f"/pods/{quote(pod['metadata']['name'], safe='')}/log", params, text=True)
                        if raw.strip():
                            observation["logs"].append({"pod": pod["metadata"]["name"], "container": container["name"],
                                                        "text": raw[-4096:], "at": observation["captured_at"]})
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                        warnings.append(f"Log unavailable for {pod['metadata']['name']}/{container['name']}: {type(error).__name__}")
        # Multiple API reads are not an atomic snapshot. Reject a recreated target or new rollout.
        final = self._get(deployment_path)
        if (final["metadata"]["uid"], final["metadata"]["generation"]) != (uid, deployment["metadata"]["generation"]):
            raise CollectionError("Deployment identity/generation changed during evidence collection")
        if final["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision") != revision:
            raise CollectionError("Deployment revision changed during evidence collection")
        observation["collection_warnings"] = warnings
        validate_observation(observation)
        return observation
