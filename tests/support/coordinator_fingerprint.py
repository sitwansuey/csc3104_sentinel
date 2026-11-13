"""Deterministic Coordinator flows and a fingerprint of the SQLite file they leave.

Used by tests/test_coordinator_db.py to show that putting the store behind
the Database port (P5.01 stage 8.1) changed nothing that SQLite stores. The
expected digests there were computed on integration/phase4 before the port
existed (2ef594a and 1adcfe3). Only values that are random or wall-clock by
design are masked: the store generation (randomblob, P3.06), its creation time
(SQLite now) and trace contexts (random span ids). statements() records the
SQL text (not the bound values) sent through Connection.execute on every
connection opened on a coordinator.db file, so the digest of that list shows
the same statements reach SQLite in the same order. It does not see
cursor.execute, executemany or executescript; the store sends none of them,
and a caller that did would escape this digest.
"""
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import itertools
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MASKED = {("store_meta", "generation"), ("store_meta", "created_at"),
          ("outbox", "trace_context"), ("incidents", "trace_context")}


def _sample(name):
    return json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8"))


@contextmanager
def statements():
    """Yield a list that collects the SQL text of every statement run on a coordinator.db connection."""
    recorded = []

    class Recording(sqlite3.Connection):
        def execute(self, sql, *args):
            recorded.append(sql)
            return super().execute(sql, *args)

    connect = sqlite3.connect

    def recording_connect(database, *args, **kwargs):
        if str(database).endswith("coordinator.db") and "factory" not in kwargs:
            kwargs["factory"] = Recording
        return connect(database, *args, **kwargs)

    with patch("sqlite3.connect", recording_connect):
        yield recorded


def digest_lines(lines):
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def fingerprint(path):
    """sha256 over the schema, user_version and every row in rowid order, with random values masked."""
    db = sqlite3.connect(str(path))
    try:
        lines = [f"user_version={db.execute('PRAGMA user_version').fetchone()[0]}"]
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        # Columns and indexes through PRAGMAs, not sqlite_master.sql, whose text can vary by SQLite version.
        for name in tables:
            lines.append(json.dumps(["columns", name, db.execute(f'PRAGMA table_info("{name}")').fetchall()]))
            for index in db.execute(f'PRAGMA index_list("{name}")').fetchall():
                columns = db.execute(f'PRAGMA index_info("{index[1]}")').fetchall()
                lines.append(json.dumps(["index", name, list(index[1:4]), columns]))
        for name in tables:
            cursor = db.execute(f'SELECT rowid AS "__rowid", * FROM "{name}" ORDER BY rowid')
            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                values = ["<masked>" if (name, column) in MASKED and value is not None else value
                          for column, value in zip(columns, row, strict=True)]
                lines.append(json.dumps([name, dict(zip(columns, values, strict=True))], sort_keys=True))
        return digest_lines(lines)
    finally:
        db.close()


def offline_flow(directory, scenario):
    """The offline demo's lifecycle (Observer to Verifier) on one Coordinator file."""
    from experiments.validation.demo_offline_flow import OfflineFlow, fixture
    flow = OfflineFlow(directory, scenario=scenario)
    try:
        flow.prepare()
        flow.authorize()
        if scenario == "failed-rollout":
            flow.restart()
        flow.execute()
        flow.now = datetime(2026, 10, 10, 12, 1, 10, tzinfo=UTC)
        flow.verify(fixture("health.json")["stable"])
        assert flow.state() == "RECOVERED", flow.state()
    finally:
        flow.close()
    return Path(directory) / "coordinator.db"


