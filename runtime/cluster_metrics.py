"""Read-only, bounded Kubernetes dashboard projections; never Guard inputs.

CPU usage is average cores over the Metrics API measurement window. Memory
usage is working-set bytes, not requests, limits, or a reservation. References:
https://kubernetes.io/docs/tasks/debug/debug-cluster/resource-metrics-pipeline/
https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
https://kubernetes.io/docs/reference/node/node-status/

Only ``kubectl get`` is run with an explicit context. Metrics Server is optional:
missing APIs, denied permissions and incomplete measurements remain unknown.
We deliberately omit Pod specs, labels, annotations, logs and arbitrary messages.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import re
import subprocess

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
LIST_LIMITS = {"nodes": 100, "pods": 300, "events": 100,
               "node_metrics": 1000, "pod_metrics": 1000}
SOURCE_NAMES = tuple(LIST_LIMITS)
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
_QUANTITY = re.compile(r"([+]?(?:\d+(?:\.\d*)?|\.\d+))([eE][+-]?\d+|[numkKMGTPE]|[KMGTPE]i)?\Z")
_DECIMAL = {"": 0, "n": -9, "u": -6, "m": -3, "k": 3, "K": 3,
            "M": 6, "G": 9, "T": 12, "P": 15, "E": 18}
_CONDITIONS = {"Ready", "MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"}
_REASONS = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "OOMKilled",
            "ContainerCreating", "CreateContainerConfigError", "CreateContainerError",
            "RunContainerError", "Error", "Completed", "Evicted", "OutOfcpu", "OutOfmemory",
            "Unschedulable", "Pending", "Running", "Succeeded", "Failed", "Unknown",
            "FailedScheduling", "BackOff", "FailedMount", "FailedAttachVolume", "Failed",
            "Unhealthy", "Killing", "FailedCreate", "FailedDelete", "FailedSync",
            "FailedCreatePodSandBox", "FailedKillPod", "NodeNotReady", "NodeNotSchedulable",
            "NetworkNotReady", "FailedGetResourceMetric", "FailedComputeMetricsReplicas",
            "FailedGetScale", "FailedRescale", "ScalingReplicaSet", "SuccessfulCreate",
            "SandboxChanged", "FailedPostStartHook", "FailedPreStopHook", "FailedBinding",
            "FailedToRetrieveImagePullSecret", "FreeDiskSpaceFailed", "ImageGCFailed"}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _name(value):
    return value if isinstance(value, str) and _NAME.fullmatch(value) else None


def _count(value):
    return value if type(value) is int and 0 <= value <= 2 ** 53 - 1 else None


def _reason(value):
    return value if isinstance(value, str) and value in _REASONS else "Other" if value else None


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is not None:
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        pass
    return None


def _window(value):
    if not isinstance(value, str) or not value or len(value) > 48:
        return None
    parts = re.findall(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)", value)
    if "".join(number + unit for number, unit in parts) != value:
        return None
    multipliers = {"ns": Decimal("1e-9"), "us": Decimal("1e-6"), "µs": Decimal("1e-6"),
                   "ms": Decimal(".001"), "s": Decimal(1), "m": Decimal(60), "h": Decimal(3600)}
    result = sum((Decimal(number) * multipliers[unit] for number, unit in parts), Decimal(0))
    return float(result) if 0 < result <= 86400 else None


def parse_quantity(value, *, memory=False):
    """Normalize bounded nonnegative Kubernetes quantities using exact decimals.

    Metrics may use nanocores, unlike CPU *requests*, which have milli precision.
    Nonintegral byte values are reported unknown, not rounded down to zero.
    """
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid resource quantity")
    matched = _QUANTITY.fullmatch(value)
    if not matched:
        raise ValueError("Invalid resource quantity")
    number, suffix = matched.groups()
    suffix = suffix or ""
    try:
        with localcontext() as context:
            context.prec = 100
            result = Decimal(number)
            if suffix.endswith("i"):
                result *= Decimal(1024) ** ("KMGTPE".index(suffix[0]) + 1)
            elif suffix in _DECIMAL:
                result *= Decimal(10) ** _DECIMAL[suffix]
            else:
                exponent = int(suffix[1:])
                if not -20 <= exponent <= 20:
                    raise ValueError("Resource exponent out of range")
                result *= Decimal(10) ** exponent
            if not result.is_finite() or result < 0 or result > Decimal(2 ** 63 - 1):
                raise ValueError("Resource quantity out of range")
            if memory and result != result.to_integral_value():
                raise ValueError("Nonintegral byte quantity")
            return result
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError("Invalid resource quantity") from exc


def _quantity(value, *, memory=False):
    try:
        quantity = parse_quantity(value, memory=memory)
        return int(quantity) if memory else float(quantity)
    except ValueError:
        return None


def _usage(document):
    usage = _mapping(document)
    return (_quantity(usage.get("cpu")), _quantity(usage.get("memory"), memory=True))


def _total(items, field, *, available=True):
    if not available or any(item.get(field) is None for item in items):
        return None
    # Decimal avoids accumulating binary float errors in charts/totals.
    value = sum((Decimal(str(item[field])) for item in items), Decimal(0))
    return int(value) if field.endswith("bytes") else float(value)


def _node(item):
    metadata, status = _mapping(item.get("metadata")), _mapping(item.get("status"))
    name = _name(metadata.get("name"))
    if not name:
        return None
    conditions_by_type = {cond.get("type"): cond.get("status")
                          for cond in _list(status.get("conditions")) if isinstance(cond, dict)
                          and isinstance(cond.get("type"), str) and cond.get("type") in _CONDITIONS
                          and isinstance(cond.get("status"), str) and cond.get("status") in {"True", "False", "Unknown"}}
    conditions = [{"type": key, "status": value} for key, value in sorted(conditions_by_type.items())]
    ready = next((cond["status"] for cond in conditions if cond["type"] == "Ready"), "Unknown")
    allocatable, capacity = _mapping(status.get("allocatable")), _mapping(status.get("capacity"))
    return {"name": name, "ready": {"True": True, "False": False}.get(ready), "conditions": conditions,
            "cpu_allocatable_cores": _quantity(allocatable.get("cpu")),
            "memory_allocatable_bytes": _quantity(allocatable.get("memory"), memory=True),
            "cpu_capacity_cores": _quantity(capacity.get("cpu")),
            "memory_capacity_bytes": _quantity(capacity.get("memory"), memory=True),
            "cpu_usage_cores": None, "memory_usage_bytes": None,
            "metrics_timestamp": None, "metrics_window_seconds": None}


def _pod(item, namespace):
    metadata, spec, status = (_mapping(item.get(key)) for key in ("metadata", "spec", "status"))
    name, pod_namespace = _name(metadata.get("name")), _name(metadata.get("namespace"))
    if not name or pod_namespace != namespace:
        return None
    statuses = [entry for entry in _list(status.get("containerStatuses")) if isinstance(entry, dict)]
    init_statuses = [entry for entry in _list(status.get("initContainerStatuses")) if isinstance(entry, dict)]
    sidecars = {_name(entry.get("name")) for entry in _list(spec.get("initContainers"))
                if isinstance(entry, dict) and entry.get("restartPolicy") == "Always" and _name(entry.get("name"))}
    active_statuses = statuses + [entry for entry in init_statuses if _name(entry.get("name")) in sidecars]
    containers = _list(spec.get("containers"))
    total = len(containers) + len(sidecars)
    ready_count = sum(entry.get("ready") is True for entry in active_statuses)
    ready_condition = next((entry.get("status") for entry in _list(status.get("conditions"))
                            if isinstance(entry, dict) and entry.get("type") == "Ready"), None)
    phase = status.get("phase") if isinstance(status.get("phase"), str) and status.get("phase") in {"Pending", "Running", "Succeeded", "Failed", "Unknown"} else "Unknown"
    reasons = []
    for entry in statuses + init_statuses:
        for state_name in ("state", "lastState"):
            state = _mapping(entry.get(state_name))
            for state_type in ("waiting", "terminated"):
                reason = _reason(_mapping(state.get(state_type)).get("reason"))
                if reason and reason != "Completed":
                    reasons.append(reason)
    reason = next((value for value in ("OOMKilled", "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull") if value in reasons), None)
    reason = reason or (reasons[0] if reasons else _reason(status.get("reason")))
    restart_values = [_count(entry.get("restartCount")) for entry in statuses + init_statuses]
    restarts = sum(restart_values) if restart_values and None not in restart_values else 0 if not total else None
    return {"name": name, "namespace": namespace, "node": _name(spec.get("nodeName")), "phase": phase,
            "ready": {"True": True, "False": False}.get(ready_condition) if isinstance(ready_condition, str) else None,
            "containers_ready": ready_count, "containers_total": total, "restarts": restarts,
            "reason": reason, "cpu_usage_cores": None, "memory_usage_bytes": None,
            "metrics_timestamp": None, "metrics_window_seconds": None}


def _event(item, namespace):
    if item.get("type") != "Warning":
        return None
    involved = _mapping(item.get("involvedObject") or item.get("regarding"))
    metadata = _mapping(item.get("metadata"))
    if metadata.get("namespace") != namespace:
        return None
    kind = involved.get("kind")
    if not isinstance(kind, str) or kind not in {"Pod", "Deployment", "ReplicaSet", "Node", "StatefulSet", "DaemonSet", "Job", "CronJob", "Service", "PersistentVolumeClaim", "HorizontalPodAutoscaler"}:
        kind = "Object"
    name = _name(involved.get("name"))
    return {"type": "Warning", "reason": _reason(item.get("reason")),
            "object": f"{kind}/{name}" if name else kind, "namespace": namespace,
            "count": _count(item.get("count", _mapping(item.get("series")).get("count", 1))),
            "last_seen": _timestamp(item.get("lastTimestamp") or _mapping(item.get("series")).get("lastObservedTime")
                                    or item.get("eventTime") or metadata.get("creationTimestamp"))}


def _source_error(response):
    stderr = str(response.stderr or "").lower()
    if "forbidden" in stderr or "unauthorized" in stderr:
        return "PERMISSION_DENIED"
    if "notfound" in stderr or "not found" in stderr or "could not find the requested resource" in stderr:
        return "API_UNAVAILABLE"
    return "READ_FAILED"


def _read(command, runner):
    try:
        response = runner(command, capture_output=True, text=True, timeout=10)
        if response.returncode:
            return None, {"status": "unavailable", "error_code": _source_error(response)}
        if not isinstance(response.stdout, str) or len(response.stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
            return None, {"status": "unavailable", "error_code": "RESPONSE_TOO_LARGE"}
        document = json.loads(response.stdout, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
        items = document.get("items") if isinstance(document, dict) else None
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("Invalid Kubernetes list")
        return items, {"status": "available"}
    except subprocess.TimeoutExpired:
        return None, {"status": "unavailable", "error_code": "TIMEOUT"}
    except FileNotFoundError:
        return None, {"status": "unavailable", "error_code": "KUBECTL_UNAVAILABLE"}
    except (OSError, subprocess.SubprocessError):
        return None, {"status": "unavailable", "error_code": "READ_FAILED"}
    except (ValueError, TypeError, RecursionError):
        return None, {"status": "unavailable", "error_code": "INVALID_RESPONSE"}


def collect_cluster_snapshot(*, context, cluster_id, namespace, runner=subprocess.run, clock=None):
    """Read five independent API lists concurrently; no kubectl mutation commands."""
    if not isinstance(context, str) or not context.strip() or not isinstance(cluster_id, str) or not cluster_id.strip() or not _name(namespace):
        raise ValueError("Explicit context, cluster ID and a valid namespace are required")
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if now.utcoffset() is None:
        raise ValueError("Collector clock must be timezone aware")
    prefix = ["kubectl", "--context", context, "--request-timeout=8s", "get"]
    commands = {
        "nodes": prefix + ["nodes", "-o", "json"],
        "pods": prefix + ["pods", "--namespace", namespace, "-o", "json"],
        "events": prefix + ["events", "--namespace", namespace, "--field-selector=type=Warning", "-o", "json"],
        "node_metrics": prefix + ["--raw", "/apis/metrics.k8s.io/v1beta1/nodes"],
        "pod_metrics": prefix + ["--raw", f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods"],
    }
    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="sentinel-metrics") as executor:
        futures = {key: executor.submit(_read, command, runner) for key, command in commands.items()}
        documents, sources = {}, {}
        for key, future in futures.items():
            documents[key], sources[key] = future.result()
    return normalize_cluster_snapshot(documents, sources, cluster_id=cluster_id, namespace=namespace,
                                      observed_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))


def normalize_cluster_snapshot(documents, sources, *, cluster_id, namespace, observed_at):
    """Project allowlisted metadata and measurements, retaining source uncertainty."""
    snapshot = {"schema_version": "1.0", "cluster_id": cluster_id, "namespace": namespace,
                "observed_at": observed_at, "sources": {key: dict(sources[key]) for key in SOURCE_NAMES},
                "nodes": [], "pods": [], "events": [], "summary": {}, "truncation": {}}
    sources = snapshot["sources"]
    for key in SOURCE_NAMES:
        items = documents.get(key) or []
        if key == "events":
            items = sorted(items, key=lambda item: _timestamp(item.get("lastTimestamp") or _mapping(item.get("series")).get("lastObservedTime")
                                                             or item.get("eventTime") or _mapping(item.get("metadata")).get("creationTimestamp")) or "", reverse=True)
        snapshot["truncation"][key] = max(0, len(items) - LIST_LIMITS[key])
        if snapshot["truncation"][key]:
            sources[key]["partial"] = True
            sources[key]["error_code"] = "LIST_TRUNCATED"
        documents[key] = items[:LIST_LIMITS[key]]
    for key, projector in (("nodes", _node), ("pods", lambda item: _pod(item, namespace)),
                           ("events", lambda item: _event(item, namespace))):
        for item in documents[key]:
            projected = projector(item)
            if projected is not None:
                snapshot[key].append(projected)
            elif key != "events":
                sources[key].update(partial=True, error_code="INVALID_ITEM")
    node_metrics = {}
    for item in documents["node_metrics"]:
        name = _name(_mapping(item.get("metadata")).get("name"))
        if name:
            node_metrics[name] = (*_usage(item.get("usage")), _timestamp(item.get("timestamp")), _window(item.get("window")))
    pod_metrics = {}
    for item in documents["pod_metrics"]:
        metadata = _mapping(item.get("metadata"))
        name = _name(metadata.get("name"))
        if name and metadata.get("namespace") == namespace:
            containers = _list(item.get("containers"))
            values = [_usage(entry.get("usage")) for entry in containers if isinstance(entry, dict)]
            names = {_name(entry.get("name")) for entry in containers if isinstance(entry, dict)}
            if values and len(values) == len(containers) and len(names) == len(containers) and None not in names and all(cpu is not None and memory is not None for cpu, memory in values):
                pod_metrics[name] = (float(sum((Decimal(str(cpu)) for cpu, _ in values), Decimal(0))), sum(memory for _, memory in values),
                                     names, _timestamp(item.get("timestamp")), _window(item.get("window")))
    expected_containers = {}
    for item in documents["pods"]:
        spec, metadata = _mapping(item.get("spec")), _mapping(item.get("metadata"))
        pod_name = _name(metadata.get("name"))
        if not pod_name or metadata.get("namespace") != namespace:
            continue
        expected_containers[pod_name] = {_name(entry.get("name")) for entry in _list(spec.get("containers"))
                                         if isinstance(entry, dict)} | {
            _name(entry.get("name")) for entry in _list(spec.get("initContainers"))
            if isinstance(entry, dict) and entry.get("restartPolicy") == "Always"}
    for node in snapshot["nodes"]:
        values = node_metrics.get(node["name"], (None, None, None, None))
        node["cpu_usage_cores"], node["memory_usage_bytes"], node["metrics_timestamp"], node["metrics_window_seconds"] = values
    for pod in snapshot["pods"]:
        values = pod_metrics.get(pod["name"])
        if values and expected_containers.get(pod["name"], {None}).issubset(values[2]):
            pod["cpu_usage_cores"], pod["memory_usage_bytes"] = values[:2]
            pod["metrics_timestamp"], pod["metrics_window_seconds"] = values[3:]
    for key, items in (("node_metrics", snapshot["nodes"]), ("pod_metrics", snapshot["pods"])):
        missing = sum(item["cpu_usage_cores"] is None or item["memory_usage_bytes"] is None for item in items)
        if missing and sources[key]["status"] == "available":
            sources[key].update(partial=True, error_code="METRICS_INCOMPLETE", missing_items=missing)
    nodes, pods = snapshot["nodes"], snapshot["pods"]
    have_nodes = sources["nodes"]["status"] == "available" and not sources["nodes"].get("partial")
    have_pods = sources["pods"]["status"] == "available" and not sources["pods"].get("partial")
    node_usage = have_nodes and sources["node_metrics"]["status"] == "available" and not sources["node_metrics"].get("partial")
    pod_usage = have_pods and sources["pod_metrics"]["status"] == "available" and not sources["pod_metrics"].get("partial")
    snapshot["summary"] = {
        "nodes_total": len(nodes) if have_nodes else None,
        "nodes_ready": sum(node["ready"] is True for node in nodes) if have_nodes and all(node["ready"] is not None for node in nodes) else None,
        "pods_total": len(pods) if have_pods else None,
        "pods_ready": sum(pod["ready"] is True for pod in pods) if have_pods and all(pod["ready"] is not None for pod in pods) else None,
        "pods_pending": sum(pod["phase"] == "Pending" for pod in pods) if have_pods else None,
        "pods_failed": sum(pod["phase"] == "Failed" for pod in pods) if have_pods else None,
        "restarts": sum(pod["restarts"] for pod in pods) if have_pods and all(pod["restarts"] is not None for pod in pods) else None,
        "cpu_usage_cores": _total(nodes, "cpu_usage_cores", available=node_usage),
        "memory_usage_bytes": _total(nodes, "memory_usage_bytes", available=node_usage),
        "cpu_allocatable_cores": _total(nodes, "cpu_allocatable_cores", available=have_nodes),
        "memory_allocatable_bytes": _total(nodes, "memory_allocatable_bytes", available=have_nodes),
        "pod_cpu_usage_cores": _total(pods, "cpu_usage_cores", available=pod_usage),
        "pod_memory_usage_bytes": _total(pods, "memory_usage_bytes", available=pod_usage),
    }
    snapshot["events"].sort(key=lambda item: item["last_seen"] or "", reverse=True)
    if len(json.dumps(snapshot, allow_nan=False).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Cluster snapshot exceeds 2 MiB")
    return snapshot
