"""Interactive Chaos Mesh demonstrations with local run evidence."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import uuid4

from experiments.boutique.harness.chaos import (
    APPLICATIONS, CLUSTER, CONTEXT, FAULTS, KINDS, ROOT, TRAFFIC_IMAGE,
    Cluster, deployments_ready, injected, scenario, setup, validate_scope,
)
from experiments.boutique.harness.collection import Collector, Writer, metric_rows, namespace_summary, save, utc

OWNER = "sentinel.dev/run-id"
LOCK = "sentinel-chaos-demo-lock"


def metadata(name, namespace, run_id):
    return {"name": name, "namespace": namespace,
            "labels": {OWNER: run_id, "app.kubernetes.io/managed-by": "sentinel-chaos-demo"}}


def traffic_resources(namespace, run_id, config):
    name = "traffic-" + run_id
    source = ROOT / "experiments/boutique/traffic"
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata(name, namespace, run_id),
          "data": {filename: (source / filename).read_text(encoding="utf-8")
                   for filename in ("storefront.py", "worker.py")}}
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": metadata(name, namespace, run_id), "spec": {
        "restartPolicy": "Never", "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001},
        "containers": [{"name": "shopper", "image": TRAFFIC_IMAGE,
                        "command": ["python", "-u", "/traffic/worker.py"],
                        "env": [{"name": k, "value": str(v)} for k, v in {
                            "STOREFRONT_URL": f"http://frontend.{namespace}.svc.cluster.local:80",
                            "RUN_ID": run_id, "TRAFFIC_INTERVAL": config["traffic_interval"],
                            "TRAFFIC_WORKERS": config["traffic_workers"], "PYTHONDONTWRITEBYTECODE": "1"}.items()],
                        "resources": {"requests": {"cpu": "50m", "memory": "32Mi"},
                                      "limits": {"cpu": "500m", "memory": "128Mi"}},
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                            "capabilities": {"drop": ["ALL"]}},
                        "volumeMounts": [{"name": "scripts", "mountPath": "/traffic", "readOnly": True}]}],
        "volumes": [{"name": "scripts", "configMap": {"name": name}}]}}
    return cm, pod


def owned_remove(cluster, kind, name, run_id):
    raw = cluster.call("get", kind, name, "--ignore-not-found=true", "-o", "json")
    if not raw.strip():
        return
    if json.loads(raw)["metadata"].get("labels", {}).get(OWNER) != run_id:
        raise RuntimeError("Refusing cleanup of a resource owned by another run")
    cluster.remove(kind, name)


def read_traffic(cluster, name, directory):
    # Full bounded snapshots avoid losing requests when the host process reconnects.
    text = cluster.call("logs", name, "-c", "shopper", "--tail=-1", "--limit-bytes=52428800")
    path = directory / "telemetry/traffic.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if len(text.encode("utf-8")) >= 52428800:
        raise RuntimeError("Traffic log limit reached; shorten this run")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not rows or any("journey" not in row or "started_at" not in row or "completed_at" not in row for row in rows):
        raise RuntimeError("No valid shopping telemetry received")
    return rows


def window_rows(rows, start, end):
    return [r for r in rows if start <= r["started_at"] and r["completed_at"] <= end]


def healthy_window(rows, start, end):
    samples = window_rows(rows, start, end)
    return bool(samples) and all(r["journey"].get("passed") is True for r in samples)


def wait_seconds(seconds):
    # Short sleeps keep Ctrl+C responsive on Windows.
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(min(.5, max(0, end - time.monotonic())))


def preflight(cluster, config):
    namespace = cluster.get("namespace", cluster.namespace)
    if namespace["metadata"].get("annotations", {}).get("chaos-mesh.org/inject") != "enabled":
        raise RuntimeError("Namespace must have chaos-mesh.org/inject=enabled; use --setup for a new lab")
    deployments = cluster.get("deployments")
    if not deployments_ready(deployments["items"]):
        raise RuntimeError("All eleven Boutique deployments must be ready before recording")
    for kind in KINDS:
        if cluster.get(kind)["items"]:
            raise RuntimeError("An existing Chaos Mesh experiment is present; finish it before the demo")
    chaos_pods = cluster.get("pods", namespace="chaos-mesh")["items"]
    components = {p.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
                  for p in chaos_pods if any(c.get("type") == "Ready" and c.get("status") == "True"
                                             for c in p.get("status", {}).get("conditions", []))}
    if not {"controller-manager", "chaos-daemon", "chaos-dns-server"}.issubset(components):
        raise RuntimeError("Chaos Mesh controller, daemon and DNS server must be Ready in chaos-mesh")
    for fault in config["faults"]:
        cluster.create(scenario(fault, cluster.namespace, "sentinel-schema-check", config["fault_seconds"],
                                config.get("target")), dry_run=True)
    nodes = cluster.get("nodes")["items"]
    measured_pods = set()
    # Require real CPU/memory measurements; never silently substitute requests/limits.
    for node in nodes:
        summary = cluster.raw("/api/v1/nodes/" + node["metadata"]["name"] + "/proxy/stats/summary")
        for row in metric_rows(namespace_summary(summary, cluster.namespace)):
            if row["cpu_cores"] is not None and row["memory_working_set_bytes"] is not None:
                measured_pods.add(row["pod"].get("name"))
    pods = cluster.get("pods")["items"]
    targets = {scenario(f, cluster.namespace, "check", config["fault_seconds"], config.get("target"))
               ["spec"]["selector"]["labelSelectors"]["app"] for f in config["faults"]}
    for target in targets:
        if not any(p["metadata"]["name"] in measured_pods and
                   p["metadata"].get("labels", {}).get("app") == target for p in pods):
            raise RuntimeError("CPU/memory measurements are missing for " + target + "; wait for kubelet sampling")
    versions = sorted({p.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/version", "unknown")
                       for p in chaos_pods})
    return {"deployments": deployments, "boutique_pods": pods, "chaos_pods": chaos_pods, "nodes": nodes,
            "chaos_mesh_version": ",".join(versions), "kubernetes": cluster.raw("/version"),
            "source_files_sha256": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (ROOT / "experiments/boutique").rglob("*") if p.suffix in {".py", ".json"}}}


def source_revision():
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except OSError:
        return "unknown"


def checksum_files(directory):
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob("*")) if p.is_file() and p.name != "checksums.json"}


def run_case(cluster, config, fault, directory, environment):
    run_id = directory.name
    directory.mkdir(parents=True, exist_ok=False)
    writer = Writer(directory, run_id)
    fault_doc = scenario(fault, cluster.namespace, "fault-" + run_id, config["fault_seconds"], config.get("target"))
    fault_doc["metadata"]["labels"][OWNER] = run_id
    fault_name, kind = fault_doc["metadata"]["name"], fault_doc["kind"]
    cm, traffic = traffic_resources(cluster.namespace, run_id, config)
    traffic_name = traffic["metadata"]["name"]
    started = utc()
    collector = Collector(cluster, writer, config["sample_interval"], started)
    manifest = {"schema_version": 1, "run_id": run_id, "mode": config["mode"],
                "scenario_id": fault, "started_at": started, "source_revision": source_revision(),
                "versions": {"boutique": "See environment.json for actual images and pod imageIDs",
                             "chaos_mesh": environment["chaos_mesh_version"],
                             "kubernetes": environment["kubernetes"].get("gitVersion", "unknown")},
                "configuration": {"traffic": {k: config[k] for k in ("traffic_interval", "traffic_workers")},
                                  "telemetry": {"sample_interval": config["sample_interval"],
                                                "metrics_source": "kubelet-stats-summary", "traces": False},
                                  "fault": fault_doc}, "recovery_enabled": False}
    save(directory / "manifest.json", manifest)
    save(directory / "environment.json", environment)
    save(directory / "fault.json", fault_doc)
    save(directory / "traffic-resources.json", {"items": [cm, traffic]})
    outcome = {"run_id": run_id, "status": "failed", "injection_confirmed": False,
               "cleanup_confirmed": False, "baseline_healthy": False, "recovery_healthy": False,
               "recovery_attribution": "no_sentinel_action; chaos_expiry_or_harness_cleanup",
               "errors": [], "telemetry_complete": False}
    windows, rows = {}, []
    try:
        if not deployments_ready(cluster.get("deployments")["items"]):
            raise RuntimeError("Boutique is not healthy before this case")
        collector.start()
        cluster.create(cm)
        cluster.create(traffic)
        cluster.call("wait", "--for=condition=Ready", "pod/" + traffic_name, "--timeout=120s", timeout=140)
        baseline_start = utc()
        writer.append("timeline.jsonl", event="baseline_started")
        wait_seconds(config["baseline_seconds"])
        baseline_end = utc()
        windows["baseline"] = [baseline_start, baseline_end]
        rows = read_traffic(cluster, traffic_name, directory)
        outcome["baseline_healthy"] = healthy_window(rows, baseline_start, baseline_end)
        if not outcome["baseline_healthy"]:
            raise RuntimeError("Baseline requires successful shopping journeys with no failed/unknown samples")
        if config["operator_gates"]:
            input(f"Baseline healthy. Press Enter to inject {fault} into {fault_doc['spec']['selector']['labelSelectors']['app']}...")
        writer.append("timeline.jsonl", event="injection_requested")
        created = cluster.create(fault_doc)
        writer.append("chaos-status.jsonl", data=created)
        deadline = time.monotonic() + config["fault_seconds"]
        confirm_deadline = min(deadline, time.monotonic() + 60)
        while True:
            document = cluster.get(kind, fault_name)
            writer.append("chaos-status.jsonl", data=document)
            if injected(document):
                outcome["injection_confirmed"] = True
                writer.append("timeline.jsonl", event="injection_confirmed", status=document.get("status"))
                break
            if time.monotonic() >= confirm_deadline:
                raise RuntimeError("Chaos Mesh did not confirm injection into selected pods")
            wait_seconds(1)
        fault_start = utc()
        while time.monotonic() < deadline:
            wait_seconds(min(2, max(0, deadline - time.monotonic())))
            document = cluster.get(kind, fault_name)
            writer.append("chaos-status.jsonl", data=document)
            if not injected(document):
                conditions = {c.get("type"): c.get("status") for c in document.get("status", {}).get("conditions", [])}
                writer.append("timeline.jsonl", event="injection_no_longer_observed", status=document.get("status"))
                outcome["injection_end_conditions"] = conditions
                break
        windows["fault"] = [fault_start, utc()]
        writer.append("timeline.jsonl", event="cleanup_requested")
        owned_remove(cluster, kind, fault_name, run_id)
        outcome["cleanup_confirmed"] = True
        writer.append("timeline.jsonl", event="cleanup_confirmed")
        # Wait for replacement/restarted pods before measuring the recovery window.
        for name in sorted(APPLICATIONS):
            cluster.call("rollout", "status", "deployment/" + name, "--timeout=120s", timeout=140)
        recovery_start = utc()
        writer.append("timeline.jsonl", event="recovery_window_started")
        wait_seconds(config["recovery_seconds"])
        windows["recovery"] = [recovery_start, utc()]
        rows = read_traffic(cluster, traffic_name, directory)
        outcome["recovery_healthy"] = (healthy_window(rows, *windows["recovery"])
                                        and deployments_ready(cluster.get("deployments")["items"]))
        if not outcome["recovery_healthy"]:
            raise RuntimeError("Application recovery was not confirmed")
    except (Exception, KeyboardInterrupt) as error:
        outcome["errors"].append(type(error).__name__ + ": " + str(error))
        writer.append("timeline.jsonl", event="run_failed", error=outcome["errors"][-1])
        if isinstance(error, KeyboardInterrupt):
            outcome["interrupted"] = True
    finally:
        # A create timeout can still have created a resource. Verify ownership before cleanup.
        try:
            writer.append("timeline.jsonl", event="final_cleanup_requested")
            owned_remove(cluster, kind, fault_name, run_id)
            outcome["cleanup_confirmed"] = True
            writer.append("timeline.jsonl", event="final_cleanup_confirmed")
        except Exception as error:
            outcome["errors"].append("Fault cleanup: " + str(error))
            outcome["cleanup_confirmed"] = False
        try:
            collector.stop()
            collector.collect()
        except Exception as error:
            outcome["errors"].append("Collector: " + str(error))
        try:
            rows = read_traffic(cluster, traffic_name, directory)
        except Exception as error:
            outcome["errors"].append("Traffic export: " + str(error))
        for resource in (traffic, cm):
            try:
                owned_remove(cluster, resource["kind"], traffic_name, run_id)
            except Exception as error:
                outcome["errors"].append("Traffic cleanup: " + str(error))
        outcome["telemetry_complete"] = collector.errors == 0 and collector.samples > 0 and bool(rows)
        outcome.update(collection_errors=collector.errors, log_gaps=collector.log_gaps, collection_samples=collector.samples,
                       collection_overruns=collector.overruns, completed_at=utc())
        outcome["fault_window_journeys"] = len(window_rows(rows, *windows["fault"])) if "fault" in windows else 0
        outcome["observed_failed_fault_journeys"] = sum(r["journey"].get("passed") is False
            for r in window_rows(rows, *windows["fault"])) if "fault" in windows else 0
        if (not outcome["errors"] and outcome["telemetry_complete"] and outcome["injection_confirmed"]
                and outcome["cleanup_confirmed"] and outcome["baseline_healthy"] and outcome["recovery_healthy"]
                and outcome["fault_window_journeys"] > 0):
            outcome["status"] = "complete"
        save(directory / "ground-truth.json", {"run_id": run_id, "fault": fault,
             "target": fault_doc["spec"]["selector"], "windows": windows,
             "injection_confirmed": outcome["injection_confirmed"],
             "note": "Host-observed windows; authoritative controller transitions are in chaos-status.jsonl. Injection is not proof of user-visible impact."})
        save(directory / "outcome.json", outcome)
        save(directory / "checksums.json", checksum_files(directory))
    return outcome


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--faults", nargs="+", choices=(*FAULTS, "all"))
    parser.add_argument("--target", choices=sorted(APPLICATIONS), help="Override default target; HTTP requires frontend")
    parser.add_argument("--context", default=CONTEXT)
    parser.add_argument("--namespace", default=CLUSTER)
    parser.add_argument("--setup", action="store_true", help="Provision the dedicated kind lab and install Chaos Mesh")
    parser.add_argument("--dry-run", action="store_true", help="Write the plan and six fault manifests without cluster access")
    parser.add_argument("--runs-dir", type=Path, default=ROOT / "data/experimentation/runs")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--kind", default="kind")
    parser.add_argument("--helm", default="helm")
    for key in ("baseline-seconds", "fault-seconds", "recovery-seconds", "sample-interval",
                "traffic-interval", "traffic-workers"):
        parser.add_argument("--" + key, type=int)
    args = parser.parse_args(argv)
    config = json.loads((ROOT / "experiments/boutique/profiles" / "demo.json").read_text())
    for key in config:
        value = getattr(args, key, None)
        if value is not None:
            config[key] = value
    if "all" in config["faults"]:
        config["faults"] = list(FAULTS)
    config["faults"] = list(dict.fromkeys(config["faults"]))
    config["target"] = args.target
    limits = {"baseline_seconds": (5, 3600), "fault_seconds": (5, 3600),
              "recovery_seconds": (5, 3600), "sample_interval": (1, 60), "traffic_interval": (1, 60),
              "traffic_workers": (1, 16)}
    for key, (minimum, maximum) in limits.items():
        if not minimum <= config[key] <= maximum:
            parser.error(f"{key} must be {minimum}..{maximum}")
    try:
        validate_scope(args.context, args.namespace)
        for fault in config["faults"]:
            scenario(fault, args.namespace, "validation", config["fault_seconds"], args.target)
    except ValueError as error:
        parser.error(str(error))
    return args, config


def main(argv=None):
    args, config = arguments(argv)
    batch_id = "boutique-chaos-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]
    batch_dir = args.runs_dir.resolve() / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    order = config["faults"]
    batch = {"schema_version": 1, "batch_id": batch_id, "started_at": utc(), "configuration": config,
             "context": args.context, "namespace": args.namespace, "order": order, "runs": [],
             "status": "planned" if args.dry_run else "running"}
    save(batch_dir / "batch.json", batch)
    if args.dry_run:
        for fault in config["faults"]:
            save(batch_dir / "manifests" / (fault + ".json"), scenario(fault, args.namespace,
                 "preview-" + fault, config["fault_seconds"], args.target))
        print("Dry-run plan (no cluster changes): " + str(batch_dir))
        return 0
    cluster = Cluster(args.context, args.namespace, args.kubectl)
    locked = False
    def interrupt(*_):
        raise KeyboardInterrupt()

    previous_term = signal.signal(signal.SIGTERM, interrupt)
    try:
        if args.setup:
            print("Provisioning the dedicated Boutique + Chaos Mesh lab...", flush=True)
            setup(cluster, args.kind, args.helm)
        lock_doc = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata(LOCK, args.namespace, batch_id),
                    "data": {"started_at": utc(), "batch_directory": str(batch_dir)}}
        cluster.create(lock_doc)
        locked = True
        environment = preflight(cluster, config)
        save(batch_dir / "environment.json", environment)
        for index, fault in enumerate(order, 1):
            run_id = f"{index:04d}-{fault}-{uuid4().hex[:8]}"
            print(f"[{index}/{len(order)}] {fault}: recording baseline, fault and recovery", flush=True)
            outcome = run_case(cluster, config, fault, batch_dir / run_id, environment)
            batch["runs"].append(outcome)
            save(batch_dir / "batch.json", batch)
            print(f"  {outcome['status']}; {outcome['collection_samples']} telemetry samples", flush=True)
            if outcome["status"] != "complete":
                raise RuntimeError("Stopping after an incomplete demonstration: " + "; ".join(outcome["errors"] or ["insufficient telemetry"]))
        batch["status"] = "complete"
    except (Exception, KeyboardInterrupt) as error:
        batch["status"] = "failed"
        batch["error"] = type(error).__name__ + ": " + str(error)
        print(batch["error"], file=sys.stderr)
    finally:
        if locked:
            try:
                owned_remove(cluster, "ConfigMap", LOCK, batch_id)
            except Exception as error:
                batch["status"] = "failed"
                batch["lock_cleanup_error"] = str(error)
        signal.signal(signal.SIGTERM, previous_term)
        batch["completed_at"] = utc()
        save(batch_dir / "batch.json", batch)
        print("Run evidence: " + str(batch_dir))
    return 0 if batch["status"] == "complete" else 1
