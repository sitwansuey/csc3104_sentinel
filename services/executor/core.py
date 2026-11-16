"""Restricted typed actions (ROLLBACK, RESTART, SCALE) with a durable journal and table-driven reconciliation.

Adapters provide trusted authorization and a conditional Deployment API. Runtime
wiring lives outside this service; there is no shell or relative revision undo.
ROLLBACK and RESTART write one exact Pod template chosen by their handler in
actions.py, through the same conditional write. SCALE writes the reviewed
replica count through a conditional scale write with the same UID and
resourceVersion tests and evidence. APPLIED confirms a write, never sustained
health or incident recovery. Reconciliation decisions come from the pure table
in reconciliation.py, for every action alike.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from services.executor.actions import (
    candidate_template, evidence as action_evidence, implemented, scale_replicas,
)
from services.executor.reconciliation import (
    ABSENT, ACTION_ANNOTATION, Basis, classify, fence_marker, fence_nonce, observe,
)
from services.executor.store import ActionConflict, canonical_json, fingerprint
from shared import log
from shared.contracts import ContractError, validate_message


LOG = log.get_logger("executor")
ESCALATION_NOTE = "Operator action required"
# Minimum spacing between two fences of one action. It is longer than the
# kubectl process timeout (15 s), so an earlier fence has landed or failed.
FENCE_RETRY_SECONDS = 30.0

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
    Basis.FENCED: ("A metadata-only fence conditioned on the same resource version committed first, so this "
                   "write never committed and never can. No write was repeated; obtain a new proposal from "
                   "current state."),
    Basis.DEFINITE_REJECTION: ("The API server definitely rejected this write, or the request was never sent. "
                               "No write was repeated."),
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


class WriteRejected(RuntimeError):
    """This one write request definitely did not commit, and it is never resent (P5.08).

    Only a structured transport raises it (services/executor/rest_adapter.py):
    the API server answered this exact request with a definite 4xx rejection,
    or the connection failed before any byte of the request was sent. status
    is the HTTP status code, or "not_sent"; reason is a short metav1
    StatusReason word, or "Unknown". The server's message is never kept. The
    kubectl transport never raises it, because kubectl's exit code and text
    cannot tell a rejection from a lost response.
    """

    def __init__(self, status, reason="Unknown"):
        # Anything else is a transport bug; the ValueError then makes the write's outcome unknown.
        if status != "not_sent" and not (type(status) is int and 400 <= status <= 499):
            raise ValueError("A definite rejection is a 4xx status or 'not_sent'")
        if not (isinstance(reason, str) and reason.isascii() and reason.isalpha() and len(reason) <= 64):
            reason = "Unknown"
        super().__init__(f"Write definitely not committed (status {status}, reason {reason})")
        self.status, self.reason = status, reason


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
    With fence_after_seconds set, cluster.fence(target, version, marker) must
    commit only the marker annotation under the same UID/version tests.
    ledger follows SQLiteExecutionLedger, including record_fence, confirm_fence
    and fences; classifying a journaled action calls fences() even with fencing off.
    A RESTART also calls restart_history(target), the persisted restart budget input.
    A SCALE calls cluster.autoscalers(target), a read-only list of the
    HorizontalPodAutoscalers that target the Deployment (the Guard escalates when
    it is missing), and cluster.conditional_scale(target, version, replicas,
    evidence), which must commit spec.replicas and evidence together under the
    same UID/version tests, or explicitly refuse the precondition.
    Any write (restore, scale or fence) may raise WriteRejected when the
    transport knows the request definitely did not commit; reconciliation
    still reads state first and state evidence wins.
    """

    ledger: object
    coordination: object
    cluster: object
    authorizer: object
    clock: object = lambda: datetime.now(timezone.utc)
    read_attempts: int = 3
    # A write still in doubt after this long gets one 'operator action required' result.
    reconcile_escalation_seconds: float = 600.0
    # None disables fencing. Otherwise a reconcile pass fences a write still in
    # doubt (same UID, version V0) once it has been in doubt this long. Keep it
    # above the kubectl process timeout so slow but successful writes are not aborted.
    fence_after_seconds: float | None = None


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


