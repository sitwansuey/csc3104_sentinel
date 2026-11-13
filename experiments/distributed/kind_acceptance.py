"""Acceptance of the networked layer on the four-node kind lab sentinel-dist (P4.01).

python -m experiments.distributed.kind_acceptance plan
python -m experiments.distributed.kind_acceptance preflight --context kind-sentinel-dist
python -m experiments.distributed.kind_acceptance deploy --context kind-sentinel-dist --run-dir DIR
python -m experiments.distributed.kind_acceptance run --context kind-sentinel-dist --run-dir DIR \
    --scenarios all --repeat 5
python -m experiments.distributed.kind_acceptance table --run-dir DIR
python -m experiments.distributed.kind_acceptance teardown --context kind-sentinel-dist [--delete-cluster]

The lab is deploy/kind/kind-dist.yaml (one control plane and three workers,
labelled sentinel.io/host=a|b|c) with Chaos Mesh 2.8.2 and the manifests in
deploy/sentinel/sentinel-dist: the Coordinator on node c, executor-a on node a,
executor-b on node b, and the Planner, Guard, Verifier and Observer as their
own Deployments, each with its own Secret, PVC and Coordinator identity. The
exact recipe, with the commands that build the cluster, is in
docs/distributed-acceptance.md.

Only the context kind-sentinel-dist is used, and only when it points at the
kind cluster sentinel-dist (check_context, preflight). Any context or cluster
naming sentinel-lab is refused before anything else; so is every other
context, the current context included: every kubectl and helm call names
--context explicitly, and the user's current context is never changed. The
one exception is a read: preflight lists Chaos Mesh experiments with the
connected lab's context (kubectl get only). preflight refuses to start
without enough free Docker memory (docker stats against docker info) and
while another heavy lab run may be going on: the shared heavy-lab lock
(experiments/lab_lock.py, also taken by the P4.03 chaos and P4.05 pair runs)
is held or left stale, or Chaos Mesh experiments exist in the connected lab.
deploy and run hold that lock for their whole run; --break-stale-lock
removes one left by a process that is no longer running.

What deploy writes to the cluster: the namespaces, workload, RBAC, network
policy, Coordinator, Observer and five workers of
deploy/sentinel/sentinel-dist; the Executor admission policy bound to the
workload namespace; per-identity
Secrets with fresh random credentials (sent to kubectl on stdin, never in
argv or a file); a throwaway CA and server certificate; and the target's
inputs (baseline, policy, allowed targets, Verifier inventory, approval
permissions bound to the target's UID). It also registers the loaded demo
image under its manifest digest on the target's node and points the
Deployment at that digest, as experiments/recovery/live.py does, because
the Guard admits only digest-pinned images.

What run does per scenario and repeat: the operator injects the target's
fault (STARTUP_FAILURE=true, an operator write recorded as such), approves
each action through the authenticated approval API over a port-forward (the
harness approves on the operator's behalf with the operator's credential: no
person reads or types an approval, so a run is not evidence of a human in the
loop), and applies the scenario's fault: Chaos Mesh PodChaos, NetworkChaos or TimeChaos
objects in sentinel-dist only, docker pause of one executor node, a fault
point armed inside a worker Pod, or a Pod deletion. It then copies the
Coordinator's and every worker's databases out of their Pods (read-only
SQLite backups through python in the container, which has no shell or tar),
reads the Pods' logs and the API server audit log, and computes I1-I6 and
X1-X4 (experiments/distributed/invariants.py) and I7 from the audit log
(experiments/distributed/audit.py), reading the rotated audit files too. The
audit window and the log reads are moved onto the cluster's clock by the
offset measured against the control-plane container before and after the
run (clock_offset). I7 fails when the extract may be short
(audit.completeness): the operator's injection missing from it, a generation
the target gained with no write in it, or a log that starts after the run
began. Each scenario's expected outcome is checked too (expectation), and a
run passes only when the invariants, its expectation and the extract's
completeness all hold. After each run the target is put back to
its baseline template when Sentinel did not (an operator write, recorded).
Each run writes summary.json, timeline.jsonl, audit-extract.jsonl,
records.json and versions.json under <run-dir>/<date>/<scenario>-r<n>-<time>z/,
after checking that none holds a Secret name, a credential or key material.
One Coordinator store serves every run, so the invariants are narrowed to the
incidents each run opened (scope_records).

Measured, not assumed: takeover latency (fault to the other Executor's first
claim) and Coordinator recovery (kill to Pod Ready, to its first answered
claim and to the next journal transition), with the fault time moved onto
the cluster's clock by the measured offset. These are four containers on one
Docker host: they share one kernel, one clock and one disk, and are not
separate machines.

Status: written and tested offline only (tests/test_kind_acceptance.py, with
a fake kubectl, docker and helm). Nothing here has run against a cluster.
"""
import argparse
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time

from experiments import lab_lock
from experiments.distributed import audit, invariants

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = "kind-sentinel-dist"
CLUSTER = "sentinel-dist"
FORBIDDEN = "sentinel-lab"
NAMESPACE = "sentinel-dist"
WORKLOAD = "sentinel-dist-workload"
TARGET = "demo-api"
MANIFESTS = ROOT / "deploy/sentinel/sentinel-dist"
ADMISSION = ROOT / "deploy/sentinel/executor-admission.yaml"
KIND_CONFIG = ROOT / "deploy/kind/kind-dist.yaml"
NODE_IMAGE = ("kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5")
CHAOS_VERSION = "2.8.2"
CHAOS_NAMESPACE = "chaos-mesh"
CONNECTED_CONTEXT = "kind-sentinel-boutique-connected"  # Where P4.03 runs Chaos Mesh; read here, never written.
CHAOS_KINDS = ("podchaos", "networkchaos", "stresschaos", "timechaos")
# The heavy-lab lock shared with the P4.03 chaos and P4.05 pair harnesses (experiments/lab_lock.py); the check
# for Chaos Mesh experiments in the connected lab (connected_chaos) also catches a run started without it.
LOCK_NAME = lab_lock.LOCK_NAME
AUDIT_LOG = "/var/log/kubernetes/kube-apiserver-audit.log"
AUDIT_DIR = "/var/log/kubernetes"
# The live log and the backups the API server rotates it into (--audit-log-maxbackup in kind-dist.yaml).
AUDIT_FILE = re.compile(r"/var/log/kubernetes/kube-apiserver-audit(-[0-9T:.-]+)?\.log\Z")
CONTROL_PLANE = CLUSTER + "-control-plane"
LOCAL_IMAGES = {"coordinator": "sentinel-coordinator:0.1.0", "observer": "sentinel-observer:0.2.0",
                "worker": "sentinel-worker:local", "demo": "sentinel-demo:0.1.0"}
# Deployment name -> the node label value it must run on (deploy/sentinel/sentinel-dist).
PINNED = {"sentinel-coordinator": "c", "sentinel-executor-a": "a", "sentinel-executor-b": "b"}
DEPLOYMENTS = ("sentinel-coordinator", "sentinel-observer-1", "sentinel-planner-1", "sentinel-guard-1",
               "sentinel-executor-a", "sentinel-executor-b", "sentinel-verifier-1")
# Coordinator identity -> (Deployment, container, roles with a credential): delivery always, approval when listed.
IDENTITIES = {"observer-1": ("sentinel-observer-1", "observer", False),
              "planner-1": ("sentinel-planner-1", "worker", False),
              "guard-1": ("sentinel-guard-1", "worker", True),
              "executor-a": ("sentinel-executor-a", "worker", True),
              "executor-b": ("sentinel-executor-b", "worker", True),
              "verifier-1": ("sentinel-verifier-1", "worker", False)}
