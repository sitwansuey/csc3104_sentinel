"""Namespace-scoped logs, Kubernetes observations and kubelet resource samples."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

from experiments.boutique.harness.chaos import APPLICATIONS
from shared import log

LOG = log.get_logger("telemetry")


def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class Writer:
    def __init__(self, directory, run_id):
        self.directory, self.run_id = Path(directory), run_id
        self.lock = threading.Lock()

    def append(self, file, **fields):
        record = {"run_id": self.run_id, "captured_at": utc(), **fields}
        with self.lock:
            path = self.directory / file
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")


def namespace_summary(document, namespace):
    return {"node": document.get("node", {}),
            "pods": [p for p in document.get("pods", []) if p.get("podRef", {}).get("namespace") == namespace]}


def metric_rows(document):
    rows = []
    for pod in document.get("pods", []):
        for container in pod.get("containers", []):
            cpu, memory = container.get("cpu", {}), container.get("memory", {})
            rows.append({"pod": pod.get("podRef", {}), "container": container.get("name"),
                         "cpu_time": cpu.get("time"), "memory_time": memory.get("time"),
                         "cpu_cores": cpu["usageNanoCores"] / 1e9 if "usageNanoCores" in cpu else None,
                         "cpu_usage_core_nanoseconds": cpu.get("usageCoreNanoSeconds"),
                         "memory_working_set_bytes": memory.get("workingSetBytes"),
                         "memory_usage_bytes": memory.get("usageBytes")})
    return rows


class Collector:
    def __init__(self, cluster, writer, interval, started_at):
        self.cluster, self.writer, self.interval = cluster, writer, interval
        self.since = started_at
        self.stop_event = threading.Event()
        self.thread = None
        self.errors = 0
        self.samples = 0
        self.overruns = 0
        self.log_gaps = 0
        self.previous = set()

    def error(self, source, error, *, expected_unavailability=False):
        if expected_unavailability:
            self.log_gaps += 1
        else:
            self.errors += 1
        self.writer.append("telemetry/collection-errors.jsonl", source=source, error=str(error),
                           expected_unavailability=expected_unavailability)

    def logs(self, pod, since):
        metadata = pod["metadata"]
        for status in pod.get("status", {}).get("containerStatuses", []):
            container = status["name"]
            key = (metadata["uid"], container, status.get("restartCount", 0))
            variants = [False]
            if key[2] and key not in self.previous:
                variants.append(True)
            for previous in variants:
                try:
                    args = ["logs", metadata["name"], "-c", container, "--timestamps=true",
                            "--since-time=" + since, "--limit-bytes=1048576", "--tail=-1"]
                    if previous:
                        args.append("--previous=true")
                    body = self.cluster.call(*args, timeout=25)
                    truncated = len(body.encode("utf-8")) >= 1048576
                    self.writer.append("telemetry/logs/application.jsonl", pod=metadata["name"],
                                       pod_uid=metadata["uid"], container=container, previous=previous,
                                       restart_count=key[2], window_start=since,
                                       possibly_truncated=truncated, text=body)
                    if truncated:
                        self.error("logs", "Log byte limit reached: " + metadata["name"])
                    if previous:
                        self.previous.add(key)
                except Exception as error:  # noqa: BLE001 - a pod can vanish mid-fault; the gap is recorded
                    log.failure(LOG, "telemetry.logs.unavailable", error)
                    # A fault can remove a pod/container before its logs can be fetched.
                    # Preserve that gap; do not misclassify expected unavailability as a broken collector.
                    unavailable = (not status.get("state", {}).get("running") or previous
                                   or any(s in str(error) for s in ("NotFound", "not found", "waiting to start")))
                    self.error("logs/" + metadata["name"] + "/" + container, error,
                               expected_unavailability=unavailable)

    def collect(self):
        cycle_start = utc()
        pods = []
        for resource in ("pods", "deployments", "events"):
            try:
                document = self.cluster.get(resource)
                self.writer.append("telemetry/kubernetes/" + resource + ".jsonl", data=document)
                if resource == "pods":
                    pods = [p for p in document["items"] if p.get("metadata", {}).get("labels", {}).get("app") in APPLICATIONS]
            except Exception as error:  # noqa: BLE001 - the gap is recorded and collection continues
                log.failure(LOG, "telemetry.kubernetes.failed", error)
                self.error(resource, error)
        nodes = sorted({p["spec"]["nodeName"] for p in pods if p.get("spec", {}).get("nodeName")})
        if not nodes:
            self.error("metrics", "No scheduled Boutique pods found")
        for node in nodes:
            try:
                document = namespace_summary(self.cluster.raw(f"/api/v1/nodes/{node}/proxy/stats/summary"), self.cluster.namespace)
                rows = metric_rows(document)
                self.writer.append("telemetry/metrics/kubelet.jsonl", node=node, data=document, containers=rows)
                if not rows:
                    self.error("metrics", "Kubelet returned no container measurements")
            except Exception as error:  # noqa: BLE001 - the gap is recorded and collection continues
                log.failure(LOG, "telemetry.metrics.failed", error)
                self.error("metrics/" + node, error)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda pod: self.logs(pod, self.since), pods))
        # Deliberate overlap: retain Kubernetes timestamps and identities for downstream deduplication.
        self.since = cycle_start
        self.samples += 1

    def _loop(self):
        while not self.stop_event.is_set():
            clock = time.monotonic()
            try:
                self.collect()
            except Exception as error:  # noqa: BLE001 - the gap is recorded and the next cycle still runs
                log.failure(LOG, "telemetry.cycle.failed", error)
                self.error("collector", error)
            elapsed = time.monotonic() - clock
            if elapsed > self.interval:
                self.overruns += 1
                self.writer.append("telemetry/collection-cadence.jsonl", elapsed_seconds=elapsed,
                                   requested_interval_seconds=self.interval)
            self.stop_event.wait(max(0, self.interval - elapsed))

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="boutique-telemetry", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=180)
            if self.thread.is_alive():
                raise RuntimeError("Telemetry collector did not stop; evidence is incomplete")