def _finish(proposal, adapters, status, version, details, *, conflict=None):
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
    if conflict is not None:
        result["conflict"] = conflict
    validate_message(result)
    return adapters.ledger.finish(proposal["action_id"], result)


def _current(proposal, adapters):
    """A fresh read of the target after a refused write, or None when it cannot be read."""
    try:
        return _read(proposal, adapters)
    except Exception as error:  # noqa: BLE001 - an unread target only withholds the conflict evidence
        log.failure(LOG, "executor.conflict.unread", error, action_id=_action_id(proposal))
        return None


def _conflict(basis, proposal, adapters, snapshot):
    """The evidence a CONFLICT result gives the Coordinator for a re-plan, or None.

    basis is NOT_SENT (this host sent no write: its own read already showed
    another version) or REFUSED (the adapter refused the write's
    preconditions, which it guarantees did not commit). Evidence is given
    only when snapshot, a read made after that, shows by the decision table
    that no write of this action committed or ever can: the same UID at a
    version other than the proposal's, without this action's evidence or
    fence (ADVANCED_WITHOUT_EVIDENCE). It names that version and the
    Deployment's metadata.generation, which the Coordinator compares with the
    incident's (services/coordinator/core.py::replan_after_conflict): equal
    means no spec change at all since the incident, so the version moved by a
    status write. Anything else (unreadable, another UID, a version still at
    the proposal's, evidence of this action, no valid generation) gives None,
    and the CONFLICT stays final.
    """
    if snapshot is None:
        return None
    row = adapters.ledger.get(proposal["action_id"])
    decision = _classify(row, proposal, observe(snapshot, proposal["target"]), adapters)
    deployment = snapshot.get("deployment") if isinstance(snapshot, dict) else None
    metadata = deployment.get("metadata") if isinstance(deployment, dict) else None
    generation = metadata.get("generation") if isinstance(metadata, dict) else None
    if (decision.basis is not Basis.ADVANCED_WITHOUT_EVIDENCE or type(generation) is not int or generation < 1
            or not isinstance(decision.version, str) or len(decision.version) > 256):
        return None
    return {"basis": basis, "resource_version": decision.version, "generation": generation}


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
            and (proposal["action"] != "SCALE"
                 or snapshot["deployment"]["spec"]["replicas"] == proposal["parameters"]["desired_replicas"])
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


def _classify(row, proposal, observation, adapters, rejection=None):
    """Apply the table with the fence nonces this host journaled for the action.

    rejection is a WriteRejected from this attempt's own write, or None.
    """
    fences = adapters.ledger.fences(proposal["action_id"]) if row is not None else []
    return classify(row, proposal, observation, rejection,
                    fence_nonces=frozenset(fence["nonce"] for fence in fences),
                    fence_confirmed=any(fence["committed_version"] is not None for fence in fences))


