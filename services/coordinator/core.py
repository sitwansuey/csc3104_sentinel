"""SQLite lifecycle and durable at-least-once local routing.

Routing envelopes are local commands, not shared wire messages or permission to
write Kubernetes state. The executor must still obtain fresh Guard validation.
"""

from copy import deepcopy
import json

from shared import log, tracing
from shared.actions import STALE_READ_SPEC
from shared.contracts import validate_message
from services.coordinator.store import (
    CoordinatorError, LOG, SQLiteStore, SupersededResult, _digest, _json, _require, _time,
)
from services.executor.delivery_store import _ensure_reviews, _record_review

# The requests each worker result kind answers (runtime/worker_transport.py::TOPICS).
ANSWERS = {"dependency_result": ("dependency.request",), "guard_result": ("guard.request",),
           "execution_result": ("executor.request", "executor.reconcile"), "outcome": ("verifier.request",),
           "fence_result": ("executor.seal",)}


class LocalEventBus:
    """Queued envelopes. Consumers acknowledge durable event IDs after success.

    Drain outside coordinator transactions. A failing subscriber leaves its
    envelope queued; earlier successful subscribers may see it again on retry.
    """

    def __init__(self):
        self.events = []
        self._handlers = {}

    def subscribe(self, topic, handler):
        self._handlers.setdefault(topic, []).append(handler)

    def publish(self, event):
        if not any(item["event_id"] == event["event_id"] for item in self.events):
            self.events.append(deepcopy(event))

    def cancel(self, event_id):
        self.events[:] = [event for event in self.events if event["event_id"] != event_id]

    def drain(self):
        while self.events:
            event = self.events[0]
            handlers = self._handlers.get(event["topic"], [])
            if not handlers:
                return
            for handler in handlers:
                handler(deepcopy(event))
            self.cancel(event["event_id"])


def _event_key(message):
    identity = message.get("action_id", message["incident_id"])
    # Result retries preserve their timestamps; each observation is immutable.
    stamp = next((message[key] for key in ("checked_at", "recorded_at", "window_end")
                  if key in message), "")
    return _json([message["kind"], identity, stamp])


def _superseded_unless(condition, reason):
    if not condition:
        raise SupersededResult(reason)


def _issued_after(store, answers, kind, action_id):
    """True when request answers was issued after the newest accepted kind for the action was applied."""
    meta = store.accepted_meta(kind, action_id)
    return meta is None or answers > (meta["high_water"] or 0)


def _answers_current_request(message, store, answers):
    """Sequence mode: a result must answer the newest request of its kind for the
    action, issued after the previous result of the same kind was accepted."""
    kind, action_id = message["kind"], message["action_id"]
    _superseded_unless(answers == store.newest_request(action_id, *ANSWERS[kind]),
                       "Result answers a superseded request")
    _superseded_unless(_issued_after(store, answers, kind, action_id),
                       "Result answers a request issued before the stored result")


SEAL_REVIEW_REASONS = {
    "APPLIED": "The seal found this action's write on the target: it landed after the action was abandoned",
    "UNSEALED": "The seal could not fence the target within its bounded attempts; the workload stays blocked",
}


def _apply_fence_result(message, store, answers):
    """Apply an Executor's answer to executor.seal (P3.04).

    SEALED: no write of the action can commit any more, so the workload is
    released; an action that had not finished becomes FAILED, because the
    resource version it was planned on is gone. APPLIED: the abandoned write
    landed, so it is saved for review and the incident moves to NEEDS_REVIEW,
    never hidden. UNSEALED: the same review, and the workload stays blocked.
    A result whose epoch is not the workload's current one is refused.
    """
    incident_id, action_id = message["incident_id"], message["action_id"]
    action = store.action(action_id)
    _require(action is not None and action["incident_id"] == incident_id, "Seal result does not match an action")
    if answers is not None:
        _answers_current_request(message, store, answers)
    owner = store.target_ownership(message["target"])
    _require(owner is not None and owner["seal_required"] and owner["seal_action_id"] == action_id,
             "No seal is outstanding for this action")
    _superseded_unless(message["epoch"] == owner["epoch"], "Seal result carries a stale ownership epoch")
    outcome = message["outcome"]
    store.finish_seal(message["target"], message["epoch"], release=outcome != "UNSEALED")
    if outcome == "SEALED":
        if action["state"] in {"VALIDATING", "AWAITING_APPROVAL", "READY"}:
            store.set_state(incident_id, action_id, "FAILED")
        return
    _ensure_reviews(store)
    _record_review(store, answers or 0, message, SEAL_REVIEW_REASONS[outcome])
    store.set_state(incident_id, action_id, "NEEDS_REVIEW")
    store.enqueue("incident.review", {"fence": message}, action_id)


