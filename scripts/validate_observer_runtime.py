"""Operator-run live validation; always restore the demo's saved healthy revision."""
from datetime import datetime, timezone
import json
import argparse
import uuid
from pathlib import Path
import subprocess
import time

REPO = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, help="New evidence directory; must not already exist")
args = parser.parse_args()
RUN = args.output or (REPO / "data/sentinel/runs" / ("observer-runtime-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]))
RUN.mkdir(parents=True, exist_ok=False)
print("Evidence: " + str(RUN), flush=True)
PREFIX = ["kubectl", "--context", "kind-sentinel-lab", "--namespace", "sentinel-demo", "--request-timeout=10s"]
history = []
report = {"passed": False, "checks": {}, "started_at": datetime.now(timezone.utc).isoformat()}


def kube(*args, timeout=25):
    result = subprocess.run(PREFIX + list(args), capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def deployment():
    return json.loads(kube("get", "deployment/demo-api", "-o", "json"))


def worker():
    code = ("import sqlite3,json; c=sqlite3.connect('/data/observer.sqlite3'); "
            "s=c.execute('SELECT state,status FROM observer_checkpoint WHERE id=1').fetchone(); "
            "o=c.execute('SELECT payload,delivered FROM observer_outbox ORDER BY rowid').fetchall(); "
            "print(json.dumps({'state':json.loads(s[0]),'status':json.loads(s[1]),"
            "'outbox':[{'message':json.loads(p),'delivered':d} for p,d in o]})); c.close()")
    value = json.loads(kube("exec", "deployment/sentinel-observer", "--", "python", "-c", code))
    latest = value["state"]["latest"]
    if latest and (not history or latest[0]["captured_at"] != history[-1]["captured_at"]):
        history.append(latest[0])
    return value


def healthy(observation):
    d = observation["deployment"]
    return (d["spec"]["replicas"] > 0 and d["status"]["observedGeneration"] >= d["metadata"]["generation"]
            and d["status"]["readyReplicas"] >= d["spec"]["replicas"]
            and d["status"]["availableReplicas"] >= d["spec"]["replicas"])


def parsed(at):
    return datetime.fromisoformat(at.replace("Z", "+00:00"))


def wait_for(check, *, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = worker()
        assert value["status"]["collection_errors"] == 0, value["status"]
        assert value["status"]["last_delivery_error"] is None, value["status"]
        if check(value):
            return value
        time.sleep(4)
    raise TimeoutError("Live Observer validation condition timed out")


baseline = deployment()
revision = baseline["metadata"]["annotations"]["deployment.kubernetes.io/revision"]
assert next(env["value"] for c in baseline["spec"]["template"]["spec"]["containers"] if c["name"] == "demo-api"
            for env in c["env"] if env["name"] == "STARTUP_FAILURE") == "false"
(RUN / "baseline-deployment.json").write_text(json.dumps(baseline, indent=2))
fault_attempted = False
try:
    permissions = {}
    for verb, resource, expected in (("get", "deployment/demo-api", True), ("list", "pods", True),
                                      ("patch", "deployments", False), ("get", "secrets", False),
                                      ("create", "pods/exec", False)):
        result = subprocess.run(PREFIX + ["auth", "can-i", verb, resource,
                                "--as=system:serviceaccount:sentinel-demo:sentinel-observer"], capture_output=True, text=True, timeout=20)
        allowed = result.stdout.strip() == "yes"
        assert allowed == expected, (verb, resource, result.stdout, result.stderr)
        permissions[f"{verb} {resource}"] = allowed
    report["checks"]["rbac"] = permissions
    start = worker()
    baseline_incidents = len(start["outbox"])
    assert all(item["delivered"] for item in start["outbox"]), "Earlier delivery must finish before validation"
    threshold = start["state"]["failure_duration_seconds"]
    report["configuration"] = {"context": "kind-sentinel-lab", "namespace": "sentinel-demo", "deployment": "demo-api",
                               "threshold_seconds": threshold, "baseline_incidents": baseline_incidents}
    first_healthy = parsed(start["state"]["latest"][0]["captured_at"])
    print("Checking the deployed worker against a healthy app for at least 60 seconds...", flush=True)
    value = wait_for(lambda value: (parsed(value["state"]["latest"][0]["captured_at"]) - first_healthy).total_seconds() >= 60)
    assert all(healthy(observation) for observation in history)
    assert len(value["outbox"]) == baseline_incidents
    report["checks"]["healthy_seconds"] = (parsed(history[-1]["captured_at"]) - first_healthy).total_seconds()
    print("PASS: healthy collection, no incidents. Injecting startup failure...", flush=True)
    fault_attempted = True
    kube("set", "env", "deployment/demo-api", "--containers=demo-api", "STARTUP_FAILURE=true")
    value = wait_for(lambda value: len(value["outbox"]) == baseline_incidents + 1 and value["outbox"][-1]["delivered"] == 1)
    incident = value["outbox"][-1]["message"]
    first_at = parsed(value["state"]["episodes"][0]["first"]["captured_at"])
    emitted_at = parsed(incident["observed_at"])
    eligible = next(observation for observation in history
                    if (parsed(observation["captured_at"]) - first_at).total_seconds() >= threshold)
    assert eligible["captured_at"] == incident["observed_at"], "Not the first eligible poll"
    report["checks"].update(incident_id=incident["incident_id"], detection_delay_seconds=(emitted_at - first_at).total_seconds(),
                            incident_sources=sorted({signal["source"] for signal in incident["signals"]}))
    (RUN / "incident.json").write_text(json.dumps(incident, indent=2))
    pods = json.loads(kube("get", "pods", "-l", "app=sentinel-observer", "-o", "json"))["items"]
    assert len(pods) == 1 and pods[0]["metadata"]["name"].startswith("sentinel-observer-")
    old_pod = pods[0]["metadata"]["name"]
    report["checks"]["first_observer_pod_uid"] = pods[0]["metadata"]["uid"]
    (RUN / "before-restart-log.jsonl").write_text(kube("logs", old_pod), encoding="utf-8")
    print("PASS: threshold incident delivered. Replacing only the Observer Pod to test persistent recovery...", flush=True)
    kube("delete", "pod", old_pod, "--wait=true", "--timeout=45s", timeout=55)
    kube("rollout", "status", "deployment/sentinel-observer", "--timeout=120s", timeout=130)
    restarted = json.loads(kube("get", "pods", "-l", "app=sentinel-observer", "-o", "json"))["items"][0]
    assert restarted["metadata"]["uid"] != report["checks"]["first_observer_pod_uid"]
    report["checks"]["replacement_observer_pod_uid"] = restarted["metadata"]["uid"]
    restart_at = parsed(worker()["state"]["latest"][0]["captured_at"])
    value = wait_for(lambda value: (parsed(value["state"]["latest"][0]["captured_at"]) - restart_at).total_seconds() >= 60)
    assert len(value["outbox"]) == baseline_incidents + 1
    assert value["outbox"][-1]["message"] == incident
    latest = value["state"]["latest"][0]
    assert not healthy(latest)
    assert latest["pods"], "Expected real correlated failed Pod evidence"
    assert latest["logs"], "Expected actual failed container log evidence"
    report["checks"]["duplicate_suppression_after_restart_seconds"] = (parsed(latest["captured_at"]) - restart_at).total_seconds()
    report["checks"]["total_duplicate_suppression_seconds"] = (parsed(latest["captured_at"]) - emitted_at).total_seconds()
    (RUN / "persistent-failure-observation.json").write_text(json.dumps(latest, indent=2))
    counts_code = ("import sqlite3,json; c=sqlite3.connect('/data/coordinator.sqlite3'); "
                   "print(json.dumps({'incidents':c.execute('SELECT count(*) FROM incidents').fetchone()[0],"
                   "'planner_requests':c.execute(\"SELECT count(*) FROM outbox WHERE topic='planner.request'\").fetchone()[0]})); c.close()")
    counts = json.loads(kube("exec", "deployment/sentinel-observer", "--", "python", "-c", counts_code))
    assert counts == {"incidents": baseline_incidents + 1, "planner_requests": baseline_incidents + 1}, counts
    report["checks"]["coordinator"] = counts
    print("PASS: persistent state and duplicate suppression after Pod replacement; one additional Coordinator planner request.", flush=True)
finally:
    if fault_attempted:
        print("Restoring the exact saved healthy rollout revision...", flush=True)
        if deployment()["metadata"]["uid"] != baseline["metadata"]["uid"]:
            raise RuntimeError("Demo Deployment was recreated; saved revision does not apply")
        kube("rollout", "undo", "deployment/demo-api", f"--to-revision={revision}")
        kube("rollout", "status", "deployment/demo-api", "--timeout=120s", timeout=130)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    (RUN / "report.json").write_text(json.dumps(report, indent=2))
    (RUN / "observations.json").write_text(json.dumps(history, indent=2))

value = wait_for(lambda value: healthy(value["state"]["latest"][0]))
recovered_at = parsed(value["state"]["latest"][0]["captured_at"])
print("Checking the restored app through another 60-second healthy window...", flush=True)
value = wait_for(lambda value: (parsed(value["state"]["latest"][0]["captured_at"]) - recovered_at).total_seconds() >= 60)
assert len(value["outbox"]) == baseline_incidents + 1
assert healthy(value["state"]["latest"][0])
report["checks"]["recovered_seconds"] = (parsed(value["state"]["latest"][0]["captured_at"]) - recovered_at).total_seconds()
report["passed"] = True
report["finished_at"] = datetime.now(timezone.utc).isoformat()
(RUN / "report.json").write_text(json.dumps(report, indent=2))
(RUN / "observations.json").write_text(json.dumps(history, indent=2))
(RUN / "after-restart-log.jsonl").write_text(kube("logs", "deployment/sentinel-observer"), encoding="utf-8")
print(json.dumps(report, indent=2), flush=True)