OPERATOR = "operator-1"
EXECUTORS = ("executor-a", "executor-b")
MIN_FREE_GIB = 3.0  # Free Docker memory a run needs once the four nodes and Chaos Mesh are up.
CREATE_FREE_GIB = 6.0  # Before kind create: four nodes, Chaos Mesh and Sentinel need about 5-6 GB (A6 risks).
LEASE_SECONDS = 15
DEADLINE_SECONDS = 300
COORDINATOR_DNS = ("sentinel-coordinator.sentinel-dist.svc", "sentinel-coordinator.sentinel-dist.svc.cluster.local")
# TEST-NET-1 placeholders in networkpolicy.yaml for the kubernetes Service and the API server endpoint.
API_PLACEHOLDERS = {"service": "192.0.2.1/32", "endpoint": "192.0.2.2/32"}
SHIPPED_ADMISSION_NAMESPACE = 'values: ["sentinel-demo"]'
CHAOS_LABEL = {"app.kubernetes.io/managed-by": "sentinel-kind-acceptance"}
# Fault-point helper run with the worker's own python (the images have no shell). Arguments are
# a fixed operation, a fault point name from runtime/fault_points.py and an action word; never a secret.
# A Pod's /faults outlives a container restart, so arm first removes the last run's .reached and .release:
# otherwise a repeat would see the old holder at once, and a blocked point would pass straight through.
FAULT_HELPER = (
    "import pathlib, sys\n"
    "d = pathlib.Path('/faults'); op, stem = sys.argv[1], sys.argv[2]\n"
    "if op == 'arm':\n"
    "    for old in ('.reached', '.release'): (d / (stem + old)).unlink(missing_ok=True)\n"
    "    (d / (stem + '.action')).write_text(sys.argv[3])\n"
    "elif op == 'disarm': (d / (stem + '.action')).unlink(missing_ok=True)\n"
    "elif op == 'release': (d / (stem + '.release')).write_text('go')\n"
    "elif op == 'reached': p = d / (stem + '.reached'); print(p.read_text() if p.exists() else '')\n")
# Read-only SQLite helpers run with the Pod's own python. QUERY prints JSON rows; BACKUP writes a
# consistent copy of one database file to stdout, so no shell, tar or kubectl cp is needed.
QUERY_HELPER = (
    "import json, sqlite3, sys\n"
    "c = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True, timeout=10); c.row_factory = sqlite3.Row\n"
    "print(json.dumps([dict(r) for r in c.execute(sys.argv[2], json.loads(sys.argv[3]))]))\n")
BACKUP_HELPER = (
    "import pathlib, sqlite3, sys\n"
    "p = pathlib.Path(sys.argv[1])\n"
    "if p.is_file():\n"
    "    s = sqlite3.connect('file:' + str(p) + '?mode=ro', uri=True, timeout=10); m = sqlite3.connect(':memory:')\n"
    "    s.backup(m); sys.stdout.buffer.write(m.serialize())\n")
PROBE_HELPER = (
    "import socket, sys\n"
    "try:\n"
    "    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=float(sys.argv[3])).close(); print('open')\n"
    "except OSError as error: print(type(error).__name__)\n")


class AcceptanceError(RuntimeError):
    """The lab is not the one this harness may use, or not in the state a run needs."""


def utc_now():
    return datetime.now(UTC)


def stamp(moment):
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def shift(text, seconds):
    """The UTC stamp text moved by seconds."""
    return stamp(datetime.fromisoformat(str(text).replace("Z", "+00:00")) + timedelta(seconds=seconds))


def clock_offset(before, remote_text, after):
    """The control plane's clock minus the harness's, from one reading taken between before and after.

    remote_text is `date +%s.%N` run in the control-plane container. As in
    Cristian's algorithm (runtime/worker_http.py::ClockEstimator), the reading
    is taken to be at the midpoint of the exchange, so the error is at most
    half its round trip. Every container on the Docker host reads the same
    kernel clock, so this is also the offset of the Pods' and the API
    server's stamps, except where TimeChaos shifts a container's own clock.
    """
    try:
        remote = float(str(remote_text).strip())
    except ValueError:
        raise AcceptanceError("Unreadable control-plane clock") from None
    span = (after - before).total_seconds()
    if span < 0:
        raise AcceptanceError("The harness clock went backwards while reading the control-plane clock")
    return {"offset_seconds": round(remote - (before.timestamp() + span / 2), 3),
            "uncertainty_seconds": round(span / 2, 3), "at": stamp(before)}


def control_plane_window(since, until, offsets):
    """(since, until) on the API server's clock: shifted by every measured offset and widened by its error.

    since and until are harness stamps; until may be None (open). With no
    offset measured the window is returned unchanged.
    """
    if not offsets:
        return since, until
    low = min(item["offset_seconds"] - item["uncertainty_seconds"] for item in offsets)
    high = max(item["offset_seconds"] + item["uncertainty_seconds"] for item in offsets)
    return shift(since, low), (shift(until, high) if until is not None else None)


def check_context(context):
    """kind-sentinel-dist only. Anything naming sentinel-lab is refused first, then every other context."""
    if not isinstance(context, str) or FORBIDDEN in context:
        raise AcceptanceError("The acceptance harness never targets " + FORBIDDEN)
    if context != CONTEXT:
        raise AcceptanceError(f"Only {CONTEXT} is accepted; this context is refused")
    return context


# Shell ---------------------------------------------------------------------------------------------------


class Shell:
    """kubectl, docker, helm and kind with an explicit context; runner is injected by tests."""

    def __init__(self, context, *, runner=None, popen=None):
        self.context = check_context(context)
        # Looked up at call time, so a test that patches subprocess.run also covers a Shell made by main().
        self.runner = runner or (lambda *args, **options: subprocess.run(*args, **options))
        self.popen = popen or (lambda *args, **options: subprocess.Popen(*args, **options))

    def run(self, command, *, stdin=None, timeout=60, check=True, binary=False):
        result = self.runner(command, input=stdin, capture_output=True, text=not binary, timeout=timeout,
                             check=False)
        if check and result.returncode:
            raise AcceptanceError(f"{Path(command[0]).name} {command[1]} failed (exit {result.returncode})")
        return result

    def kubectl(self, *args, namespace=None, stdin=None, timeout=60, check=True, binary=False):
        command = ["kubectl", "--context=" + self.context, "--request-timeout=30s"]
        if namespace is not None:
            command.append("--namespace=" + namespace)
        return self.run(command + list(args), stdin=stdin, timeout=timeout, check=check, binary=binary)

    def json(self, *args, namespace=None):
        return json.loads(self.kubectl(*args, "--output=json", namespace=namespace).stdout)

    def apply(self, document, *, namespace=None):
        """Apply one JSON document read from stdin (never a Secret: see create)."""
        if document.get("kind") == "Secret":
            raise AcceptanceError("Secrets are created, not applied, so no last-applied copy holds a credential")
        return self.kubectl("apply", "--filename=-", namespace=namespace, stdin=json.dumps(document))

    def create(self, document, *, namespace=None):
        """Create one JSON document read from stdin, so no credential reaches argv, a file or an annotation."""
        return self.kubectl("create", "--filename=-", namespace=namespace, stdin=json.dumps(document))

    def docker(self, *args, timeout=60, check=True):
        return self.run(["docker", *args], timeout=timeout, check=check)

    def helm(self, *args, timeout=60, check=True):
        return self.run(["helm", "--kube-context", self.context, *args], timeout=timeout, check=check)


# Preflight -----------------------------------------------------------------------------------------------

_SIZE = re.compile(r"\s*([0-9.]+)\s*([KMGT]?i?B)\s*\Z")
_UNITS = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4,
          "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4}


def parse_size(text):
    """Bytes in one docker stats size such as '512MiB' or '1.5GB'."""
    match = _SIZE.match(text or "")
    if not match:
        raise AcceptanceError("Unreadable docker memory size")
    return int(float(match.group(1)) * _UNITS[match.group(2).upper()])


def memory_headroom(total_bytes, stats_lines):
    """(total, used, free) bytes: docker info's MemTotal against every running container's usage."""
    used = 0
    for line in stats_lines:
        if not line.strip():
            continue
        usage = json.loads(line).get("MemUsage", "")
        used += parse_size(usage.split("/", 1)[0])
    return total_bytes, used, max(0, total_bytes - used)


