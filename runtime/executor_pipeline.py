"""Compose the restricted executor with the real Guard and local Coordinator.

Readers supply trusted records; envelopes and cached decisions are routing data,
not execution authority. Kubernetes writes and the executor ledger remain outside
the Coordinator transaction, so redelivery must replay the durable executor result.
"""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json

from services.coordinator.core import acknowledge, dispatch_pending, record_message
from services.executor.core import GuardCheck, execute, reconcile
from services.executor.store import TargetBusy
from services.guard.core import assert_decision_usable, evaluate


class GuardAuthorizer:
    """Reload policy, baseline, dependencies and approval for each validation."""

    def __init__(self, *, policy_reader, baseline_reader, dependency_reader,
                 approval_reader, preview_reader=None):
        self.policy_reader = policy_reader
        self.baseline_reader = baseline_reader
        self.dependency_reader = dependency_reader
        self.approval_reader = approval_reader
        self.preview_reader = preview_reader

    def validate(self, proposal, live_state, *, now):
        inputs = {
            "proposal": deepcopy(proposal), "live_state": deepcopy(live_state),
            "policy": deepcopy(self.policy_reader(deepcopy(proposal))),
            "baseline": deepcopy(self.baseline_reader(deepcopy(proposal))),
            "dependencies": deepcopy(self.dependency_reader(deepcopy(proposal))),
            "approval": deepcopy(self.approval_reader(deepcopy(proposal))),
            "preview": (deepcopy(self.preview_reader(deepcopy(proposal)))
                        if self.preview_reader else None),
        }
        decision = evaluate(**inputs, now=now)
        return GuardCheck(decision=decision, baseline=inputs["baseline"], inputs=inputs)

    def assert_usable(self, check, *, now):
        return assert_decision_usable(check.decision, **check.inputs, now=now)


class _CoordinatorAuthority:
    """Check local lifecycle authority alongside each fresh Guard validation.

    This final read narrows revocation races; it is not atomic with a separate
    Kubernetes API write. Production revocation needs a reviewed fencing design.
    """

    def __init__(self, store, action, authorizer):
        self.store, self.action, self.authorizer = store, action, authorizer

    def _assert_current(self):
        current = self.store.action(self.action["action_id"])
        incident = self.store.incident(self.action["incident_id"])
        if (current is None or incident is None
                or incident["active_action_id"] != self.action["action_id"]
                or current["state"] not in {"READY", "NEEDS_RECONCILIATION"}
                or current["proposal"] != self.action["proposal"]
                or current["guard_result"] != self.action["guard_result"]):
            raise ValueError("Coordinator no longer authorizes this active action")

    def validate(self, proposal, live_state, *, now):
        self._assert_current()
        return self.authorizer.validate(proposal, live_state, now=now)

    def assert_usable(self, check, *, now):
        self._assert_current()
        return self.authorizer.assert_usable(check, now=now)


def _eligible(store, request):
    action = store.action(request["action_id"])
    if action is None:
        return None
    incident = store.incident(action["incident_id"])
    expected = {"executor.request": "READY", "executor.reconcile": "NEEDS_RECONCILIATION"}
    payload = json.loads(request["payload"])
    if (incident["active_action_id"] != action["action_id"]
            or action["state"] != expected[request["topic"]]
            or payload.get("proposal") != action["proposal"]):
        return None
    if (request["topic"] == "executor.request"
            and payload.get("guard_result") != action["guard_result"]):
        return None
    return action


def _ensure_reviews(store):
    store.db.execute("""
        CREATE TABLE IF NOT EXISTS executor_delivery_reviews (
            id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL,
            action_id TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL,
            coordinator_state TEXT, guard_result TEXT,
            UNIQUE(event_id, action_id, result)
        )
    """)


def _record_review(store, event_id, result, reason):
    action = store.action(result["action_id"])
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    guard = json.dumps(action["guard_result"], sort_keys=True) if action else None
    store.db.execute(
        "INSERT OR IGNORE INTO executor_delivery_reviews"
        "(event_id,action_id,result,reason,coordinator_state,guard_result) VALUES(?,?,?,?,?,?)",
        (event_id, result["action_id"], encoded, reason, action["state"] if action else None, guard),
    )


def execution_delivery_reviews(store):
    """Read durable conflicts needing manual lifecycle reconciliation.

    Saved executor results remain unchanged. A later Guard decision or terminal
    lifecycle state can prevent automatic delivery without disproving a past write.
    Event ID zero identifies reconciliation without a pending routing command.
    """
    _ensure_reviews(store)
    reviews = []
    for row in store.db.execute("SELECT * FROM executor_delivery_reviews ORDER BY id"):
        item = dict(row)
        for field in ("result", "guard_result"):
            item[field] = json.loads(item[field]) if item[field] is not None else None
        reviews.append(item)
    return reviews


