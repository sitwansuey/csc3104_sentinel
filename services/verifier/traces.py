"""Project bounded OTLP JSON parent-span evidence into caller-to-callee edges."""

from datetime import datetime, timedelta, timezone
import re


def utc_time(value):
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("Evidence clock must be a timezone-aware UTC datetime")
    return value


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def _identifier(value, length):
    if not isinstance(value, str) or not re.fullmatch(rf"[a-fA-F0-9]{{{length}}}", value) or int(value, 16) == 0:
        raise ValueError("Invalid trace/span identifier")
    return value.lower()


def _attributes(values):
    if not isinstance(values, list):
        raise ValueError("Expected resource attributes")
    result = {}
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            raise ValueError("Invalid resource attribute")
        if item["key"] in result:
            raise ValueError("Duplicate resource attribute")
        value = item.get("value", {})
        if not isinstance(value, dict):
            raise ValueError("Invalid resource attribute value")
        result[item["key"]] = value.get("stringValue")
    return result


def trace_edges(payload, aliases, *, now, max_age_seconds=300, namespaces=None):
    """Use cross-service parent relationships, never temporal proximity alone.

    aliases maps configured OTLP service.name values to workload names. This
    adapter consumes OTLP JSON exports; it is not an OTLP ingestion collector.
    """
    utc_time(now)
    if (not isinstance(aliases, dict) or any(not isinstance(key, str) or not key
            or not isinstance(value, str) or not value for key, value in aliases.items())):
        raise ValueError("Expected configured service aliases")
    if type(max_age_seconds) is not int or not 0 < max_age_seconds <= 86400:
        raise ValueError("Trace age limit must be positive and at most a day")
    if not isinstance(payload, dict) or not isinstance(payload.get("resourceSpans"), list):
        raise ValueError("Expected an OTLP JSON resourceSpans export")
    records, invalid, unknowns = {}, set(), set()
    count = 0
    for resource in payload["resourceSpans"]:
        try:
            attributes = _attributes(resource["resource"]["attributes"])
            alias = attributes.get("service.name")
            if alias not in aliases:
                raise ValueError("Unconfigured trace service")
            if (namespaces and alias in namespaces and "k8s.namespace.name" in attributes
                    and attributes["k8s.namespace.name"] != namespaces[alias]):
                raise ValueError("Trace service namespace mismatch")
            scopes = resource["scopeSpans"]
            if not isinstance(scopes, list):
                raise ValueError("Invalid scopeSpans")
        except (KeyError, TypeError, ValueError) as error:
            unknowns.add(str(error) if isinstance(error, ValueError) else "Incomplete trace resource")
            continue
        for scope in scopes:
            spans = scope.get("spans") if isinstance(scope, dict) else None
            if not isinstance(spans, list):
                unknowns.add("Incomplete trace scope")
                continue
            for span in spans:
                count += 1
                if count > 10000:
                    raise ValueError("Trace export exceeds 10000 spans")
                try:
                    trace = _identifier(span["traceId"], 32)
                    identity = _identifier(span["spanId"], 16)
                    parent = span.get("parentSpanId")
                    parent = _identifier(parent, 16) if parent else None
                    raw = span["endTimeUnixNano"]
                    if type(raw) not in (str, int) or not str(raw).isdigit():
                        raise ValueError("Invalid span time")
                    seconds, nanoseconds = divmod(int(raw), 1_000_000_000)
                    end = datetime.fromtimestamp(seconds, timezone.utc) + timedelta(microseconds=nanoseconds // 1000)
                    if not 0 <= (now - end).total_seconds() <= max_age_seconds:
                        raise ValueError("Stale or future trace span")
                    kind = span.get("kind")
                    if type(kind) is not int or kind not in range(1, 6):
                        raise ValueError("Unsupported span kind")
                    key = (trace, identity)
                    record = (aliases[alias], parent, kind, end)
                    if key in records and records[key] != record:
                        invalid.add(key)
                        raise ValueError("Conflicting span identity")
                    records[key] = record
                except (KeyError, TypeError, ValueError, OverflowError, OSError) as error:
                    unknowns.add(str(error) if isinstance(error, ValueError) else "Incomplete trace span")
    for key in invalid:
        records.pop(key, None)
    evidence, edges = [], set()
    for (trace, identity), (service, parent, kind, end) in records.items():
        if parent is None:
            continue
        caller = records.get((trace, parent))
        if caller is None:
            if kind in (2, 5):
                unknowns.add("Missing parent span for server/consumer")
            continue
        if caller[0] != service:
            if kind not in (2, 5):
                unknowns.add("Unsupported cross-service span relationship")
                continue
            edges.add((caller[0], service))
            evidence.append({"from": caller[0], "to": service, "source": "otlp_parent_span",
                             "trace_id": trace, "span_id": identity, "observed_at": stamp(end)})
    if not records:
        unknowns.add("No fresh mapped spans")
    return {"edges": [{"from": caller, "to": callee} for caller, callee in sorted(edges)],
            "evidence": evidence, "unknowns": sorted(unknowns), "captured_at": stamp(now),
            "source": "otlp_json"}