def _apply(message, store, answers=None, producer=None):
    """Apply one message. answers is the outbox id of the request a worker result
    answers. With it, lifecycle order uses Coordinator sequence numbers only
    (sequence mode); without it, the producers' timestamps (single-clock mode)."""
    kind, incident_id = message["kind"], message["incident_id"]
    if kind == "incident":
        store.add_incident(incident_id, message["target"], "OPEN")
        store.enqueue("planner.request", {"incident": message})
        return
    incident = store.incident(incident_id)
    _require(incident is not None, "Incident must be recorded first")
    _require(incident["target"] == message["target"], "Incident target mismatch")
    action_id = message["action_id"]
    if kind == "fence_result":
        _apply_fence_result(message, store, answers)
        return
    if kind == "proposal":
        _require(incident["state"] in {"OPEN", "REJECTED", "FAILED"},
                 "Only one active action is permitted; recovered incidents cannot reopen")
        _require(store.action(action_id) is None, "Action ID already belongs to a proposal")
        _require(store.planning_blocker(incident_id) is None,
                 "Another incident is recovering this workload; proposal must wait")
        # One RESTART per incident: after a REJECTED or FAILED restart, restarting
        # again would loop. The Guard's per-target budget covers other incidents.
        _require(message["action"] != "RESTART" or not any(
                     action["proposal"]["action"] == "RESTART" for action in store.actions_for_incident(incident_id)),
                 "Restart budget for this incident is exhausted; ESCALATE instead")
        next_state = "NEEDS_REVIEW" if message["action"] == "ESCALATE" else "VALIDATING"
        store.add_action(action_id, incident_id, message, next_state)
        store.activate_action(incident_id, action_id, next_state)
        store.enqueue("incident.review" if next_state == "NEEDS_REVIEW" else "guard.request",
                      {"proposal": message}, action_id)
        return
    action = store.action(action_id)
    _require(action is not None and action["incident_id"] == incident_id
             and incident["active_action_id"] == action_id, "Result does not match active action")
    state = action["state"]
    sequenced = answers is not None and kind in ANSWERS
    if sequenced:
        _answers_current_request(message, store, answers)
    if kind == "dependency_result":
        _require(state in {"VALIDATING", "AWAITING_APPROVAL", "READY"},
                 "Dependency result arrived out of order")
        previous = store.accepted_meta("dependency_result", action_id)
        previous_finding = previous and previous["message"]
        # Sequence mode was checked above. Otherwise timestamps are compared only
        # from one producer (one clock); across producers, acceptance order decides.
        if previous_finding and not sequenced and previous["producer"] == producer:
            _require(_time(message["checked_at"]) > _time(previous_finding["checked_at"]),
                "Dependency result must be newer than stored evidence")
        if state == "READY":
            # Revoke queued execution before accepting changed evidence. The
            # executor also rechecks current lifecycle before a conditional write.
            store.set_state(incident_id, action_id, "VALIDATING")
        if state == "READY" or previous_finding or action["guard_result"] or store.required_dependency_hash(action_id):
            # Keep this requirement after escalation/approval too: a delayed
            # decision must not restore READY using the superseded evidence.
            store.require_dependency_revalidation(action_id, _digest(message))
        store.enqueue("guard.request", {"proposal": action["proposal"], "dependencies": message,
                                       "approval": store.approval(action_id)}, action_id)
    elif kind == "guard_result":
        _require(state in {"VALIDATING", "AWAITING_APPROVAL", "READY"},
                 "Guard result arrived after action completion or rejection")
        previous = action["guard_result"]
        if previous and not sequenced:
            _require(_time(message["checked_at"]) > _time(previous["checked_at"]),
                     "Guard result must be newer than the stored decision")
        if message["verdict"] == "ALLOW":
            _require(message["validated_resource_version"] == action["proposal"]["based_on_resource_version"],
                     "Guard allowed a different resource version")
            if sequenced:
                _superseded_unless(_issued_after(store, answers, "dependency_result", action_id),
                                   "Guard ALLOW answers a request issued before the latest dependency evidence")
            else:
                latest = store.dependency_result(action_id)
                if latest:
                    _require(_time(message["checked_at"]) >= _time(latest["checked_at"]),
                             "Guard ALLOW predates the latest dependency evidence")
            if message["schema_version"] == "2.0":
                _require(message["proposal_sha256"] == _digest(action["proposal"]),
                         "Guard allowed a different proposal")
            required = store.required_dependency_hash(action_id)
            if required:
                report = store.guard_report(_digest(message), action_id) or {}
                _require(message["schema_version"] == "2.0"
                         and isinstance(report, dict)
                         and report.get("decision_sha256") == _digest(message)
                         and report.get("proposal_sha256") == message["proposal_sha256"]
                         and report.get("inputs_sha256") == message["inputs_sha256"]
                         and isinstance(report.get("input_fingerprints"), dict)
                         and report["input_fingerprints"].get("dependencies") == required,
                         "Dependency revalidation requires a Guard report bound to the latest evidence")
            if store.has_managed_approval(action_id):
                approval = store.current_approval(action_id, managed_only=True)
                report = store.guard_report(_digest(message), action_id) or {}
                _require(approval is not None and message["schema_version"] == "2.0"
                         and approval["proposal_sha256"] == _digest(action["proposal"])
                         and approval["policy_version"] == message["policy_version"]
                         and report.get("decision_sha256") == _digest(message)
                         and report.get("proposal_sha256") == _digest(action["proposal"])
                         and report.get("inputs_sha256") == message["inputs_sha256"]
                         and report.get("input_fingerprints", {}).get("approval") == _digest(approval),
                         "Guard ALLOW requires the current authenticated approval and bound report")
        next_state = {"ALLOW": "READY", "REJECT": "REJECTED", "ESCALATE": "AWAITING_APPROVAL"}[message["verdict"]]
        store.save_guard_result(action_id, message)
        store.set_state(incident_id, action_id, next_state)
        if next_state == "READY":
            store.enqueue("executor.request", {"proposal": action["proposal"], "guard_result": message,
                                              "approval": store.approval(action_id)}, action_id)
    elif kind == "execution_result":
        _require(state in {"READY", "NEEDS_RECONCILIATION"}, "Execution requires an allowed active action")
        guard = action["guard_result"]
        _require(guard and guard["verdict"] == "ALLOW", "Execution requires Guard ALLOW")
        if sequenced:
            _superseded_unless(_issued_after(store, answers, "guard_result", action_id),
                               "Execution answers a request issued before the current Guard decision")
        else:
            _require(_time(message["recorded_at"]) >= _time(guard["checked_at"]),
                     "Execution predates Guard validation")
            if action["execution_result"]:
                _require(_time(message["recorded_at"]) > _time(action["execution_result"]["recorded_at"]),
                         "Execution result is older than the stored result")
        status = message["status"]
        # SUPERSEDED: the write can no longer commit. Fail and replan from live state.
        next_state = {"APPLIED": "VERIFYING", "NOOP": "VERIFYING", "FAILED": "FAILED",
                      "CONFLICT": "FAILED", "SUPERSEDED": "FAILED",
                      "NEEDS_RECONCILIATION": "NEEDS_RECONCILIATION"}[status]
        if next_state == "VERIFYING":
            _require(message["resource_version"] is not None, "Successful execution needs a resource version")
        store.save_execution_result(action_id, message)
        store.set_state(incident_id, action_id, next_state)
        if next_state == "VERIFYING":
            store.enqueue("verifier.request", {"execution": message, "proposal": action["proposal"]}, action_id)
        elif next_state == "NEEDS_RECONCILIATION":
            store.enqueue("executor.reconcile", {"proposal": action["proposal"], "execution": message}, action_id)
    elif kind == "outcome":
        _require(state == "VERIFYING", "Outcome requires successful execution first")
        if sequenced:
            _superseded_unless(_issued_after(store, answers, "execution_result", action_id),
                               "Outcome answers a request issued before the current execution")
        else:
            _require(_time(message["window_start"]) >= _time(action["execution_result"]["recorded_at"]),
                     "Health window predates execution")
        recovered = message["status"] == "RECOVERED"
        _require(not recovered or all(check["passed"] for check in message["checks"]),
                 "Recovered outcome cannot contain failed health checks")
        next_state = {"RECOVERED": "RECOVERED", "NOT_RECOVERED": "FAILED", "UNKNOWN": "NEEDS_REVIEW"}[message["status"]]
        store.set_state(incident_id, action_id, next_state)
        store.enqueue("incident.outcome", {"outcome": message}, action_id)


