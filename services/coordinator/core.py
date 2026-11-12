"""SQLite lifecycle and durable at-least-once local routing.

Routing envelopes are local commands, not shared wire messages or permission to
write Kubernetes state. The executor must still obtain fresh Guard validation.
"""

from copy import deepcopy
import json

from shared import log, tracing
from shared.contracts import validate_message
from services.coordinator.store import (
    CoordinatorError, LOG, SQLiteStore, SupersededResult, _digest, _json, _require, _time,
)

# The requests each worker result kind answers (runtime/worker_transport.py::TOPICS).
ANSWERS = {"dependency_result": ("dependency.request",), "guard_result": ("guard.request",),
           "execution_result": ("executor.request", "executor.reconcile"), "outcome": ("verifier.request",)}


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


def record_message(message: dict, store: SQLiteStore, *, answers=None, producer=None) -> bool:
    """Record within an existing transaction, without committing or publishing.

    Runtime adapters use this to commit a result and its input acknowledgment
    together. Most callers should use handle instead. The worker transport
    passes answers (the outbox id of the request answered) and producer (the
    authenticated identity), so lifecycle order is checked by Coordinator
    sequence numbers instead of the producers' clocks.
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
                newest = store.db.execute("SELECT max(id) FROM outbox WHERE topic='guard.request' AND action_id=?",
                                          (row["action_id"],)).fetchone()[0]
                obsolete = obsolete or row["id"] != newest
            elif row["topic"] == "dependency.request":
                newest = store.db.execute("SELECT max(id) FROM outbox WHERE topic='guard.request' AND action_id=?",
                                          (row["action_id"],)).fetchone()[0]
                obsolete = obsolete or payload.get("guard_event_id") != newest
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
