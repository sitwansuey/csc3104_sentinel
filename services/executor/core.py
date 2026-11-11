"""Restricted ROLLBACK with a durable journal and table-driven reconciliation.

Adapters provide trusted authorization and a conditional Deployment API. Runtime
wiring lives outside this service; there is no shell or relative revision undo.
APPLIED confirms a write, never sustained health or incident recovery.
Reconciliation decisions come from the pure table in reconciliation.py.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from services.executor.reconciliation import ABSENT, ACTION_ANNOTATION, Basis, classify, observe
from services.executor.store import ActionConflict, canonical_json, fingerprint
from shared import log
from shared.contracts import ContractError, validate_message


LOG = log.get_logger("executor")
ESCALATION_NOTE = "Operator action required"

# Fixed sentences per basis; server text is never echoed into results.
_SENTENCES = {
    Basis.EVIDENCE_MATCH: "Exact template and persisted action evidence confirm the write; health verification is pending.",
    Basis.COMMITTED_THEN_OVERWRITTEN: ("This action's write committed, but a later writer changed the Pod template. "
                                       "No write was repeated; obtain a new proposal from current state."),
    Basis.TARGET_DELETED: ("The target Deployment no longer exists, so this conditional write can never commit. "
                           "No write was repeated; obtain a new proposal from current state."),
    Basis.TARGET_REPLACED: ("The target was recreated with another UID, so this conditional write can never commit. "
                            "No write was repeated; obtain a new proposal from current state."),
    Basis.ADVANCED_WITHOUT_EVIDENCE: ("The resource version moved without this action's evidence, so this conditional "
                                      "write can never commit; whether it briefly committed is unknown. "
                                      "No write was repeated; obtain a new proposal from current state."),
    Basis.FENCED: "This action was fenced before its write could commit. No write was repeated.",
    Basis.DEFINITE_REJECTION: "The API server definitely rejected this write. No write was repeated.",
    Basis.UNCHANGED_IN_DOUBT: ("Live state does not confirm this attempt and the resource version is unchanged, "
                               "so a request in flight could still commit. Retain workload exclusion; "
                               "no write was repeated."),
    Basis.READ_UNAVAILABLE: "Write outcome is unknown; current state could not be read. No retry was issued.",
    Basis.INCONSISTENT: ("Live state and the journal are inconsistent. Retain workload exclusion and investigate; "
                         "no write was repeated."),
}


class ConditionalWriteConflict(RuntimeError):
    """Adapter guarantees the write did not commit because a precondition failed."""


class ReadUnavailable(OSError):
    """An adapter explicitly identifies a transient, side-effect-free read error."""


class TargetAbsent(LookupError):
    """The API definitely reports that the named target does not exist."""


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
    # A write still in doubt after this long gets one 'operator action required' result.
    reconcile_escalation_seconds: float = 600.0


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


def _basis(result):
    """Read the basis code that _details writes at the start of result details."""
    details = result.get("details", "") if isinstance(result, dict) else ""
    if isinstance(details, str) and details.startswith("[basis=") and "]" in details:
        return details[len("[basis="):details.index("]")]
    return None


def _details(decision, *, escalated=False, stateless=False):
    sentence = _SENTENCES[decision.basis]
    if decision.basis is Basis.EVIDENCE_MATCH and decision.writer != "own":
        sentence = ("Exact template and this action's evidence from another attempt confirm the write "
                    "(adopted peer write); health verification is pending.")
    elif stateless:
        sentence += " This host had no journaled write; it reconciled from the proposal."
    if escalated:
        sentence += " " + ESCALATION_NOTE + ": the write has been in doubt longer than the configured limit."
    return "[basis=" + decision.basis.value + "] " + sentence


def _escalated(row, adapters, now):
    started = row.get("write_started_at") if row else None
    if not started:
        return False
    began = datetime.fromisoformat(started.replace("Z", "+00:00"))
    return (now - began).total_seconds() >= adapters.reconcile_escalation_seconds


def _record(proposal, adapters, row, decision, *, schedule):
    """Save a decision. Unchanged uncertainty returns the saved result as is.

    A repeated NEEDS_RECONCILIATION with the same version, basis and escalation
    adds no ledger row and no new timestamp, so nothing new is delivered.
    """
    stateless = row is not None and row["attempts"] == 0
    if decision.status != "NEEDS_RECONCILIATION":
        return _finish(proposal, adapters, decision.status, decision.version,
                       _details(decision, stateless=stateless))
    now = _now(adapters)
    escalated = _escalated(row, adapters, now)
    previous = row["result"] if row else None
    if (previous is not None and previous["status"] == "NEEDS_RECONCILIATION"
            and previous["resource_version"] == decision.version
            and _basis(previous) == decision.basis.value
            and (ESCALATION_NOTE in previous["details"]) == escalated):
        result = previous
    else:
        result = _finish(proposal, adapters, decision.status, decision.version,
                         _details(decision, escalated=escalated, stateless=stateless))
    if schedule:
        adapters.ledger.schedule_reconcile(proposal["action_id"], now)
    return result


def _action_id(proposal):
    return proposal.get("action_id") if isinstance(proposal, dict) else None


def _observation(proposal, adapters, snapshot=None):
    if snapshot is not None:
        return observe(snapshot, proposal["target"])
    try:
        return observe(_read(proposal, adapters), proposal["target"])
    except TargetAbsent:
        return ABSENT
    except Exception as error:  # noqa: BLE001 - any read failure leaves the outcome unknown; reconcile later
        log.failure(LOG, "executor.observe.unavailable", error, action_id=_action_id(proposal))
        return None


def _reconcile(proposal, adapters, snapshot=None, *, schedule=True):
    """No mutation, even if state is unchanged: an old request may still commit.

    schedule=False is for the attempt's own first check, which must not delay
    the first later reconcile pass.
    """
    row = adapters.ledger.get(proposal["action_id"])
    decision = classify(row, proposal, _observation(proposal, adapters, snapshot))
    return _record(proposal, adapters, row, decision, schedule=schedule)


def _adopted_write(proposal, adapters, snapshot=None):
    """Finish APPLIED if live state proves this action committed (for example by a peer)."""
    row = adapters.ledger.get(proposal["action_id"])
    decision = classify(row, proposal, _observation(proposal, adapters, snapshot))
    if decision.status != "APPLIED":
        return None
    return _finish(proposal, adapters, "APPLIED", decision.version, _details(decision))


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


def _check_adapters(adapters):
    if type(adapters.read_attempts) is not int or not 1 <= adapters.read_attempts <= 10:
        raise ValueError("read_attempts must be an integer between 1 and 10")
    limit = adapters.reconcile_escalation_seconds
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not 0 < limit < float("inf"):
        raise ValueError("reconcile_escalation_seconds must be a positive finite number")
    _now(adapters)


def reconcile(proposal: dict, adapters: ExecutionAdapters) -> dict:
    """Inspect a write, with no path to a new mutation.

    Runtime restart scans use this even if Coordinator canceled the command.
    A canceled permission cannot erase evidence of a write that already happened.
    A host with no journaled write (another host may own it) reconciles
    statelessly from the proposal and live state.
    """
    proposal = deepcopy(proposal)
    validate_message(proposal)
    if proposal["kind"] != "proposal":
        raise ContractError("Reconciliation requires a proposal")
    _check_adapters(adapters)
    with adapters.coordination.hold(proposal["target"]):
        row = adapters.ledger.get(proposal["action_id"])
        if row is None or row["phase"] == "PREPARED":
            # Journal the action as in doubt here, so this host can never start it.
            row = adapters.ledger.adopt(proposal, _now(adapters))
        if row["fingerprint"] != fingerprint(proposal) or row["proposal"] != proposal:
            raise ActionConflict("Reconciliation proposal differs from the persisted action")
        if row["phase"] == "DONE":
            return row["result"]
        return _reconcile(proposal, adapters)


def unchanged_uncertainty(result: dict, known) -> bool:
    """True when a reconcile result tells Coordinator nothing newer than ``known``.

    ``known`` is Coordinator's latest execution result for the action. It may
    come from another host, so a host's own saved, deduplicated result can be
    older than it. Such a result would be dropped as stale and its reconcile
    command acknowledged, leaving nothing to report a later commit. Callers keep
    the command pending instead.
    """
    if result == known:
        return True
    if not isinstance(known, dict) or result.get("status") != "NEEDS_RECONCILIATION":
        return False
    try:
        mine = datetime.fromisoformat(result["recorded_at"].replace("Z", "+00:00"))
        theirs = datetime.fromisoformat(known["recorded_at"].replace("Z", "+00:00"))
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    return mine <= theirs


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
    _check_adapters(adapters)
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
                # Another host may already have committed this same action.
                adopted = _adopted_write(proposal, adapters, snapshot)
                if adopted is not None:
                    return adopted
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
        except Exception as error:  # noqa: BLE001 - fail closed: any pre-write error refuses the write
            log.failure(LOG, "executor.prewrite.refused", error, action_id=_action_id(proposal))
            return _finish(proposal, adapters, "FAILED", version,
                           "Required state, baseline or fresh authorization is unavailable or invalid; no write was attempted.")

        evidence = {"action_id": proposal["action_id"], "proposal_sha256": row["fingerprint"],
                    "target_uid": proposal["target"]["uid"],
                    "template_sha256": proposal["parameters"]["template_sha256"], "attempt_id": uuid4().hex}
        adapters.ledger.start_write(proposal["action_id"], version, evidence, check.decision, _now(adapters))
        # Journal persistence can take time. Recheck expiry/injected authority at
        # the last local point before the external mutation.
        try:
            adapters.authorizer.assert_usable(check, now=_now(adapters))
        except Exception as error:  # noqa: BLE001 - fail closed: authority must be usable at the last moment
            log.failure(LOG, "executor.authorization.withdrawn", error, action_id=_action_id(proposal))
            return _finish(proposal, adapters, "FAILED", version,
                           "Authorization expired or was withdrawn before the API call; no write was attempted.")
        try:
            applied = adapters.cluster.conditional_restore(
                target=deepcopy(proposal["target"]), version=version,
                template=template, evidence=deepcopy(evidence),
            )
        except ConditionalWriteConflict:
            # Ours did not commit; another attempt of this same action still may have.
            adopted = _adopted_write(proposal, adapters)
            if adopted is not None:
                return adopted
            return _finish(proposal, adapters, "CONFLICT", version,
                           "Conditional write was refused without committing; refresh state and create a new proposal.")
        except Exception as error:  # noqa: BLE001 - an unknown write error may have committed; reconcile
            # Timeouts/transport errors cannot distinguish rejection from commit.
            log.failure(LOG, "executor.write.indeterminate", error, action_id=_action_id(proposal))
            return _reconcile(proposal, adapters, schedule=False)
        # A success response also needs the exact action/template evidence. An
        # invalid or incomplete response triggers a fresh read, never a retry.
        row = adapters.ledger.get(proposal["action_id"])
        if _matches_write(proposal, row, applied):
            return _reconcile(proposal, adapters, applied, schedule=False)
        return _reconcile(proposal, adapters, schedule=False)