STALE_REPLAN_CAUSE = "stale_replan"
CONFLICT_REPLAN_CAUSE = "conflict_replan"
# The most re-plans one incident may get, for both causes together, whatever the configuration asks for.
MAX_STALE_REPLANS = 5


def _replans(store, incident_id):
    """Re-plans the incident has had, either cause: one budget, and attempt n keys the Planner's row n."""
    return sum(store.reopenings(incident_id, cause) for cause in (STALE_REPLAN_CAUSE, CONFLICT_REPLAN_CAUSE))


def _replan_declined(message, store, action):
    """Why a stale-only refusal of the active ROLLBACK must stand; None when a re-plan may be queued.

    A re-plan is only for status churn on the spec the incident observed,
    inside the proposal's first approval cycle:
    - SPEC_CHANGED / SPEC_UNKNOWN: the Guard's report must show the live
      metadata.generation it read equal to the incident's observed_generation.
      A status write moves the resourceVersion and leaves the generation; a
      change to the spec (an operator's fix, a new deploy, another
      controller's write) moves both, and a ROLLBACK planned from the older
      evidence would overwrite it. Without the report's record the refusal
      stands.
    - APPROVAL_REVOKED: the operator withdrew an approval of the action.
      The re-check after a revocation must not turn into new work.
    - ALLOWED_BEFORE / SEALED: the action was ALLOWed once (an Executor
      command existed) or a seal was required for it. Such an action ends
      as P3.04 says: the version it was planned on is gone.
    """
    action_id = message["action_id"]
    report = store.guard_report(_digest(message), action_id) or {}
    records = [check for check in report.get("checks", []) if isinstance(check, dict)
               and check.get("code") == STALE_READ_SPEC] if isinstance(report, dict) else []
    live = records[0].get("actual") if len(records) == 1 else None
    generation = live.get("generation") if isinstance(live, dict) else None
    observed = store.message_payload(_json(["incident", message["incident_id"], ""]))
    _require(observed is not None, "The incident message must be recorded first")
    expected = json.loads(observed)["observed_generation"]
    if type(generation) is not int:
        return "SPEC_UNKNOWN"
    if generation != expected:
        return "SPEC_CHANGED"
    if any(entry["revocation"] for entry in store.approval_history(action_id)):
        return "APPROVAL_REVOKED"
    if store.database.execute("SELECT 1 FROM transitions WHERE action_id=? AND to_state='READY' LIMIT 1",
                              (action_id,)).fetchone():
        return "ALLOWED_BEFORE"
    owner = store.target_ownership(action["proposal"]["target"])
    if (owner is not None and owner["seal_action_id"] == action_id) or store.database.execute(
            "SELECT 1 FROM outbox WHERE topic='executor.seal' AND action_id=? LIMIT 1", (action_id,)).fetchone():
        return "SEALED"
    return None