def reconcile_pending_executions(adapters):
    """Observe every unfinished write independently of Coordinator eligibility.

    The dedicated service entrypoint cannot start a write, even if a stale scan
    races with another worker. Busy targets remain pending for the next run.
    Results are persisted in the executor ledger and returned as audit evidence;
    this helper does not change incident lifecycle or reauthorize execution.
    The ledger result outbox also exposes completed writes whose delivery was
    interrupted. Its exact saved result is replayed without a new timestamp.
    """
    proposals = {proposal["action_id"]: proposal for proposal in adapters.ledger.pending_proposals()}
    for result in adapters.ledger.undelivered_results():
        saved = adapters.ledger.get(result["action_id"])
        proposals[result["action_id"]] = saved["proposal"]
    results = []
    for proposal in proposals.values():
        try:
            results.append(reconcile(proposal, adapters))
        except TargetBusy:
            continue
    return results


def _delivery_conflict(store, result):
    action = store.action(result["action_id"])
    if action and action["execution_result"] == result:
        return None  # An exact duplicate is safe for record_message to deduplicate.
    incident = store.incident(result["incident_id"])
    if (action is None or incident is None
            or incident["active_action_id"] != result["action_id"]
            or action["state"] not in {"READY", "NEEDS_RECONCILIATION"}
            or not action["guard_result"] or action["guard_result"]["verdict"] != "ALLOW"):
        return "Current Coordinator lifecycle does not accept this saved execution result."
    recorded_at = datetime.fromisoformat(result["recorded_at"].replace("Z", "+00:00"))
    checked_at = datetime.fromisoformat(action["guard_result"]["checked_at"].replace("Z", "+00:00"))
    if recorded_at < checked_at:
        return "Saved execution result predates the latest Guard decision; its timestamp was preserved."
    previous = action["execution_result"]
    if previous and recorded_at <= datetime.fromisoformat(previous["recorded_at"].replace("Z", "+00:00")):
        return "Saved execution result does not follow the Coordinator's recorded execution result."
    return None


def process_executor_requests(store, event_bus, *, adapters):
    """Process pending local execution commands and atomically acknowledge results.

    Busy targets, changed action IDs and storage failures propagate without an
    acknowledgment; invalid pre-write inputs produce a saved FAILED result.
    Unfinished ledger writes are reconciled even when routing was
    cancelled by a later Guard decision. Incompatible saved results create durable
    review records; they never override lifecycle decisions or gain new timestamps.
    This worker never holds its SQLite transaction over execution and does not
    claim distributed message or lock ownership.
    """
    _ensure_reviews(store)
    reconciled = {result["action_id"]: result for result in reconcile_pending_executions(adapters)}
    pending = store.db.execute(
        "SELECT id FROM outbox WHERE topic IN ('executor.request','executor.reconcile') "
        "AND delivered=0 ORDER BY id"
    ).fetchall()
    results = []
    seen_actions = set()
    for pending_row in pending:
        request = store.db.execute("SELECT * FROM outbox WHERE id=?", (pending_row[0],)).fetchone()
        if request["delivered"]:
            continue
        seen_actions.add(request["action_id"])
        action = _eligible(store, request)
        if action is None:
            saved = adapters.ledger.get(request["action_id"])
            store.db.execute("BEGIN IMMEDIATE")
            try:
                if saved and saved["result"]:
                    conflict = _delivery_conflict(store, saved["result"])
                    latest = store.action(saved["result"]["action_id"])
                    if latest is None or latest["execution_result"] != saved["result"]:
                        _record_review(store, request["id"], saved["result"], conflict or
                                       "Execution result belongs to an obsolete routing command; inspect current lifecycle.")
                acknowledge(store, request["id"])
                store.db.execute("COMMIT")
            except BaseException:
                store.db.execute("ROLLBACK")
                raise
            if saved and saved["result"]:
                adapters.ledger.acknowledge_result(saved["result"])
            continue
        request_adapters = replace(
            adapters, authorizer=_CoordinatorAuthority(store, action, adapters.authorizer),
        )
        result = reconciled.get(action["action_id"])
        if result is None:
            result = execute(deepcopy(action["proposal"]), request_adapters)
        store.db.execute("BEGIN IMMEDIATE")
        try:
            current = store.db.execute("SELECT delivered FROM outbox WHERE id=?", (request["id"],)).fetchone()
            conflict = _delivery_conflict(store, result)
            if current["delivered"]:
                latest = store.action(result["action_id"])
                if latest is None or latest["execution_result"] != result:
                    _record_review(store, request["id"], result, conflict or
                                   "Routing command was acknowledged before this saved result was delivered.")
            else:
                if conflict:
                    _record_review(store, request["id"], result, conflict)
                else:
                    record_message(result, store)
                    results.append(result)
                acknowledge(store, request["id"])
            store.db.execute("COMMIT")
        except BaseException:
            store.db.execute("ROLLBACK")
            raise
        adapters.ledger.acknowledge_result(result)
    for action_id, result in reconciled.items():
        if action_id not in seen_actions:
            store.db.execute("BEGIN IMMEDIATE")
            try:
                action = store.action(action_id)
                if action is None or action["execution_result"] != result:
                    _record_review(store, 0, result,
                                   "Execution result has no pending command; inspect lifecycle before delivery.")
                store.db.execute("COMMIT")
            except BaseException:
                store.db.execute("ROLLBACK")
                raise
            adapters.ledger.acknowledge_result(result)
    dispatch_pending(store, event_bus)
    return results