class LabLock(lab_lock.HeavyLabLock):
    """The shared heavy-lab lock (experiments/lab_lock.py) as P4.01 takes it; refusals are AcceptanceError."""

    def __init__(self, path=None, holder="P4.01 kind_acceptance", **options):
        super().__init__(path, holder, **options)

    def check(self):
        try:
            super().check()
        except lab_lock.LabLockError as error:
            raise AcceptanceError(str(error)) from None

    def acquire(self):
        try:
            return super().acquire()
        except lab_lock.LabLockError as error:
            raise AcceptanceError(str(error)) from None


def connected_chaos(shell):
    """Names of Chaos Mesh experiments in the connected lab (P4.03), read with its context; [] when absent."""
    contexts = shell.kubectl("config", "get-contexts", "--output=name").stdout.split()
    if CONNECTED_CONTEXT not in contexts:
        return []
    command = ["kubectl", "--context=" + CONNECTED_CONTEXT, "--request-timeout=15s", "get", ",".join(CHAOS_KINDS),
               "--all-namespaces", "--ignore-not-found", "--output=name"]
    result = shell.run(command, check=False, timeout=30)
    if result.returncode:
        if "the server doesn't have a resource type" in (result.stderr or ""):
            return []  # Chaos Mesh is not installed there.
        raise AcceptanceError("Could not tell whether the P4.03 lab is running; check it and retry")
    return [line for line in result.stdout.split() if line]


def host_checks(shell, *, min_free_gib, lock):
    """Docker memory headroom, the shared lock and P4.03's Chaos Mesh experiments; AcceptanceError when one fails."""
    info = json.loads(shell.docker("info", "--format", "{{json .}}").stdout)
    stats = shell.docker("stats", "--no-stream", "--format", "{{json .}}", timeout=120).stdout.splitlines()
    total, used, free = memory_headroom(int(info["MemTotal"]), stats)
    gib = 1024 ** 3
    checks = {"docker_memory": {"total_gib": round(total / gib, 2), "used_gib": round(used / gib, 2),
                                "free_gib": round(free / gib, 2), "required_free_gib": min_free_gib}}
    if free < min_free_gib * gib:
        raise AcceptanceError(f"Only {free / gib:.1f} GiB of Docker memory is free; {min_free_gib} GiB is needed")
    (lock or LabLock()).check()  # Held by another run, or stale: refused, never taken over here.
    running = connected_chaos(shell)
    if running:
        raise AcceptanceError(f"{len(running)} Chaos Mesh experiment(s) exist in {CONNECTED_CONTEXT}: "
                              "P4.03 may be running; do not run both at once")
    checks["p4_03_running"] = False
    return checks


def preflight(shell, *, min_free_gib=MIN_FREE_GIB, lock=None, before_create=False):
    """Every check a run needs before it touches the cluster; AcceptanceError on the first that fails.

    Returns the checks, which hold no credential: tool presence, the context
    and its cluster, the node image and labels, the audit log, the Chaos Mesh
    release, Docker memory headroom and that P4.03 is not running. With
    before_create, the gate for kind create cluster: sentinel-dist must not
    exist yet, and only the host checks run (with CREATE_FREE_GIB by default).
    """
    checks = {}
    for tool in ("kubectl", "kind", "docker", "helm"):
        if shutil.which(tool) is None:
            raise AcceptanceError(f"Missing {tool}")
    clusters = shell.run(["kind", "get", "clusters"]).stdout.split()
    if any(FORBIDDEN in name for name in clusters):
        checks["other_clusters_seen"] = "a cluster this harness never uses exists and is left alone"
    if before_create:
        if CLUSTER in clusters:
            raise AcceptanceError(f"kind cluster {CLUSTER} already exists; delete it first or skip creation")
        return {**checks, "cluster": "absent", **host_checks(shell, min_free_gib=min_free_gib, lock=lock)}
    if CLUSTER not in clusters:
        raise AcceptanceError(f"kind cluster {CLUSTER} does not exist; create it from {KIND_CONFIG.name}")
    checks.update(cluster_identity(shell))
    names = checks["nodes"]["names"]
    images = {shell.docker("inspect", "--format", "{{.Config.Image}}", name).stdout.strip() for name in names}
    if images != {NODE_IMAGE}:
        raise AcceptanceError("A node does not run the pinned node image " + NODE_IMAGE)
    checks["node_image"] = NODE_IMAGE
    if shell.docker("exec", CONTROL_PLANE, "test", "-s", AUDIT_LOG, check=False).returncode:
        raise AcceptanceError(f"The API server audit log is missing; create the cluster from {KIND_CONFIG.name}")
    checks["audit_log"] = AUDIT_LOG
    releases = json.loads(shell.helm("list", "--namespace", CHAOS_NAMESPACE, "--output", "json").stdout or "[]")
    charts = [release.get("chart") for release in releases if release.get("name") == "chaos-mesh"]
    if charts != ["chaos-mesh-" + CHAOS_VERSION]:
        raise AcceptanceError(f"Chaos Mesh chart {CHAOS_VERSION} is not installed in {CHAOS_NAMESPACE}")
    checks["chaos_mesh"] = CHAOS_VERSION
    checks.update(host_checks(shell, min_free_gib=min_free_gib, lock=lock))
    return checks


def cluster_identity(shell):
    """That kind-sentinel-dist names the local kind cluster sentinel-dist and its four labelled nodes.

    Run by preflight and by teardown before it deletes anything, so neither
    acts on a context that was renamed onto another cluster.
    """
    checks = {}
    view = json.loads(shell.kubectl("config", "view", "--minify", "--output=json").stdout)
    cluster_names = [item.get("name") for item in view.get("clusters", [])]
    server = (view.get("clusters") or [{}])[0].get("cluster", {}).get("server", "")
    if cluster_names != [CONTEXT] or not re.match(r"https://(127\.0\.0\.1|localhost):\d+\Z", server):
        raise AcceptanceError(f"{CONTEXT} does not point at the local kind cluster {CLUSTER}")
    checks["context"] = {"context": CONTEXT, "cluster": cluster_names[0], "loopback_https": True}
    nodes = shell.json("get", "nodes")["items"]
    names = sorted(node["metadata"]["name"] for node in nodes)
    hosts = {node["metadata"].get("labels", {}).get("sentinel.io/host"): node["metadata"]["name"] for node in nodes}
    if len(nodes) != 4 or any(not name.startswith(CLUSTER + "-") for name in names) \
            or set(hosts) != {None, "a", "b", "c"} or hosts[None] != CONTROL_PLANE:
        raise AcceptanceError("The cluster must be the four sentinel-dist nodes with hosts a, b and c")
    versions = {node["status"]["nodeInfo"]["kubeletVersion"] for node in nodes}
    checks["nodes"] = {"names": names, "hosts": {key: value for key, value in hosts.items() if key},
                       "kubelet_versions": sorted(versions)}
    return checks


# Rendering -----------------------------------------------------------------------------------------------


def render_api_server_policy(text, service_ip, endpoint_ip):
    """networkpolicy.yaml with its two TEST-NET placeholders replaced by the API server's addresses."""
    for value in (service_ip, endpoint_ip):
        if not re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})", value or "") \
                or any(int(part) > 255 for part in value.split(".")):
            raise AcceptanceError("The API server address must be one IPv4 address")
    for placeholder, value in ((API_PLACEHOLDERS["service"], service_ip), (API_PLACEHOLDERS["endpoint"], endpoint_ip)):
        if text.count("cidr: " + placeholder) != 1:
            raise AcceptanceError("networkpolicy.yaml no longer holds exactly one placeholder for " + placeholder)
        text = text.replace("cidr: " + placeholder, f"cidr: {value}/32")
    return text