def replan_after_stale(message, store, *, limit):
    """Queue a re-plan after a Guard REJECT whose only reason is STALE_RESOURCE_VERSION; True when queued.

    The Planner binds a proposal to the incident's observed_resource_version.
    A Planner with no Kubernetes access (the deterministic lab planner) cannot
    read a newer one, and a Deployment's resourceVersion moves with status
    writes the controller makes after the Observer's snapshot: on sentinel-dist
    the Recreate rollout scales up the new ReplicaSet about 30 s after the
    fault, after the incident opened, and again at the progress deadline. The
    Guard then refuses the proposal as stale, before or after an approval
    (docs/distributed-acceptance.md#first-live-attempts).

    The Guard's refusal carries the version it read from the live target
    (validated_resource_version). This sends that version back to the Planner
    in a new planner.request and reopens the incident: the Planner makes a new
    proposal bound to it, with a new action ID, which the Guard checks against
    live state again and which needs its own approval (an approval is bound to
    the proposal's digest). Nothing is retried around the check: the refused
    action stays REJECTED and its decision is kept. Only a ROLLBACK is re-planned
    (a second RESTART for one incident is refused by the restart budget), only
    the incident's active action, only while the live spec is the one the
    incident observed and only inside the action's first approval cycle
    (_replan_declined), and at most limit times per incident (counted durably
    in the transitions journal, shared with replan_after_conflict); otherwise
    the stale REJECT stands. limit is 0 by default (record_message, the worker
    transport and runtime/coordinator_server.py --stale-replans), so nothing
    changes unless the Coordinator is configured for it.
    """
    if (not limit or message["kind"] != "guard_result" or message["verdict"] != "REJECT"
            or message["reason_codes"] != ["STALE_RESOURCE_VERSION"]):
        return False
    incident_id, action_id = message["incident_id"], message["action_id"]
    incident, action = store.incident(incident_id), store.action(action_id)
    if (incident is None or action is None or incident["active_action_id"] != action_id
            or incident["state"] != "REJECTED" or action["state"] != "REJECTED"
            or action["guard_result"] != message or action["proposal"]["action"] != "ROLLBACK"
            or message["validated_resource_version"] == action["proposal"]["based_on_resource_version"]):
        return False
    attempt = _replans(store, incident_id) + 1
    declined = "LIMIT_REACHED" if attempt > min(limit, MAX_STALE_REPLANS) else _replan_declined(message, store, action)
    if declined is not None:
        store.after_commit(lambda: log.emit(LOG, "coordinator.replan.declined", incident_id=incident_id,
                                            action_id=action_id, reason=declined))
        return False
    observed = store.message_payload(_json(["incident", incident_id, ""]))
    store.reopen_incident(incident_id, action_id, STALE_REPLAN_CAUSE)
    store.enqueue("planner.request", {"incident": json.loads(observed), "replan": {
        "attempt": attempt, "after_action_id": action_id,
        "resource_version": message["validated_resource_version"]}})
    store.after_commit(lambda: log.emit(LOG, "coordinator.replan.queued", incident_id=incident_id,
                                        action_id=action_id, attempt=attempt, reason="STALE_RESOURCE_VERSION"))
    return True


