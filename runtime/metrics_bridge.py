"""Read-only metrics-server bridge into the Observer's gauge thresholds; never a Guard input.

Online Boutique emits no OpenTelemetry metrics, so the Observer's threshold path
(services/observer/telemetry.py) has nothing to alert on. This bridge reads the
Metrics API (metrics.k8s.io PodMetrics, which metrics-server serves) and turns
each owned Pod's usage into OTLP/HTTP JSON gauge points that TelemetryStore
ingests like any other OTLP request:

- k8s.pod.cpu.usage, unit cores: the sum of the Pod's container CPU usage,
  average cores over the Metrics API window (nanocores are exact decimals);
- k8s.pod.memory.working_set, unit By: the sum of container working-set bytes.

PodMetrics carry no Pod UID, so the bridge joins them by name to the Pods the
Observer's own collection owns, and stamps the owned UID. A PodMetrics item for
any other Pod is dropped. A Pod whose metrics are missing, cover other
containers than the Pod reports, or hold a quantity that does not parse is
left out, never filled with zero: the threshold rule in TelemetryStore.context
then reports thresholds_complete false and raises no alert. Quantities are
parsed exactly with runtime.cluster_metrics.parse_quantity (decimal, then one
rounding to the nearest double for CPU; memory must be whole bytes).

The bridge is Observer evidence only. Nothing it produces reaches the Guard,
the Executor or the Verifier.
"""

from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

from runtime.cluster_metrics import parse_quantity

CPU_METRIC, CPU_UNIT = "k8s.pod.cpu.usage", "cores"
MEMORY_METRIC, MEMORY_UNIT = "k8s.pod.memory.working_set", "By"
MAX_ITEMS = 1000


class MetricsBridgeError(ValueError):
    """The Metrics API response or the bridge request is malformed."""


def pod_metrics_path(namespace):
    """The namespaced PodMetrics list path; a read-only GET."""
    if not isinstance(namespace, str) or not namespace or "/" in namespace:
        raise MetricsBridgeError("An explicit namespace is required")
    return "/apis/metrics.k8s.io/v1beta1/namespaces/" + quote(namespace, safe="") + "/pods"


def _nanos(value):
    """RFC 3339 UTC time as OTLP decimal nanoseconds; None when it is not a timezone-aware time."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.utcoffset() is None:
        return None
    moment = moment.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = moment - epoch
    return str((delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000)


def pod_usage(item, expected_containers):
    """(cpu_cores Decimal, memory_bytes int) for one PodMetrics item, or (None, reason)."""
    containers = item.get("containers") if isinstance(item, dict) else None
    if not isinstance(containers, list) or not containers:
        return None, "no_container_metrics"
    names, cpu, memory = set(), Decimal(0), 0
    for entry in containers:
        usage = entry.get("usage") if isinstance(entry, dict) else None
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or name in names or not isinstance(usage, dict):
            return None, "invalid_container_metrics"
        names.add(name)
        try:
            cpu += parse_quantity(usage.get("cpu"))
            memory += int(parse_quantity(usage.get("memory"), memory=True))
        except ValueError:
            return None, "invalid_quantity"
    if not expected_containers or names != set(expected_containers):
        return None, "container_coverage"
    return (cpu, memory), None


def _gauge(name, unit, nanos, point):
    return {"name": name, "unit": unit, "gauge": {"dataPoints": [{"timeUnixNano": nanos, **point}]}}


def convert(pod_metrics, pods, *, namespace, service):
    """Return (otlp_document, report) for the owned Pods' current usage.

    pod_metrics is the items list of a metrics.k8s.io PodMetricsList; pods are
    the Observer observation's owned Pods ({metadata: {name, uid}, status:
    {containerStatuses}}). The report counts owned Pods, Pods with complete
    metrics, foreign items dropped and each incomplete Pod's reason; complete
    is true only when every owned Pod has a complete point.
    """
    if not isinstance(service, str) or not service.strip():
        raise MetricsBridgeError("An explicit service name is required")
    if not isinstance(pod_metrics, list) or len(pod_metrics) > MAX_ITEMS:
        raise MetricsBridgeError("PodMetrics must be a bounded list")
    owned = {}
    for pod in pods:
        metadata = pod["metadata"]
        owned[metadata["name"]] = (metadata["uid"], [entry.get("name") for entry in
                                                     pod.get("status", {}).get("containerStatuses", [])])
    by_name, foreign = {}, 0
    for item in pod_metrics:
        metadata = item.get("metadata") if isinstance(item, dict) else None
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if not isinstance(name, str) or metadata.get("namespace") != namespace or name not in owned:
            foreign += 1
            continue
        if name in by_name:
            raise MetricsBridgeError("PodMetrics repeat a Pod")
        by_name[name] = item
    resources, incomplete = [], {}
    for name, (uid, containers) in sorted(owned.items()):
        item = by_name.get(name)
        if item is None:
            incomplete[name] = "missing"
            continue
        nanos = _nanos(item.get("timestamp"))
        usage, reason = pod_usage(item, containers)
        if nanos is None or usage is None:
            incomplete[name] = reason or "invalid_timestamp"
            continue
        cpu, memory = usage
        resources.append({
            "resource": {"attributes": [
                {"key": "k8s.namespace.name", "value": {"stringValue": namespace}},
                {"key": "k8s.pod.uid", "value": {"stringValue": uid}},
                {"key": "service.name", "value": {"stringValue": service}}]},
            "scopeMetrics": [{"scope": {"name": "sentinel.metrics_bridge"}, "metrics": [
                _gauge(CPU_METRIC, CPU_UNIT, nanos, {"asDouble": float(cpu)}),
                _gauge(MEMORY_METRIC, MEMORY_UNIT, nanos, {"asInt": str(memory)})]}]})
    report = {"pods_owned": len(owned), "pods_with_metrics": len(resources), "foreign_items_dropped": foreign,
              "incomplete": incomplete, "complete": bool(owned) and not incomplete}
    return {"resourceMetrics": resources}, report


class MetricsBridgeCollector:
    """Wrap an Observer collector: after each collection, ingest the owned Pods' Metrics API usage.

    Wrap this in services.observer.telemetry.CorrelatedEvidenceCollector with
    the scenario's metric_thresholds, so the threshold rule reads what was just
    ingested. reader.get(path) is a read-only Kubernetes GET (KubectlReader).
    A failed Metrics API read is recorded and ingests nothing, which leaves
    the thresholds incomplete; it never fails the Observer collection.
    """

    def __init__(self, collector, reader, telemetry, *, service, now=None):
        self.collector, self.reader, self.telemetry = collector, reader, telemetry
        self.service = service
        self.now = now or (lambda: datetime.now(UTC))
        self.reports = []

    @property
    def target(self):
        return self.collector.target

    def collect(self):
        observation = self.collector.collect()
        namespace = observation["deployment"]["metadata"]["namespace"]
        try:
            response = self.reader.get(pod_metrics_path(namespace))
            items = response.get("items") if isinstance(response, dict) else None
            document, report = convert(items, observation["pods"], namespace=namespace, service=self.service)
            report["ingest"] = self.telemetry.ingest("metrics", document, now=self.now())
        except (MetricsBridgeError, ValueError, RuntimeError, OSError) as error:
            # Unknown, never zero: no points means the thresholds stay incomplete.
            report = {"complete": False, "error": type(error).__name__}
        report["captured_at"] = observation["captured_at"]
        self.reports = (self.reports + [report])[-50:]
        return observation