def _since(stamp, now):
    return (now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()


def _fence_due(decision, row, adapters, now):
    """Fence only a write still in doubt, after the settle delay, at most once per retry interval."""
    settle = adapters.fence_after_seconds
    if not decision.may_fence or settle is None or row is None or not row.get("write_started_at"):
        return False
    fences = adapters.ledger.fences(row["action_id"])
    if not fences:
        return _since(row["write_started_at"], now) >= settle
    return _since(fences[-1]["started_at"], now) >= max(settle, FENCE_RETRY_SECONDS)


def _fence(proposal, adapters):
    """Journal a new nonce, send one fence, then decide again from fresh state.

    The fence tests UID and version V0 like the write. If it commits, the write
    never committed and never can (CONFLICT/FENCED). If it is refused because
    the version moved, or its outcome is unknown, the next read decides.
    """
    action_id, target = proposal["action_id"], proposal["target"]
    version, nonce = proposal["based_on_resource_version"], uuid4().hex
    # Journal first: a fence that lands after a crash is still recognised as ours.
    adapters.ledger.record_fence(action_id, nonce, _now(adapters))
    snapshot = None
    try:
        fenced = adapters.cluster.fence(target=deepcopy(target), version=version,
                                        marker=fence_marker(action_id, nonce))
    except (ConditionalWriteConflict, WriteRejected):
        log.emit(LOG, "executor.fence.refused", action_id=action_id)
    except Exception as error:  # noqa: BLE001 - an unknown fence outcome may have committed; a fresh read decides
        log.failure(LOG, "executor.fence.indeterminate", error, action_id=action_id)
    else:
        observation = observe(deepcopy(fenced), target)
        if (observation is not None and observation.uid == target["uid"] and observation.resource_version != version
                and fence_nonce(observation.fence_annotation, action_id) == nonce):
            adapters.ledger.confirm_fence(action_id, nonce, observation.resource_version)
            log.emit(LOG, "executor.fence.committed", action_id=action_id)
            snapshot = fenced
    row = adapters.ledger.get(action_id)
    return _classify(row, proposal, _observation(proposal, adapters, snapshot), adapters)


def _reconcile(proposal, adapters, snapshot=None, *, schedule=True, rejection=None):
    """No spec mutation, even if state is unchanged: an old request may still commit.

    The only write is a precondition-guarded metadata fence, when fencing is
    enabled and the write has stayed in doubt past the settle delay. schedule=False
    is for the attempt's own first check, which must not delay the first later
    reconcile pass and never fences. rejection (the attempt's own WriteRejected)
    is not journaled: if this read fails, later passes decide without it.
    """
    row = adapters.ledger.get(proposal["action_id"])
    decision = _classify(row, proposal, _observation(proposal, adapters, snapshot), adapters, rejection)
    if schedule and _fence_due(decision, row, adapters, _now(adapters)):
        decision = _fence(proposal, adapters)
        row = adapters.ledger.get(proposal["action_id"])
    return _record(proposal, adapters, row, decision, schedule=schedule)


def _adopted_write(proposal, adapters, snapshot=None):
    """Finish APPLIED if live state proves this action committed (for example by a peer)."""
    row = adapters.ledger.get(proposal["action_id"])
    decision = _classify(row, proposal, _observation(proposal, adapters, snapshot), adapters)
    if decision.status != "APPLIED":
        return None
    return _finish(proposal, adapters, "APPLIED", decision.version, _details(decision))


def _restart_history(proposal, adapters, snapshot):
    """Give the Guard this host's persisted RESTART writes for the target.

    Only a RESTART needs it, so ROLLBACK inputs are unchanged. The ledger is read
    under the target lock, so another local RESTART cannot start in between.
    """
    if proposal["action"] == "RESTART":
        snapshot["restart_history"] = adapters.ledger.restart_history(proposal["target"])
    return snapshot


def _autoscalers(proposal, adapters, snapshot):
    """Give the Guard the HorizontalPodAutoscalers that target a SCALE's Deployment.

    Only a SCALE needs it, so other inputs are unchanged. An adapter without the
    read leaves it out, and the Guard escalates (SCALE_AUTOSCALER_UNKNOWN).
    """
    reader = getattr(adapters.cluster, "autoscalers", None)
    if proposal["action"] == "SCALE" and reader is not None:
        snapshot["autoscalers"] = deepcopy(reader(deepcopy(proposal["target"])))
    return snapshot


def _check_adapters(adapters):
    if type(adapters.read_attempts) is not int or not 1 <= adapters.read_attempts <= 10:
        raise ValueError("read_attempts must be an integer between 1 and 10")
    limit = adapters.reconcile_escalation_seconds
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not 0 < limit < float("inf"):
        raise ValueError("reconcile_escalation_seconds must be a positive finite number")
    settle = adapters.fence_after_seconds
    if settle is not None and (isinstance(settle, bool) or not isinstance(settle, (int, float))
                               or not 0 <= settle < float("inf")):
        raise ValueError("fence_after_seconds must be None or a non-negative finite number")
    _now(adapters)


def reconcile(proposal: dict, adapters: ExecutionAdapters) -> dict:
    """Inspect a write, with no path to a spec mutation.

    At most it sends a precondition-guarded metadata fence (see _fence).

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


def settle(proposal: dict, adapters: ExecutionAdapters) -> dict | None:
    """Close this host's in-doubt ledger row when live state alone decides it; never write.

    A host that crashed during a write keeps the action WRITE_STARTED. If a
    peer then finishes the action, no command for it reaches this host again
    (P3.01 S1b, S1c), so a restarted worker runs this over its in-doubt rows.
    The same decision table as reconcile() is used, but nothing is sent: no
    patch and no fence. A row whose target still has the same UID and version
    V0 with no evidence of the action stays in doubt and backs off; its own
    command, or a Coordinator seal (seal()), moves it on. Return the result
    saved for a closed row, else None.
    """
    proposal = deepcopy(proposal)
    validate_message(proposal)
    if proposal["kind"] != "proposal":
        raise ContractError("Settling requires a proposal")
    _check_adapters(adapters)
    with adapters.coordination.hold(proposal["target"]):
        row = adapters.ledger.get(proposal["action_id"])
        if row is None or row["phase"] not in {"WRITE_STARTED", "UNCERTAIN"}:
            return None
        if row["fingerprint"] != fingerprint(proposal) or row["proposal"] != proposal:
            raise ActionConflict("Settled proposal differs from the persisted action")
        decision = _classify(row, proposal, _observation(proposal, adapters), adapters)
        if decision.status == "NEEDS_RECONCILIATION":
            adapters.ledger.schedule_reconcile(proposal["action_id"], _now(adapters))
            return None
        log.emit(LOG, "executor.ledger.settled", action_id=proposal["action_id"], status=decision.status,
                 basis=decision.basis.value)
        return _record(proposal, adapters, row, decision, schedule=False)


# Fence attempts of one seal before it gives up with UNSEALED, which the Coordinator sends to review.
SEAL_ATTEMPTS = 5
_LANDED = frozenset({Basis.EVIDENCE_MATCH, Basis.COMMITTED_THEN_OVERWRITTEN})
_CANNOT_COMMIT = frozenset({Basis.TARGET_DELETED, Basis.TARGET_REPLACED, Basis.ADVANCED_WITHOUT_EVIDENCE,
                            Basis.FENCED})


def _fence_result(proposal, epoch, outcome, version, adapters):
    result = {"schema_version": "1.0", "kind": "fence_result", "incident_id": proposal["incident_id"],
              "action_id": proposal["action_id"], "target": deepcopy(proposal["target"]), "epoch": epoch,
              "outcome": outcome, "resource_version": version,
              "recorded_at": _now(adapters).isoformat().replace("+00:00", "Z")}
    validate_message(result)
    return result


def seal(proposal: dict, epoch: int, adapters: ExecutionAdapters, *, check=None) -> dict:
    """Make sure no write of an abandoned action can commit any more; return a fence_result.

    The Coordinator commands this when it withdraws an executor.request that
    some Executor may still be executing (P3.04). Every write of the action is
    a JSON Patch that tests UID and resourceVersion == V0, the proposal's
    based_on_resource_version. Each pass reads the target:

    - this action's evidence is on it: the write landed; APPLIED (for review);
    - the target is gone, replaced or past V0 without that evidence: no such
      patch can ever commit; SEALED, with nothing sent;
    - same UID at V0: send a metadata-only fence with the same tests and a
      marker holding a fresh nonce and the epoch (fence()). If it commits,
      the version has left V0 for good: SEALED. If it is refused or its
      outcome is unknown, the next pass reads again.

    After SEAL_ATTEMPTS passes it gives up: UNSEALED. A read failure raises,
    so the command is retried and the workload stays blocked. check, when
    given, runs before each fence (the remote worker passes its lease check).
    This host's ledger is neither read nor changed.
    """
    proposal = deepcopy(proposal)
    validate_message(proposal)
    if proposal["kind"] != "proposal":
        raise ContractError("A seal requires a proposal")
    if type(epoch) is not int or epoch < 1:
        raise ValueError("A seal needs the positive ownership epoch of its command")
    _check_adapters(adapters)
    action_id, target, base = proposal["action_id"], proposal["target"], proposal["based_on_resource_version"]
    nonces = set()
    version = None
    with adapters.coordination.hold(target):
        for _attempt in range(SEAL_ATTEMPTS):
            try:
                observation = observe(_read(proposal, adapters), target)
            except TargetAbsent:
                observation = ABSENT
            decision = classify(None, proposal, observation, fence_nonces=frozenset(nonces))
            version = decision.version
            if decision.basis in _LANDED:
                log.emit(LOG, "executor.seal.write_found", level=log.WARNING, action_id=action_id, fence_epoch=epoch)
                return _fence_result(proposal, epoch, "APPLIED", version, adapters)
            if decision.basis in _CANNOT_COMMIT:
                log.emit(LOG, "executor.seal.sealed", action_id=action_id, fence_epoch=epoch,
                         basis=decision.basis.value)
                return _fence_result(proposal, epoch, "SEALED", version, adapters)
            if decision.basis is not Basis.UNCHANGED_IN_DOUBT:
                continue  # READ_UNAVAILABLE (an unusable snapshot) or INCONSISTENT: read again.
            nonce = uuid4().hex
            nonces.add(nonce)  # Before sending, so a fence whose reply is lost is still recognised.
            if check is not None:
                check()
            try:
                adapters.cluster.fence(target=deepcopy(target), version=base,
                                       marker=fence_marker(action_id, nonce, epoch))
            except (ConditionalWriteConflict, WriteRejected):
                log.emit(LOG, "executor.seal.refused", action_id=action_id, fence_epoch=epoch)
            except Exception as error:  # noqa: BLE001 - an unknown fence outcome may have committed; the next read decides
                log.failure(LOG, "executor.seal.indeterminate", error, action_id=action_id, fence_epoch=epoch)
    log.emit(LOG, "executor.seal.exhausted", level=log.WARNING, action_id=action_id, fence_epoch=epoch)
    return _fence_result(proposal, epoch, "UNSEALED", version, adapters)


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
        if not implemented(proposal):
            return _finish(proposal, adapters, "FAILED", None,
                           "Only explicit baseline ROLLBACK, RESTART and SCALE are implemented.")

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
                               "Target identity or resource version changed; obtain a new proposal and validation.",
                               conflict=_conflict("NOT_SENT", proposal, adapters, snapshot))
            live_state = _autoscalers(proposal, adapters, _restart_history(proposal, adapters, snapshot))
            check = adapters.authorizer.validate(deepcopy(proposal), live_state, now=_now(adapters))
            validate_message(check.decision)
            adapters.ledger.record_validation(proposal["action_id"], check.decision)
            if check.decision["verdict"] != "ALLOW":
                return _finish(proposal, adapters, "FAILED", version,
                               "Fresh Guard validation refused execution: " + ", ".join(check.decision["reason_codes"]))
            if proposal["action"] == "SCALE":
                template, replicas = None, scale_replicas(proposal, snapshot["deployment"], check.baseline)
            else:
                template, replicas = candidate_template(
                    proposal, snapshot["deployment"]["spec"]["template"], check.baseline), None
            adapters.authorizer.assert_usable(check, now=_now(adapters))
        except Exception as error:  # noqa: BLE001 - fail closed: any pre-write error refuses the write
            log.failure(LOG, "executor.prewrite.refused", error, action_id=_action_id(proposal))
            return _finish(proposal, adapters, "FAILED", version,
                           "Required state, baseline or fresh authorization is unavailable or invalid; no write was attempted.")

        evidence = action_evidence(proposal, row["fingerprint"], uuid4().hex)
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
            if replicas is not None:
                applied = adapters.cluster.conditional_scale(
                    target=deepcopy(proposal["target"]), version=version,
                    replicas=replicas, evidence=deepcopy(evidence),
                )
            else:
                applied = adapters.cluster.conditional_restore(
                    target=deepcopy(proposal["target"]), version=version,
                    template=template, evidence=deepcopy(evidence),
                )
        except ConditionalWriteConflict:
            # Ours did not commit; another attempt of this same action still may have.
            current = _current(proposal, adapters)
            adopted = _adopted_write(proposal, adapters, current)
            if adopted is not None:
                return adopted
            return _finish(proposal, adapters, "CONFLICT", version,
                           "Conditional write was refused without committing; refresh state and create a new proposal.",
                           conflict=_conflict("REFUSED", proposal, adapters, current))
        except WriteRejected as rejection:
            # This one request definitely did not commit and is never resent. One fresh read still
            # decides: a peer's committed write of this action is APPLIED, a moved version is CONFLICT
            # and an unchanged one FAILED (rows R3, R5 and R6), with no fence and no settle delay.
            # The transport already logged the status and reason (executor.rest.rejected); only the class here.
            log.failure(LOG, "executor.write.rejected", rejection, action_id=_action_id(proposal))
            return _reconcile(proposal, adapters, schedule=False, rejection=rejection)
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