def _conflict_replan_declined(message, store, action):
    """Why a CONFLICT of the active ROLLBACK must stand; None when a re-plan may be queued.

    - NO_EVIDENCE: the result does not carry the Executor's conflict record
      (services/executor/core.py::_conflict), which it gives only when its
      own read after the refusal shows that no write of the action committed
      or can commit. A write that went in doubt (reconciled later, FENCED
      included) or landed and was overwritten never carries it.
    - VERSION_UNMOVED: the version read is the proposal's or the incident's.
    - SPEC_CHANGED: the generation the Executor read is not the incident's
      observed_generation, so the spec changed after the incident (an
      operator's fix, a new deploy, or a write of this action); a ROLLBACK
      planned from the older evidence would overwrite it.
    - EARLIER_RESULT: the Executor reported on this action before (for
      example NEEDS_RECONCILIATION): the write was in doubt at some point.
    - APPROVAL_REVOKED / NOT_FIRST_CYCLE: an approval was revoked, or the
      action was approved more than once or ALLOWed more than once; a
      re-plan belongs to the first approval cycle only.
    - SEALED: a seal was required for the action (P3.04).
    """
    action_id, incident_id = message["action_id"], message["incident_id"]
    evidence = message.get("conflict")
    if not isinstance(evidence, dict):
        return "NO_EVIDENCE"
    incident = json.loads(store.message_payload(_json(["incident", incident_id, ""])))
    if evidence["resource_version"] in {action["proposal"]["based_on_resource_version"],
                                        incident["observed_resource_version"]}:
        return "VERSION_UNMOVED"
    if evidence["generation"] != incident["observed_generation"]:
        return "SPEC_CHANGED"
    if store.database.execute("SELECT 1 FROM messages WHERE kind='execution_result' AND action_id=? LIMIT 1",
                        (action_id,)).fetchone():
        return "EARLIER_RESULT"
    approvals = store.approval_history(action_id)
    if any(entry["revocation"] is not None for entry in approvals):
        return "APPROVAL_REVOKED"
    allowed = store.database.execute("SELECT count(*) FROM transitions WHERE action_id=? AND to_state='READY'",
                               (action_id,)).fetchone()[0]
    if len(approvals) > 1 or allowed != 1:
        return "NOT_FIRST_CYCLE"
    owner = store.target_ownership(action["proposal"]["target"])
    if (owner is not None and owner["seal_action_id"] == action_id) or store.database.execute(
            "SELECT 1 FROM outbox WHERE topic='executor.seal' AND action_id=? LIMIT 1", (action_id,)).fetchone():
        return "SEALED"
    return None


