"""Independent persistent Observer targets sharing one explicit cluster reader."""

from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from runtime.observer_pipeline import ObserverStore, is_ready, status_file
from services.observer.kubernetes import KubernetesEvidenceCollector
from services.observer.telemetry import CorrelatedEvidenceCollector, TelemetryStore
from shared import log

LOG = log.get_logger("observer")


def load_targets(path):
    targets = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
        raise ValueError("Expected 1-20 explicit Observer targets")
    seen = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) - {"namespace", "deployment", "service_names"}:
            raise ValueError("Invalid Observer target")
        for key in ("namespace", "deployment"):
            name = target.get(key)
            if not isinstance(name, str) or len(name) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", name):
                raise ValueError("Invalid Kubernetes target name")
        identity = (target["namespace"], target["deployment"])
        if identity in seen:
            raise ValueError("Duplicate Observer target")
        seen.add(identity)
        names = target.setdefault("service_names", [target["deployment"]])
        if (not isinstance(names, list) or not 1 <= len(names) <= 20
                or any(not isinstance(s, str) or not s.strip() or len(s) > 128 for s in names)):
            raise ValueError("Explicit bounded service names are required")
    return targets


def state_path(base, cluster_id, target):
    digest = hashlib.sha256(json.dumps([cluster_id, target["namespace"], target["deployment"]]).encode()).hexdigest()[:20]
    base = Path(base)
    return base.with_name(base.stem + "-" + digest + base.suffix)


def fleet_health(base, cluster_id, targets):
    results = []
    for target in targets:
        try:
            status = status_file(state_path(base, cluster_id, target))
            ready = is_ready(status)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            ready = False
        results.append({**target, "ready": ready})
    return {"ready": all(t["ready"] for t in results), "targets": results}


def run_fleet(args, reader, policy, sink, stop):
    targets = load_targets(args.targets)
    telemetry = TelemetryStore(args.telemetry_db) if args.telemetry_db else None
    with ExitStack() as stack:
        monitors = []
        for target in targets:
            collector = KubernetesEvidenceCollector(reader, namespace=target["namespace"], name=target["deployment"],
                cluster_id=args.cluster_id, collect_logs=args.collect_logs)
            if telemetry:
                collector = CorrelatedEvidenceCollector(collector, telemetry, target["service_names"], policy.get("telemetry", {}))
            path = state_path(args.state, args.cluster_id, target)
            scoped_policy = {**policy, "observer_service_names": target["service_names"]}
            store = stack.enter_context(ObserverStore(path, scoped_policy, collector.target))
            monitors.append((collector, store, path))
        polls = 0
        while not stop.is_set() and (args.poll_count == 0 or polls < args.poll_count):
            for collector, store, path in monitors:
                try:
                    observation = collector.collect()
                    incident = store.observe(observation)
                    event = {"kind": "observer.poll", "incident_id": incident["incident_id"] if incident else None}
                except Exception as error:  # noqa: BLE001 - one target's failed poll must not stop the others
                    log.failure(LOG, "observer.collection.failed", error)
                    store.collection_failed(error)
                    event = {"kind": "observer.collection_error", "error": type(error).__name__}
                delivery = store.flush(sink)
                print(json.dumps({**event, "target": collector.target, "state": str(path),
                                  "delivery": delivery, "status": store.status()}, sort_keys=True), flush=True)
            polls += 1
            if not stop.is_set() and (args.poll_count == 0 or polls < args.poll_count):
                stop.wait(args.interval_seconds)
        return int(any(s.status()["last_collection_error"] or s.status()["last_delivery_error"] for _, s, _ in monitors))
