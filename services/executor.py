"""Restricted ROLLBACK with a durable journal and conservative reconciliation.

Adapters provide trusted authorization and a conditional Deployment API. Runtime
wiring lives outside this service; there is no shell or relative revision undo.
APPLIED confirms a write, never sustained health or incident recovery.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from services.executor_store import ActionConflict, canonical_json, fingerprint
from shared.contracts import ContractError, validate_message


ACTION_ANNOTATION = "sentinel.io/execution"


class ConditionalWriteConflict(RuntimeError):
    """Adapter guarantees the write did not commit because a precondition failed."""


class ReadUnavailable(OSError):
    """An adapter explicitly identifies a transient, side-effect-free read error."""


def encode_evidence(evidence):
    return canonical_json(evidence)


@dataclass
class GuardCheck:
    decision: dict
    baseline: dict | None
    inputs: dict


@dataclass
class ExecutionAdapters:
    """Injected boundaries; the offline implementation needs no cluster client.

    cluster.read(target) returns a Guard snapshot; conditional_restore accepts
    target/version/template/evidence and returns the resulting snapshot. It must
    commit template and evidence together, or explicitly refuse the precondition.
    authorizer.validate returns GuardCheck; assert_usable checks its full binding
    and expiry. Runtime may add current lifecycle checks to that boundary.
    """

    ledger: object
    coordination: object
    cluster: object
    authorizer: object
    clock: object = lambda: datetime.now(timezone.utc)
    read_attempts: int = 3


def _now(adapters):
    value = adapters.clock()
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("Executor clock must return a timezone-aware UTC datetime")
    return value


def _read(proposal, adapters):
    for attempt in range(adapters.read_attempts):
        try:
            return deepcopy(adapters.cluster.read(deepcopy(proposal["target"])))
        except ReadUnavailable:
            if attempt + 1 == adapters.read_attempts:
                raise


def _version(snapshot):
    metadata = snapshot["deployment"]["metadata"]
    value = metadata["resourceVersion"]
    if not isinstance(value, str) or not value.strip():
        raise ContractError("Deployment resource version is unavailable")
    return value


def _identity(snapshot):
    deployment = snapshot["deployment"]
    metadata = deployment["metadata"]
    return {"cluster_id": snapshot["cluster_id"], "kind": deployment["kind"],
            "namespace": metadata["namespace"], "name": metadata["name"], "uid": metadata["uid"]}


def _finish(proposal, adapters, status, version, details):
    # Coordinator accepts only newer follow-up results. Preserve exact results on
    # DONE replay; advance a fixed or coarse clock for a new reconciliation result.
    row = adapters.ledger.get(proposal["action_id"])
    recorded = _now(adapters)
    if row["result"] is not None:
        previous = datetime.fromisoformat(row["result"]["recorded_at"].replace("Z", "+00:00"))
        if recorded <= previous:
            recorded = previous + timedelta(microseconds=1)
    result = {"schema_version": "1.0", "kind": "execution_result",
              "incident_id": proposal["incident_id"], "action_id": proposal["action_id"],
              "target": deepcopy(proposal["target"]), "status": status,
              "resource_version": version, "recorded_at": recorded.isoformat().replace("+00:00", "Z"),
              "details": details}
    validate_message(result)
    return adapters.ledger.finish(proposal["action_id"], result)


def _matches_write(proposal, row, snapshot):
    """Desired-state equality alone cannot prove who applied it."""
    try:
        metadata = snapshot["deployment"]["metadata"]
        annotations = metadata.get("annotations", {})
        if not isinstance(annotations, dict):
            return False
        return (
            _identity(snapshot) == proposal["target"]
            and _version(snapshot) != row["expected_version"]
            and annotations.get(ACTION_ANNOTATION) == encode_evidence(row["evidence"])
            and fingerprint(snapshot["deployment"]["spec"]["template"]) == proposal["parameters"]["template_sha256"]
        )
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        return False


def _reconcile(proposal, adapters, snapshot=None):
    """No mutation, even if state is unchanged: an old request may still commit."""
    row = adapters.ledger.get(proposal["action_id"])
    if snapshot is None:
        try:
            snapshot = _read(proposal, adapters)
        except Exception:
            return _finish(proposal, adapters, "NEEDS_RECONCILIATION", None,
                           "Write outcome is unknown; current state could not be read. No retry was issued.")
    if _matches_write(proposal, row, snapshot):
        return _finish(proposal, adapters, "APPLIED", _version(snapshot),
                       "Exact template and persisted action evidence confirm the write; health verification is pending.")
    try:
        version = _version(snapshot)
    except (KeyError, TypeError, ValueError):
        version = None
    return _finish(proposal, adapters, "NEEDS_RECONCILIATION", version,
                   "Live state does not confirm this attempt. Retain workload exclusion and investigate; no write was repeated.")


def _template(proposal, baseline):
    if not isinstance(baseline, dict):
        raise ContractError("Stored baseline is unavailable")
    try:
        template = deepcopy(baseline["pod_template"])
        digest = fingerprint(template)
        valid = (baseline["target"] == proposal["target"]
                 and baseline["baseline_id"] == proposal["parameters"]["baseline_id"]
                 and baseline["template_sha256"] == digest == proposal["parameters"]["template_sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("Stored baseline identity or hash is invalid") from exc
    if not valid or not isinstance(template, dict):
        raise ContractError("Stored baseline identity or hash does not match the proposal")
    return template


def reconcile(proposal: dict, adapters: ExecutionAdapters) -> dict:
    """Inspect an existing journaled write, with no path to a new mutation.

    Runtime restart scans use this even if Coordinator canceled the command.
    A canceled permission cannot erase evidence of a write that already happened.
    """
    proposal = deepcopy(proposal)
    validate_message(proposal)
    if proposal["kind"] != "proposal":
        raise ContractError("Reconciliation requires a proposal")
    if type(adapters.read_attempts) is not int or not 1 <= adapters.read_attempts <= 10:
        raise ValueError("read_attempts must be an integer between 1 and 10")
    _now(adapters)
    with adapters.coordination.hold(proposal["target"]):
        row = adapters.ledger.get(proposal["action_id"])
        if row is None or row["phase"] == "PREPARED":
            raise ValueError("No journaled write exists to reconcile")
        if row["fingerprint"] != fingerprint(proposal) or row["proposal"] != proposal:
            raise ActionConflict("Reconciliation proposal differs from the persisted action")
        if row["phase"] == "DONE":
            return row["result"]
        return _reconcile(proposal, adapters)


def execute(proposal: dict, adapters: ExecutionAdapters) -> dict:
    """Execute once, replay a completed result, or reconcile an uncertain attempt.

    TargetBusy leaves delivery pending. ActionConflict rejects reused IDs with
    changed intent. BaseException (process death/cancellation) deliberately escapes:
    committed WRITE_STARTED records force read-only reconciliation on restart.
    """
    proposal = deepcopy(proposal)
    validate_message(proposal)
    if proposal["kind"] != "proposal":
        raise ContractError("Executor requires a proposal")
    if type(adapters.read_attempts) is not int or not 1 <= adapters.read_attempts <= 10:
        raise ValueError("read_attempts must be an integer between 1 and 10")
    _now(adapters)
    with adapters.coordination.hold(proposal["target"]):
        row = adapters.ledger.prepare(proposal)
        if row["phase"] == "DONE":
            return row["result"]
        if row["phase"] in {"WRITE_STARTED", "UNCERTAIN"}:
            return _reconcile(proposal, adapters)
        if proposal["action"] != "ROLLBACK":
            return _finish(proposal, adapters, "FAILED", None, "Only explicit baseline ROLLBACK is implemented.")

        version = None
        try:
            snapshot = _read(proposal, adapters)
            version = _version(snapshot)
            if _identity(snapshot) != proposal["target"] or version != proposal["based_on_resource_version"]:
                return _finish(proposal, adapters, "CONFLICT", version,
                               "Target identity or resource version changed; obtain a new proposal and validation.")
            check = adapters.authorizer.validate(deepcopy(proposal), snapshot, now=_now(adapters))
            validate_message(check.decision)
            adapters.ledger.record_validation(proposal["action_id"], check.decision)
            if check.decision["verdict"] != "ALLOW":
                return _finish(proposal, adapters, "FAILED", version,
                               "Fresh Guard validation refused execution: " + ", ".join(check.decision["reason_codes"]))
            template = _template(proposal, check.baseline)
            adapters.authorizer.assert_usable(check, now=_now(adapters))
        except Exception:
            return _finish(proposal, adapters, "FAILED", version,
                           "Required state, baseline or fresh authorization is unavailable or invalid; no write was attempted.")

        evidence = {"action_id": proposal["action_id"], "proposal_sha256": row["fingerprint"],
                    "target_uid": proposal["target"]["uid"],
                    "template_sha256": proposal["parameters"]["template_sha256"], "attempt_id": uuid4().hex}
        adapters.ledger.start_write(proposal["action_id"], version, evidence, check.decision)
        # Journal persistence can take time. Recheck expiry/injected authority at
        # the last local point before the external mutation.
        try:
            adapters.authorizer.assert_usable(check, now=_now(adapters))
        except Exception:
            return _finish(proposal, adapters, "FAILED", version,
                           "Authorization expired or was withdrawn before the API call; no write was attempted.")
        try:
            applied = adapters.cluster.conditional_restore(
                target=deepcopy(proposal["target"]), version=version,
                template=template, evidence=deepcopy(evidence),
            )
        except ConditionalWriteConflict:
            return _finish(proposal, adapters, "CONFLICT", version,
                           "Conditional write was refused without committing; refresh state and create a new proposal.")
        except Exception:
            # Timeouts/transport errors cannot distinguish rejection from commit.
            return _reconcile(proposal, adapters)
        # A success response also needs the exact action/template evidence. An
        # invalid or incomplete response triggers a fresh read, never a retry.
        row = adapters.ledger.get(proposal["action_id"])
        if _matches_write(proposal, row, applied):
            return _reconcile(proposal, adapters, applied)
        return _reconcile(proposal, adapters)