def replan_after_conflict(message, store, *, limit):
    """Queue a re-plan after an Executor CONFLICT that provably wrote nothing; True when queued.

    On sentinel-dist the Deployment controller writes the target's status
    (the Recreate rollout, its progress condition) at any moment, so the
    resourceVersion can move between the Guard's ALLOW and the Executor's
    conditional patch, which must then refuse: the Executor's own read
    already shows another version (NOT_SENT), or the adapter's precondition
    is refused (REFUSED). The Executor is right to refuse, but the incident
    ended FAILED with nothing repaired
    (docs/distributed-acceptance.md#third-deployment-recreate-and-conflicts).

    This gives such a CONFLICT the stale re-plan's path: the action stays
    FAILED with its result, the incident goes back to OPEN (journal cause
    conflict_replan) and a planner.request carries the version the Executor
    read, so the Planner makes a new proposal with a new action ID, which the
    Guard checks against live state and the operator approves again. Nothing
    is retried around the Guard, the approval or the Executor's precondition.
    Only a ROLLBACK is re-planned (a RESTART keeps its per-incident budget of
    one), only the incident's active action, only when the Executor's record
    proves nothing was written and the spec is the incident's, and only in the
    action's first approval cycle with no revocation or seal
    (_conflict_replan_declined). limit is the same budget as
    replan_after_stale (--stale-replans), counted over both causes; 0, the
    default, leaves every CONFLICT final as before.
    """
    if (not limit or message["kind"] != "execution_result" or message["status"] != "CONFLICT"):
        return False
    incident_id, action_id = message["incident_id"], message["action_id"]
    incident, action = store.incident(incident_id), store.action(action_id)
    if (incident is None or action is None or incident["active_action_id"] != action_id
            or incident["state"] != "FAILED" or action["state"] != "FAILED"
            or action["execution_result"] != message or action["proposal"]["action"] != "ROLLBACK"):
        return False
    attempt = _replans(store, incident_id) + 1
    declined = ("LIMIT_REACHED" if attempt > min(limit, MAX_STALE_REPLANS)
                else _conflict_replan_declined(message, store, action))
    if declined is not None:
        store.after_commit(lambda: log.emit(LOG, "coordinator.replan.declined", incident_id=incident_id,
                                            action_id=action_id, reason=declined))
        return False
    observed = store.message_payload(_json(["incident", incident_id, ""]))
    store.reopen_incident(incident_id, action_id, CONFLICT_REPLAN_CAUSE)
    store.enqueue("planner.request", {"incident": json.loads(observed), "replan": {
        "attempt": attempt, "after_action_id": action_id,
        "resource_version": message["conflict"]["resource_version"], "cause": "EXECUTOR_CONFLICT"}})
    store.after_commit(lambda: log.emit(LOG, "coordinator.replan.queued", incident_id=incident_id,
                                        action_id=action_id, attempt=attempt, reason="EXECUTOR_CONFLICT"))
    return True


