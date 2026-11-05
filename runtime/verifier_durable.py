"""Local durable Verifier routing; live sample collection is an injected adapter."""

from copy import deepcopy
from collections.abc import Iterator
from datetime import datetime, timezone
import json
from pathlib import Path

from services.coordinator.core import acknowledge, dispatch_pending, record_message
from services.verifier.core import VerifierInputError
from shared.contracts import validate_message


def _separate_stores(coordinator, ledger):
    first = coordinator.db.execute("PRAGMA database_list").fetchone()[2]
    second = ledger.db.execute("PRAGMA database_list").fetchone()[2]
    if coordinator.db is ledger.db or (first and second and Path(first).resolve() == Path(second).resolve()):
        raise VerifierInputError("Coordinator and Verifier require separate databases")


def _execution(store, execution, *, active):
    validate_message(execution)
    if execution["kind"] != "execution_result":
        raise VerifierInputError("Expected an execution result in verifier.request")
    action = store.action(execution["action_id"])
    incident = store.incident(execution["incident_id"])
    if (action is None or incident is None
            or action["incident_id"] != execution["incident_id"]
            or action["proposal"]["target"] != execution["target"]
            or incident["target"] != execution["target"]
            or action["execution_result"] != execution):
        raise VerifierInputError("Verification request does not match stored execution identity")
    if active and (incident["active_action_id"] != execution["action_id"] or action["state"] != "VERIFYING"):
        raise VerifierInputError("Verification requires the active VERIFYING action")


def _deliver(store, ledger, outcome):
    """Commit Coordinator outcome+input acknowledgment before local acknowledgment.

    A crash between databases replays the exact saved message; record_message
    deduplicates it even after the incident reaches a terminal state.
    """
    job = ledger.get(outcome["action_id"])
    store.db.execute("BEGIN IMMEDIATE")
    try:
        _execution(store, job["execution"], active=False)
        record_message(outcome, store)
        requests = store.db.execute(
            "SELECT id,payload FROM outbox WHERE topic='verifier.request' AND action_id=? AND delivered=0",
            (outcome["action_id"],),
        ).fetchall()
        for request in requests:
            if json.loads(request["payload"]).get("execution") == job["execution"]:
                acknowledge(store, request["id"])
        store.db.execute("COMMIT")
    except BaseException:
        store.db.execute("ROLLBACK")
        raise
    ledger.acknowledge_outcome(outcome)


def process_verifier_requests(store, event_bus, *, ledger, policy_reader,
                              sample_reader, clock=None):
    """Run one bounded polling pass over pending local verification commands.

    Readers receive a copied execution result. sample_reader returns currently
    available target-scoped readings, not fabricated defaults. Policy is frozen
    on first intake. Reader/storage failures leave work pending for a later pass.
    Separate SQLite files provide local durability, not distributed transport.
    """
    _separate_stores(store, ledger)
    clock = clock or (lambda: datetime.now(timezone.utc))
    delivered = []
    for outcome in ledger.pending_outcomes():
        _deliver(store, ledger, outcome)
        delivered.append(outcome)
    pending = store.db.execute(
        "SELECT id FROM outbox WHERE topic='verifier.request' AND delivered=0 ORDER BY id"
    ).fetchall()
    for row in pending:
        request = store.db.execute("SELECT * FROM outbox WHERE id=?", (row[0],)).fetchone()
        if request["delivered"]:
            continue
        payload = json.loads(request["payload"])
        execution = payload.get("execution")
        _execution(store, execution, active=False)
        if request["action_id"] != execution["action_id"]:
            raise VerifierInputError("Routing action does not match execution")
        job = ledger.get(execution["action_id"])
        if job is None:
            _execution(store, execution, active=True)
            job = ledger.start(execution, policy_reader(deepcopy(execution)), now=clock())
        elif job["execution"] != execution:
            raise VerifierInputError("Saved job belongs to a different execution")
        if job["outcome"] is None:
            _execution(store, execution, active=True)
            outcome = ledger.evaluate(execution["action_id"], now=clock())
            if outcome is None:
                samples = sample_reader(deepcopy(execution))
                if not isinstance(samples, (list, Iterator)):
                    raise VerifierInputError("Sample reader must return a list or incremental iterator")
                try:
                    chunks = [samples] if isinstance(samples, list) else ([sample] for sample in samples)
                    for index, chunk in enumerate(chunks):
                        if index >= 10000:
                            raise VerifierInputError("Reader exceeds 10000 incremental readings")
                        now = clock()
                        ledger.record_samples(execution["action_id"], chunk, now=now)
                        outcome = ledger.evaluate(execution["action_id"], now=now)
                        if outcome is not None:
                            break
                finally:
                    if isinstance(samples, Iterator) and hasattr(samples, "close"):
                        samples.close()
        else:
            outcome = job["outcome"]
        if outcome is not None:
            _deliver(store, ledger, outcome)
            delivered.append(outcome)
    dispatch_pending(store, event_bus)
    return delivered
