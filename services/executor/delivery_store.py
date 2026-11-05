"""Persist delivery conflicts without changing saved execution results or lifecycle.

Call mutations within the Coordinator transaction; this table is audit evidence.
"""
import json


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
