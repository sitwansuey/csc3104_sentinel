"""Invariants for a multi-process run, computed from durable records and logs.

Numbering follows the A1 design and the P3.01 plan entry:

- I1: at most one committed template write per action, and none without
  that action's evidence annotation.
- I2: no template write after a revocation unless the action went to review.
- I3: every incident reaches a terminal or reviewable state (RECOVERED,
  FAILED, NEEDS_REVIEW or REJECTED) within the scenario deadline.
- I4: no delivery has two different acknowledged results.
- I5: no poison loop: delivery attempts per command stay at or below K, and
  no command's result is refused with HTTP 400 more than twice.
- I6: no Executor ledger ends with a write in doubt (WRITE_STARTED or UNCERTAIN).

Four cross-checks complete the set:

- X1: lease ownership is exclusive: no command is claimed by a second
  identity while another identity's lease on it is still current.
- X2: no generated credential or lease token appears in any log, any
  configuration file or any process's argv.
- X3: no acknowledged message is lost: every result a worker saw acknowledged,
  and every incident the Observer saw acknowledged, is recorded by the Coordinator.
- X4: the transitions journal is consistent with incidents and the outbox: each
  incident's journal is one unbroken chain ending in its current state, and each
  Executor or Verifier command follows a transition into READY or VERIFYING.

check() takes plain records (see collect()) so the checks are unit-testable
without processes. Details hold counts and identifiers only.
"""
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from shared.canonical import sha256_hex as digest

TERMINAL = frozenset({"RECOVERED", "FAILED", "NEEDS_REVIEW", "REJECTED"})
IN_DOUBT = frozenset({"WRITE_STARTED", "UNCERTAIN"})
MAX_ATTEMPTS = 6
MAX_REFUSALS = 2
LEASE_SLACK_SECONDS = 0.25
NAMES = {
    "I1": "At most one committed template write per action",
    "I2": "No template write after revocation unless surfaced for review",
    "I3": "Every incident terminal or reviewable within the deadline",
    "I4": "No delivery has two acknowledged results",
    "I5": "No poison loop (bounded attempts, at most two HTTP 400 refusals per command)",
    "I6": "No Executor ledger ends with a write in doubt",
    "X1": "Lease ownership exclusive",
    "X2": "No secret in logs, configuration files or argv",
    "X3": "No acknowledged message lost",
    "X4": "Transitions journal consistent with incidents and outbox",
}