def record_message(message: dict, store: SQLiteStore, *, answers=None, producer=None, stale_replans=0) -> bool:
    """Record within an existing transaction, without committing or publishing.

    Runtime adapters use this to commit a result and its input acknowledgment
    together. Most callers should use handle instead. The worker transport
    passes answers (the outbox id of the request answered) and producer (the
    authenticated identity), so lifecycle order is checked by Coordinator
    sequence numbers instead of the producers' clocks. stale_replans above 0
    lets a Guard refusal for a stale resource version (replan_after_stale), or
    an Executor CONFLICT that provably wrote nothing (replan_after_conflict),
    queue up to that many re-plans per incident in all.
    """
    _require(store.in_transaction, "record_message requires an active transaction")
    validate_message(message)
    payload, key = _json(message), _event_key(message)
    ids = {"incident_id": message["incident_id"], "action_id": message.get("action_id"),
           "message_kind": message["kind"]}
    old = store.message_payload(key)
    if old:
        _require(old == payload, "Stable message ID reused with changed payload")
        store.after_commit(lambda: log.emit(LOG, "coordinator.message.duplicate", level=log.DEBUG, **ids))
        return False
    # Continue the caller's span; otherwise join the incident's stored trace, or
    # start a new one for a new incident. Trace data never affects the decision.
    parent = None if tracing.current() or message["kind"] == "incident" else store.trace_context(
        incident_id=message["incident_id"])
    high_water = store.outbox_high_water()  # Before _apply enqueues this message's own commands.
    with store.transition_cause(key, message["kind"]), tracing.start_span(
            "coordinator.record", parent=parent, defer=store.after_commit, **ids):
        _apply(message, store, answers, producer)
        replan_after_stale(message, store, limit=stale_replans)
        replan_after_conflict(message, store, limit=stale_replans)
    store.add_message(key, payload, high_water=high_water, answers=answers, producer=producer,
                      incident_id=message["incident_id"])
    store.after_commit(lambda: log.emit(LOG, "coordinator.message.recorded", **ids))
    return True


def handle(message: dict, store: SQLiteStore, event_bus) -> bool:
    """Atomically record and route. Return False for an exact retry.

    Publication failure propagates after commit. Retry handle or dispatch_pending
    after restart to replay commands from the durable outbox.
    """
    with store.transaction():
        changed = record_message(message, store)
    dispatch_pending(store, event_bus)
    return changed


def approve(action_id: str, operator_id: str, store: SQLiteStore, *, expires_at: str | None = None,
            expected_proposal_sha256=None, expected_policy_version=None, managed=False) -> dict:
    """Persist approval and request fresh Guard evaluation; never grant execution.

    Direct calls are a trusted local/demo interface. Deployed callers use the
    authenticated approval API and its explicit proposal/policy preconditions.
    """
    _require(isinstance(operator_id, str) and bool(operator_id.strip()), "Operator identity is required")
    with store.transaction():
        action = store.action(action_id)
        if managed:
            _require(action is not None and action["guard_result"] is not None
                     and expected_proposal_sha256 == _digest(action["proposal"])
                     and expected_policy_version == action["guard_result"]["policy_version"],
                     "Approval proposal or policy precondition changed")
            _require(expires_at is not None, "Authenticated approval requires expiry")
            _require(isinstance(expires_at, str) and _time(expires_at) > _time(store.now()),
                     "Approval expiry must be a future UTC timestamp")
        previous = store.approval(action_id)
        if (action and action["state"] in ({"VALIDATING", "READY"} if managed else {"VALIDATING"}) and previous
                and (not managed or previous == store.current_approval(action_id, managed_only=True))
                and previous["operator_id"] == operator_id
                and previous["proposal_sha256"] == _digest(action["proposal"])
                and previous["policy_version"] == action["guard_result"]["policy_version"]
                and previous.get("expires_at") == expires_at):
            return previous
        _require(action is not None and action["state"] == "AWAITING_APPROVAL",
                 "Only an escalated active proposal can receive approval")
        guard = action["guard_result"]
        _require(set(guard["reason_codes"]) <= {"OPERATOR_APPROVAL_REQUIRED", "APPROVAL_EXPIRED", "APPROVAL_MISMATCH"},
                 "Approval cannot override state, dependency or policy escalation")
        now = store.now()
        # AWAITING_APPROVAL already proves the Guard decision was accepted first.
        # A sequenced decision carries the Guard's clock, so it is not compared
        # with the Coordinator's; a single-clock one still is.
        meta = store.accepted_meta("guard_result", action_id)
        if meta is None or meta["answers"] is None:
            _require(_time(now) >= _time(guard["checked_at"]), "Approval predates the Guard decision")
        if expires_at is not None:
            try:
                valid_expiry = isinstance(expires_at, str) and _time(expires_at) > _time(now)
            except (ValueError, TypeError, AttributeError) as exc:
                raise CoordinatorError("Approval expiry must be a future UTC timestamp") from exc
            _require(valid_expiry, "Approval expiry must be a future UTC timestamp")
        record = {"action_id": action_id, "operator_id": operator_id,
                  "proposal_sha256": _digest(action["proposal"]),
                  "policy_version": guard["policy_version"], "decision": "APPROVED", "approved_at": now}
        if expires_at is not None:
            record["expires_at"] = expires_at
        store.add_approval(action_id, record, managed=managed)
        store.set_state(action["incident_id"], action_id, "VALIDATING", cause="approval")
        store.enqueue("guard.request", {"proposal": action["proposal"], "approval": record}, action_id)
    return record


