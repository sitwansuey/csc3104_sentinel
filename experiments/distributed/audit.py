"""Count Sentinel's template writes from the API server audit log, and sanitise the extract (P4.01, I7).

The sentinel-dist lab (deploy/kind/kind-dist.yaml) runs the API server with
deploy/kind/audit-policy.yaml, which logs every change to a Deployment in the
lab namespaces with its request and response bodies, and every other request
a Sentinel ServiceAccount makes at Metadata level. That log is written by the
API server, not by Sentinel, so it counts writes independently of the
Coordinator's records, the Executor's ledger and the harness's own reads.

classify() turns one audit event into one allowlisted row. Only these fields
are kept: the audit ID, the verb, the user name (a person's or an admin's
user name becomes "<operator>"), the Pod and node names bound into a
ServiceAccount token, the resource, namespace, object name (a Secret's name
becomes "<secret>"), the response code and reason, the authorization
decision, whether an admission policy denied it, whether it was a dry run, the
JSON Patch operation paths (never their values), the action and fence epoch
named by a Sentinel annotation in the patch, and the resourceVersion and
generation of the stored object. Request and response bodies, source IPs,
user agents, credential IDs and the request URI are dropped. assert_clean()
refuses an extract that still holds credential material or a listed Secret
name.

A template write is a request whose effect is to write the Pod template: a
successful (2xx), complete, non-dry-run patch or update of a Deployment whose
JSON Patch writes at, under or above /spec/template, or whose merge patch has
spec.template. A JSON Patch is classified exactly as the file test cluster
classifies one (tests/support/file_cluster.py::patch_action), so a fence or a
Coordinator seal, which changes only annotations, is never a template write,
and neither is a denied request (403 or 422), a dry run or a read. An update
(a full object) cannot be told apart by its paths, so a successful update is
counted as a template write; Sentinel never sends one.

I7 (invariants.i7) then needs at most one template write per action, each
made by the Executor's ServiceAccount and carrying that action's evidence
annotation.
"""
from collections import Counter
from datetime import datetime
import json
import re
from urllib.parse import parse_qs, urlsplit

from scripts.evidence.sanitise import HARD_FAIL
from services.executor.reconciliation import ACTION_ANNOTATION, FENCE_ANNOTATION, FENCE_EPOCH_ANNOTATION
from tests.support.file_cluster import annotation_action_id, patch_action, writes_template

NAMESPACES = ("sentinel-dist", "sentinel-dist-workload")
SERVICE_ACCOUNT_PREFIX = "system:serviceaccount:sentinel-dist:"
EXECUTOR_USER = SERVICE_ACCOUNT_PREFIX + "sentinel-executor"
POD_NAME = "authentication.kubernetes.io/pod-name"
NODE_NAME = "authentication.kubernetes.io/node-name"
DECISION = "authorization.k8s.io/decision"
ADMISSION_FAILURE = "validation.policy.admission.k8s.io/validation_failure"
MUTATING = frozenset({"patch", "update"})
_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?\Z")


class AuditError(ValueError):
    """The audit log cannot be read, or an extract still holds something it must not."""


def parse(text):
    """(events, skipped): every JSON object line of an audit log; a line that is not one is skipped.

    The log is read while the API server may still be writing it, so a last,
    partial line is expected; skipped counts such lines so a summary shows them.
    """
    events, skipped = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(value, dict) and value.get("kind") == "Event":
            events.append(value)
        else:
            skipped += 1
    return events, skipped


def _name(value):
    return value if isinstance(value, str) and _NAME.match(value) else None


def _user(username):
    if not isinstance(username, str):
        return None
    if username.startswith("system:"):
        return username
    return "<operator>"  # A person's or an admin's user name is never kept.


def _dry_run(uri):
    try:
        return bool(parse_qs(urlsplit(uri or "").query).get("dryRun"))
    except ValueError:
        return True  # An unreadable URI is never counted as a write.


def _merge_annotation(body, key):
    metadata = body.get("metadata") if isinstance(body, dict) else None
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    value = annotations.get(key) if isinstance(annotations, dict) else None
    return value if isinstance(value, str) else None


