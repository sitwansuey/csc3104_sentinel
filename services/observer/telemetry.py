"""Bounded OTLP/HTTP JSON evidence, scoped to current Kubernetes Pod identities.

This is an Observer evidence store, not a dependency graph or authorization
source. Arbitrary attributes, log bodies and span events are deliberately omitted.
"""

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

from shared.canonical import canonical_json as encoded

MAX_RECORDS = 1000
SIGNALS = {"traces": ("resourceSpans", "scopeSpans", "spans"),
           "logs": ("resourceLogs", "scopeLogs", "logRecords"),
           "metrics": ("resourceMetrics", "scopeMetrics", "metrics")}
ATTRIBUTES = {"http.request.method", "http.response.status_code", "http.status_code",
              "rpc.system", "rpc.service", "rpc.method", "rpc.grpc.status_code",
              "peer.service", "server.address", "event.name"}


class TelemetryError(ValueError):
    pass


def stamp(value):
    if not isinstance(value, str):
        raise TelemetryError("Expected UTC time")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TelemetryError("Invalid time") from error
    if result.utcoffset() != timedelta(0):
        raise TelemetryError("Expected UTC time")
    return result


def utc(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def text(value, limit=128):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise TelemetryError("Invalid bounded text")
    return value


def number(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise TelemetryError("Expected a finite number")
    return value


def attributes(items):
    if not isinstance(items, list) or len(items) > 128:
        raise TelemetryError("Invalid attributes")
    result = {}
    for item in items:
        key, value = item.get("key"), item.get("value", {})
        if not isinstance(key, str) or key in result or not isinstance(value, dict):
            raise TelemetryError("Invalid or duplicate attribute")
        scalar = next((value[k] for k in ("stringValue", "intValue", "doubleValue", "boolValue") if k in value), None)
        if isinstance(scalar, (str, int, float, bool)) and len(str(scalar)) <= 256:
            result[key] = scalar
    return result


def nanos(value):
    if not isinstance(value, str) or not value.isdecimal() or len(value) > 20:
        raise TelemetryError("OTLP nanoseconds must be a decimal string")
    seconds, remainder = divmod(int(value), 1_000_000_000)
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=remainder // 1000)
    except (ValueError, OverflowError, OSError) as error:
        raise TelemetryError("Invalid OTLP time") from error


def identifier(value, length):
    if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{" + str(length) + "}", value) or int(value, 16) == 0:
        raise TelemetryError("Invalid trace/span identifier")
    return value.lower()


def normalize(signal, document, *, now):
    """Project OTLP protobuf-JSON; invalid records are explicitly rejected."""
    if signal not in SIGNALS or not isinstance(document, dict):
        raise TelemetryError("Unsupported OTLP request")
    resource_key, scope_key, record_key = SIGNALS[signal]
    resources = document.get(resource_key, [])
    if not isinstance(resources, list):
        raise TelemetryError("Invalid OTLP resource list")
    records, rejected, seen = [], 0, 0
    for resource in resources:
        if not isinstance(resource, dict) or not isinstance(resource.get(scope_key, []), list):
            raise TelemetryError("Invalid OTLP resource")
        try:
            attrs = attributes(resource.get("resource", {}).get("attributes", []))
            identity = {"namespace": text(attrs.get("k8s.namespace.name")),
                        "pod_uid": text(attrs.get("k8s.pod.uid")),
                        "service": text(attrs.get("service.name"))}
        except (TelemetryError, AttributeError, TypeError):
            identity = None
        for scope in resource.get(scope_key, []):
            if not isinstance(scope, dict) or not isinstance(scope.get(record_key, []), list):
                raise TelemetryError("Invalid OTLP scope")
            for item in scope.get(record_key, []):
                points = [item]
                metric_kind = None
                if signal == "metrics":
                    if not isinstance(item, dict):
                        raise TelemetryError("Invalid metric")
                    metric_kind = next((k for k in ("gauge", "sum", "histogram") if k in item), None)
                    if metric_kind is None:
                        rejected += 1
                        continue
                    if not isinstance(item[metric_kind], dict):
                        raise TelemetryError("Invalid metric data")
                    points = item[metric_kind].get("dataPoints", [])
                    if not isinstance(points, list):
                        raise TelemetryError("Invalid metric points")
                for point in points:
                    seen += 1
                    if seen > MAX_RECORDS:
                        raise TelemetryError("OTLP request exceeds record budget")
                    try:
                        if identity is None or not isinstance(point, dict):
                            raise TelemetryError("Missing Kubernetes identity")
                        at = nanos(point.get("endTimeUnixNano") if signal == "traces" else point.get("timeUnixNano"))
                        if not 0 <= (now - at).total_seconds() <= 1800:
                            raise TelemetryError("Old or future telemetry")
                        safe_attrs = {k: v for k, v in attributes(point.get("attributes", [])).items() if k in ATTRIBUTES}
                        row = {**identity, "signal": signal, "at": utc(at), "attributes": safe_attrs}
                        if signal == "traces":
                            start = nanos(point.get("startTimeUnixNano"))
                            if start > at:
                                raise TelemetryError("Span ends before it starts")
                            row.update(trace_id=identifier(point.get("traceId"), 32),
                                       span_id=identifier(point.get("spanId"), 16),
                                       duration_seconds=(at - start).total_seconds(),
                                       kind=point.get("kind", 0), status=point.get("status", {}).get("code", 0))
                            if (type(row["kind"]) is not int or type(row["status"]) is not int
                                    or row["kind"] not in (0, 1, 2, 3, 4, 5) or row["status"] not in (0, 1, 2)):
                                raise TelemetryError("Invalid span enum")
                            parent = point.get("parentSpanId")
                            if parent:
                                row["parent_span_id"] = identifier(parent, 16)
                        elif signal == "logs":
                            severity = point.get("severityNumber", 0)
                            if type(severity) is not int or not 0 <= severity <= 24:
                                raise TelemetryError("Invalid log severity")
                            row["severity"] = severity
                            for key, size in (("traceId", 32), ("spanId", 16)):
                                if point.get(key):
                                    row["trace_id" if key == "traceId" else "span_id"] = identifier(point[key], size)
                        else:
                            if point.get("flags", 0) != 0:
                                raise TelemetryError("Metric point has no recorded value")
                            row.update(name=text(item.get("name")), unit=text(item.get("unit") or "1", 32), metric_kind=metric_kind)
                            if metric_kind == "histogram":
                                count = point.get("count")
                                if not isinstance(count, str) or not count.isdecimal():
                                    raise TelemetryError("Invalid histogram count")
                                row.update(count=int(count), sum=number(point.get("sum", 0)))
                            elif "asDouble" in point:
                                row["value"] = number(point["asDouble"])
                            else:
                                value = point.get("asInt")
                                if not isinstance(value, str) or not re.fullmatch(r"-?\d{1,20}", value):
                                    raise TelemetryError("Invalid integer data point")
                                row["value"] = int(value)
                        if signal == "traces":
                            key = (signal, identity, row["trace_id"], row["span_id"])
                        elif signal == "metrics":
                            key = (signal, identity, row["name"], row["at"], row["metric_kind"], safe_attrs)
                        else:
                            key = row
                        row["evidence_id"] = "OTEL-" + hashlib.sha256(encoded(key).encode()).hexdigest()
                        records.append(row)
                    except (TelemetryError, AttributeError, TypeError, KeyError):
                        rejected += 1
    return records, rejected


class TelemetryStore:
    """Each operation owns its SQLite connection; safe for receiver/worker threads."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS otlp_evidence (
                    evidence_id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
                    pod_uid TEXT NOT NULL, service TEXT NOT NULL, signal TEXT NOT NULL,
                    at TEXT NOT NULL, received_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS otlp_scope ON otlp_evidence(namespace,pod_uid,service,at);
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def ingest(self, signal, document, *, now=None):
        now = now or datetime.now(timezone.utc)
        rows, rejected = normalize(signal, document, now=now)
        with self.connect() as db:
            for row in rows:
                payload = encoded(row)
                old = db.execute("SELECT payload FROM otlp_evidence WHERE evidence_id=?", (row["evidence_id"],)).fetchone()
                if old and old[0] != payload:
                    raise TelemetryError("Changed payload for an existing span identity")
                db.execute("INSERT OR IGNORE INTO otlp_evidence VALUES(?,?,?,?,?,?,?,?)",
                           (row["evidence_id"], row["namespace"], row["pod_uid"], row["service"], signal,
                            row["at"], utc(now), payload))
            db.execute("DELETE FROM otlp_evidence WHERE received_at < ?", (utc(now - timedelta(minutes=30)),))
            db.execute("DELETE FROM otlp_evidence WHERE rowid IN (SELECT rowid FROM otlp_evidence ORDER BY rowid DESC LIMIT -1 OFFSET 5000)")
        return {"accepted": len(rows), "rejected": rejected}

    def context(self, observation, service_names, *, max_age_seconds=60, limit=20, thresholds=()):
        """Only evidence from current, owned Pod UIDs can join this observation."""
        if not service_names or any(not isinstance(s, str) or not s.strip() for s in service_names):
            raise TelemetryError("Explicit service names are required")
        if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 300 or not 1 <= limit <= 100:
            raise TelemetryError("Invalid context age or record limit")
        at = stamp(observation["captured_at"])
        metadata = observation["deployment"]["metadata"]
        pods = [p["metadata"]["uid"] for p in observation["pods"]]
        result = {"schema_version": "1.0", "target_uid": metadata["uid"], "records": {},
                  "coverage": {}, "alerts": [], "thresholds_complete": True}
        metric_rows, metric_overflow = [], False
        with self.connect() as db:
            for signal in SIGNALS:
                rows = []
                if pods:
                    query = ("SELECT payload FROM otlp_evidence WHERE namespace=? AND signal=? AND at>=? AND at<=? "
                             + "AND pod_uid IN (" + ",".join("?" for _ in pods) + ") "
                             + "AND service IN (" + ",".join("?" for _ in service_names) + ") ORDER BY at DESC LIMIT ?")
                    rows = [json.loads(r[0]) for r in db.execute(query, (
                        metadata["namespace"], signal, utc(at - timedelta(seconds=max_age_seconds)), utc(at),
                        *pods, *service_names, 1001 if signal == "metrics" else limit + 1))]
                if signal == "metrics":
                    metric_rows, metric_overflow = rows[:1000], len(rows) > 1000
                result["records"][signal] = rows[:limit]
                result["coverage"][signal] = {"status": "available" if rows else "unavailable",
                    "retained": min(limit, len(rows)), "truncated": len(rows) > limit}
        for rule in thresholds:
            name, unit, above = text(rule.get("name")), text(rule.get("unit"), 32), number(rule.get("above"))
            # Thresholds use fresh GAUGES only. Cumulative sums/histograms require
            # an explicit rate/window adapter; interpreting them as gauges is unsafe.
            candidates = [r for r in metric_rows if r["name"] == name
                          and r["unit"] == unit and r["metric_kind"] == "gauge" and "value" in r]
            if not candidates or metric_overflow:
                result["thresholds_complete"] = False
                continue
            latest = {}
            for row in candidates:
                latest.setdefault(row["pod_uid"] + encoded(row["attributes"]), row)
            # Require coverage of every currently owned Pod; missing points are unknown.
            if set(pods) - {r["pod_uid"] for r in latest.values()}:
                result["thresholds_complete"] = False
                continue
            if any(row["value"] > above for row in latest.values()):
                result["alerts"].append({"name": name, "at": min(r["at"] for r in latest.values()),
                    "detail": encoded({"metric": name, "unit": unit, "above": above,
                                       "values": [r["value"] for r in latest.values()]})})
        return result


class CorrelatedEvidenceCollector:
    def __init__(self, collector, telemetry, service_names, policy):
        if not isinstance(policy, dict) or set(policy) - {"max_age_seconds", "metric_thresholds"}:
            raise TelemetryError("Invalid telemetry policy")
        age, rules = policy.get("max_age_seconds", 60), policy.get("metric_thresholds", [])
        if type(age) is not int or not 1 <= age <= 300 or not isinstance(rules, list) or len(rules) > 8:
            raise TelemetryError("Invalid telemetry age or threshold budget")
        for rule in rules:
            if not isinstance(rule, dict) or set(rule) != {"name", "unit", "above"}:
                raise TelemetryError("Thresholds require name, unit and above")
            text(rule["name"])
            text(rule["unit"], 32)
            number(rule["above"])
        self.collector, self.telemetry = collector, telemetry
        self.service_names, self.policy = service_names, policy

    @property
    def target(self):
        return self.collector.target

    def collect(self):
        observation = self.collector.collect()
        observation["telemetry"] = self.telemetry.context(observation, self.service_names,
            max_age_seconds=self.policy.get("max_age_seconds", 60),
            thresholds=self.policy.get("metric_thresholds", []))
        return observation


def validate_context(context, observation):
    """Validate persisted context before it can influence detection/checkpoints."""
    metadata = observation["deployment"]["metadata"]
    captured = stamp(observation["captured_at"])
    if (not isinstance(context, dict) or context.get("schema_version") != "1.0"
            or context.get("target_uid") != metadata["uid"]
            or type(context.get("thresholds_complete")) is not bool):
        raise TelemetryError("Telemetry does not match this observation")
    pods = {p["metadata"].get("uid") for p in observation["pods"]}
    for signal in SIGNALS:
        records = context.get("records", {}).get(signal)
        if not isinstance(records, list) or len(records) > 100:
            raise TelemetryError("Invalid context records")
        for row in records:
            if (row.get("signal") != signal or row.get("pod_uid") not in pods
                    or row.get("namespace") != metadata["namespace"]
                    or stamp(row["at"]) > captured or len(encoded(row)) > 4096):
                raise TelemetryError("Uncorrelated context record")
    alerts = context.get("alerts")
    if not isinstance(alerts, list) or len(alerts) > 8:
        raise TelemetryError("Invalid telemetry alerts")
    for alert in alerts:
        text(alert["name"])
        text(alert["detail"], 2048)
        if stamp(alert["at"]) > captured:
            raise TelemetryError("Future telemetry alert")