def render_admission(text):
    """The shipped Executor admission policy and binding, bound to the workload namespace only.

    Only the binding's namespace changes; every CEL expression is the shipped,
    pinned text (tests/test_deploy_manifests.py::PINNED_VALIDATIONS).
    """
    if text.count(SHIPPED_ADMISSION_NAMESPACE) != 1:
        raise AcceptanceError("The admission binding no longer selects exactly one shipped namespace")
    return text.replace(SHIPPED_ADMISSION_NAMESPACE, f'values: ["{WORKLOAD}"]')


def env_name(prefix, identity):
    return prefix + identity.upper().replace("-", "_")


def credentials():
    """Fresh random credentials: {identity: {"delivery-token": ..., "approval-token": ...}}."""
    values = {}
    for identity, (_, _, approval) in IDENTITIES.items():
        values[identity] = {"delivery-token": secrets.token_urlsafe(32)}
        if approval:
            values[identity]["approval-token"] = secrets.token_urlsafe(32)
    values[OPERATOR] = {"approval-token": secrets.token_urlsafe(32)}
    return values


def secret_name(identity):
    return f"{CLUSTER}-{identity}"


def secret_documents(values):
    """One Opaque Secret per identity in sentinel-dist; each worker Pod mounts only its own."""
    return [{"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
             "metadata": {"name": secret_name(identity), "namespace": NAMESPACE,
                          "labels": {"sentinel.io/lab": CLUSTER}},
             "stringData": dict(keys)} for identity, keys in sorted(values.items())]


def certificate_names():
    """Subject alternative names of the Coordinator's throwaway server certificate."""
    return tuple("DNS:" + name for name in COORDINATOR_DNS) + ("DNS:localhost", "IP:127.0.0.1")


def target_inputs(target, template, window):
    """The files of the sentinel-dist-inputs ConfigMap: no credential, only the target's reviewed facts."""
    from experiments.recovery.live import LAB_CEILINGS, policy_ceilings
    from services.executor.store import fingerprint
    digest = fingerprint(template)
    source = CLUSTER + "-kind-acceptance"
    baseline = {"baseline_id": "BASE-DIST-" + secrets.token_hex(6), "target": target,
                "healthy_observed_at": window[-1]["at"], "pod_template": template, "template_sha256": digest,
                "reference_versions": [],
                "health_evidence": {"source": source, "evidence_id": "HEALTH-" + secrets.token_hex(16),
                                    "status": "HEALTHY", "target": target, "template_sha256": digest,
                                    "window_start": window[0]["at"], "window_end": window[-1]["at"]}}
    policy = {"policy_id": "sentinel-dist-acceptance", "policy_version": "sentinel-dist-acceptance-1",
              "profile": "live", "targets": [target], "allowed_actions": ["ROLLBACK"],
              "require_operator_approval": True, "unknown_dependency_result": "ESCALATE",
              "max_state_age_seconds": 30, "max_dependency_age_seconds": 300, "decision_ttl_seconds": 300,
              "max_approval_lifetime_seconds": 300, "require_server_dry_run": False,
              "baseline_constraints": {"max_age_seconds": 14400, "min_healthy_duration_seconds": 30,
                                       "trusted_sources": [source], "require_health_evidence": True,
                                       "require_compatible_references": True, "require_image_digests": True},
              "security_constraints": {"deny_privileged": True, "deny_host_access": True,
                                       "allowed_image_registries": ["docker.io"],
                                       "allowed_service_accounts": ["default"],
                                       "forbid_service_account_change": True},
              **policy_ceilings(LAB_CEILINGS),
              "detection": {"failure_duration_seconds": 15},
              "health": {"protocol": "http", "required_http_status": 200, "minimum_samples": 2,
                         "stable_window_seconds": 10, "max_sample_gap_seconds": 20,
                         "require_target_identity": True, "require_template_match": True,
                         "require_available_replicas": True, "convergence_grace_seconds": 60,
                         "verification_timeout_seconds": 90},
              "note": "sentinel-dist acceptance lab (P4.01): demo-api health over cluster HTTP plus replica "
                      "readiness, collected by the in-cluster Verifier."}
    inventory = {"cluster_id": CLUSTER,
                 "services": {TARGET: {"target": target,
                                       "health_url": f"http://{TARGET}.{WORKLOAD}.svc:8080/readyz"}},
                 "declared_edges": [], "dependency_inventory_complete": True, "require_traces": False,
                 "max_collection_seconds": 20, "max_health_age_seconds": 10, "max_trace_age_seconds": 300}
    return {"baseline.json": baseline, "policy.json": policy, "allowed-targets.json": [target],
            "verifier-inventory.json": inventory, "observer-policy.json": {"detection": policy["detection"]}}


def approval_permissions(target):
    """Approval permissions bound to the target's exact UID: the operator may approve, others only read."""
    permissions = {OPERATOR: [{"target": target, "operations": ["create", "read", "revoke", "history"]}]}
    for identity in ("guard-1", *EXECUTORS):
        permissions[identity] = [{"target": target, "operations": ["read"]}]
    return permissions


def config_map(name, files):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": NAMESPACE, "labels": {"sentinel.io/lab": CLUSTER}},
            "data": {key: value if isinstance(value, str) else json.dumps(value, indent=2)
                     for key, value in files.items()}}


# Chaos Mesh objects --------------------------------------------------------------------------------------

_LABEL_VALUE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\Z")


def _selector(app):
    if not isinstance(app, str) or not _LABEL_VALUE.match(app) or not app.startswith("sentinel-"):
        raise AcceptanceError("Chaos selects only sentinel-* Pods by their app label")
    return {"namespaces": [NAMESPACE], "labelSelectors": {"app": app}}


def _duration(seconds):
    if type(seconds) is not int or not 5 <= seconds <= 900:
        raise AcceptanceError("A chaos duration must be 5 to 900 whole seconds")
    return f"{seconds}s"


def chaos(kind, name, spec):
    if kind not in {"PodChaos", "NetworkChaos", "TimeChaos"} or not _LABEL_VALUE.match(name):
        raise AcceptanceError("Unsupported chaos object")
    return {"apiVersion": "chaos-mesh.org/v1alpha1", "kind": kind,
            "metadata": {"name": name, "namespace": NAMESPACE, "labels": dict(CHAOS_LABEL)}, "spec": spec}


def pod_kill(name, app):
    return chaos("PodChaos", name, {"action": "pod-kill", "mode": "one", "gracePeriod": 0, "selector": _selector(app)})


def pod_failure(name, app, seconds):
    return chaos("PodChaos", name, {"action": "pod-failure", "mode": "one", "duration": _duration(seconds),
                                    "selector": _selector(app)})


def partition(name, app, targets, seconds, direction="both"):
    """Drop traffic between app's Pod and the targets' Pods (direction: to, from or both)."""
    if direction not in {"to", "from", "both"} or not targets:
        raise AcceptanceError("A partition needs a direction and at least one target")
    target = {"mode": "all", "selector": {"namespaces": [NAMESPACE],
                                          "expressionSelectors": [{"key": "app", "operator": "In",
                                                                   "values": sorted(targets)}]}}
    for value in targets:
        _selector(value)
    return chaos("NetworkChaos", name, {"action": "partition", "mode": "all", "selector": _selector(app),
                                        "direction": direction, "target": target, "duration": _duration(seconds)})


def delay_to_api_server(name, app, address, seconds, latency_ms=8000):
    """Delay app's traffic to the API server (S2 of the A4 design: below kubectl's 10 s request timeout)."""
    if type(latency_ms) is not int or not 1 <= latency_ms < 10000:
        raise AcceptanceError("The delay must stay below kubectl's 10 s request timeout")
    return chaos("NetworkChaos", name, {"action": "delay", "mode": "all", "selector": _selector(app),
                                        "direction": "to", "externalTargets": [address],
                                        "delay": {"latency": f"{latency_ms}ms", "jitter": "0ms", "correlation": "0"},
                                        "duration": _duration(seconds)})