def reseal(action_id: str, store: SQLiteStore) -> bool:
    """Queue another executor.seal for a workload still blocked by this action's seal.

    For an operator after an UNSEALED result, once the API server accepts
    writes again; the workload stays blocked until a seal reports. Like
    approve(), a trusted local interface. Return True when a seal was queued.
    """
    with store.transaction():
        action = store.action(action_id)
        owner = store.target_ownership(action["proposal"]["target"]) if action else None
        _require(owner is not None and owner["seal_required"] and owner["seal_action_id"] == action_id,
                 "No seal is outstanding for this action")
        return store.require_seal(action)


def dispatch_pending(store: SQLiteStore, event_bus) -> int:
    """Replay pending envelopes; acknowledge only after consumer success.

    publish only queues a command. A crash can repeat a delivered event, so
    consumers must deduplicate event_id and action_id. Call before bus.drain().
    """
    count = 0
    for row in store.pending_routes():
        obsolete = False
        if row["action_id"]:
            action = store.action(row["action_id"])
            payload = json.loads(row["payload"])
            eligible = {"guard.request": {"VALIDATING", "AWAITING_APPROVAL"},
                        "dependency.request": {"VALIDATING", "AWAITING_APPROVAL", "READY"},
                        "executor.request": {"READY"}, "verifier.request": {"VERIFYING"},
                        "executor.reconcile": {"NEEDS_RECONCILIATION"}}
            obsolete = (row["topic"] in eligible and action["state"] not in eligible[row["topic"]])
            if row["topic"] == "executor.request":
                obsolete = obsolete or action["guard_result"] != payload["guard_result"]
                if store.has_managed_approval(row["action_id"]):
                    obsolete = obsolete or store.current_approval(row["action_id"], managed_only=True) != payload.get("approval")
            elif row["topic"] == "guard.request":
                # A newer request supersedes earlier approval/dependency inputs,
                # including a request that was already leased to a worker.
                newest = store.database.execute("SELECT max(id) FROM outbox WHERE topic='guard.request' AND action_id=?",
                                                (row["action_id"],)).fetchone()[0]
                obsolete = obsolete or row["id"] != newest
            elif row["topic"] == "dependency.request":
                newest = store.database.execute("SELECT max(id) FROM outbox WHERE topic='guard.request' AND action_id=?",
                                                (row["action_id"],)).fetchone()[0]
                obsolete = obsolete or payload.get("guard_event_id") != newest
            elif row["topic"] == "executor.seal":
                # A seal stays wanted, whatever the action's state, until its workload is released.
                owner = store.target_ownership(payload["proposal"]["target"])
                obsolete = not (owner and owner["seal_required"] and owner["seal_action_id"] == row["action_id"])
        elif row["topic"] == "planner.request":
            incident = store.incident(json.loads(row["payload"])["incident"]["incident_id"])
            obsolete = incident["state"] != "OPEN"
            if not obsolete and store.planning_blocker(incident["incident_id"]):
                if hasattr(event_bus, "cancel"):
                    event_bus.cancel(row["id"])
                continue  # Remains durable and pending until the earlier recovery ends.
        if obsolete:
            acknowledge(store, row["id"])
            if hasattr(event_bus, "cancel"):
                event_bus.cancel(row["id"])
            continue
        event_bus.publish({"event_id": row["id"], "topic": row["topic"], "payload": json.loads(row["payload"])})
        count += 1
    return count


def acknowledge(store: SQLiteStore, event_id: int) -> None:
    """Call only after successfully handling a routing envelope."""
    store.acknowledge_route(event_id)