def _patch_annotation(operations, key):
    escaped = "/metadata/annotations/" + key.replace("~", "~0").replace("/", "~1")
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("op") == "test":
            continue
        if operation.get("path") == escaped and isinstance(operation.get("value"), str):
            return operation["value"]
        if operation.get("path") == "/metadata/annotations" and isinstance(operation.get("value"), dict):
            value = operation["value"].get(key)
            if isinstance(value, str):
                return value
    return None


def _shape(verb, body):
    """(paths, writes_template, marker, action_id, fence_epoch) of one request body."""
    if verb == "update":
        return [], True, *_annotations(body)
    if isinstance(body, list):
        paths = [operation.get("path") for operation in body
                 if isinstance(operation, dict) and isinstance(operation.get("path"), str)]
        _, marker, action_id = patch_action(body)
        return paths, writes_template(body), marker, action_id, _patch_annotation(body, FENCE_EPOCH_ANNOTATION)
    if isinstance(body, dict):
        spec = body.get("spec")
        paths = sorted("/" + key for key in body)
        return paths, isinstance(spec, dict) and "template" in spec, *_annotations(body)
    return [], False, "none", "unannotated", None


def _annotations(body):
    for marker, key in (("write", ACTION_ANNOTATION), ("fence", FENCE_ANNOTATION)):
        value = _merge_annotation(body, key)
        if value is not None:
            return marker, annotation_action_id(value), _merge_annotation(body, FENCE_EPOCH_ANNOTATION)
    return "none", "unannotated", _merge_annotation(body, FENCE_EPOCH_ANNOTATION)


def classify(event):
    """One allowlisted row for one audit event (module docstring); None for a stage that is not final."""
    stage = event.get("stage")
    if stage not in {"ResponseComplete", "Panic"}:
        return None
    reference = event.get("objectRef") if isinstance(event.get("objectRef"), dict) else {}
    user = event.get("user") if isinstance(event.get("user"), dict) else {}
    extra = user.get("extra") if isinstance(user.get("extra"), dict) else {}
    status = event.get("responseStatus") if isinstance(event.get("responseStatus"), dict) else {}
    annotations = event.get("annotations") if isinstance(event.get("annotations"), dict) else {}
    response = event.get("responseObject") if isinstance(event.get("responseObject"), dict) else {}
    metadata = response.get("metadata") if isinstance(response.get("metadata"), dict) else {}
    verb, resource = event.get("verb"), reference.get("resource")
    code = status.get("code") if type(status.get("code")) is int else None
    success = stage == "ResponseComplete" and code is not None and 200 <= code < 300
    dry_run = _dry_run(event.get("requestURI"))
    deployment_change = (verb in MUTATING and resource == "deployments" and reference.get("apiGroup") == "apps"
                         and not reference.get("subresource"))
    paths, template, marker, action_id, epoch = (_shape(verb, event.get("requestObject")) if deployment_change
                                                 else ([], False, "none", "unannotated", None))
    if not deployment_change:
        effect = "request"
    elif not success:
        effect = "denied" if code in {401, 403, 409, 422} else "failed"
    elif dry_run:
        effect = "dry_run"
    elif template:
        effect = "template_write"
    elif marker == "fence":
        effect = "fence"
    else:
        effect = "metadata_write"
    pods = extra.get(POD_NAME) if isinstance(extra.get(POD_NAME), list) else []
    nodes = extra.get(NODE_NAME) if isinstance(extra.get(NODE_NAME), list) else []
    username = _user(user.get("username"))
    return {
        "audit_id": event.get("auditID") if isinstance(event.get("auditID"), str) else None,
        "at": event.get("stageTimestamp") if isinstance(event.get("stageTimestamp"), str) else None,
        "verb": verb if isinstance(verb, str) else None, "user": username,
        "sentinel": isinstance(username, str) and username.startswith(SERVICE_ACCOUNT_PREFIX),
        "pod": _name(pods[0]) if pods else None, "node": _name(nodes[0]) if nodes else None,
        "resource": resource if isinstance(resource, str) else None,
        "subresource": reference.get("subresource") if isinstance(reference.get("subresource"), str) else None,
        "namespace": _name(reference.get("namespace")),
        "name": "<secret>" if resource == "secrets" and reference.get("name") else _name(reference.get("name")),
        "code": code, "reason": status.get("reason") if isinstance(status.get("reason"), str) else None,
        "decision": annotations.get(DECISION) if isinstance(annotations.get(DECISION), str) else None,
        "admission_denied": ADMISSION_FAILURE in annotations, "dry_run": dry_run, "effect": effect,
        "paths": paths, "marker": marker, "action_id": action_id,
        "fence_epoch": epoch if isinstance(epoch, str) and epoch.isdigit() else None,
        "resource_version": metadata.get("resourceVersion") if success and isinstance(
            metadata.get("resourceVersion"), str) else None,
        "generation": metadata.get("generation") if success and type(metadata.get("generation")) is int else None,
    }