def transport_flow(directory):
    """Worker transport paths: ingest, claim, renew, retry, finish, clocks, approvals, a seal and a review."""
    from runtime.worker_transport import Delivery
    from services.coordinator.approvals import ApprovalAccess
    from services.coordinator.core import LocalEventBus, approve, handle
    from services.coordinator.store import SQLiteStore, _digest
    from services.executor.delivery_store import _ensure_reviews, _record_review, execution_delivery_reviews

    path = Path(directory) / "coordinator.db"
    now = [100.0]
    stamp = datetime(2026, 10, 10, 12, 0, 36, tzinfo=UTC)
    ids = (f"{n:032x}" for n in itertools.count(1))
    incident, proposal = _sample("messages/incident.json"), _sample("messages/proposal.json")
    with patch("runtime.worker_transport.uuid4", lambda: type("U", (), {"hex": next(ids)})()), \
            SQLiteStore(path, clock=lambda: stamp) as store:
        delivery = Delivery(store, clock=lambda: now[0], lease_seconds=5, retention_seconds=20,
                            dependency_requests=True, max_clock_skew=2.0)
        delivery.ingest("observer", incident, identity="observer-1")
        delivery.report_clock("p", (0.25, 0.01))
        first = delivery.claim("p", "planner", (0.25, 0.01))["delivery"]
        delivery.renew("p", "planner", first["delivery_id"], first["lease_token"])
        delivery.finish("p", "planner", first["delivery_id"], first["lease_token"], None, retry=True)
        now[0] += 7
        second = delivery.claim("p", "planner", (0.5, 0.02))["delivery"]
        delivery.finish("p", "planner", second["delivery_id"], second["lease_token"], proposal,
                        clock_estimate=(0.5, 0.02))
        delivery.finish("p", "planner", second["delivery_id"], second["lease_token"], proposal)
        other = dict(incident, incident_id="INC-FINGERPRINT-2", target={**incident["target"], "name": "other-api",
                                                                        "uid": "other-uid"})
        delivery.ingest("observer", other, identity="observer-1")
        dependency = delivery.claim("v", "verifier", (0.0, 0.01))["delivery"]
        bus = LocalEventBus()
        handle(_sample("messages/dependency-result.json"), store, bus)
        guard = _sample("messages/guard-result.json")
        handle(guard, store, bus)
        approve(proposal["action_id"], "operator-1", store)
        access = ApprovalAccess(store, {"operator": [{"target": proposal["target"],
                                                      "operations": ["create", "read", "revoke", "history"]}]})
        body = {"action_id": proposal["action_id"], "proposal_sha256": _digest(proposal)}
        row = store.approval_row(proposal["action_id"])
        access.call("operator", "operator", "revoke", dict(body, approval_id=row["id"], reason="fingerprint"))
        access.call("operator", "operator", "history", body)
        with store.transaction():
            store.require_seal(store.action(proposal["action_id"]))
            store.claim_target(proposal["target"], "e", "d-1", 1)
            store.finish_seal(proposal["target"], 1, release=False)
            _ensure_reviews(store)
            _record_review(store, 0, _sample("messages/execution-result.json"), "fingerprint")
        execution_delivery_reviews(store)
        now[0] += 30
        with delivery.transaction():
            pass
        assert dependency is not None and dependency["topic"] == "dependency.request", dependency
    return path


def conflict_replan_flow(directory):
    """An ALLOWed rollback whose Executor CONFLICT wrote nothing, re-planned (core.py::replan_after_conflict).

    Runs every query of _conflict_replan_declined: the earlier-result lookup,
    the count of READY transitions and the seal lookup.
    """
    from experiments.validation.demo_offline_flow import OfflineFlow, fixture
    from services.coordinator.core import record_message
    from services.observer.core import detect
    from services.planner.core import mock_provider, retrieve_runbooks
    from services.planner.store import PlannerStore
    from services.planner.worker import prepare

    flow = OfflineFlow(directory)
    try:
        store = flow.store
        incident = detect(fixture("observations.json")["failed-rollout"], flow.policy)
        journal = PlannerStore(":memory:", clock=lambda: flow.now)
        try:
            proposal = prepare(incident, journal=journal, baseline_reader=lambda _: [flow.baseline],
                               retriever=retrieve_runbooks, provider=mock_provider)
        finally:
            journal.close()
        for message in (incident, proposal):
            with store.transaction():
                record_message(message, store, stale_replans=3)
        with store.transaction():
            store.save_guard_result(proposal["action_id"], {
                "schema_version": "1.0", "kind": "guard_result", "incident_id": proposal["incident_id"],
                "action_id": proposal["action_id"], "target": proposal["target"], "verdict": "ALLOW",
                "reason_codes": ["POLICY_SATISFIED"], "policy_version": "policy-1",
                "validated_resource_version": proposal["based_on_resource_version"],
                "checked_at": "2026-10-10T12:00:40Z"})
            store.set_state(incident["incident_id"], proposal["action_id"], "READY")
        result = {"schema_version": "1.0", "kind": "execution_result", "incident_id": proposal["incident_id"],
                  "action_id": proposal["action_id"], "target": proposal["target"], "status": "CONFLICT",
                  "resource_version": proposal["based_on_resource_version"], "recorded_at": "2026-10-10T12:00:50Z",
                  "details": "x", "conflict": {"basis": "NOT_SENT", "resource_version": "rv-live",
                                               "generation": incident["observed_generation"]}}
        with store.transaction():
            record_message(result, store, stale_replans=3)
        assert store.incident(incident["incident_id"])["state"] == "OPEN", store.incident(incident["incident_id"])
    finally:
        flow.close()
    return Path(directory) / "coordinator.db"
