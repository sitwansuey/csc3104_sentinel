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
counted as a template write; Sentinel never sends one. A successful,
non-dry-run change of a Deployment whose request body the log does not hold
(an event logged at a level other than RequestResponse, say because the audit
policy's level or namespaces are wrong) cannot be classified: it is
"unclassified", never a metadata write, and I7 fails on one a Sentinel
ServiceAccount made. Only acknowledged successes are counted: a write the API
server stored but answered with a 5xx or a timeout is "failed" here, and only
the generation check in completeness() notices it.

I7 (invariants.i7) then needs at most one template write per action, each
made by the Executor's ServiceAccount and carrying that action's evidence
annotation, and an extract that completeness() found whole: the operator's
fault injection is in it (a positive control), every generation the target
gained during the run is accounted for by a write in it, and the log reaches
back before the run began (nothing rotated out).
"""
from collections import Counter
from datetime import UTC, datetime
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
TARGET_REF = ("sentinel-dist-workload", "demo-api")  # (namespace, name) of the lab's target Deployment.
# Effects of a stored change: each raises the Deployment's generation when it changes the spec or annotations.
STORED = frozenset({"template_write", "fence", "metadata_write"})
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
    body = event.get("requestObject")
    readable = event.get("level") == "RequestResponse" and (
        isinstance(body, dict) or (verb == "patch" and isinstance(body, list)))
    paths, template, marker, action_id, epoch = (_shape(verb, body) if deployment_change and readable
                                                 else ([], False, "none", "unannotated", None))
    if not deployment_change:
        effect = "request"
    elif not success:
        effect = "denied" if code in {401, 403, 409, 422} else "failed"
    elif dry_run:
        effect = "dry_run"
    elif not readable:
        effect = "unclassified"  # A stored change whose body the log does not hold: never assumed harmless.
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


def template_writes(rows, problems=()):
    """Rows in the shape the I1 and I2 checks read (experiments/distributed/invariants.py).

    Only Sentinel's own writes: a write by the lab operator (the fault
    injection or a restore) is not an action's write, and is reported apart.
    A Sentinel change the log could not classify, and an extract that
    completeness() found short (problems), each add an "unobserved" row, on
    which I1 fails as it does for a kind-mode polling gap: a template write
    may be hidden.
    """
    writes = []

    def unobserved(at, unaccounted):
        return {"host": "audit", "action_id": "unknown", "kind": "write", "marker": "unobserved",
                "template_changed": 0, "state": "committed", "resource_version": None, "decided_at": at,
                "unaccounted": unaccounted, "source": "audit"}
    for row in rows:
        at = (_instant(row["at"]) if row["at"] else None) or 0.0
        if row["effect"] == "unclassified" and row["sentinel"]:
            writes.append(unobserved(at, 1))
        if row["effect"] != "template_write" or not row["sentinel"]:
            continue
        writes.append({"host": row["pod"] or "unknown", "action_id": row["action_id"], "kind": "write",
                       "marker": row["marker"], "template_changed": 1, "state": "committed",
                       "resource_version": row["resource_version"], "decided_at": at, "source": "audit"})
    if problems:
        writes.append(unobserved(0.0, "some"))
    return writes


def first_event_at(events):
    """The earliest stageTimestamp in a parsed log (RFC 3339 UTC), or None when no event has one."""
    instants = [value for value in (_instant(event.get("stageTimestamp")) for event in events
                                    if isinstance(event.get("stageTimestamp"), str)) if value is not None]
    if not instants:
        return None
    return datetime.fromtimestamp(min(instants), UTC).isoformat().replace("+00:00", "Z")


def completeness(rows, *, injected, generations, first_at, window_start, target=TARGET_REF):
    """Why the extract may not hold every write of the run; [] when nothing says so.

    rows are the run's sanitised rows; injected is whether the operator wrote
    the fault this run; generations is (before, after) of the target's
    metadata.generation, read before the injection and after the restore;
    first_at is the earliest event in every audit file read (first_event_at)
    and window_start the start of the extract window, both on the API
    server's clock. Four checks: the operator's injection is in the extract
    (a positive control: an empty or shifted window fails it); the
    generation did not go down (the target was not recreated); every
    generation the target gained is the generation stored by one change in
    the extract (template write, fence or seal, other metadata write, by
    anyone), so a change the extract missed, or holds without its response,
    is found; and the log reaches back to the window's start, so nothing
    rotated out. A no-op patch raises no generation and needs no row.
    """
    namespace, name = target
    own = [row for row in rows if row["namespace"] == namespace and row["name"] == name]
    problems = []
    if injected and not any(row["effect"] == "template_write" and not row["sentinel"] for row in own):
        problems.append("the operator's fault injection is not in the extract (positive control)")
    before, after = generations if generations else (None, None)
    if type(before) is not int or type(after) is not int:
        problems.append("the target's generation was not read before and after the run")
    elif after < before:
        problems.append(f"the target's generation went from {before} to {after}: it was recreated")
    else:
        seen = {row["generation"] for row in own if row["effect"] in STORED and type(row["generation"]) is int}
        missing = [value for value in range(before + 1, after + 1) if value not in seen]
        if missing:
            problems.append(f"{len(missing)} of {after - before} generation(s) the target gained have no write "
                            f"in the extract ({', '.join(str(value) for value in missing[:5])})")
    start, first = _instant(window_start), (_instant(first_at) if first_at else None)
    if first is None:
        problems.append("the audit log holds no event")
    elif start is None or first > start:
        problems.append("the audit log starts after the run began: earlier events may have rotated out")
    return problems


def summary(rows):
    """Counts by effect and writer, and the template writes per action, for summary.json."""
    effects = Counter(row["effect"] for row in rows)
    per_action = Counter(row["action_id"] for row in rows if row["effect"] == "template_write" and row["sentinel"])
    operator = sum(1 for row in rows if row["effect"] == "template_write" and not row["sentinel"])
    denied = Counter(f"{row['user']} {row['verb']} {row['resource']}" for row in rows
                     if row["sentinel"] and row["code"] in {401, 403, 422})
    unclassified = Counter("sentinel" if row["sentinel"] else "operator" for row in rows
                           if row["effect"] == "unclassified")
    return {"events": len(rows), "effects": dict(sorted(effects.items())),
            "sentinel_template_writes_per_action": dict(sorted(per_action.items())),
            "max_template_writes_per_action": max(per_action.values(), default=0),
            "sentinel_template_writes": sum(per_action.values()),
            "operator_template_writes": operator, "sentinel_denials": dict(sorted(denied.items())),
            "unclassified": {"sentinel": unclassified["sentinel"], "operator": unclassified["operator"]}}


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
