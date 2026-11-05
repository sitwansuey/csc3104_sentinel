"""Consume durable post-execution requests using Role 6's actual evaluator.

Collection happens outside the Coordinator transaction. Recheck the active action
after collection, then atomically record the outcome and acknowledge the request.
This worker has no Kubernetes mutation or proposal-generation authority.
"""

from copy import deepcopy
import json

from services.coordinator import acknowledge, dispatch_pending, record_message
from services.verifier import verify


def _eligible(store, request):
    if request is None or request["delivered"]:
        return None
    action = store.action(request["action_id"])
    incident = store.incident(action["incident_id"]) if action else None
    execution = json.loads(request["payload"]).get("execution")
    if (not action or not incident or incident["active_action_id"] != action["action_id"]
            or action["state"] != "VERIFYING" or incident["state"] != "VERIFYING"
            or action["execution_result"] != execution):
        return None
    return action


def process_verifier_requests(store, event_bus, *, sample_reader, policy_reader, ledger=None, clock=None):
    """Evaluate exact executions; incomplete collection cannot establish recovery.

The expected rollback hash comes from the persisted proposal, not a sample's
claim. Reader failures produce UNKNOWN evidence; storage/contract failures leave
requests pending. A committed outcome and acknowledgment are replay-safe.
"""
    if ledger is not None:
        from runtime.verifier_durable import process_verifier_requests as process_durable

        def bound_policy(execution):
            policy = deepcopy(policy_reader(deepcopy(execution)))
            if policy.get("health", {}).get("require_template_match"):
                action = store.action(execution["action_id"])
                policy["health"]["expected_template_sha256"] = action["proposal"]["parameters"].get("template_sha256")
            return policy

        return process_durable(store, event_bus, ledger=ledger, sample_reader=sample_reader,
                               policy_reader=bound_policy, clock=clock)
    # Compatibility for callers that already collect a complete window in one call.
    # Incremental/live workers must supply a VerificationStore to persist readings.
    pending = store.pending_routes("verifier.request")
    outcomes = []
    for row in pending:
        request = store.route(row[0])
        action = _eligible(store, request)
        if action is None:
            acknowledge(store, row[0])
            continue
        execution = deepcopy(action["execution_result"])
        policy = deepcopy(policy_reader(deepcopy(execution)))
        if policy.get("health", {}).get("require_template_match"):
            policy["health"]["expected_template_sha256"] = action["proposal"]["parameters"].get("template_sha256")
        collection_error = None
        try:
            samples = sample_reader(deepcopy(execution))
        except Exception as error:
            samples, collection_error = [], type(error).__name__
        outcome = verify(execution, samples, policy)
        if collection_error:
            outcome["checks"].append({"name": "evidence_collection", "passed": False,
                                      "detail": "Post-action collection unavailable: " + collection_error})
        with store.transaction():
            current = store.route(row[0])
            latest = _eligible(store, current)
            if latest is not None and latest["execution_result"] == execution:
                record_message(outcome, store)
                outcomes.append(outcome)
            acknowledge(store, row[0])
    dispatch_pending(store, event_bus)
    return outcomes
