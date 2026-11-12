"""Check a restored Coordinator database for acknowledged work it no longer holds.

    python scripts/coordinator_restore_check.py --db coordinator.db --journal state/executor-a/transport.db
    python scripts/coordinator_restore_check.py --db coordinator.db --previous-commit 1234 --json

Run it after the Coordinator's database was restored from a backup and before
the workers are told to resume (worker_service --accept-store). Every file is
opened read-only; nothing is changed.

It reports:

- the restored database's store generation and commit counter
  (services/coordinator/store.py, migration 5);
- whether the restore went back in time: --previous-commit (and
  --previous-generation), noted before the restore, or the position each
  worker journal remembers, is ahead of the restored counter or names
  another generation;
- per worker journal (runtime/remote_worker.py::WorkerJournal), every result
  the Coordinator acknowledged that the restored database does not hold, and
  how to re-drive it. "replay": the restored outbox still holds the same
  request, so the worker resends its saved result without redoing the work
  once it claims again. Clear those workers first and the others once the
  replays are back, or a peer of the same role may claim the request first
  and report a result of its own. "review": nothing pending asks for it yet.
  A replay can issue the same request again (an Executor's result issues
  the Verifier's request), so run the check again after the replays; what
  stays under review an operator decides, using the worker's journal and,
  for an Executor, the Deployment's evidence annotation;
- saved results not yet acknowledged, which workers send by themselves.

A restore of this database to an earlier state is visible only through the
commit counter. Without --previous-commit or a worker journal that saw a
later counter, such a restore cannot be told from an untouched database.

Exit code: 0 when nothing acknowledged is missing and no rollback is seen,
1 when acknowledged work may be lost, 2 when an input cannot be read.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.coordinator.core import _event_key  # noqa: E402 - after the path set-up above
from shared.canonical import canonical_json  # noqa: E402


def _read_only(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"No such database: {label(path)}")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def label(path):
    """A short name for a file in the report: its folder and file name, never a full path."""
    path = Path(path)
    return f"{path.parent.name}/{path.name}" if path.parent.name else path.name


def _table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def store_position(db):
    """(generation, commit_counter) of a Coordinator database, or (None, None) before migration 5."""
    if not _table(db, "store_meta"):
        return None, None
    row = db.execute("SELECT generation, commit_counter FROM store_meta WHERE id=1").fetchone()
    return (row[0], row[1]) if row else (None, None)


def compare(current, previous_generation, previous_commit):
    """Whether the current position shows a restore relative to a remembered one."""
    generation, counter = current
    different = previous_generation is not None and generation is not None and generation != previous_generation
    lost = (previous_commit - counter if previous_commit is not None and counter is not None and not different
            and previous_commit > counter else 0)
    return {"different_database": different, "rolled_back": lost > 0, "lost_commits": lost}


def _same_request(outbox_payload, delivered_payload):
    """The delivered payload is the outbox payload, or it with a protected approval withheld."""
    if outbox_payload == delivered_payload:
        return True
    return "approval" in outbox_payload and dict(outbox_payload, approval=None) == delivered_payload


def _pending_request(db, topic, payload):
    for row in db.execute("SELECT payload FROM outbox WHERE delivered=0 AND topic=?", (topic,)):
        if _same_request(json.loads(row[0]), payload):
            return True
    return False


def check_journal(coordinator, path, current):
    journal = _read_only(path)
    try:
        report = {"journal": label(path), "remembered": None, "acknowledged": 0, "missing": [], "unsent": 0}
        if _table(journal, "coordinator_store"):
            row = journal.execute("SELECT generation, commit_counter, change FROM coordinator_store WHERE id=1").fetchone()
            if row is not None:
                report["remembered"] = {"generation": row[0], "commit_counter": row[1],
                                        "change": json.loads(row[2]) if row[2] else None}
                report.update(compare(current, row[0], row[1]))
        if not _table(journal, "worker_deliveries"):
            return report
        extra = [name for name in ("accepted_generation", "accepted_commit")
                 if name in _columns(journal, "worker_deliveries")]
        query = "SELECT topic, payload, result, accepted" + "".join("," + name for name in extra)
        for row in journal.execute(query + " FROM worker_deliveries WHERE result IS NOT NULL ORDER BY rowid"):
            if not row["accepted"]:
                report["unsent"] += 1
                continue
            report["acknowledged"] += 1
            result = json.loads(row["result"])
            stored = coordinator.execute("SELECT payload FROM messages WHERE event_key=?",
                                         (_event_key(result),)).fetchone()
            if stored is not None and stored[0] == canonical_json(result):
                continue
            item = {"kind": result.get("kind"), "incident_id": result.get("incident_id"),
                    "action_id": result.get("action_id"), "topic": row["topic"],
                    "redrive": "replay" if stored is None and _pending_request(
                        coordinator, row["topic"], json.loads(row["payload"])) else "review"}
            if stored is not None:
                item["conflict"] = "the restored database holds a different message under this key"
            for name in extra:
                item[name] = row[name]
            report["missing"].append(item)
        return report
    finally:
        journal.close()


def check(database, journals=(), *, previous_generation=None, previous_commit=None):
    coordinator = _read_only(database)
    try:
        version = coordinator.execute("PRAGMA user_version").fetchone()[0]
        current = store_position(coordinator)
        report = {"database": {"schema_version": version, "generation": current[0], "commit_counter": current[1]},
                  "previous": {"generation": previous_generation, "commit_counter": previous_commit},
                  **compare(current, previous_generation, previous_commit),
                  "journals": [check_journal(coordinator, path, current) for path in journals]}
    finally:
        coordinator.close()
    report["missing"] = sum(len(item["missing"]) for item in report["journals"])
    report["may_lose_acknowledged_work"] = bool(
        report["rolled_back"] or report["different_database"] or report["missing"]
        or any(item.get("rolled_back") or item.get("different_database") for item in report["journals"]))
    return report


def render(report):
    database = report["database"]
    lines = [f"Restored database: schema {database['schema_version']}, generation {database['generation'] or 'none'}, "
             f"commit counter {database['commit_counter'] if database['commit_counter'] is not None else 'none'}"]
    if database["generation"] is None:
        lines.append("  The database predates store generations (migration 5); only the journals can be checked.")
    if report["different_database"]:
        lines.append("  It is a different database from the one noted before the restore.")
    if report["rolled_back"]:
        lines.append(f"  It is {report['lost_commits']} commits behind the counter noted before the restore.")
    for item in report["journals"]:
        lines.append(f"Journal {item['journal']}: {item['acknowledged']} acknowledged, "
                     f"{len(item['missing'])} missing, {item['unsent']} not yet sent")
        if item.get("different_database"):
            lines.append("  The worker last saw another database (another generation).")
        if item.get("rolled_back"):
            lines.append(f"  The worker saw commit {item['remembered']['commit_counter']}; "
                         f"the restored database is {item['lost_commits']} commits behind.")
        for missing in item["missing"]:
            how = ("replay: the request is pending again; clear this worker first, and it resends its saved "
                   "result when it claims it" if missing["redrive"] == "replay"
                   else "review: no pending request asks for this result; check again after the replays")
            lines.append(f"  missing {missing['kind']} for {missing['action_id'] or missing['incident_id']} "
                         f"({missing['topic']}): {how}")
    lines.append("Acknowledged work may be lost." if report["may_lose_acknowledged_work"]
                 else "Nothing acknowledged is missing.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, type=Path, help="The restored Coordinator database")
    parser.add_argument("--journal", action="append", default=[], type=Path,
                        help="A worker's transport.db; repeat for every worker")
    parser.add_argument("--previous-commit", type=int, help="The commit counter noted before the restore")
    parser.add_argument("--previous-generation", help="The store generation noted before the restore")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    args = parser.parse_args(argv)
    if args.previous_commit is not None and args.previous_commit < 0:
        parser.error("--previous-commit must not be negative")
    try:
        report = check(args.db, args.journal, previous_generation=args.previous_generation,
                       previous_commit=args.previous_commit)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"Cannot check the restore: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 1 if report["may_lose_acknowledged_work"] else 0


if __name__ == "__main__":
    sys.exit(main())
