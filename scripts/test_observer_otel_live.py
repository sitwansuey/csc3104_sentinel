"""Opt-in real SDK -> Collector -> Observer -> Coordinator validation.

Creates only sentinel-observer-otel and removes it unless --keep-cluster is set.
The existing Sentinel review app and other clusters are not changed.
"""

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.observer_otel_manifest import manifest, NAMESPACE, OBSERVER_IMAGE, WORKLOAD_IMAGE, COLLECTOR_IMAGE

CLUSTER = "sentinel-observer-otel"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-cluster", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args(argv)
    directory = ROOT / "data/sentinel/runs" / ("observer-otel-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:6])
    directory.mkdir(parents=True)
    environment = {**os.environ, "KUBECONFIG": str(directory / "kubeconfig")}
    report = {"status": "RUNNING", "scope": "real instrumented validation services; not Boutique tracing", "checks": {}}
    print("Buddy 2 live evidence: " + str(directory), flush=True)

    def save(name, value):
        (directory / name).write_text(json.dumps(value, indent=2), encoding="utf-8")

    def run(command, *, input=None, timeout=180, allow_failure=False):
        result = subprocess.run(command, env=environment, cwd=ROOT, input=input,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if result.returncode and not allow_failure:
            raise RuntimeError("Command failed: " + " ".join(command[:4]) + "\n" + result.stderr[-1500:])
        return result.stdout

    def kube(*arguments, input=None):
        return run(["kubectl", "--context=kind-" + CLUSTER, "--namespace=" + NAMESPACE,
                    "--request-timeout=130s" if arguments[0] == "rollout" else "--request-timeout=10s", *arguments], input=input)

    def snapshot():
        code = """
import json,sqlite3,pathlib
from contextlib import closing
result={'workers':[]}
for p in pathlib.Path('/data').glob('observer-*.sqlite3'):
    with closing(sqlite3.connect(p)) as c:
        state,status=c.execute('SELECT state,status FROM observer_checkpoint WHERE id=1').fetchone()
        messages=[json.loads(r[0]) for r in c.execute('SELECT payload FROM observer_outbox ORDER BY rowid')]
        contexts=[json.loads(r[0]) for r in c.execute('SELECT payload FROM observer_incident_context')]
    result['workers'].append({'state':json.loads(state),'status':json.loads(status),'incidents':messages,'contexts':contexts})
result.update(coordinator_incidents=0,planner_requests=0)
if pathlib.Path('/data/coordinator.sqlite3').exists():
    with closing(sqlite3.connect('file:/data/coordinator.sqlite3?mode=ro',uri=True)) as c:
        result['coordinator_incidents']=c.execute('SELECT count(*) FROM incidents').fetchone()[0]
        result['planner_requests']=c.execute("SELECT count(*) FROM outbox WHERE topic='planner.request'").fetchone()[0]
print(json.dumps(result))
"""
        return json.loads(kube("exec", "deployment/sentinel-observer-otel", "-c", "worker", "--", "python", "-c", code))

    def wait(predicate, timeout=180):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = snapshot()
                if predicate(last):
                    return last
            except (RuntimeError, ValueError):
                pass
            time.sleep(3)
        save("last-snapshot.json", last)
        raise RuntimeError("Live Observer condition timed out")

    def readings(value):
        return [worker["state"]["latest"][0] for worker in value["workers"] if worker["state"]["latest"]]

    def complete(value):
        rows = readings(value)
        return len(rows) == 2 and all(all(row["telemetry"]["records"][kind] for kind in ("traces", "metrics", "logs")) for row in rows)

    def connected(value):
        if not complete(value):
            return False
        traces = [{row["trace_id"] for row in item["telemetry"]["records"]["traces"]} for item in readings(value)]
        return bool(traces[0] & traces[1])

    def fault(enabled):
        code = ("from urllib.request import Request,urlopen; "
                "r=Request('http://otel-callee:8080/validation/fault',"
                + repr(json.dumps({"enabled": enabled}).encode())
                + ",{'Content-Type':'application/json'},method='POST'); "
                "assert urlopen(r,timeout=5).status==200")
        kube("exec", "deployment/otel-load", "--", "python", "-c", code)

    created = False
    try:
        if CLUSTER in run(["kind", "get", "clusters"]).split():
            raise RuntimeError("Reserved live-test cluster already exists; inspect it before rerunning")
        if not args.skip_build:
            run(["docker", "build", "-t", OBSERVER_IMAGE, "-f", "deploy/docker/observer.Dockerfile", "."], timeout=600)
            run(["docker", "build", "-t", WORKLOAD_IMAGE, "experiments/workloads/otel"], timeout=600)
        run(["docker", "pull", COLLECTOR_IMAGE], timeout=600)
        created = True
        print("Creating the dedicated Buddy 2 cluster...", flush=True)
        run(["kind", "create", "cluster", "--name", CLUSTER, "--config", "deploy/kind/kind.yaml", "--wait", "120s"], timeout=300)
        run(["kind", "load", "docker-image", OBSERVER_IMAGE, WORKLOAD_IMAGE, "--name", CLUSTER], timeout=300)
        kube("create", "namespace", NAMESPACE)
        # Secret is sent on stdin and never saved in the evidence or command line.
        secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "observer-otlp-token", "namespace": NAMESPACE},
                  "data": {"token": base64.b64encode(secrets.token_bytes(32).hex().encode()).decode()}}
        kube("apply", "-f", "-", input=json.dumps(secret))
        resources = manifest()
        save("manifest.json", resources)
        kube("apply", "-f", "-", input=json.dumps(resources))
        for name in ("sentinel-observer-otel", "otel-collector", "otel-callee", "otel-caller", "otel-load"):
            kube("rollout", "status", "deployment/" + name, "--timeout=120s")
        healthy = wait(lambda value: connected(value) and value["coordinator_incidents"] == 0)
        save("healthy.json", healthy)
        traces = [set(row["trace_id"] for row in item["telemetry"]["records"]["traces"]) for item in readings(healthy)]
        if not traces[0] & traces[1]:
            raise RuntimeError("Connected services did not expose a shared real trace")
        report["checks"]["real_traces_metrics_logs_from_both_services"] = True
        print("PASS: connected SDK traces, metrics and logs collected; no healthy incidents", flush=True)
        for verb, resource in (("patch", "deployments"), ("get", "secrets"), ("create", "pods/exec")):
            result = run(["kubectl", "--context=kind-" + CLUSTER, "--namespace=" + NAMESPACE,
                "auth", "can-i", verb, resource, "--as=system:serviceaccount:" + NAMESPACE + ":sentinel-observer"], allow_failure=True)
            if result.strip() != "no":
                raise RuntimeError("Observer has unexpected write/secret/exec permission")
        report["checks"]["read_only_rbac"] = True
        fault(True)
        failed = wait(lambda value: value["coordinator_incidents"] == 2 and all(len(w["incidents"]) == 1 for w in value["workers"]))
        for worker in failed["workers"]:
            sources = {signal["source"] for signal in worker["incidents"][0]["signals"]}
            if not {"otel_metric_alert", "otel_traces", "otel_metrics", "otel_logs"} <= sources:
                raise RuntimeError("Incident lacks correlated telemetry signals")
            latest = worker["state"]["latest"][0]["deployment"]["status"]
            if latest.get("readyReplicas") != 1:
                raise RuntimeError("Telemetry fault test must keep Kubernetes Pods ready")
        save("failed.json", failed)
        report["checks"]["telemetry_only_incidents_with_ready_pods"] = True
        print("PASS: real HTTP failures detected for both workloads while Pods remain ready", flush=True)
        kube("rollout", "restart", "deployment/sentinel-observer-otel")
        kube("rollout", "status", "deployment/sentinel-observer-otel", "--timeout=120s")
        time.sleep(15)
        restarted = wait(complete)
        if restarted["coordinator_incidents"] != 2 or restarted["planner_requests"] != 2:
            raise RuntimeError("Restart duplicated incident delivery")
        save("restarted.json", restarted)
        report["checks"]["pvc_restart_no_duplicate_delivery"] = True
        fault(False)
        restored = wait(lambda value: complete(value) and all(
            row["telemetry"]["thresholds_complete"] and not row["telemetry"]["alerts"] for row in readings(value)))
        save("restored.json", restored)
        report["checks"]["healthy_telemetry_resets_detection"] = True
        report["status"] = "PASS"
        print("PASS: Buddy 2 live telemetry, incidents, delivery and restart validation", flush=True)
    except Exception as error:
        report.update(status="FAILED", error=str(error)[:2000])
        if created:
            try:
                save("pods.json", json.loads(kube("get", "pods", "-o", "json")))
                save("events.json", json.loads(kube("get", "events", "-o", "json")))
            except Exception:
                pass
            for container in ("worker", "receiver"):
                try:
                    (directory / (container + ".log")).write_text(kube("logs", "deployment/sentinel-observer-otel", "-c", container, "--tail=80"))
                except Exception:
                    pass
        raise
    finally:
        if created and not args.keep_cluster:
            run(["kind", "delete", "cluster", "--name", CLUSTER], timeout=120)
            report["cluster_removed"] = CLUSTER not in run(["kind", "get", "clusters"]).split()
            (directory / "kubeconfig").unlink(missing_ok=True)
        save("summary.json", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