def _time(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


def _result(identifier, failures, passed_detail):
    return {"id": identifier, "name": NAMES[identifier], "passed": not failures,
            "detail": "; ".join(failures[:5]) if failures else passed_detail}


def i1(records):
    """Writes come from the cluster's own record, classified by effect (tests/support/file_cluster.py).

    A committed template write that does not carry a valid evidence annotation
    of its own action (unannotated, malformed, or a fence that changed the
    template) fails, since it cannot be tied to one action. So does a kind-mode
    "unobserved" row: commits between two reads of the Deployment that the
    reads do not account for, which may hide a template write
    (experiments/distributed/topology.py::observed_writes).
    """
    writes, failures = defaultdict(int), []
    for row in records.get("cluster_writes", []):
        if row["marker"] == "unobserved":
            failures.append(f"{row.get('unaccounted', 'some')} commit(s) between two reads not accounted for; "
                            "a template write may be hidden")
            continue
        if row["marker"] != "write" or row["action_id"] in {"unknown", "unannotated"}:
            failures.append(f"{row['action_id']}: template written by a "
                            f"{'fence' if row['marker'] == 'fence' else 'patch without valid action evidence'}")
        writes[row["action_id"]] += 1
    failures += [f"{action}: {count} committed writes" for action, count in sorted(writes.items()) if count > 1]
    return _result("I1", failures, f"{sum(writes.values())} committed write(s) over {len(writes)} action(s)")


def i2(records):
    revoked = {row["action_id"]: _time(row["revoked_at"]) for row in records.get("revocations", [])}
    states = {row["action_id"]: row["state"] for row in records.get("actions", [])}
    failures = [f"{row['action_id']}: write after revocation" for row in records.get("cluster_writes", [])
                if row["action_id"] in revoked and row["decided_at"] > revoked[row["action_id"]]
                and states.get(row["action_id"]) != "NEEDS_REVIEW"]
    return _result("I2", failures, f"{len(revoked)} revocation(s), no write after any")


def i3(records):
    incidents = records.get("incidents", [])
    failures = [f"{row['incident_id']}: {row['state']}" for row in incidents if row["state"] not in TERMINAL]
    if not incidents:
        failures.append("no incident was recorded")
    if not records.get("terminal_in_time", False) and not failures:
        failures.append("terminal only after the deadline")
    return _result("I3", failures, ", ".join(sorted({row["state"] for row in incidents})))


def i4(records):
    accepted = defaultdict(set)
    for row in records.get("worker_results", []):
        if row["accepted"]:
            accepted[row["event_id"]].add(row["result_digest"])
    receipts = {row["event_id"]: row["result_digest"] for row in records.get("receipts", []) if row["finished"]}
    failures = [f"command {event}: {len(digests)} acknowledged results" for event, digests in sorted(accepted.items())
                if len(digests) > 1]
    failures += [f"command {event}: acknowledged result differs from the receipt"
                 for event, digests in sorted(accepted.items())
                 if len(digests) == 1 and event in receipts and receipts[event] not in digests]
    return _result("I4", failures, f"{len(accepted)} acknowledged command(s), one result each")


def i5(records):
    by_delivery = {row["delivery_id"]: row["event_id"] for row in records.get("receipts", [])}
    refusals = defaultdict(int)
    for line in records.get("access", []):
        if line.get("operation") == "result" and line.get("http_status") == 400:
            refusals[by_delivery.get(line.get("delivery_id"), line.get("delivery_id"))] += 1
    failures = [f"command {row['event_id']}: {row['attempts']} attempts" for row in records.get("receipts", [])
                if row["attempts"] > MAX_ATTEMPTS]
    failures += [f"command {event}: {count} results refused with HTTP 400"
                 for event, count in sorted(refusals.items(), key=str) if count > MAX_REFUSALS]
    most = max((row["attempts"] for row in records.get("receipts", [])), default=0)
    return _result("I5", failures, f"at most {most} attempt(s) per command, {sum(refusals.values())} HTTP 400 result(s)")


def i6(records):
    failures = [f"{identity}: {row['action_id']} {row['phase']}"
                for identity, rows in sorted(records.get("executor_ledgers", {}).items())
                for row in rows if row["phase"] in IN_DOUBT]
    count = sum(len(rows) for rows in records.get("executor_ledgers", {}).values())
    return _result("I6", failures, f"{count} ledger row(s), none in doubt")


def x1(records):
    lease = records.get("lease_seconds", 30)
    holders, failures = {}, []
    for line in sorted(records.get("access", []), key=lambda item: item["ts"]):
        delivery, identity, operation = line.get("delivery_id"), line.get("identity"), line.get("operation")
        if not delivery or line.get("http_status") != 200:
            continue
        at = _time(line["ts"])
        current = holders.get(delivery)
        if operation == "claim" and line.get("outcome") == "claimed":
            if current and current[0] != identity and at < current[1] - LEASE_SLACK_SECONDS:
                failures.append(f"delivery {delivery}: claimed by a second identity during a current lease")
            holders[delivery] = (identity, at + lease)
        elif operation == "renew" and current and current[0] == identity:
            holders[delivery] = (identity, at + lease)
        elif operation in {"result", "retry"} and current and current[0] == identity:
            holders[delivery] = (identity, at)
    return _result("X1", failures, f"{len(holders)} delivery lease(s), each held by one identity at a time")


def x2(records):
    scan = records.get("secret_scan", {})
    hits = scan.get("hits", [])
    return _result("X2", [f"credential found in {name}" for name in hits],
                   f"{scan.get('files_scanned', 0)} file(s) and {scan.get('argv_scanned', 0)} argv list(s) clean")


def x3(records):
    recorded = set(records.get("message_digests", []))
    failures = [f"command {row['event_id']}: acknowledged result not recorded"
                for row in records.get("worker_results", []) if row["accepted"] and row["message_digest"] not in recorded]
    incidents = {row["incident_id"] for row in records.get("incidents", [])}
    failures += [f"incident {row['incident_id']}: acknowledged to the Observer but not recorded"
                 for row in records.get("observer_outbox", []) if row["delivered"] and row["incident_id"] not in incidents]
    count = sum(1 for row in records.get("worker_results", []) if row["accepted"])
    return _result("X3", failures, f"{count} acknowledged result(s) all recorded")


def x4(records):
    failures, chains = [], defaultdict(list)
    for row in sorted(records.get("transitions", []), key=lambda item: item["seq"]):
        chains[row["incident_id"]].append(row)
    for incident in records.get("incidents", []):
        chain = chains.get(incident["incident_id"], [])
        if not chain or chain[-1]["to_state"] != incident["state"]:
            failures.append(f"incident {incident['incident_id']}: journal does not end in its state")
        for before, after in zip(chain, chain[1:], strict=False):
            if after["from_state"] is not None and after["from_state"] != before["to_state"]:
                failures.append(f"incident {incident['incident_id']}: journal chain broken at seq {after['seq']}")
    reached = {(row["action_id"], row["to_state"]) for row in records.get("transitions", [])}
    needs = {"executor.request": "READY", "verifier.request": "VERIFYING"}
    failures += [f"command {row['id']}: {row['topic']} without a transition to {needs[row['topic']]}"
                 for row in records.get("outbox", [])
                 if row["topic"] in needs and (row["action_id"], needs[row["topic"]]) not in reached]
    return _result("X4", failures, f"{len(records.get('transitions', []))} transition(s) consistent")


CHECKS = (i1, i2, i3, i4, i5, i6, x1, x2, x3, x4)


def check(records):
    return [function(records) for function in CHECKS]


# Collection ------------------------------------------------------------------

def _rows(path, sql, parameters=()):
    path = Path(path)
    if not path.is_file():
        return []
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, parameters)]
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()


