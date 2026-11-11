"""Public bounded read interface for Coordinator records.

Consumers depend on this response, not private SQLite tables. Opening a reader
never creates or migrates a database and executes one consistent read transaction.
"""
import json
from pathlib import Path
import sqlite3


def _decode(row, fields):
    item = dict(row)
    for field in fields:
        item[field] = json.loads(item[field]) if item[field] else None
    return item


def read_records(path, *, limit=600):
    """Return incidents, actions, messages, approvals, routes, Guard reports and transitions.

    Transitions are the newest lifecycle changes, newest first. A database made
    before the transitions journal existed returns an empty list.
    """
    if type(limit) is not int or not 1 <= limit <= 600:
        raise ValueError("Read limit must be an integer between 1 and 600")
    result = {key: [] for key in ('incidents', 'actions', 'messages', 'approvals', 'outbox', 'reports',
                                  'transitions')}
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        result["incidents"] = [_decode(row, ("target",)) for row in db.execute(
            "SELECT rowid AS sequence,* FROM incidents ORDER BY rowid DESC LIMIT ?", (limit,))]
        result["actions"] = [_decode(row, ("proposal", "guard_result", "execution_result")) for row in db.execute(
            "SELECT rowid AS sequence,* FROM actions ORDER BY rowid DESC LIMIT ?", (limit,))]
        result["messages"] = [{"sequence": row[0], "message": json.loads(row[1])} for row in db.execute(
            "SELECT rowid,payload FROM messages ORDER BY rowid DESC LIMIT ?", (limit,))]
        has_managed = db.execute("SELECT 1 FROM sqlite_master WHERE name='managed_approvals'").fetchone()
        approval_query = ("SELECT a.id,a.record,m.approval_id IS NOT NULL AS protected FROM approvals a "
                          "LEFT JOIN managed_approvals m ON m.approval_id=a.id ORDER BY a.id DESC LIMIT ?"
                          if has_managed else "SELECT id,record,0 AS protected FROM approvals ORDER BY id DESC LIMIT ?")
        result["approvals"] = [{"id": row[0], "record": (
            {"action_id": json.loads(row[1])["action_id"], "decision": "APPROVED", "protected": True}
            if row["protected"] else json.loads(row[1]))} for row in db.execute(approval_query, (limit,))]
        result["outbox"] = [dict(row) for row in db.execute(
            "SELECT id,topic,action_id,delivered FROM outbox ORDER BY id DESC LIMIT ?", (limit,))]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='guard_reports'").fetchone():
            result["reports"] = [json.loads(row[0]) for row in db.execute(
                "SELECT report FROM guard_reports ORDER BY rowid DESC LIMIT ?", (limit,))]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='transitions'").fetchone():
            result["transitions"] = [dict(row) for row in db.execute(
                "SELECT seq,incident_id,action_id,from_state,to_state,cause,outbox_high_water,recorded_at "
                "FROM transitions ORDER BY seq DESC LIMIT ?", (limit,))]
        return result
    finally:
        db.close()