def extract(events, *, since=None, until=None, namespaces=NAMESPACES):
    """Sanitised rows for the lab namespaces (plus every Sentinel request), between since and until.

    since and until are RFC 3339 UTC stamps. They are compared as instants,
    not as text: "10:00:30Z" sorts after "10:00:30.000000Z" as text. A row
    without a readable time is kept only when no window is given.
    """
    start, end = (_instant(since) if since is not None else None), (_instant(until) if until is not None else None)
    if (since is not None and start is None) or (until is not None and end is None):
        raise AuditError("The extract window must be RFC 3339 stamps")
    rows = []
    for event in events:
        row = classify(event)
        if row is None or (row["namespace"] not in namespaces and not row["sentinel"]):
            continue
        at = _instant(row["at"]) if row["at"] else None
        if (start is not None or end is not None) and at is None:
            continue
        if (start is not None and at < start) or (end is not None and at > end):
            continue
        rows.append(row)
    return rows


def _instant(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def template_writes(rows):
    """Rows in the shape the I1 and I2 checks read (experiments/distributed/invariants.py).

    Only Sentinel's own writes: a write by the lab operator (the fault
    injection or a restore) is not an action's write, and is reported apart.
    """
    writes = []
    for row in rows:
        if row["effect"] != "template_write" or not row["sentinel"]:
            continue
        at = (_instant(row["at"]) if row["at"] else None) or 0.0
        writes.append({"host": row["pod"] or "unknown", "action_id": row["action_id"], "kind": "write",
                       "marker": row["marker"], "template_changed": 1, "state": "committed",
                       "resource_version": row["resource_version"], "decided_at": at, "source": "audit"})
    return writes


def summary(rows):
    """Counts by effect and writer, and the template writes per action, for summary.json."""
    effects = Counter(row["effect"] for row in rows)
    per_action = Counter(row["action_id"] for row in rows if row["effect"] == "template_write" and row["sentinel"])
    operator = sum(1 for row in rows if row["effect"] == "template_write" and not row["sentinel"])
    denied = Counter(f"{row['user']} {row['verb']} {row['resource']}" for row in rows
                     if row["sentinel"] and row["code"] in {401, 403, 422})
    return {"events": len(rows), "effects": dict(sorted(effects.items())),
            "sentinel_template_writes_per_action": dict(sorted(per_action.items())),
            "max_template_writes_per_action": max(per_action.values(), default=0),
            "operator_template_writes": operator, "sentinel_denials": dict(sorted(denied.items()))}


def assert_clean(text, *, secret_names=(), name="audit extract"):
    """AuditError when text holds credential material (the evidence sanitiser's patterns) or a Secret name."""
    for label, pattern in HARD_FAIL:
        if pattern.search(text):
            raise AuditError(f"{name}: refused, contains {label}")
    for secret in secret_names:
        if secret and secret in text:
            raise AuditError(f"{name}: refused, names a Secret")
    return text


def dumps(rows, *, secret_names=()):
    """The JSON Lines text of an extract, checked by assert_clean."""
    text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    return assert_clean(text, secret_names=secret_names)
