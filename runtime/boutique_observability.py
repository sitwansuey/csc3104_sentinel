"""Bounded Boutique projections for the local experiment dashboard.

These observations never authorize a repair or replace Role 6 outcomes. Keep raw
Pod specs, response HTML, cookies and synthetic payment inputs out of this journal.
"""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def service_inventory(deployments, names):
    """Missing objects/read failures stay unknown, including during setup."""
    indexed = {item["metadata"]["name"]: item for item in deployments or []}
    result = []
    for name in sorted(names):
        item = indexed.get(name)
        metadata = item.get("metadata", {}) if item else {}
        spec = item.get("spec", {}) if item else {}
        status = item.get("status", {}) if item else {}
        values = {"desired": spec.get("replicas", 1) if item else None,
                  "ready": status.get("readyReplicas", 0) if item else None,
                  "available": status.get("availableReplicas", 0) if item else None,
                  "generation": metadata.get("generation"), "observed_generation": status.get("observedGeneration")}
        values = {key: value if type(value) is int and value >= 0 else None for key, value in values.items()}
        known = all(value is not None for value in values.values())
        ready = (values["desired"] > 0 and values["ready"] >= values["desired"]
                 and values["available"] >= values["desired"]
                 and values["observed_generation"] >= values["generation"]) if known else None
        result.append({"name": name, **values, "ready_state": ready,
                       "recovery_target": name == "shippingservice"})
    return result


def shopping_summary(journey):
    """Allowlist measured fields; never copy arbitrary request/response data."""
    statuses = {key: journey.get(key) for key in
                ("home_status", "add_to_cart_status", "cart_status", "checkout_status")}
    statuses = {key: code if type(code) is int and 100 <= code <= 599 else None for key, code in statuses.items()}
    return {**statuses, "passed": journey.get("passed") if type(journey.get("passed")) is bool else None,
            "order_confirmed": journey.get("order_confirmation_found") is True
                and journey.get("order_page_confirmed") is True and statuses["checkout_status"] == 200}


def emit_observation(path, *, cluster_id, namespace, stage, services=None, journey=None,
                     incident_id=None, action_id=None):
    if stage not in {"inventory", "baseline", "primary-fault", "crash-fault", "verification"}:
        raise ValueError("Unknown Boutique observation stage")
    if (stage == "inventory") != (services is not None) or (stage != "inventory") != (journey is not None):
        raise ValueError("Inventory and shopping observations must remain separate")
    payload = {"cluster_id": cluster_id, "namespace": namespace, "observed_at": timestamp(),
               "stage": stage, "services": services, "journey": shopping_summary(journey) if journey is not None else None,
               "incident_id": incident_id, "action_id": action_id}
    encoded = json.dumps(payload, allow_nan=False)
    if len(encoded.encode()) > 32768:
        raise ValueError("Boutique observation exceeds 32 KiB")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    kind = "inventory" if stage == "inventory" else "journey"
    with closing(sqlite3.connect(path, timeout=5)) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS boutique_observations (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("INSERT INTO boutique_observations(kind,payload) VALUES(?,?)", (kind, encoded))
            # Fast inventory polling must not evict the fault/checkout evidence.
            db.execute("DELETE FROM boutique_observations WHERE kind=? AND id NOT IN "
                       "(SELECT id FROM boutique_observations WHERE kind=? ORDER BY id DESC LIMIT ?)",
                       (kind, kind, 60 if kind == "inventory" else 120))
    return payload
