"""P4.02 control-plane drills; live commands are restricted to kind-sentinel-dist.

Live commands use an existing kind_acceptance deploy --run-dir. The Coordinator
must already have SENTINEL_LOG_LEVEL=DEBUG: idle claims and renewals otherwise
leave no access evidence. Raw database copies stay in a temporary directory;
only allowlisted measurements, restore-check listings and audit rows are saved.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from uuid import uuid4

from experiments.distributed import audit, kind_acceptance as ka, kind_acceptance_live as live
from scripts import coordinator_restore_check

WORKERS = tuple(identity for identity in ka.IDENTITIES if identity != "observer-1")
COORDINATOR = "sentinel-coordinator"
DATABASE = "/data/coordinator.sqlite3"
BACKUP = "/data/p402-backup.sqlite3"
FAILURES = (ka.AcceptanceError, AssertionError, TimeoutError, OSError, ValueError, LookupError,
            sqlite3.Error, subprocess.SubprocessError)
MANUAL = ("Check the summary's cleanup and restore-check listing. Remove only this drill's NetworkChaos or "
          "maintenance Pod in sentinel-dist; ensure the maintenance Pod has terminated before scaling "
          "sentinel-coordinator to one. Run coordinator_restore_check before accepting any stopped worker; "
          "resolve review items, then restore demo-api from inputs/baseline.json and settle the lab by hand.")

# Each source connection is read-only. The backup lives on the Coordinator PVC,
# so scaling its Deployment to zero cannot discard the backup.
BACKUP_SCRIPT = """import json, sqlite3, sys
s = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True, timeout=10)
d = sqlite3.connect(sys.argv[2], timeout=10)
try:
    s.backup(d)
    row = d.execute('SELECT generation, commit_counter FROM store_meta WHERE id=1').fetchone()
    print(json.dumps({'generation': row[0], 'commit_counter': row[1]}))
finally:
    d.close(); s.close()
"""
RESTORE_SCRIPT = """import json, sqlite3, sys
s = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True, timeout=10)
d = sqlite3.connect(sys.argv[2], timeout=10)
try:
    s.backup(d)
    if d.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise RuntimeError('Restored database failed integrity_check')
    row = d.execute('SELECT generation, commit_counter FROM store_meta WHERE id=1').fetchone()
    print(json.dumps({'generation': row[0], 'commit_counter': row[1]}))
finally:
    d.close(); s.close()
"""
PROBE_SCRIPT = """import json, urllib.error, urllib.request
try:
    r = urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=3)
except urllib.error.HTTPError as error:
    r = error
