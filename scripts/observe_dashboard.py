"""Read Kubernetes workload/cluster metrics and route Observer incidents.

Only kubectl get is used. The collector never plans, approves or applies recovery.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.cluster_metrics import collect_cluster_snapshot
from runtime.telemetry import emit_cluster_snapshot, emit_event
from services.coordinator.core import LocalEventBus, SQLiteStore, handle
from services.observer.kubernetes_adapter import deployment_to_observation
from services.observer.core import Observer, validate_observation


def collect_once(*, context, cluster_id, namespace, observer, store, telemetry,
                 runner=subprocess.run, clock=None):
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    response = runner(["kubectl", "--context", context, "--request-timeout=8s", "get",
                       "deployments", "--namespace", namespace, "-o", "json"],
                      capture_output=True, text=True, timeout=10)
    if response.returncode:
        # Avoid publishing kubeconfig paths, credentials or arbitrary stderr.
        raise RuntimeError("Kubernetes read failed; check the context, namespace and read permissions")
    document = json.loads(response.stdout)
    items = document.get("items") if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise ValueError("Deployment list is invalid")
    count = 0
    for deployment in items:
        observation = deployment_to_observation(deployment, cluster_id=cluster_id, observed_at=now)
        observed = validate_observation(observation)
        emit_event(telemetry, component=2, event_type="observation", target=observed.target,
                   metrics={"desired": observed.desired_replicas, "ready": observed.ready_replicas,
                            "available": observed.available_replicas, "generation": observed.generation,
                            "observed_generation": observed.observed_generation},
                   summary="Kubernetes Deployment replica observation", at=observation["captured_at"])
        incident = observer.observe(observation)
        if incident:
            handle(incident, store, LocalEventBus())
        count += 1
    emit_event(telemetry, component=2, event_type="heartbeat",
               summary=f"Read {count} Deployments in {namespace}", at=now.isoformat().replace("+00:00", "Z"))
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--cluster-id", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--policy", type=Path, default=Path("data/sentinel/samples/policy.json"))
    parser.add_argument("--db", type=Path, default=Path("data/sentinel/runs/coordinator.sqlite3"))
    parser.add_argument("--telemetry", type=Path, default=Path("data/sentinel/runs/telemetry.sqlite3"))
    parser.add_argument("--interval", type=float, default=3)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.interval <= 60:
        parser.error("--interval must be between 1 and 60 seconds")
    observer = Observer(json.loads(args.policy.read_text(encoding="utf-8-sig")))
    args.db.parent.mkdir(parents=True, exist_ok=True)
    try:
        with SQLiteStore(args.db) as store:
            while True:
                failed = False
                try:
                    count = collect_once(context=args.context, cluster_id=args.cluster_id,
                                         namespace=args.namespace, observer=observer, store=store,
                                         telemetry=args.telemetry)
                    print(f"Observed {count} Deployments", flush=True)
                except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
                    summary = f"Collector read/processing failed ({type(exc).__name__}); no recovery attempted"
                    emit_event(args.telemetry, component=2, event_type="error", summary=summary)
                    print(summary, file=sys.stderr, flush=True)
                    failed = True
                # This is independent of Deployment observation. A missing
                # Metrics API or cluster-wide nodes permission must not discard
                # available namespace Pod health and Warning event observations.
                try:
                    cluster = collect_cluster_snapshot(context=args.context, cluster_id=args.cluster_id,
                                                       namespace=args.namespace)
                    emit_cluster_snapshot(args.telemetry, cluster)
                    available = sum(value["status"] == "available" for value in cluster["sources"].values())
                    print(f"Cluster metrics: {available}/5 sources available", flush=True)
                    if not any(cluster["sources"][name]["status"] == "available" for name in ("nodes", "pods")):
                        failed = True
                except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
                    summary = f"Cluster metrics collection failed ({type(exc).__name__}); no recovery attempted"
                    emit_event(args.telemetry, component=2, event_type="error", summary=summary)
                    print(summary, file=sys.stderr, flush=True)
                    failed = True
                if args.once:
                    return 1 if failed else 0
                time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
