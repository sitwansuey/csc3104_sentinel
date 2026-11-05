"""Isolated, labelled playback: real Observer/Guard, synthetic other producers."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import tempfile
import time

from runtime.dashboard import ROOT, snapshot
from runtime.guard_pipeline import process_guard_requests
from runtime.telemetry import emit_cluster_snapshot, emit_event
from services.coordinator.core import LocalEventBus, SQLiteStore, approve, handle
from services.observer.core import detect


def start_demo():
    directory = tempfile.TemporaryDirectory(prefix="sentinel-dashboard-demo-")
    db_path = Path(directory.name) / "coordinator.sqlite3"
    journal = Path(directory.name) / "telemetry.sqlite3"
    frames = []

    def fixture(name):
        return json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8"))

    policy = fixture("policy.json")
    observations = fixture("observations.json")["failed-rollout"]
    incident = detect(observations, policy)
    proposal = fixture("messages/proposal.json")
    proposal["incident_id"] = incident["incident_id"]
    deps = fixture("messages/dependency-result.json")
    deps["incident_id"] = incident["incident_id"]
    target = proposal["target"]
    # The 12 playback frames are 3 s apart and end now, so no fabricated time lies in the future.
    started_at = datetime.now(timezone.utc) - timedelta(seconds=36)
    replicas_ready = [1]

    def cluster_sample(index, at):
        """Fabricated time series; never collected from a Kubernetes API."""
        failing = replicas_ready[0] == 0
        phase = index / 3
        nodes = []
        for node_index in range(3):
            nodes.append({
                "name": f"demo-worker-{node_index + 1}", "ready": True,
                "conditions": [{"type": "Ready", "status": "True"}],
                "cpu_allocatable_cores": 4, "memory_allocatable_bytes": 8 * 1024 ** 3,
                "cpu_capacity_cores": 4, "memory_capacity_bytes": 8 * 1024 ** 3,
                "cpu_usage_cores": round(0.9 + node_index * .2 + .18 * math.sin(phase + node_index) + (.5 if failing else 0), 3),
                "memory_usage_bytes": int((2.8 + node_index * .35 + .12 * math.cos(phase / 2 + node_index)) * 1024 ** 3),
                "metrics_timestamp": at.isoformat().replace("+00:00", "Z"), "metrics_window_seconds": 15,
            })
        names = ["demo-api", "mock-db", "sentinel-coordinator", "sentinel-observer",
                 "sentinel-planner", "sentinel-guard", "sentinel-executor", "sentinel-verifier"]
        pods = [{"name": f"{name}-synthetic-001", "namespace": target["namespace"],
                 "node": nodes[i % 3]["name"], "phase": "Running", "ready": not (i == 0 and failing),
                 "containers_ready": 0 if i == 0 and failing else 1, "containers_total": 1,
                 "restarts": 5 if i == 0 and failing else 0,
                 "reason": "CrashLoopBackOff" if i == 0 and failing else None,
                 "cpu_usage_cores": round(.07 + i * .012, 3), "memory_usage_bytes": (48 + i * 8) * 1024 ** 2,
                 "metrics_timestamp": at.isoformat().replace("+00:00", "Z"), "metrics_window_seconds": 15}
                for i, name in enumerate(names)]
        return {"schema_version": "1.0", "cluster_id": target["cluster_id"], "namespace": target["namespace"],
                "observed_at": at.isoformat().replace("+00:00", "Z"),
                "sources": {name: {"status": "available"} for name in ("nodes", "pods", "events", "node_metrics", "pod_metrics")},
                "nodes": nodes, "pods": pods,
                "events": [{"type": "Warning", "reason": "BackOff", "object": "Pod/demo-api-synthetic-001",
                            "namespace": target["namespace"], "count": 5, "last_seen": at.isoformat().replace("+00:00", "Z")}] if failing else [],
                "summary": {"nodes_total": 3, "nodes_ready": 3, "pods_total": 8,
                            "pods_ready": sum(p["ready"] for p in pods), "pods_pending": 0, "pods_failed": 0,
                            "restarts": sum(p["restarts"] for p in pods),
                            "cpu_usage_cores": sum(n["cpu_usage_cores"] for n in nodes),
                            "memory_usage_bytes": sum(n["memory_usage_bytes"] for n in nodes),
                            "cpu_allocatable_cores": 12, "memory_allocatable_bytes": 24 * 1024 ** 3,
                            "pod_cpu_usage_cores": sum(p["cpu_usage_cores"] for p in pods),
                            "pod_memory_usage_bytes": sum(p["memory_usage_bytes"] for p in pods)},
                "truncation": {name: 0 for name in ("nodes", "pods", "events", "node_metrics", "pod_metrics")}}

    def frame_time():
        """Producer time of the next frame; events share it so chart markers align with samples."""
        return started_at + timedelta(seconds=len(frames) * 3)

    def frame(label):
        emit_cluster_snapshot(journal, cluster_sample(len(frames), frame_time()))
        current = snapshot(db_path, journal)
        current.update(mode="synthetic", demo_step=label)
        frames.append(current)

    def observation(ready):
        replicas_ready[0] = ready
        emit_event(journal, component=2, event_type="observation", target=target,
                   summary="Synthetic Deployment observation", metrics={"desired": 1, "ready": ready, "available": ready},
                   at=frame_time().isoformat().replace("+00:00", "Z"))

    try:
        with SQLiteStore(db_path, clock=lambda: datetime(2026, 10, 10, 12, 0, 36, tzinfo=timezone.utc)) as store:
            bus = LocalEventBus()
            for index in range(-24, 0):
                emit_cluster_snapshot(journal, cluster_sample(index, started_at + timedelta(seconds=index * 10)))
            observation(1)
            frame("Healthy workload")
            emit_event(journal, component=1, event_type="fault", target=target,
                       summary="SYNTHETIC injection: startup configuration failure",
                       at=frame_time().isoformat().replace("+00:00", "Z"))
            observation(0)
            frame("Fault injected — synthetic marker")
            handle(incident, store, bus)
            frame("Member 2 detected persistent failure")
            handle(proposal, store, bus)
            frame("Member 3 mock proposal recorded")
            handle(deps, store, bus)
            frame("Member 6 synthetic dependency result")

            def guard(second):
                process_guard_requests(
                    store, bus, state_reader=lambda p: observations[-1], policy_reader=lambda p: policy,
                    baseline_reader=lambda p: fixture("baseline.json"),
                    clock=lambda: datetime(2026, 10, 10, 12, 0, second, tzinfo=timezone.utc))

            guard(35)
            frame("Member 4 escalated: operator approval required")
            approve(proposal["action_id"], "SYNTHETIC-DEMO-OPERATOR", store)
            frame("Member 1 synthetic approval — revalidation queued")
            guard(40)
            frame("Member 4 allowed — real Guard evaluation, fabricated inputs")
            execution = fixture("messages/execution-result.json")
            execution["incident_id"] = incident["incident_id"]
            handle(execution, store, bus)
            frame("Member 5 mock execution result — no Kubernetes write")
            observation(1)
            frame("Synthetic health samples return")
            outcome = fixture("messages/outcome.json")
            outcome["incident_id"] = incident["incident_id"]
            handle(outcome, store, bus)
            frame("Member 6 mock verification — synthetic recovery")
            frame("Playback complete — restarts automatically")
    except BaseException:
        directory.cleanup()
        raise
    started = time.monotonic()

    def provider():
        result = deepcopy(frames[int((time.monotonic() - started) // 3) % len(frames)])
        result["generated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return result

    return provider, directory.cleanup