print(json.dumps({'http_status': r.code, 'body': json.load(r)}))
"""


def seconds(text):
    moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("A measurement timestamp has no timezone")
    return moment.timestamp()


def first_after(start, values):
    origin = seconds(start)
    later = [seconds(value) - origin for value in values if seconds(value) >= origin]
    return round(min(later), 3) if later else None


def recovery(fault_at, ready_at, access, transitions):
    """Kill-relative RTO; only the five delivery workers count, never the Observer/operator.

    A claimed command is distinct from a served idle poll. This preserves a
    meaningful None for first claim when no command was pending after S5.
    """
    served = [line for line in access if line.get("identity") in WORKERS and line.get("http_status") == 200]
    return {"to_ready": first_after(fault_at, [ready_at] if ready_at else []),
            "to_first_claim": first_after(fault_at, [line["ts"] for line in served
                                                    if line.get("operation") == "claim"
                                                    and line.get("outcome") == "claimed"]),
            "to_first_served_request": first_after(fault_at, [line["ts"] for line in served]),
            "to_next_transition": first_after(fault_at, [row["recorded_at"] for row in transitions])}


def rto_from_runs(paths):
    files = set()
    for path in map(Path, paths):
        # A run directory's own summary.json is its overview, not a run: always search below it too.
        files.update([path] if path.is_file() else path.rglob("summary.json"))
    runs, errors = [], []
    for path in sorted(files):
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
            if summary.get("scenario") != "S5":
                continue
            records = json.loads(path.with_name("records.json").read_text(encoding="utf-8"))
            fault_at = summary.get("clock", {}).get("fault_at_cluster_clock")
            if not fault_at:
                offsets = summary.get("clock", {}).get("control_plane_minus_harness", [])
                if not offsets:
                    raise ValueError("S5 has no measured clock offset")
                fault_at = ka.shift(summary["fault_at"], offsets[0]["offset_seconds"])
            ready = summary.get("notes", {}).get("pod_delete", {}).get("ready_at")
            measured = recovery(fault_at, ready, records["access"], records["transitions"])
            runs.append({"run": path.parent.name, "source_passed": summary.get("passed"),
                         "fault_at": fault_at, **measured})
        except (OSError, ValueError, LookupError, TypeError) as error:
            errors.append({"run": path.parent.name, "error": str(error) if isinstance(error, ValueError)
                           else type(error).__name__})
    aggregates = {}
    for key in ("to_ready", "to_first_claim", "to_first_served_request", "to_next_transition"):
        values = [row[key] for row in runs if row[key] is not None]
        aggregates[key] = {"samples": len(values), "missing": len(runs) - len(values),
                           "min": min(values) if values else None,
                           "median": statistics.median(values) if values else None,
                           "max": max(values) if values else None}
    return {"drill": "rto-from-runs", "runs": runs, "aggregate_seconds": aggregates, "errors": errors,
            "worker_identities": WORKERS, "origin": "Coordinator Pod delete, on the cluster clock",
            "passed": bool(runs) and not errors and all(row["to_ready"] is not None
                      and row["to_first_served_request"] is not None and row["source_passed"] is True for row in runs)}


def json_lines(text):
    records = []
    for raw in text.splitlines():
        try:
            record = json.loads(raw)
        except ValueError:
            continue  # Worker stdout also contains plain status lines; access completeness is checked separately.
        if isinstance(record, dict):
            records.append(record)
    return records


def access_lines(text):
    fields = ("ts", "identity", "operation", "http_status", "outcome")
    return [{key: row.get(key) for key in fields} for row in json_lines(text)
            if row.get("event") in {"http.access", "auth.denied"}]


class Steps:
    """Record bounded operations, including failures, without command arguments or credentials."""

    def __init__(self, lab, report):
        self.lab, self.report = lab, report
        report["steps"] = []

    def call(self, name, function, *args, **kwargs):
        row = {"step": name, "started_at": ka.stamp(self.lab.clock())}
        self.report["steps"].append(row)
        start = self.lab.monotonic()
        try:
            value = function(*args, **kwargs)
        except BaseException as error:  # Keep failed/interrupted steps, then preserve the original exception.
            row.update(status="failed", error_type=type(error).__name__)
            raise
        else:
            row["status"] = "completed"
            return value
        finally:
            row.update(ended_at=ka.stamp(self.lab.clock()), seconds=round(self.lab.monotonic() - start, 3))


class DrillLab(ka.Lab):
    """Small additional Pod operations; every mutation is namespace/context constrained."""

    def exclusive_volume(self, allowed=()):
        """Refuse an earlier maintenance Pod or any other potential writer of this PVC."""
        pods = self.shell.json("get", "pods", namespace=ka.NAMESPACE)["items"]
        for pod in pods:
            if pod["metadata"]["name"] in allowed:
                continue
            if any(volume.get("persistentVolumeClaim", {}).get("claimName") == "sentinel-dist-coordinator-data"
                   for volume in pod["spec"].get("volumes", [])):
                raise ka.AcceptanceError("Another Pod mounts the Coordinator PVC; stop and inspect it by hand")

    def require_debug(self):
        ka.check_context(self.shell.context)
        deployment = self.shell.json("get", "deployment", COORDINATOR, namespace=ka.NAMESPACE)
        if deployment["spec"].get("replicas") != 1 or deployment["spec"]["strategy"]["type"] != "Recreate":
            raise ka.AcceptanceError("The drill requires one Recreate Coordinator replica")
        container = next(row for row in deployment["spec"]["template"]["spec"]["containers"]
                         if row["name"] == "coordinator")
        if {item["name"]: item.get("value") for item in container.get("env", [])}.get("SENTINEL_LOG_LEVEL") != "DEBUG":
            raise ka.AcceptanceError("Set SENTINEL_LOG_LEVEL=DEBUG on the Coordinator and wait for its rollout first")
        ready = self.read("the Ready DEBUG Coordinator", lambda: live._ready_coordinator(self))
        if not ready:
            raise ka.AcceptanceError("The Coordinator is not Ready")
        active = next(row for row in ready["spec"]["containers"] if row["name"] == "coordinator")
        if {item["name"]: item.get("value") for item in active.get("env", [])}.get("SENTINEL_LOG_LEVEL") != "DEBUG":
            raise ka.AcceptanceError("The DEBUG Coordinator rollout is not complete")
        self.exclusive_volume([ready["metadata"]["name"]])
        status = next(item for item in ready["status"]["containerStatuses"] if item["name"] == "coordinator")
        return {"level": "DEBUG", "pod_uid": ready["metadata"]["uid"], "restarts": status["restartCount"]}

    def position(self):
        [row] = self.query("SELECT generation, commit_counter FROM store_meta WHERE id=1", strict=True)
        if not row["generation"] or type(row["commit_counter"]) is not int:
            raise ka.AcceptanceError("The Coordinator has no store generation/counter")
        return row

    def backup(self):
        answer = self.exec_python(COORDINATOR, "coordinator", BACKUP_SCRIPT, DATABASE, BACKUP, timeout=120)
        return json.loads(answer.stdout)

    def rtt(self):
        start = self.monotonic()
        self.shell.kubectl("get", "deployment", COORDINATOR, "--output=name", namespace=ka.NAMESPACE, timeout=30)
        return self.monotonic() - start

    def probe_worker(self, identity):
        if identity not in WORKERS:
            raise ka.AcceptanceError("Only delivery workers have a readiness probe")
        return json.loads(self.exec_python(ka.IDENTITIES[identity][0], "worker", PROBE_SCRIPT, timeout=15).stdout)

    def accept_worker(self, identity):
        if identity not in WORKERS:
            raise ka.AcceptanceError("Only checked delivery workers may be cleared")
        pod, _ = self.pod(ka.IDENTITIES[identity][0])
        result = self.shell.kubectl("exec", pod, "--container=worker", "--", "python", "-m",
                                    "runtime.worker_service", "--config", "/config/worker.json", "--accept-store",
                                    namespace=ka.NAMESPACE, timeout=60)
        if not any(row.get("status") == "store_accepted" for row in json_lines(result.stdout)):
            raise ka.AcceptanceError("--accept-store did not confirm store_accepted")

    def stop_coordinator(self):
        ka.check_context(self.shell.context)
        self.shell.kubectl("scale", "deployment/" + COORDINATOR, "--current-replicas=1", "--replicas=0",
                           namespace=ka.NAMESPACE, timeout=60)
        self.wait(lambda: not self.shell.json("get", "pods", "--selector=app=" + COORDINATOR,
                                              namespace=ka.NAMESPACE)["items"],
                  180, "Coordinator Pods did not terminate; do not copy the database", interval=1)

    def maintenance(self, name):
        ka.check_context(self.shell.context)
        deployment = self.shell.json("get", "deployment", COORDINATOR, namespace=ka.NAMESPACE)
        if deployment["spec"].get("replicas") != 0 or self.shell.json(
                "get", "pods", "--selector=app=" + COORDINATOR, namespace=ka.NAMESPACE)["items"]:
            raise ka.AcceptanceError("The Coordinator must be fully stopped before mounting its PVC")
        self.exclusive_volume()
        spec = deployment["spec"]["template"]["spec"]
        container = next(row for row in spec["containers"] if row["name"] == "coordinator")
        data = next(row for row in spec["volumes"] if row["name"] == "data")
        if data.get("persistentVolumeClaim", {}).get("claimName") != "sentinel-dist-coordinator-data":
            raise ka.AcceptanceError("Unexpected Coordinator PVC; refused")
        document = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": ka.NAMESPACE},
                    "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                             "nodeSelector": {"sentinel.io/host": "c"},
                             "activeDeadlineSeconds": 600,
                             "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                                 "runAsGroup": 10001, "fsGroup": 10001,
                                                 "seccompProfile": {"type": "RuntimeDefault"}},
                             "containers": [{"name": "maintenance", "image": container["image"],
                                             "imagePullPolicy": "Never",
                                             "command": ["python", "-c", "import time; time.sleep(580)"],
                                             "securityContext": {"allowPrivilegeEscalation": False,
                                                                 "readOnlyRootFilesystem": True,
                                                                 "capabilities": {"drop": ["ALL"]}},
                                             "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                             "volumes": [data]}}
        self.shell.create(document, namespace=ka.NAMESPACE)
        self.shell.kubectl("wait", "pod/" + name, "--for=condition=Ready", "--timeout=120s",
                           namespace=ka.NAMESPACE, timeout=135)

    def maintenance_exec(self, name, script, *args, binary=False):
        # Recheck the zero-replica/no-Pod exclusion immediately before the copy.
        if self.shell.json("get", "deployment", COORDINATOR, namespace=ka.NAMESPACE)["spec"].get("replicas") != 0:
            raise ka.AcceptanceError("Coordinator replicas changed during maintenance; do not restore")
        if self.shell.json("get", "pods", "--selector=app=" + COORDINATOR, namespace=ka.NAMESPACE)["items"]:
            raise ka.AcceptanceError("A Coordinator Pod appeared during maintenance; do not restore")
        self.exclusive_volume([name])
        return self.shell.kubectl("exec", name, "--container=maintenance", "--", "python", "-c", script, *args,
                                  namespace=ka.NAMESPACE, timeout=120, binary=binary)

    def remove_maintenance(self, name):
        self.shell.kubectl("delete", "pod", name, "--ignore-not-found=true", "--wait=true", "--timeout=120s",
                           namespace=ka.NAMESPACE, timeout=135)
        if self.shell.kubectl("get", "pod", name, "--ignore-not-found=true", "--output=name",
                              namespace=ka.NAMESPACE).stdout.strip():
            raise ka.AcceptanceError("The maintenance Pod still exists; keep the Coordinator stopped")

    def start_coordinator(self):
        self.exclusive_volume()
        self.shell.kubectl("scale", "deployment/" + COORDINATOR, "--current-replicas=0", "--replicas=1",
                           namespace=ka.NAMESPACE, timeout=60)
        ready = self.wait(lambda: live._ready_coordinator(self), 180, "Coordinator did not become Ready", interval=1)
        return ka.ready_since(ready)


def incident_recovered(lab, policy_version, timeout=ka.DEADLINE_SECONDS):
    def recovered():
        lab.approve_pending(policy_version)
        rows = lab.incidents()
        if rows and all(row["state"] in ka.invariants.TERMINAL for row in rows):
            if any(row["state"] != "RECOVERED" for row in rows):
                raise ka.AcceptanceError("An incident ended without RECOVERED")
            return rows
        return None
    return lab.wait(recovered, timeout, "incident did not reach RECOVERED", interval=1)


def partition_measurements(start, end, heal, access, transitions, rows, rtt, uncertainty=0):
    """Confirmed isolation window; recovery uses a conservative pre-delete anchor.

    Widen the isolation window by the measured clock error. Recovery latency
    adds that error too, so clock uncertainty cannot turn a slow run into a pass.
    """
    low, high = seconds(start) - uncertainty, seconds(end) + uncertainty
    writes = [row for row in rows if low <= seconds(row["at"]) <= high and row["sentinel"]
              and row["effect"] in {"template_write", "unclassified"}]
    renewals = [line for line in access if low <= seconds(line["ts"]) <= high
                and line.get("identity") in WORKERS and line.get("operation") == "renew"
                and line.get("http_status") == 200]
    result = recovery(heal, None, access, transitions)
    bound = 15 + rtt
    failures = []
    if writes:
        failures.append("Sentinel wrote a template (or an unclassified change) during isolation")
    if renewals:
        failures.append("A worker lease renewal was answered 200 during isolation")
    for key in ("to_first_served_request", "to_next_transition"):
        value = result[key]
        if value is None or value + uncertainty > bound:
            failures.append(f"{key} exceeded 15 s + measured kubectl RTT, or was not observed")
    return {"template_writes": len(writes), "renewals_answered": len(renewals),
            "renewal_operation": "renew", "recovery_seconds": result, "bound_seconds": bound,
            "rtt_seconds": rtt, "clock_uncertainty_seconds": uncertainty, "failures": failures,
            "passed": not failures}


def partition_drill(lab, run, steps, report, *, duration):
    if not math.isfinite(duration) or not 1 <= duration <= 540:
        raise ka.AcceptanceError("--seconds must be finite and between 1 and 540 (Chaos Mesh duration bound)")
    row = steps.call("wait for approval or READY", lab.wait_state, {"AWAITING_APPROVAL", "READY"}, 180)
    # As in S6, partition before approval so the pending action cannot race
    # through a Kubernetes write between approval and injection.
    workers = [ka.IDENTITIES[identity][0] for identity in ka.IDENTITIES]
    cut = ka.partition(run.name("all"), COORDINATOR, workers,
                       math.ceil(duration + ka.CHAOS_CONFIRM_SECONDS + ka.APPROVAL_RESEND_SECONDS + 240))
    steps.call("create and confirm all-worker partition", run.create, cut)
    confirmed = ka.stamp(lab.clock())
    if row["state"] == "AWAITING_APPROVAL":
        steps.call("approve partitioned action", lab.read, "proposal to approve",
                   lambda: lab.approve(row["active_action_id"], run.policy_version))
    if run.window_left(cut) < duration:
        raise ka.AcceptanceError("Confirmed partition has insufficient duration left")
    # Small sleeps let an operator interrupt a long hold; the deadline is
    # monotonic and the final in-effect check rejects early chaos expiry.
    deadline = lab.monotonic() + duration
    while lab.monotonic() < deadline:
        lab.sleep(min(1, deadline - lab.monotonic()))
    rtt = steps.call("measure kubectl round trip", lab.rtt)
    removal_requested = ka.stamp(lab.clock())
    steps.call("remove confirmed partition", run.remove, cut)
    removed = ka.stamp(lab.clock())
    offset = run.offset
    cluster = lambda value: ka.shift(value, offset["offset_seconds"])
    report["partition"] = {"confirmed_at": confirmed, "start": cluster(confirmed), "end": cluster(removal_requested),
                           "seconds": duration, "removal_requested_at": cluster(removal_requested),
                           "removed_at": cluster(removed), "rtt_seconds": rtt,
                           "rtt_method": "harness monotonic duration of kubectl get deployment sentinel-coordinator",
                           "recovery_origin": "delete request start; conservative upper bound including removal"}
    # Polling records may take much longer than the bound; judge timestamps,
    # never the time the harness happened to copy the records.
    def progress():
        access = access_lines(lab.log_text("coordinator", run.since))
        transitions = lab.query("SELECT * FROM transitions WHERE incident_id=? ORDER BY seq", (row["incident_id"],))
        return (access, transitions) if (recovery(cluster(removal_requested), None, access, transitions)[
            "to_first_served_request"] is not None and recovery(cluster(removal_requested), None, access,
                                                               transitions)["to_next_transition"] is not None) else None
    access, transitions = steps.call("wait for served request and incident progress", lab.wait, progress,
                                     90, "no post-heal request/incident transition")
    report["partition_access"] = access
    report["partition_transitions"] = transitions


def run_check(lab, scratch, journals, previous, steps, *, restored=None):
    database = restored or scratch / "current.db"
    if restored is None and not steps.call("copy current Coordinator read-only", lab.copy_database,
                                          "coordinator", DATABASE, database):
        raise ka.AcceptanceError("The Coordinator database copy is missing")
    command = [sys.executable, str(ka.ROOT / "scripts/coordinator_restore_check.py"), "--db", str(database),
               "--previous-generation", previous["generation"], "--previous-commit", str(previous["commit_counter"]),
               "--json"]
    for journal in journals:
        command += ["--journal", str(journal)]
    answer = steps.call("coordinator_restore_check --previous-commit", lab.shell.run,
                        command, timeout=60, check=False)
    recorded = {"exit_code": answer.returncode}
    steps.report.setdefault("restore_checks", []).append(recorded)
    if answer.returncode not in {0, 1}:
        recorded["listing"] = (answer.stdout or answer.stderr).strip()
        raise ka.AcceptanceError(f"Restore check failed with exit {answer.returncode}; workers remain stopped")
    checked = json.loads(answer.stdout)
    if not isinstance(checked, dict) or not all(key in checked for key in (
            "database", "journals", "missing", "rolled_back", "lost_commits", "may_lose_acknowledged_work")):
        raise ka.AcceptanceError("Restore check returned an incomplete report; workers remain stopped")
    if answer.returncode != int(bool(checked["may_lose_acknowledged_work"])):
        raise ka.AcceptanceError("Restore check exit code disagrees with its report; workers remain stopped")
    recorded.update(report=checked, listing=coordinator_restore_check.render(checked))
    return recorded


def check_replays(lab, scratch, journals, previous, initial, steps, report):
    """Clear a replay holder, verify its results are back, then reconsider review items.

    Frozen journal copies retain the pre-restore acknowledged messages even
    if a worker's live delivery row is subsequently rebound by a replay.
    No worker is cleared until the checker has successfully read every copy.
    """
    checked, cleared = initial, set()
    report["workers_cleared"] = []
    report["rechecks"] = []
    # A replayed result can make the restored Coordinator ask again for the next result in the chain, so
    # a "review" item may turn into a "replay" one, also for a worker already cleared; bound the rounds by
    # the items that can still be resent rather than by the number of workers.
    for _ in range(4 * len(WORKERS)):
        missing = [item for journal in checked["report"]["journals"] for item in journal["missing"]]
        if not missing:
            for identity in WORKERS:
                if identity not in cleared:
                    steps.call("accept checked store: " + identity, lab.accept_worker, identity)
                    report["workers_cleared"].append(identity)
            return
        staged = {replay_key(journal, item) for journal in checked["report"]["journals"]
                  for item in journal["missing"] if item["redrive"] == "replay"}
        if not staged or any(identity not in WORKERS for identity, *_ in staged):
            raise ka.AcceptanceError("Restore check has unresolved review items; workers require manual review")
        # A worker already cleared resends its saved result by itself when it claims; only new holders are cleared.
        for identity in sorted({identity for identity, *_ in staged} - cleared):
            steps.call("accept replay holder: " + identity, lab.accept_worker, identity)
            report["workers_cleared"].append(identity)
            cleared.add(identity)

        def returned(staged=frozenset(staged)):
            # Wait for exactly the results being replayed; other items of the same worker may stay under review.
            current = run_check(lab, scratch, journals, previous, steps)
            report["rechecks"].append(current)
            still = {replay_key(journal, item) for journal in current["report"]["journals"]
                     for item in journal["missing"]}
            return current if not staged & still else None
        checked = steps.call("wait for acknowledged replays", lab.wait, returned, 120,
                             "Acknowledged replay results did not return; do not clear peers", interval=2)
    raise ka.AcceptanceError("Restore replay staging exhausted its bound; review the remaining results by hand")


def replay_key(journal, item):
    """One acknowledged result in a restore check: the worker, the result kind, its action and topic."""
    return (Path(journal["journal"]).parent.name, item.get("kind"), item.get("action_id"), item.get("topic"))


def detection(lab, since, restored_at, backup, steps, report, *, timeout=120, offset_seconds=0):
    found = {}

    def all_detected():
        for identity in WORKERS:
            if identity in found:
                continue
            lines = json_lines(lab.log_text(identity, since))
            changes = [row for row in lines if row.get("event") in {
                "worker.store_generation_changed", "worker.store_rollback_detected"}
                and seconds(row["ts"]) >= seconds(restored_at)]
            if not changes:
                continue
            change = min(changes, key=lambda row: seconds(row["ts"]))
            probe = lab.probe_worker(identity)
            if probe["http_status"] != 503 or probe["body"].get("store_changed") is not True:
                continue
            previous_commit = change.get("previous_commit")
            if (change.get("previous_generation") != backup["generation"]
                    or type(previous_commit) is not int or previous_commit <= backup["commit_counter"]
                    or type(change.get("commit_counter")) is not int or change["commit_counter"] >= previous_commit):
                raise ka.AcceptanceError("Worker detection did not prove it saw the later store counter")
            found[identity] = {key: change.get(key) for key in ("ts", "event", "previous_commit", "commit_counter")}
            observed_at = ka.shift(ka.stamp(lab.clock()), offset_seconds)
            found[identity].update(readyz=probe, seconds=round(seconds(change["ts"]) - seconds(restored_at), 3),
                                   readyz_observed_at=observed_at,
                                   readyz_observed_seconds=round(seconds(observed_at) - seconds(restored_at), 3))
        report["detection"] = dict(found)
        return len(found) == len(WORKERS)
    steps.call("detect store rollback on every delivery worker", lab.wait, all_detected,
               timeout, "Workers did not all log store change and answer readyz 503", interval=1)
    steps.call("observe stopped claims", lab.sleep, 6)
    access = access_lines(lab.log_text("coordinator", since))
    claimed_after = [row for row in access if row["operation"] == "claim" and row["identity"] in found
                     and seconds(row["ts"]) > seconds(found[row["identity"]]["ts"])]
    if claimed_after:
        raise ka.AcceptanceError("A worker continued claiming after logging store change")
    report["claims_after_detection"] = len(claimed_after)
    report["detection_log_seconds"] = max(item["seconds"] for item in found.values())
    report["detection_seconds"] = max(max(item["seconds"], item["readyz_observed_seconds"])
                                      for item in found.values())


def restore_drill(lab, run, steps, report, scratch):
    ka.check_context(lab.shell.context)  # Also enforced when called directly, before even arming a fault point.
    stem = "executor.before_patch"
    run.armed += [(identity, stem) for identity in ka.EXECUTORS]
    steps.call("arm Executor before patch", lab.arm, ka.EXECUTORS, stem, "block")
    steps.call("inject S0 incident", lab.inject)
    row = steps.call("wait for managed approval", lab.wait_state, {"AWAITING_APPROVAL", "READY"}, 180)
    if row["state"] == "AWAITING_APPROVAL":
        steps.call("approve initial incident", lab.approve, row["active_action_id"], run.policy_version)
    holder = steps.call("wait for held Executor", lab.first_reached, ka.EXECUTORS, stem, timeout=180)
    run.armed.remove((holder, stem))
    run.hold(holder, stem)
    steps.call("confirm READY backup point", lab.wait_state, {"READY"}, 30)
    report["backup"] = steps.call("SQLite read-only consistent backup inside Coordinator Pod", lab.backup)
    steps.call("release Executor", run.release, holder, stem)
    report["backup_fault_cleanup"] = steps.call("disarm backup fault points", run.cleanup)
    if report["backup_fault_cleanup"]["problems"]:
        raise ka.AcceptanceError("Backup fault points did not clear: "
                                 + "; ".join(report["backup_fault_cleanup"]["problems"]))
    report["first_incident"] = steps.call("finish S0 incident", incident_recovered, lab, run.policy_version)
    report["before_stop"] = steps.call("note previous store counter before shutdown", lab.position)
    if (report["before_stop"]["generation"] != report["backup"]["generation"]
            or report["before_stop"]["commit_counter"] <= report["backup"]["commit_counter"]):
        raise ka.AcceptanceError("Work did not commit after the consistent backup")
    # Snapshot all journals read-only before any clearing; retain the original
    # acknowledged results for every subsequent checker invocation.
    journals = []
    for identity in WORKERS:
        destination = scratch / identity / "transport.db"
        if not steps.call("copy journal read-only: " + identity, lab.copy_database,
                          identity, f"/data/{identity}/transport.db", destination):
            raise ka.AcceptanceError("A worker journal is missing; no worker may be cleared")
        journals.append(destination)
    name, stopped = "p402-restore-" + uuid4().hex[:12], False
    report["maintenance_pod"] = name
    report["maintenance_removed"], report["coordinator_restarted"] = False, False
    restored = scratch / "restored.db"
    try:
        # Mark before scale: even a lost scale reply requires reconciliation
        # in finally; never assume a failed command did not take effect.
        stopped = True
        steps.call("scale Coordinator to zero and wait for all Pods to terminate", lab.stop_coordinator)
        steps.call("create PVC maintenance Pod", lab.maintenance, name)
        position_script = ka.QUERY_HELPER
        report["before_restore"] = json.loads(steps.call("note final counter with Coordinator stopped",
            lab.maintenance_exec, name, position_script, DATABASE,
            "SELECT generation, commit_counter FROM store_meta WHERE id=1", "[]").stdout)[0]
        if (report["before_restore"]["generation"] != report["backup"]["generation"]
                or report["before_restore"]["commit_counter"] <= report["backup"]["commit_counter"]):
            raise ka.AcceptanceError("Stopped Coordinator does not contain the later store position")
        report["restored"] = json.loads(steps.call("restore backup with no Coordinator writers",
            lab.maintenance_exec, name, RESTORE_SCRIPT, BACKUP, DATABASE).stdout)
        if report["restored"] != report["backup"]:
            raise ka.AcceptanceError("Restored store position differs from the backup")
        report["restore_completed_at"] = ka.shift(ka.stamp(lab.clock()), run.offset["offset_seconds"])
        data = steps.call("copy restored database read-only before startup", lab.maintenance_exec,
                          name, ka.BACKUP_HELPER, DATABASE, binary=True).stdout
        if not data:
            raise ka.AcceptanceError("The restored database copy is empty")
        restored.write_bytes(data)
    finally:
        if stopped:
            # A failure to terminate the helper prevents restarting the writer.
            steps.call("remove maintenance Pod and confirm termination", lab.remove_maintenance, name)
            report["maintenance_removed"] = True
            # Scale may have failed before changing replicas. Only restart from
            # zero; a different replica count is refused by the scale precondition.
            report["ready_at"] = steps.call("restart Coordinator and wait Ready", lab.start_coordinator)
            report["coordinator_restarted"] = True
    report["rpo_lost_commits"] = (report["before_restore"]["commit_counter"]
                                  - report["restored"]["commit_counter"])
    report["detection_origin"] = "restore copy completion on cluster clock; includes Coordinator restart"
    detection_error = None
    try:
        steps.call("measure restore detection", detection, lab, run.since, report["restore_completed_at"],
                   report["backup"], steps, report, offset_seconds=run.offset["offset_seconds"])
    except FAILURES as error:
        detection_error = f"{type(error).__name__}: {error}"
        report["detection_error"] = detection_error
    checked = run_check(lab, scratch, journals, report["before_restore"], steps, restored=restored)
    report["restore_check"] = checked
    check = checked["report"]
    if (checked["exit_code"] != 1 or not check["rolled_back"] or not check["missing"]
            or check["lost_commits"] != report["rpo_lost_commits"]):
        raise ka.AcceptanceError("Restore check did not confirm acknowledged work lost; workers remain stopped")
    missing_kinds = {item["kind"] for journal in check["journals"] for item in journal["missing"]}
    if not {"execution_result", "outcome"} <= missing_kinds:
        raise ka.AcceptanceError("The snapshots do not prove both execution and verification acknowledgements lost")
    if detection_error:
        raise ka.AcceptanceError("Restore detection failed; checker listing recorded, no workers cleared: "
                                 + detection_error)
    steps.call("stage replay holders before clearing peers", check_replays, lab, scratch, journals,
               report["before_restore"], checked, steps, report)
    report["replayed_incident"] = steps.call("recover restored incident", incident_recovered, lab, run.policy_version)
    report["after_replay_check"] = run_check(lab, scratch, journals, report["before_restore"], steps)
    if report["after_replay_check"]["report"]["missing"]:
        raise ka.AcceptanceError("Acknowledged results remain missing after replay; manual review required")
    # Restore/settle before begin_run so only a newly injected incident can
    # satisfy the final recovery check.
    steps.call("restore target before fresh incident", lab.restore, report["baseline"])
    def leftovers():
        lab.approve_pending(run.policy_version)
        return run.leftovers()
    settled = steps.call("settle before fresh incident", lab.settle, report["baseline"], leftovers=leftovers)
    if not settled["settled"]:
        raise ka.AcceptanceError("The lab did not settle after accepting the restored store")
    steps.call("begin fresh incident scope", lab.begin_run, report["baseline"], report.get("policy"))
    steps.call("inject fresh S0 incident", lab.inject)
    report["fresh_incident"] = steps.call("recover fresh incident", incident_recovered, lab, run.policy_version)
    report["measured"] = True


def live_drill(lab, command, deployed, baseline, policy, *, duration=120):
    """Restore/settle in finally on every measured failure; never clear workers in cleanup."""
    ka.check_context(lab.shell.context)
    report = {"drill": command, "context": ka.CONTEXT, "passed": False, "failures": [],
              "baseline": baseline, "policy": policy, "started_at": ka.stamp(lab.clock()), "limits": live.LIMITS}
    steps = Steps(lab, report)
    run = live.Run(lab, ka.SCENARIOS["S6" if command == "partition" else "S0"], 402, deployed["policy_version"])
    generation = lab.target()["metadata"]["generation"]
    run.offset = steps.call("measure cluster clock offset", lab.clock_offset)
    report["clock"] = {"before": run.offset}
    run.since = ka.shift(ka.stamp(lab.clock() - timedelta(seconds=1)), run.offset["offset_seconds"])
    prior = steps.call("check settled lab and scope incidents", lab.begin_run, baseline, policy)
    report["earlier_incidents"] = len(prior)
    with tempfile.TemporaryDirectory(prefix="p402-", dir=lab.run_dir) as temporary:
        try:
            report["access_logging"] = steps.call("require DEBUG Coordinator access evidence", lab.require_debug)
            before = access_lines(lab.log_text("coordinator", run.since))
            if not any(line["identity"] in WORKERS and line["operation"] == "claim"
                       and line["outcome"] == "idle" for line in before):
                steps.call("wait for DEBUG idle-claim positive control", lab.wait,
                           lambda: any(line["identity"] in WORKERS and line["outcome"] == "idle"
                                       for line in access_lines(lab.log_text("coordinator", run.since))),
                           30, "No idle claim logged at DEBUG; renewal counts would be inconclusive")
            if command == "partition":
                steps.call("inject incident", lab.inject)
                partition_drill(lab, run, steps, report, duration=duration)
                report["incident"] = steps.call("recover after healing", incident_recovered, lab, run.policy_version)
            else:
                restore_drill(lab, run, steps, report, Path(temporary))
        except FAILURES as error:
            report["failures"].append(f"{type(error).__name__}: {error}")
        finally:
            report["cleanup"] = steps.call("cleanup all tracked chaos and fault points", run.cleanup)
            report["failures"] += report["cleanup"]["problems"]
            try:
                report["target_restore"] = steps.call("restore target baseline", lab.restore, baseline)

                def leftovers():
                    lab.approve_pending(run.policy_version)
                    left = run.leftovers()
                    if report.get("maintenance_removed") is False:
                        left.append("The maintenance Pod was not confirmed terminated")
                    if report.get("coordinator_restarted") is False:
                        left.append("The Coordinator was not confirmed Ready after maintenance")
                    return left
                report["settle"] = steps.call("settle lab", lab.settle, baseline, leftovers=leftovers)
                if not report["settle"]["settled"]:
                    report["failures"].append("The lab did not settle")
            except FAILURES as error:
                report["failures"].append(f"Restore/settle: {type(error).__name__}: {error}")
    try:
        end_offset = steps.call("measure final cluster clock offset", lab.clock_offset)
        report["clock"]["after"] = end_offset
        uncertainty = (max(run.offset["uncertainty_seconds"], end_offset["uncertainty_seconds"])
                       + abs(run.offset["offset_seconds"] - end_offset["offset_seconds"]))
        if "detection_seconds" in report:
            report["detection_upper_bound_seconds"] = report["detection_seconds"] + uncertainty
        window = ka.control_plane_window(report["started_at"], ka.stamp(lab.clock()), [run.offset, end_offset])
        log = steps.call("collect complete audit log", lab.audit_log, *window)
        completeness = audit.completeness(log["rows"], injected=True,
            generations=(generation, lab.target()["metadata"]["generation"]), first_at=log["first_at"],
            window_start=window[0])
        report["audit_completeness"] = completeness
        report["failures"] += completeness
        report["audit"] = log
        if log["skipped"]:
            report["failures"].append("Audit lines could not be parsed")
        if command == "partition" and "partition" in report:
            part = report["partition"]
            access = access_lines(lab.log_text("coordinator", run.since))
            final_logging = steps.call("verify Coordinator access logging remained intact", lab.require_debug)
            if final_logging != report["access_logging"]:
                report["failures"].append("The Coordinator Pod/container changed during the partition evidence window")
            if not any(seconds(line["ts"]) <= seconds(part["start"]) for line in access):
                report["failures"].append("The Coordinator access log no longer covers partition start")
            transitions = report.get("partition_transitions", [])
            measurement = partition_measurements(part["start"], part["end"], part["removal_requested_at"],
                access, transitions, log["rows"], part["rtt_seconds"],
                uncertainty)
            report["measurements"] = measurement
            report["failures"] += measurement["failures"]
    except FAILURES as error:
        report["failures"].append(f"Evidence: {type(error).__name__}: {error}")
    report["chaos"] = run.notes.get("chaos", [])
    report["operator_actions"] = run.actions
    report["approval_attempts"] = lab.approval_attempts
    report["read_outages"] = lab.read_outages
    measured = report.get("measurements", {}).get("passed") if command == "partition" else report.get("measured")
    report["passed"] = bool(measured and not report["failures"] and report.get("settle", {}).get("settled"))
    if (not report.get("settle", {}).get("settled") or report["cleanup"]["problems"]
            or ("restored" in report and not report.get("measured"))):
        report["manual_recovery"] = MANUAL
    report["ended_at"] = ka.stamp(lab.clock())
    report.pop("baseline")
    report.pop("policy")
    return report


def write_report(run_dir, report, forbidden=()):
    folder = Path(run_dir) / "drills" / (report["drill"] + "-" + uuid4().hex[:12])
    if report["drill"] == "rto-from-runs":
        detail = json.dumps(report["aggregate_seconds"], sort_keys=True)
    elif report["drill"] == "partition":
        detail = json.dumps(report.get("measurements", {}), sort_keys=True)
    else:
        detail = f"lost commits: {report.get('rpo_lost_commits')}; detection seconds: {report.get('detection_seconds')}"
    row = f"| {report['drill']} | {'PASS' if report['passed'] else 'FAIL'} | {detail} |\n"
    live.write_evidence(folder, {"summary.json": {"schema": "sentinel-p402-drill-v1", **report},
                                 "results.md": "| Drill | Result | Measurements |\n| --- | --- | --- |\n" + row},
                        forbidden)
    return folder


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    offline = commands.add_parser("rto-from-runs", help="Recompute S5 RTO from saved evidence, entirely offline")
    offline.add_argument("runs", nargs="+", type=Path)
    for name in ("partition", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--context", required=True)
        command.add_argument("--min-free-gib", type=float, default=ka.MIN_FREE_GIB)
        command.add_argument("--break-stale-lock", action="store_true")
        if name == "partition":
            command.add_argument("--seconds", type=float, default=120)
    for command in (offline, *[commands.choices[name] for name in ("partition", "restore")]):
        command.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "rto-from-runs":
            report = rto_from_runs(args.runs)
            folder = write_report(args.run_dir, report)
        else:
            ka.check_context(args.context)
            if args.command == "partition" and (not math.isfinite(args.seconds) or not 1 <= args.seconds <= 540):
                raise ka.AcceptanceError("--seconds must be finite and between 1 and 540")
            deployed = json.loads((args.run_dir / "lab.json").read_text(encoding="utf-8"))
            baseline = json.loads((args.run_dir / "inputs/baseline.json").read_text(encoding="utf-8"))
            policy = json.loads((args.run_dir / "inputs/policy.json").read_text(encoding="utf-8"))
            shell = ka.Shell(args.context)
            with ka.LabLock(holder="P4.02 kind_drills", break_stale=args.break_stale_lock) as lock:
                ka.preflight(shell, min_free_gib=args.min_free_gib, lock=lock)
                lab = DrillLab(shell, args.run_dir)
                try:
                    live.check_target(lab, baseline)
                    if lab.labelled_chaos():
                        raise ka.AcceptanceError("Earlier harness chaos remains; remove it before this drill")
                    tokens = live.read_tokens(shell)
                    lab.tokens = {ka.OPERATOR: tokens[ka.OPERATOR]["approval-token"]}
                    report = live_drill(lab, args.command, deployed, baseline, policy,
                                        duration=getattr(args, "seconds", 120))
                    folder = write_report(args.run_dir, report, live.forbidden_strings(tokens))
                finally:
                    lab.close()
        print(f"{'PASS' if report['passed'] else 'FAIL'}: {folder}")
        if report.get("manual_recovery"):
            raise ka.AcceptanceError(report["manual_recovery"])
        return 0 if report["passed"] else 1
    except FAILURES as error:
        print(f"Refused: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    from experiments.distributed import kind_drills as _canonical
    raise SystemExit(_canonical.main())