def time_skew(name, apps, offset_seconds, seconds):
    """Shift CLOCK_REALTIME of the worker container in each app's Pod by offset_seconds."""
    if type(offset_seconds) is not int or not 1 <= abs(offset_seconds) <= 3600:
        raise AcceptanceError("A clock offset must be 1 to 3600 whole seconds either way")
    for app in apps:
        _selector(app)
    selector = {"namespaces": [NAMESPACE], "expressionSelectors": [{"key": "app", "operator": "In",
                                                                    "values": sorted(apps)}]}
    return chaos("TimeChaos", name, {"mode": "all", "selector": selector, "timeOffset": f"{offset_seconds}s",
                                     "clockIds": ["CLOCK_REALTIME"], "containerNames": ["worker"],
                                     "duration": _duration(seconds)})


# Scenarios -----------------------------------------------------------------------------------------------

ALL_CHECKS = ("I1", "I2", "I3", "I4", "I5", "I6", "X1", "X2", "X3", "X4", "I7")


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    fault: str
    catalogue: str  # The file-mode scenario (experiments/distributed/scenarios.py) it maps onto Pods.
    expect: str
    checks: tuple = ALL_CHECKS
    options: dict = field(default_factory=dict)


SCENARIOS = {item.name: item for item in (
    Scenario("S0", "No fault: authorized recovery with operator approval through the approval API",
             "none", "S0", "RECOVERED, exactly 1 Sentinel template write"),
    Scenario("S1a", "Kill the Planner Pod mid-inference", "fault point planner.mid_inference (block), then "
             "PodChaos pod-kill of the Planner", "S1a", "RECOVERED, exactly 1 Sentinel template write"),
    Scenario("S1b", "Kill an Executor after its write intent; the other takes over",
             "fault point executor.after_start_write (block), then PodChaos pod-failure of that Executor "
             "until the incident ends", "S1b", "RECOVERED, exactly 1 Sentinel template write, takeover measured"),
    Scenario("S1c", "Kill an Executor after its patch, before it saved the result",
             "fault point executor.after_patch_before_save (block), then PodChaos pod-failure of that "
             "Executor until the incident ends", "S1c",
             "RECOVERED, exactly 1 Sentinel template write, takeover measured"),
    Scenario("S2", "Stall the lease holder's node past its lease (docker pause)",
             "fault point executor.before_patch (block), then docker pause of the holder's node past the "
             "lease, unpause after the other Executor wrote", "S2", "RECOVERED, exactly 1 Sentinel template "
             "write, not from the paused holder's Pod"),
    Scenario("S3", "Partition the lease holder from the Coordinator; it can still reach the API server",
             "fault point executor.after_start_write (block), then NetworkChaos partition holder<->Coordinator",
             "S3", "every incident terminal, at most 1 Sentinel template write"),
    Scenario("S4", "Lose the Coordinator's reply to the Executor's result",
             "fault point executor.after_patch_before_save (block), then NetworkChaos partition "
             "Coordinator->holder (direction from) until the result is recorded", "S4",
             "RECOVERED, exactly 1 Sentinel template write"),
    Scenario("S5", "Restart the Coordinator between commit and reply, then delete its Pod",
             "fault point coordinator.after_commit_before_reply@execution_result (exit), then kubectl delete "
             "pod of the Coordinator", "S5", "RECOVERED, exactly 1 Sentinel template write, Coordinator RTO to "
             "Ready measured"),
    Scenario("S6", "Partition the Coordinator from every worker for three leases",
             "NetworkChaos partition Coordinator<->all workers while an approval is pending", "S6",
             "0 template writes during the partition, every incident terminal after it",
             options={"auto_approve": False}),
    Scenario("S7", "Both Executors' clocks 300 s behind (TimeChaos)",
             "TimeChaos -300 s on executor-a and executor-b", "S7", "FAILED, none RECOVERED, 0 Sentinel template "
             "writes"),
    Scenario("S8", "Revoke the approval while the holder is paused before its patch; the other Executor seals",
             "fault point executor.before_patch (block), revoke through the approval API, then the holder's "
             "traffic to the API server is delayed 8 s (NetworkChaos) when it is released", "S8",
             "not RECOVERED, 0 template writes, a seal from the other Executor's Pod, then the holder's PATCH "
             "refused (409 or 422), generation unchanged after the seal"),
    Scenario("S9", "RBAC, admission and network policy denials", "the Executor reads a Secret, patches "
             "another Deployment, asks for pods/exec and changes spec.replicas; the Planner opens a TCP "
             "connection to the API server; the Executor to the target's port", "design S9 (A6)",
             "every probe as expected, 0 Sentinel template writes", checks=("I7",)),
)}
ORDER = tuple(SCENARIOS)


def select(names):
    """Scenario names in catalogue order; 'all' is every one."""
    if names in ("all", ["all"], ("all",)):
        return list(ORDER)
    names = [names] if isinstance(names, str) else list(names)
    unknown = sorted(set(names) - set(SCENARIOS))
    if unknown:
        raise AcceptanceError("Unknown scenario(s): " + ", ".join(unknown))
    return [name for name in ORDER if name in names]


RECOVERS = frozenset({"S0", "S1a", "S1b", "S1c", "S2", "S4", "S5"})


def _from_pod(row, identity):
    """Whether an audit row was made from a Pod of the Deployment of identity (by its bound Pod name)."""
    app = IDENTITIES[identity][0] if identity in IDENTITIES else None
    return bool(app and row.get("pod") and row["pod"].startswith(app + "-"))


def _on_target(row):
    return row.get("namespace") == WORKLOAD and row.get("name") == TARGET and row.get("resource") == "deployments"


def expectation(name, *, incidents, rows, notes=None, stalled=None, takeover=None, recovery=None,
                generation_at_end=None):
    """Failures of scenario name's expected outcome (Scenario.expect), from what the run recorded; [] when met.

    incidents are the run's scoped incident rows, rows its audit extract,
    notes the scenario's notes, stalled the Executor a fault point held,
    takeover and recovery the measurements, and generation_at_end the
    target's metadata.generation after the run and before the operator's
    restore. The invariants cap writes and need a terminal state; this checks
    what each scenario is for: S0, S1a-S1c, S2, S4 and S5 end RECOVERED with
    exactly one Sentinel template write (S2's not from the paused holder's
    Pod; S1b and S1c with a takeover measured; S5 with the Coordinator's
    return to Ready measured); S3 and S6 end with every incident terminal and
    at most one write (S6 with none during the partition); S7 ends FAILED,
    none RECOVERED, with no write; S8 ends not RECOVERED with no template
    write, a seal (fence) from the other Executor's Pod, after it a PATCH
    from the holder's Pod refused with 409 or 422, and no change to the target
    after the seal (its generation is the seal's); S9's probes all behave as
    expected with no write.
    """
    notes, failures = notes or {}, []
    states = sorted({row["state"] for row in incidents})
    writes = [row for row in rows if row["effect"] == "template_write" and row["sentinel"]]

    def need(condition, message):
        if not condition:
            failures.append(message)
    if name in RECOVERS:
        need("RECOVERED" in states, f"no incident RECOVERED (states: {', '.join(states) or 'none'})")
        need(len(writes) == 1, f"{len(writes)} Sentinel template write(s), expected exactly 1")
    if name in {"S1b", "S1c"}:
        need(takeover is not None, "no claim by the other Executor after the fault: takeover not measured")
    if name == "S2":
        need(stalled in EXECUTORS, "no paused holder was recorded")
        need(not any(_from_pod(row, stalled) for row in writes), "the paused holder's Pod wrote the template")
    if name == "S5":
        need((recovery or {}).get("to_ready") is not None, "the Coordinator's return to Ready was not measured")
    if name in {"S3", "S6"}:
        need(bool(states) and all(state in invariants.TERMINAL for state in states),
             f"not every incident terminal (states: {', '.join(states) or 'none'})")
        need(len(writes) <= 1, f"{len(writes)} Sentinel template writes, expected at most 1")
    if name == "S6":
        during = notes.get("during_partition") or {}
        need(during.get("template_writes") == 0, "template writes during the partition not recorded as 0")
    if name == "S7":
        need("FAILED" in states and "RECOVERED" not in states,
             f"expected FAILED and none RECOVERED (states: {', '.join(states) or 'none'})")
        need(not writes, f"{len(writes)} Sentinel template write(s), expected 0")
    if name == "S8":
        need(bool(states) and "RECOVERED" not in states, f"expected not RECOVERED (states: {', '.join(states) or 'none'})")
        need(not writes, f"{len(writes)} Sentinel template write(s), expected 0")
        other = [identity for identity in EXECUTORS if identity != stalled]
        seals = [row for row in rows if row["effect"] == "fence" and row["sentinel"] and _on_target(row)
                 and stalled in EXECUTORS and any(_from_pod(row, identity) for identity in other) and row["at"]]
        need(bool(seals), "no seal or fence from the other Executor's Pod in the audit log")
        if seals:
            sealed_at = min(_seconds(row["at"]) for row in seals)
            refused = [row for row in rows if row["effect"] == "denied" and row["code"] in {409, 422}
                       and row["verb"] == "patch" and _on_target(row) and _from_pod(row, stalled) and row["at"]
                       and _seconds(row["at"]) > sealed_at]
            need(bool(refused), "no PATCH from the holder's Pod refused (409 or 422) after the seal")
            sealed = max((row["generation"] for row in seals if type(row["generation"]) is int), default=None)
            need(sealed is not None and generation_at_end == sealed,
                 f"the target's generation is {generation_at_end} at the end and {sealed} after the seal: "
                 "something changed it after the seal")
    if name == "S9":
        probes = notes.get("probes") or []
        need(bool(probes) and all(item.get("passed") for item in probes), "not every denial probe behaved as expected")
        need(not writes, f"{len(writes)} Sentinel template write(s), expected 0")
    return failures


