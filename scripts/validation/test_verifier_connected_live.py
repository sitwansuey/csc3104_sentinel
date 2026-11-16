"""Opt-in real Collector, connected services, Executor and durable Verifier test."""

from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.validation.verifier_connected_lab import VerifierLab, require, save, utc
from scripts.validation.verifier_lab_manifest import COLLECTOR, IMAGE
from services.coordinator.core import SQLiteStore
from services.verifier.store import VerificationStore
from shared import log

LOG = log.get_logger("validation")


def monitor(lab, folder, *, inventory=None, trace_url=None, restart=False):
    inventory_path = folder / "inventory.json"
    save(inventory_path, inventory or lab.inventory)
    command = [sys.executable, "-m", "services.verifier.live_worker", "--coordinator-db", str(folder / "coordinator.sqlite3"),
               "--state", str(folder / "verifier.sqlite3"), "--policy", str(folder / "policy.json"),
               "--inventory", str(inventory_path), "--context", lab.verifier_context,
               "--trace-url", trace_url or lab.urls["collector"] + "/traces", "--watch", "--poll-seconds", "2.5"]
    initial = None
    passes = []
    deadline = time.monotonic() + 90
    while True:
        errors = (folder / "worker-errors.txt").open("a", encoding="utf-8")
        process = subprocess.Popen(command, cwd=ROOT, env=lab.worker_env, stdout=subprocess.PIPE,
                                   stderr=errors, text=True)
        lab.workers.append(process)
        lines = queue.Queue()

        def read_lines(proc=process, output=lines):
            for line in proc.stdout:
                output.put(line)
            output.put(None)

        thread = threading.Thread(target=read_lines, daemon=True)
        thread.start()
        should_restart = False
        try:
            while time.monotonic() < deadline:
                try:
                    line = lines.get(timeout=2)
                except queue.Empty:
                    continue
                if line is None:
                    require(process.wait(timeout=5) == 0, "Live worker failed; inspect worker-errors.txt")
                    break
                report = json.loads(line)
                passes.append(report)
                save(folder / "worker-passes.json", passes)
                if restart and initial is None and report["jobs"]:
                    initial = report["jobs"][0]
                    require(initial["sample_count"] == 1 and not initial["finished"],
                            "Restart must interrupt an incomplete observation window")
                    process.terminate()
                    process.wait(timeout=10)
                    should_restart = True
                    break
            else:
                raise RuntimeError("Live monitoring exceeded the experiment deadline")
        finally:
            errors.close()
            if process.poll() is not None:
                thread.join(timeout=3)
        if should_restart:
            time.sleep(1)
            continue
        break
    with_store = VerificationStore(folder / "verifier.sqlite3")
    try:
        jobs = with_store.db.execute("SELECT action_id FROM verification_jobs").fetchall()
        require(len(jobs) == 1, "Expected one monitoring job")
        job = with_store.get(jobs[0][0])
        require(job["outcome"] is not None and job["delivered"], "Outcome must be saved and acknowledged")
        require(not with_store.pending_outcomes(), "No undelivered outcome may remain")
    finally:
        with_store.close()
    if restart:
        require(initial is not None and initial["started_at"] == job["started_at"]
                and initial["deadline"] == job["deadline"], "Restart must preserve the exact observation deadline")
        require(len(job["samples"]) >= 3, "Restart must retain readings across processes")
    coordinator = SQLiteStore(folder / "coordinator.sqlite3")
    try:
        state = coordinator.incident(job["execution"]["incident_id"])["state"]
        messages = [json.loads(row[0]) for row in coordinator.db.execute("SELECT payload FROM messages")]
        require(sum(message["kind"] == "outcome" for message in messages) == 1,
                "Restart/replay must publish exactly one outcome")
    finally:
        coordinator.close()
    # A completed job must safely replay through a fresh process, without another outcome.
    replay = subprocess.run(command, cwd=ROOT, env=lab.worker_env, capture_output=True, text=True, timeout=30)
    require(replay.returncode == 0 and not json.loads(replay.stdout)["outcomes"], "Completed job replay was not idempotent")
    save(folder / "job.json", job)
    save(folder / "outcome.json", job["outcome"])
    return {"outcome": job["outcome"]["status"], "incident_state": state,
            "readings": len(job["samples"]), "deadline": job["deadline"], "restart": restart,
            "window_start": job["outcome"]["window_start"], "window_end": job["outcome"]["window_end"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true", help="Build and load pinned test images into the existing kind lab")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.setup:
        for command in (["docker", "build", "-f", "experiments/workloads/verifier-lab/Dockerfile", "-t", IMAGE, "."], ["docker", "pull", COLLECTOR],
                        ["kind", "load", "docker-image", IMAGE, "--name", "sentinel-lab"],
                        ["docker", "exec", "sentinel-lab-control-plane", "crictl", "pull", COLLECTOR]):
            subprocess.run(command, cwd=ROOT, check=True, timeout=600)
    directory = args.output or ROOT / "data/sentinel/runs" / ("verifier-connected-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6])
    lab = VerifierLab(directory)
    report = {"status": "RUNNING", "mode": "real_kubernetes_http_sdk_collector_executor_verifier",
              "started_at": utc(), "namespace": lab.namespace, "scenarios": [],
              "limits": ["Operator-seeded incidents and approved lab proposals; Planner/Observer are not invoked",
                         "Exact current healthy template restored with an actual conditional write; not causal recovery evaluation",
                         "Single-host SQLite and two explicitly inventoried application services"]}
    try:
        lab.setup()
        report["trace_edge"] = {"from": "checkout", "to": "payment"}
        for name, outcome, state in (("healthy", "RECOVERED", "RECOVERED"),
                                     ("dependency_failure", "NOT_RECOVERED", "FAILED"),
                                     ("application_failure", "NOT_RECOVERED", "FAILED"),
                                     ("missing_dependency", "UNKNOWN", "NEEDS_REVIEW"),
                                     ("missing_traces", "UNKNOWN", "NEEDS_REVIEW"),
                                     ("restart", "RECOVERED", "RECOVERED")):
            folder, proposal = lab.prepare_action(name)
            inventory = json.loads(json.dumps(lab.inventory))
            traces = None
            if name in {"dependency_failure", "application_failure"}:
                lab.fault("payment" if name == "dependency_failure" else "checkout", True)
            if name == "missing_dependency":
                inventory["services"]["payment"]["health_url"] = "http://127.0.0.1:1/healthz"
            if name == "missing_traces":
                traces = "http://127.0.0.1:1/traces"
            try:
                started = time.monotonic()
                result = monitor(lab, folder, inventory=inventory, trace_url=traces, restart=name == "restart")
                require(result["outcome"] == outcome and result["incident_state"] == state,
                        "Unexpected outcome for " + name + ": " + str(result))
                if outcome == "UNKNOWN":
                    require(datetime.now(timezone.utc) >= datetime.fromisoformat(result["deadline"].replace("Z", "+00:00")),
                            "Unknown outcome must wait for the fixed deadline")
                result.update(name=name, passed=True, elapsed_seconds=round(time.monotonic() - started, 3))
                report["scenarios"].append(result)
                save(lab.directory / "report.json", report)
                print("PASS: " + name + " -> " + result["outcome"] + " / " + result["incident_state"], flush=True)
            finally:
                if name in {"dependency_failure", "application_failure"}:
                    lab.fault("payment" if name == "dependency_failure" else "checkout", False)
        report["status"] = "PASSED"
        exit_code = 0
    except Exception as error:  # noqa: BLE001 - the failure is saved in the report and the run exits 1
        log.failure(LOG, "validation.run.failed", error, level=log.ERROR)
        report.update(status="FAILED", error=str(error))
        if lab.created:
            for deployment, container in (("collector", "otel"), ("checkout", "checkout"), ("payment", "payment")):
                try:
                    (lab.directory / (deployment + "-diagnostic.log")).write_text(
                        lab.kube("logs", "deployment/" + deployment, "-c", container, "--tail=40"), encoding="utf-8")
                except Exception as diagnostic_error:  # noqa: BLE001 - best-effort diagnostics; the run still fails
                    log.failure(LOG, "validation.diagnostics.failed", diagnostic_error)
        print("FAIL: " + str(error), file=sys.stderr)
        exit_code = 1
    finally:
        try:
            lab.close()
            report["cleanup"] = "Workers and port forwards stopped; private credentials removed; unique namespace deletion requested"
        except Exception as error:  # noqa: BLE001 - the cleanup error is saved in the report
            log.failure(LOG, "validation.cleanup.failed", error, level=log.ERROR)
            report["cleanup_error"] = str(error)
            report["status"] = "FAILED"
            exit_code = 1
        report["finished_at"] = utc()
        save(lab.directory / "report.json", report)
        print("Evidence: " + str(lab.directory / "report.json"), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