def log_lines(path):
    lines = []
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if Path(path).is_file() else []:
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict) and "event" in value:
            lines.append(value)
    return lines


def collect(topology, *, terminal_in_time, cluster_writes=None):
    """Read every record the checks need. Secrets are used here only, never returned."""
    coordinator = topology.paths["coordinator"] / "coordinator.db"
    receipts = _rows(coordinator, "SELECT event_id, delivery_id, owner, attempts, finished, result_digest, lease_token "
                                  "FROM transport_receipts")
    lease_tokens = [row.pop("lease_token", None) for row in receipts]
    secrets = [*topology.tokens.values(), *topology.approval_tokens.values(), *filter(None, lease_tokens)]
    records = {
        "lease_seconds": topology.lease_seconds, "terminal_in_time": terminal_in_time,
        "incidents": _rows(coordinator, "SELECT incident_id, state, active_action_id FROM incidents"),
        "actions": _rows(coordinator, "SELECT action_id, incident_id, state FROM actions"),
        "transitions": _rows(coordinator, "SELECT * FROM transitions ORDER BY seq"),
        "outbox": _rows(coordinator, "SELECT id, topic, action_id, delivered FROM outbox ORDER BY id"),
        "receipts": receipts,
        "message_digests": [digest(json.loads(row["payload"]))
                            for row in _rows(coordinator, "SELECT payload FROM messages")],
        "revocations": [json.loads(row["record"]) for row in _rows(coordinator, "SELECT record FROM approval_revocations")],
        # Each worker's offset from the Coordinator's clock as it measured and reported it.
        "worker_clocks": _rows(coordinator, "SELECT identity, offset, uncertainty, max_abs_offset, reports "
                                            "FROM worker_clocks ORDER BY identity"),
        "cluster_writes": cluster_writes if cluster_writes is not None else _rows(
            topology.run_dir / "cluster.db", "SELECT * FROM cluster_requests WHERE kind='write' AND state='committed'"),
        "executor_ledgers": {}, "worker_results": [], "observer_outbox": [],
    }
    records["revocations"] = [{"action_id": row["action_id"], "revoked_at": row["revoked_at"]}
                              for row in records["revocations"]]
    for name in topology.processes:
        state = topology.paths["state"] / name
        if name.startswith("executor"):
            records["executor_ledgers"][name] = _rows(state / "executor.db", "SELECT action_id, phase FROM executor_actions")
        for row in _rows(state / "transport.db", "SELECT delivery, result, report, accepted FROM worker_deliveries "
                                                 "WHERE result IS NOT NULL"):
            delivery = json.loads(row["delivery"]) if row["delivery"] else {}
            result, report = json.loads(row["result"]), json.loads(row["report"]) if row["report"] else None
            records["worker_results"].append({
                "identity": name, "event_id": delivery.get("event_id"), "accepted": bool(row["accepted"]),
                "result_digest": digest(result) if report is None else digest({"result": result, "report": report}),
                "message_digest": digest(result)})
        if name.startswith("observer"):
            records["observer_outbox"] = _rows(state / "observer.db", "SELECT incident_id, delivered FROM observer_outbox")
    records["access"] = [{key: line.get(key) for key in ("ts", "identity", "operation", "http_status", "outcome",
                                                           "delivery_id", "event_id")}
                         for line in log_lines(topology.processes["coordinator"].stderr_path)
                         if line.get("event") in {"http.access", "auth.denied"}]
    records["secret_scan"] = scan_secrets(topology, secrets)
    return records


def scan_secrets(topology, secrets):
    """Names of files (or argv lists) that contain any secret. Values are never returned."""
    secrets = [value for value in secrets if value]
    files = [path for directory in ("logs", "config") for path in topology.paths[directory].rglob("*") if path.is_file()]
    hits = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(value in text for value in secrets):
            hits.append(str(path.relative_to(topology.run_dir)))
    argv = [process.command for process in topology.processes.values()]
    hits += [f"argv of {name}" for name, process in topology.processes.items()
             if any(value in item for item in process.command for value in secrets)]
    return {"files_scanned": len(files), "argv_scanned": len(argv), "hits": hits}