# Measurements --------------------------------------------------------------------------------------------


def _seconds(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


def takeover_seconds(access, fault_at, stalled):
    """Seconds from the fault to the first executor command claimed by an Executor other than stalled.

    access holds the Coordinator's access-log lines (ts, identity, operation,
    http_status, outcome); fault_at is the harness's UTC stamp of the fault.
    None when no other Executor claimed afterwards.
    """
    start = _seconds(fault_at)
    claims = sorted(_seconds(line["ts"]) for line in access
                    if line.get("operation") == "claim" and line.get("outcome") == "claimed"
                    and line.get("http_status") == 200 and line.get("identity") in EXECUTORS
                    and line.get("identity") != stalled and _seconds(line["ts"]) >= start)
    return round(claims[0] - start, 3) if claims else None


def coordinator_recovery(fault_at, ready_at, access, transitions):
    """Coordinator RTO parts in seconds from the kill: Pod Ready, first answered claim, next transition."""
    start = _seconds(fault_at)

    def first(values):
        later = sorted(value for value in values if value >= start)
        return round(later[0] - start, 3) if later else None
    return {"to_ready": first([_seconds(ready_at)] if ready_at else []),
            "to_first_claim": first([_seconds(line["ts"]) for line in access
                                     if line.get("operation") == "claim" and line.get("http_status") == 200]),
            "to_next_transition": first([_seconds(row["recorded_at"]) for row in transitions])}


# The lab -------------------------------------------------------------------------------------------------


class Lab:
    """The deployed sentinel-dist lab, driven through one Shell."""

    def __init__(self, shell, run_dir, *, clock=utc_now, sleep=time.sleep, monotonic=time.monotonic):
        self.shell, self.run_dir = shell, Path(run_dir)
        self.clock, self.sleep, self.monotonic = clock, sleep, monotonic
        self.tokens = {}
        self._forward = None
        self._forward_lock = threading.Lock()
        self.approved = set()

    # Reading the cluster --------------------------------------------------------------------------------

    def nodes(self):
        """{host label: node name} for the three workers."""
        items = self.shell.json("get", "nodes")["items"]
        return {item["metadata"]["labels"]["sentinel.io/host"]: item["metadata"]["name"]
                for item in items if "sentinel.io/host" in item["metadata"].get("labels", {})}

    def pod(self, deployment):
        """The one running Pod of a sentinel-dist Deployment: (name, node)."""
        items = self.shell.json("get", "pods", "--selector=app=" + deployment, namespace=NAMESPACE)["items"]
        running = [item for item in items if item.get("status", {}).get("phase") == "Running"
                   and not item["metadata"].get("deletionTimestamp")]
        if len(running) != 1:
            raise AcceptanceError(f"{deployment} has {len(running)} running Pods")
        return running[0]["metadata"]["name"], running[0]["spec"].get("nodeName")

    def placement(self):
        """Where every Sentinel Pod runs, and whether the pinned ones are on three different nodes."""
        hosts = {name: host for host, name in self.nodes().items()}
        rows = {}
        for deployment in DEPLOYMENTS:
            pod, node = self.pod(deployment)
            rows[deployment] = {"pod": pod, "node": node, "host": hosts.get(node)}
        pinned = {deployment: rows[deployment]["host"] for deployment in PINNED}
        return {"pods": rows, "pinned": pinned,
                "pinned_on_distinct_nodes": pinned == PINNED and len(set(pinned.values())) == len(PINNED)}

    def exec_python(self, deployment, container, script, *args, binary=False, timeout=60):
        pod, _ = self.pod(deployment)
        return self.shell.kubectl("exec", pod, "--container=" + container, "--", "python", "-c", script, *args,
                                  namespace=NAMESPACE, binary=binary, timeout=timeout)

    def query(self, sql, parameters=()):
        """Read-only rows from the Coordinator's database; [] while the Coordinator is down."""
        try:
            result = self.exec_python("sentinel-coordinator", "coordinator", QUERY_HELPER,
                                      "/data/coordinator.sqlite3", sql, json.dumps(list(parameters)))
        except (AcceptanceError, subprocess.SubprocessError):
            return []
        return json.loads(result.stdout or "[]")

    def incidents(self):
        return self.query("SELECT incident_id, state, active_action_id FROM incidents")

    def wait(self, check, timeout, what, interval=1.0):
        deadline = self.monotonic() + timeout
        while self.monotonic() < deadline:
            value = check()
            if value:
                return value
            self.sleep(interval)
        raise TimeoutError(what)

    def terminal(self):
        states = [row["state"] for row in self.incidents()]
        return bool(states) and all(state in invariants.TERMINAL for state in states)

    def wait_terminal(self, timeout):
        """True once every incident is terminal; False at the deadline."""
        try:
            return bool(self.wait(self.terminal, timeout, "every incident terminal"))
        except TimeoutError:
            return False

    def wait_state(self, states, timeout):
        """The first incident row in one of states."""
        def found():
            return next((row for row in self.incidents() if row["state"] in states), None)
        return self.wait(found, timeout, "an incident in " + "/".join(sorted(states)))

    # Fault points ---------------------------------------------------------------------------------------

    def _container(self, identity):
        return "coordinator" if identity == "coordinator" else IDENTITIES[identity][1]

    def _deployment(self, identity):
        return "sentinel-coordinator" if identity == "coordinator" else IDENTITIES[identity][0]

    def fault(self, identity, operation, stem, action=""):
        args = [operation, stem] + ([action] if action else [])
        return self.exec_python(self._deployment(identity), self._container(identity), FAULT_HELPER, *args)

    def arm(self, identities, stem, action):
        for identity in identities:
            self.fault(identity, "arm", stem, action)

    def first_reached(self, identities, stem, timeout=120):
        """The identity whose process reached stem first; the others are disarmed at once."""
        def reached():
            for identity in identities:
                with suppress(AcceptanceError, subprocess.SubprocessError):
                    if self.fault(identity, "reached", stem).stdout.strip():
                        return identity
            return None
        winner = self.wait(reached, timeout, stem + " was not reached", interval=0.5)
        for identity in identities:
            if identity != winner:
                with suppress(AcceptanceError, subprocess.SubprocessError):
                    self.fault(identity, "disarm", stem)
        return winner

    # Chaos and node faults ------------------------------------------------------------------------------

    def create_chaos(self, document):
        self.shell.apply(document, namespace=NAMESPACE)
        return {"kind": document["kind"], "name": document["metadata"]["name"], "at": stamp(self.clock())}

    def delete_chaos(self, document):
        self.shell.kubectl("delete", document["kind"].lower(), document["metadata"]["name"], "--wait=true",
                           "--timeout=90s", "--ignore-not-found=true", namespace=NAMESPACE, timeout=120)

    def pause_node(self, node):
        if not node.startswith(CLUSTER + "-worker"):
            raise AcceptanceError("Only a sentinel-dist worker node may be paused")
        self.shell.docker("pause", node)

    def unpause_node(self, node):
        if not node.startswith(CLUSTER + "-worker"):
            raise AcceptanceError("Only a sentinel-dist worker node may be unpaused")
        self.shell.docker("unpause", node, check=False)

    # The operator ---------------------------------------------------------------------------------------

    def operator_token(self):
        """The operator's approval credential, read from its Secret into memory only."""
        if OPERATOR not in self.tokens:
            document = self.shell.json("get", "secret", secret_name(OPERATOR), namespace=NAMESPACE)
            import base64
            self.tokens[OPERATOR] = base64.b64decode(document["data"]["approval-token"]).decode("ascii")
        return self.tokens[OPERATOR]

    def approval_client(self, port):
        import ssl
        from runtime.approval_http import ApprovalClient
        ca = self.run_dir / "ca.crt"
        return ApprovalClient(f"https://127.0.0.1:{port}", OPERATOR, self.operator_token(), timeout=10,
                              tls_context=ssl.create_default_context(cafile=str(ca)))

    def port_forward(self, port=18767):
        """A port-forward to the Coordinator's approval port on loopback, restarted when it died."""
        with self._forward_lock:
            if self._forward is None or self._forward.poll() is not None:
                self._forward = self.shell.popen(
                    ["kubectl", "--context=" + self.shell.context, "--namespace=" + NAMESPACE, "port-forward",
                     "service/sentinel-coordinator", f"{port}:8767"], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                self.sleep(2)
        return port

    def close(self):
        if self._forward is not None and self._forward.poll() is None:
            self._forward.terminate()
            with suppress(subprocess.TimeoutExpired):
                self._forward.wait(10)
        self._forward = None

    def approve(self, action_id, policy_version):
        from runtime.worker_transport import digest
        (row,) = self.query("SELECT proposal FROM actions WHERE action_id=?", (action_id,))
        expires = self.clock() + timedelta(seconds=240)
        self.approval_client(self.port_forward()).post("create", {
            "action_id": action_id, "proposal_sha256": digest(json.loads(row["proposal"])),
            "policy_version": policy_version, "expires_at": expires.isoformat()})
        self.approved.add(action_id)

    def revoke(self, action_id):
        from runtime.worker_transport import digest
        (row,) = self.query("SELECT proposal FROM actions WHERE action_id=?", (action_id,))
        (latest,) = self.query("SELECT id FROM approvals WHERE action_id=? ORDER BY id DESC LIMIT 1", (action_id,))
        self.approval_client(self.port_forward()).post("revoke", {
            "action_id": action_id, "proposal_sha256": digest(json.loads(row["proposal"])),
            "approval_id": latest["id"], "reason": "withdrawn by the acceptance harness"})

    def approve_pending(self, policy_version):
        for row in self.incidents():
            action_id = row["active_action_id"]
            if row["state"] == "AWAITING_APPROVAL" and action_id and action_id not in self.approved:
                self.approve(action_id, policy_version)
                return action_id
        return None

    # The target -----------------------------------------------------------------------------------------

    def target(self):
        return self.shell.json("get", "deployment", TARGET, namespace=WORKLOAD)

    def inject(self):
        """The operator's fault: STARTUP_FAILURE=true on demo-api (a lab operator write, not Sentinel's)."""
        self.shell.kubectl("set", "env", "deployment/" + TARGET, "--containers=demo-api", "STARTUP_FAILURE=true",
                           namespace=WORKLOAD)
        return {"operator_action": "inject STARTUP_FAILURE=true", "at": stamp(self.clock())}

    def restore(self, baseline):
        """Put the baseline template back under uid and resourceVersion tests when Sentinel did not."""
        from services.executor.store import fingerprint
        current = self.target()
        if fingerprint(current["spec"]["template"]) == baseline["template_sha256"]:
            return {"operator_restore": False}
        patch = [{"op": "test", "path": "/metadata/uid", "value": baseline["target"]["uid"]},
                 {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
                 {"op": "replace", "path": "/spec/template", "value": baseline["pod_template"]}]
        self.shell.kubectl("patch", "deployment", TARGET, "--type=json", "--patch-file=-", namespace=WORKLOAD,
                           stdin=json.dumps(patch))
        self.shell.kubectl("rollout", "status", "deployment/" + TARGET, "--timeout=180s", namespace=WORKLOAD,
                           timeout=200)
        return {"operator_restore": True, "at": stamp(self.clock())}

    # Evidence -------------------------------------------------------------------------------------------

    def audit_files(self):
        """The live audit log and every backup the API server rotated it into, oldest first."""
        listing = self.shell.docker("exec", CONTROL_PLANE, "find", AUDIT_DIR, "-maxdepth", "1", "-type", "f",
                                    "-name", "kube-apiserver-audit*.log", timeout=60).stdout.split()
        files = sorted(path for path in listing if AUDIT_FILE.match(path))
        if AUDIT_LOG not in files:
            raise AcceptanceError("The API server audit log is missing")
        return [path for path in files if path != AUDIT_LOG] + [AUDIT_LOG]

    def audit_log(self, since, until):
        """The extract between since and until (API server clock) from every audit file, and how far back it reaches.

        Returns {"rows", "skipped", "first_at", "files"}: first_at is the
        earliest event in all files, which audit.completeness compares with
        since, so a log that rotated past the run's start is noticed.
        """
        files = self.audit_files()
        text = self.shell.docker("exec", CONTROL_PLANE, "cat", *files, timeout=180).stdout
        events, skipped = audit.parse(text)
        return {"rows": audit.extract(events, since=since, until=until), "skipped": skipped,
                "first_at": audit.first_event_at(events), "files": len(files)}

    def audit_rows(self, since, until):
        found = self.audit_log(since, until)
        return found["rows"], found["skipped"]

    def clock_offset(self):
        """The control-plane clock minus this harness's clock (clock_offset), measured now."""
        before = self.clock()
        reading = self.shell.docker("exec", CONTROL_PLANE, "date", "+%s.%N").stdout
        return clock_offset(before, reading, self.clock())

    def copy_database(self, identity, path, destination):
        """A consistent copy of one SQLite file in a Pod, written to destination; False when it is absent."""
        result = self.exec_python(self._deployment(identity), self._container(identity), BACKUP_HELPER, path,
                                  binary=True, timeout=120)
        if not result.stdout:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(result.stdout)
        return True

    def log_text(self, identity, since):
        """The current Pod's logs since a UTC stamp: a restarted container's last run first, then the current one."""
        pod, _ = self.pod(self._deployment(identity))
        text = ""
        for previous in (True, False):
            result = self.shell.kubectl("logs", pod, "--container=" + self._container(identity),
                                        "--since-time=" + since, *(["--previous"] if previous else []),
                                        namespace=NAMESPACE, check=False, timeout=60)
            if not result.returncode:
                text += result.stdout
        return text

    def logs(self, identity, since, destination, *, prefix=""):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(prefix + self.log_text(identity, since), encoding="utf-8")
        return destination


class RunView:
    """What invariants.collect reads, built from the files a run copied out of the Pods."""

    class _Process:
        def __init__(self, stderr_path):
            self.stderr_path, self.command = stderr_path, []

    def __init__(self, folder, tokens, lease_seconds=LEASE_SECONDS):
        self.run_dir = Path(folder)
        self.paths = {name: self.run_dir / name for name in ("coordinator", "state", "logs", "config")}
        for path in self.paths.values():
            path.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds
        self.tokens = {identity: keys.get("delivery-token") for identity, keys in tokens.items()}
        self.approval_tokens = {identity: keys.get("approval-token") for identity, keys in tokens.items()}
        self.processes = {name: self._Process(self.paths["logs"] / (name + ".log"))
                          for name in ("coordinator", *IDENTITIES)}


def scope_records(records, since):
    """records narrowed to the incidents this run opened (first journal entry at or after since).

    The Coordinator keeps one store for every run of a deployment, so that
    ownership epochs stay monotonic for the admission policy, and its tables
    hold earlier runs too. Incidents, their actions, transitions and outbox
    rows, and revocations of those actions, are kept for this run only;
    receipts, worker results and the access log are left whole, so I4, I5,
    X1 and X3 check the store's whole history, which is stricter.
    """
    start = _seconds(since)
    opened = {row["incident_id"] for row in records.get("transitions", [])
              if row.get("from_state") is None and _seconds(row["recorded_at"]) >= start}
    actions = {row["action_id"] for row in records.get("actions", []) if row["incident_id"] in opened}
    scoped = dict(records)
    scoped["incidents"] = [row for row in records.get("incidents", []) if row["incident_id"] in opened]
    scoped["actions"] = [row for row in records.get("actions", []) if row["incident_id"] in opened]
    scoped["transitions"] = [row for row in records.get("transitions", []) if row["incident_id"] in opened]
    scoped["outbox"] = [row for row in records.get("outbox", []) if row.get("action_id") in actions]
    scoped["revocations"] = [row for row in records.get("revocations", []) if row["action_id"] in actions]
    return scoped


# Running ---------------------------------------------------------------------------------------------------


def results_table(summaries):
    """The results table of docs/distributed-acceptance.md from run summaries; a scenario not run says so.

    A run passes when its invariants hold, its expectation (expectation()) is
    met, the audit extract was complete and the harness raised nothing; the
    invariant and expectation columns count each part on its own.
    """
    lines = ["| Scenario | Runs | Runs passed | Invariants passed | Expectation met | "
             "Max template writes per action (audit) | Takeover latency (s) | "
             "Coordinator RTO to Ready / first claim (s) |",
             "|---|---|---|---|---|---|---|---|"]
    by_name = {}
    for summary in summaries:
        by_name.setdefault(summary["scenario"], []).append(summary)
    for name in ORDER:
        runs = by_name.get(name, [])
        if not runs:
            lines.append(f"| {name} | not yet run | - | - | - | - | - | - |")
            continue
        passed = sum(1 for run in runs if run["passed"])
        held = sum(1 for run in runs if run["invariants_passed"])
        met = sum(1 for run in runs if run["expectation"]["met"])
        writes = max(run["audit"]["max_template_writes_per_action"] for run in runs)
        takeovers = [run["takeover_seconds"] for run in runs if run.get("takeover_seconds") is not None]
        rto = [run["coordinator_recovery"] for run in runs if run.get("coordinator_recovery")]

        def span(values):
            return f"{min(values):.1f}-{max(values):.1f}" if values else "-"
        ready = [item["to_ready"] for item in rto if item.get("to_ready") is not None]
        claim = [item["to_first_claim"] for item in rto if item.get("to_first_claim") is not None]
        lines.append(f"| {name} | {len(runs)} | {passed}/{len(runs)} | {held}/{len(runs)} | {met}/{len(runs)} | "
                     f"{writes} | {span(takeovers)} | "
                     f"{span(ready) if rto else '-'} / {span(claim) if rto else '-'} |")
    return "\n".join(lines) + "\n"


def load_summaries(run_dir):
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(Path(run_dir).glob("*/*/summary.json"))]


def plan():
    """The scenario matrix and a sample of each chaos object, for review before a live run (offline)."""
    samples = [pod_kill("s1a-planner", "sentinel-planner-1"),
               pod_failure("s1b-executor", "sentinel-executor-a", DEADLINE_SECONDS),
               partition("s3-partition", "sentinel-executor-a", ["sentinel-coordinator"], 120),
               partition("s6-partition", "sentinel-coordinator",
                         [IDENTITIES[name][0] for name in IDENTITIES], 3 * LEASE_SECONDS),
               delay_to_api_server("s2-api-delay", "sentinel-executor-a", "10.96.0.1", 120),
               time_skew("s7-skew", ["sentinel-executor-a", "sentinel-executor-b"], -300, DEADLINE_SECONDS)]
    return {"context": CONTEXT, "chaos_mesh": CHAOS_VERSION, "node_image": NODE_IMAGE, "pinned": PINNED,
            "scenarios": [{"name": item.name, "title": item.title, "fault": item.fault,
                           "file_mode_counterpart": item.catalogue, "expect": item.expect,
                           "checks": list(item.checks)} for item in SCENARIOS.values()],
            "chaos_samples": samples}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Print the scenario matrix and sample chaos objects (offline)")
    for name in ("preflight", "deploy", "run", "teardown"):
        sub = commands.add_parser(name)
        sub.add_argument("--context", required=True)
        if name in {"deploy", "run"}:
            sub.add_argument("--run-dir", required=True, type=Path)
            sub.add_argument(lab_lock.BREAK_OPTION, action="store_true",
                             help="Remove a heavy-lab lock whose process is no longer running, then take it")
        if name in {"preflight", "deploy", "run"}:
            sub.add_argument("--min-free-gib", type=float,
                             help=f"Free Docker memory needed (default {MIN_FREE_GIB}, {CREATE_FREE_GIB} before create)")
        if name == "preflight":
            sub.add_argument("--before-create", action="store_true",
                             help="Gate kind create cluster: sentinel-dist must not exist yet")
        if name == "run":
            sub.add_argument("--scenarios", default="all", help="'all' or a comma-separated list such as S0,S2")
            sub.add_argument("--repeat", type=int, default=1)
        if name == "teardown":
            sub.add_argument("--delete-cluster", action="store_true",
                             help="Also delete the kind cluster sentinel-dist (a separate, explicit choice)")
    table = commands.add_parser("table", help="Render the results table from a run directory")
    table.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            print(json.dumps(plan(), indent=2))
            return 0
        if args.command == "table":
            print(results_table(load_summaries(args.run_dir)), end="")
            return 0
        shell = Shell(args.context)
        if getattr(args, "min_free_gib", None) is None and args.command != "teardown":
            args.min_free_gib = CREATE_FREE_GIB if getattr(args, "before_create", False) else MIN_FREE_GIB
        if args.command == "preflight":
            print(json.dumps(preflight(shell, min_free_gib=args.min_free_gib, before_create=args.before_create),
                             indent=2))
            return 0
        if args.command == "run" and not 1 <= args.repeat <= 20:
            raise AcceptanceError("--repeat must be 1 to 20")
        from experiments.distributed import kind_acceptance_live as live
        if args.command == "deploy":
            with LabLock(break_stale=args.break_stale_lock) as lock:
                print(json.dumps(live.deploy(shell, args.run_dir, min_free_gib=args.min_free_gib, lock=lock),
                                 indent=2))
            return 0
        if args.command == "run":
            with LabLock(break_stale=args.break_stale_lock) as lock:
                summaries = live.run(shell, args.run_dir, select(args.scenarios.split(",")), repeat=args.repeat,
                                     min_free_gib=args.min_free_gib, lock=lock)
            print(results_table(summaries), end="")
            return 0 if all(item["passed"] for item in summaries) else 1
        print(json.dumps(live.teardown(shell, delete_cluster=args.delete_cluster), indent=2))
        return 0
    except AcceptanceError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
