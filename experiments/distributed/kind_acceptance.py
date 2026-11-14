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
point armed inside a worker Pod, or a Pod deletion. Each fault is confirmed
in effect from the cluster before the scenario goes on (chaos_effect: the
selected Pod's UID gone for a pod-kill, Injected records for every selected
Pod otherwise; State.Paused for docker pause), a fault that is not is a
harness error, and whatever is still in place when the scenario ends is
undone and recorded as operator actions (kind_acceptance_live.Run.cleanup),
with Lab.settle reporting anything left. It then copies the
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

The Coordinator, the Observer and the workers are long-lived Deployments
with PVCs, so their stores outlive each run. A run starts only on a settled
lab (Lab.begin_run, run_unsettled: no earlier incident still open or still
in the Observer's outbox, no seal required, the target healthy at its
baseline, the baseline young enough for the Guard) and remembers the incidents the store already holds. Waiting,
approving, terminal detection and the invariants then cover only the
incidents it opened (Lab.incidents, scope_records); after the restore it
waits, with the operator still approving, until the target has stayed
healthy and every incident it opened has ended (Lab.settle), so an incident
opened by the restore's own rollout ends in that run and not in the next.

Measured, not assumed: takeover latency (fault to the other Executor's first
claim) and Coordinator recovery (kill to Pod Ready, to its first answered
claim and to the next journal transition), with the fault time moved onto
the cluster's clock by the measured offset. These are four containers on one
Docker host: they share one kernel, one clock and one disk, and are not
separate machines.

Status: tested offline (tests/test_kind_acceptance.py,
tests/test_kind_acceptance_repeats.py and tests/test_kind_chaos_confirmation.py,
with a fake kubectl, docker and helm, a fake Chaos Mesh and local Coordinator
and Observer databases) and deployed live several times on 13 Nov 2026. The
first deployment's S0 runs repaired nothing (stale proposals, a failed
restore). In the second, one S0 run passed and the S0 runs after it in the
same deployment opened no incident, because the harness counted the first
run's RECOVERED incident as theirs. The first matrix, at 1adcfe3, passed 4
of 5 S0 runs and stopped in S1a r1, because the harness deleted its PodChaos
before Chaos Mesh acted on it. Both are fixed here and not yet re-run
(docs/distributed-acceptance.md#first-live-attempts). The S2-S9 matrix at
b37017e (14 Nov 2026) passed S2, S3 and S4 5/5 and stopped in S5 r1 when a
kubectl exec into the Coordinator failed while its Pod was replaced; reads
now ride out such an outage within their step's limit (Lab.wait, Lab.read,
transient_failure), also not yet re-run.
"""
import argparse
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
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


class TransientRead(AcceptanceError):
    """A kubectl call failed because its Pod is restarting or not ready, or the API server or kubelet did not answer.

    reason is the class transient_failure gave it (or "no running Pod"), never
    kubectl's own text. Lab.wait, Lab.read and the operator thread ask again
    with bounded back-off; every other failure stops at once.
    """

    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


class ReadUnavailable(AcceptanceError):
    """A read kept failing transiently until its bound: a harness error that fails the run, never a pass."""


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
#
# A read through kubectl can fail for a while without anything being wrong with what it reads: the Coordinator's
# Pod is replaced in S5 and its container restarts there first, and any Pod can be between containers or the API
# server slow to answer at other times. On 14 Nov 2026 (S5 r1 at b37017e) such a failure while the Coordinator Pod
# was replaced ended the run with "kubectl --context=kind-sentinel-dist failed (exit 1)" and no summary
# (docs/distributed-acceptance.md#s2-s9-matrix-b37017e-reads-while-the-coordinator-pod-was-replaced). kubectl's
# own failures exit 1 with its error on stderr. A command that ran in the container exits with the command's own
# status, and kubectl then adds "command terminated with exit code N": that is the command's answer (a query
# error, a probe's denial), never a transient one, unless a signal ended it because its container was stopping.

READ_RETRY_SECONDS = 180  # How long a read is asked again while it fails transiently: a replaced Pod's Ready time.
READ_BACKOFF_SECONDS = 8.0  # The longest pause between two attempts of a read that keeps failing transiently.
_RAN = re.compile(r"command terminated with exit code (\d+)")
_STOPPED = {137: "process killed with its container (SIGKILL)", 143: "process stopped with its container (SIGTERM)"}
TRANSIENT_CLASSES = (
    ("pod not found", re.compile(r'\(NotFound\)[^\n]*\bpods? "[^"\n]*" not found|pod does not exist', re.I)),
    ("container not running", re.compile(
        r"container not found|container (is )?not running|is waiting to start|does not have a host assigned|"
        r"cannot exec into a container in a completed pod|cannot exec in a stopped|in CONTAINER_EXITED state",
        re.I)),
    ("connection refused", re.compile(
        r"connection refused|connection reset by peer|connection lost|unexpected EOF|error dialing backend|"
        r"unable to connect to the server|"
        r"no route to host|i/o timeout|tls handshake timeout|context deadline exceeded|"
        r"the server is currently unable to handle the request|\((ServiceUnavailable|Timeout|TooManyRequests)\)",
        re.I)),
)


def transient_failure(result):
    """The transient class of a failed kubectl call (TRANSIENT_CLASSES, _STOPPED), or None for any other result.

    Decided by the exit status and the class of kubectl's stderr: exit 1 with
    no "command terminated" (kubectl's own failure) whose stderr is a Pod not
    found, a container not running or a connection refused or timed out; or a
    command in the container ended by SIGKILL or SIGTERM (137, 143). Every
    other failure, a query or probe that ran and failed included, is None.
    """
    if not result.returncode:
        return None
    text = result.stderr or ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    ran = _RAN.search(text)
    if ran:
        code = int(ran.group(1))
        return _STOPPED.get(code) if code == result.returncode else None
    if result.returncode != 1:
        return None
    return next((label for label, pattern in TRANSIENT_CLASSES if pattern.search(text)), None)


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
            # Named by the tool and its verb only: an argument can be a Secret's name, which no evidence may hold.
            tool = Path(command[0]).name
            verb = next((str(part) for part in command[1:]
                         if not str(part).startswith("-") and part != self.context), "")
            message = f"{tool} {verb} failed (exit {result.returncode})"
            reason = transient_failure(result) if tool == "kubectl" else None
            if reason:
                raise TransientRead(f"{message}: {reason}", reason)
            raise AcceptanceError(message)
        return result

    def kubectl(self, *args, namespace=None, stdin=None, timeout=60, check=True, binary=False):
        command = ["kubectl", "--context=" + self.context]
        # kubectl turns --request-timeout into a ?timeout= query on a --raw path, and a proxied service then
        # sees /readyz?timeout=30s (the demo API answers 404). A raw call is bounded by the process timeout.
        if args[:2] != ("get", "--raw"):
            command.append("--request-timeout=30s")
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


# Confirming a fault ----------------------------------------------------------------------------------------
#
# Creating a chaos object is a request, not a fault. On 13 Nov 2026 (S1a r1 at 1adcfe3) the harness created
# PodChaos s1a-r1-planner and deleted it about 0.6 s later in its cleanup, before Chaos Mesh's controller had
# acted on it, so the Planner was never killed and stayed blocked at its fault point
# (docs/distributed-acceptance.md#first-live-attempts). Every fault is therefore confirmed from the cluster
# before the run goes on (Lab.confirm_chaos, chaos_effect) and removed only after its window.

CHAOS_CONFIRM_SECONDS = 60  # How long Chaos Mesh may take to carry out a chaos object before the run stops.
REPLACEMENT_SECONDS = 180  # How long a killed Pod's replacement may take to be Ready.
PAUSE_CONFIRM_SECONDS = 10  # docker pause and unpause are synchronous; this only absorbs a slow inspect.
INJECT_ANNOTATION = "chaos-mesh.org/inject"  # With enableFilterNamespace, Chaos Mesh injects only where this is "enabled".
_POD_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?\Z")


def chaos_selectors(document):
    """{selector key: selector} as Chaos Mesh 2.8.2 keys its records: "." for spec.selector, ".Target" for a target."""
    spec = document["spec"]
    selectors = {".": spec["selector"]}
    if spec.get("target"):
        selectors[".Target"] = spec["target"]["selector"]
    return selectors


def selector_apps(selector):
    """The app label values a harness selector names: labelSelectors {app} or an In expression on app."""
    labels = selector.get("labelSelectors") or {}
    if set(labels) - {"app"}:
        raise AcceptanceError("Chaos selects Pods by their app label only")
    apps = list(labels.values())
    for expression in selector.get("expressionSelectors") or []:
        if expression.get("key") != "app" or expression.get("operator") != "In":
            raise AcceptanceError("Chaos selects Pods by their app label only")
        apps.extend(expression.get("values") or [])
    if not apps:
        raise AcceptanceError("A chaos selector must name at least one app")
    for app in apps:
        _selector(app)
    return sorted(set(apps))


def record_pod(record_id):
    """(Pod name, container or None) from a record id "<namespace>/<pod>" or "<namespace>/<pod>/<container>".

    Any other shape, or another namespace, raises: a record the harness cannot
    place must stop the run, never be ignored.
    """
    parts = str(record_id).split("/")
    if len(parts) not in (2, 3) or parts[0] != NAMESPACE or not _POD_NAME.fullmatch(parts[1]):
        raise AcceptanceError("Unexpected Chaos Mesh record id: " + str(record_id)[:120])
    return parts[1], (parts[2] if len(parts) == 3 else None)


def pod_facts(item):
    """What confirmation needs about one Pod document: name, UID, node, phase, readiness and containers."""
    metadata, status = item.get("metadata") or {}, item.get("status") or {}
    ready = any(condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in status.get("conditions") or [])
    containers = {entry.get("name"): {"id": entry.get("containerID"), "running": "running" in (entry.get("state") or {}),
                                      "restarts": entry.get("restartCount")}
                  for entry in status.get("containerStatuses") or []}
    return {"name": metadata.get("name"), "uid": metadata.get("uid"), "node": (item.get("spec") or {}).get("nodeName"),
            "phase": status.get("phase"), "deleting": bool(metadata.get("deletionTimestamp")), "ready": ready,
            "ready_since": ready_since(item), "containers": containers}


def ready_since(item):
    """When a Pod document's Ready condition last became True (the cluster's clock), or None while it is not Ready."""
    return next((condition.get("lastTransitionTime") for condition in (item.get("status") or {}).get("conditions") or []
                 if condition.get("type") == "Ready" and condition.get("status") == "True"), None)


def chaos_effect(document, current, targets, pods_now):
    """Whether document is in effect: {"in_effect", "waiting": [reasons], "evidence"}; AcceptanceError on a wrong target.

    current is the chaos object read from the cluster (None when absent),
    targets {selector key: [pod_facts]} the Pods its selectors matched just
    before it was created, and pods_now {app: [pod_facts]} the Pods of its
    apps now (read for PodChaos only). In effect means, for pod-kill, that no
    Pod it selected is still running (its UID is gone or being deleted); for
    every other action that desiredPhase is Run and every selected Pod has a
    record Injected (both ends of a partition), and for pod-failure also that
    the container that ran in the selected Pod is no longer running. A record
    for a Pod the harness did not select, in another namespace or for another
    container is an error, not a reason to wait.
    """
    kind, spec, name = document["kind"], document["spec"], document["metadata"]["name"]
    if current is None:
        return {"in_effect": False, "waiting": [f"{kind} {name} is not in the cluster"], "evidence": {}}
    experiment = (current.get("status") or {}).get("experiment") or {}
    desired = experiment.get("desiredPhase")
    expected = {pod["name"] for pods in targets.values() for pod in pods}
    containers = spec.get("containerNames")
    records = []
    for record in experiment.get("containerRecords") or []:
        pod, container = record_pod((record or {}).get("id"))
        if pod not in expected:
            raise AcceptanceError(f"{kind} {name} has a record for Pod {pod}, which is not one of the Pods its "
                                  "selector matched before it was created")
        if container is not None and containers and container not in containers:
            raise AcceptanceError(f"{kind} {name} has a record for container {container}, not one it names")
        records.append({"id": record.get("id"), "selector_key": record.get("selectorKey"), "phase": record.get("phase")})
    evidence = {"desired_phase": desired, "records": records}
    waiting = []
    if kind == "PodChaos" and spec.get("action") == "pod-kill":
        victims = {pod["uid"]: pod["name"] for pod in targets.get(".", [])}
        alive = sorted(pod["name"] for pods in pods_now.values() for pod in pods
                       if pod["uid"] in victims and not pod["deleting"])
        waiting += [f"Pod {pod} (selected) is still running" for pod in alive]
        evidence["killed"] = [{"pod": victims[uid], "uid": uid} for uid in sorted(victims)]
        return {"in_effect": not waiting, "waiting": waiting, "evidence": evidence}
    if desired != "Run":
        waiting.append(f"desiredPhase is {desired or 'not set yet'}")
    injected = {record_pod(record["id"])[0] for record in records if record["phase"] == "Injected"}
    waiting += [f"no Injected record for Pod {pod}" for pod in sorted(expected - injected)]
    if kind == "PodChaos" and spec.get("action") == "pod-failure":
        now = {pod["uid"]: pod for pods in pods_now.values() for pod in pods}
        replaced = []
        for pod in targets.get(".", []):
            later = now.get(pod["uid"])
            for container, before in sorted(pod["containers"].items()):
                after = (later or {}).get("containers", {}).get(container) or {}
                if after.get("running") and after.get("id") == before.get("id"):
                    waiting.append(f"container {container} of Pod {pod['name']} is still the one that was running")
                replaced.append({"pod": pod["name"], "uid": pod["uid"], "container": container,
                                 "container_before": before.get("id"), "container_after": after.get("id")})
        evidence["replaced"] = replaced
    return {"in_effect": not waiting, "waiting": waiting, "evidence": evidence}


# Scenarios -----------------------------------------------------------------------------------------------

ALL_CHECKS = ("I1", "I2", "I3", "I4", "I5", "I6", "X1", "X2", "X3", "X4", "I7")


# Scenario.options["auto_approve"]: True (the default) approves each action waiting for approval from the
# injection on, False never, and AFTER_FAULT only once the scenario's runner has returned (its fault
# removed). S6 approves its first action by hand during the partition; a re-plan after a stale refusal
# (the sentinel-dist Coordinator runs with --stale-replans) then still gets its operator.
AFTER_FAULT = "after_fault"


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
             options={"auto_approve": AFTER_FAULT}),
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


# Between runs --------------------------------------------------------------------------------------------

# After its restore a run waits up to SETTLE_SECONDS for the lab to settle (Lab.settle): the target healthy at
# its baseline for SETTLE_QUIET_SECONDS, longer than an Observer poll (2 s) plus its delivery, so an incident the
# restore's own Recreate rollout opens is already recorded, and every incident the run opened terminal.
SETTLE_SECONDS = 240
SETTLE_QUIET_SECONDS = 20


def target_unsettled(deployment, baseline):
    """Why the target is not healthy at its baseline template; [] when it is.

    deployment is the live Deployment, baseline the recorded baseline.json
    (its template_sha256). Healthy means the rollout is observed and every
    desired replica is updated, ready and available, as the Observer reads
    the Deployment's status.
    """
    from services.executor.store import fingerprint
    metadata, spec = deployment.get("metadata") or {}, deployment.get("spec") or {}
    status = deployment.get("status") or {}
    reasons = []
    expected = (baseline or {}).get("template_sha256")
    if not expected or spec.get("template") is None or fingerprint(spec["template"]) != expected:
        reasons.append("its template is not the baseline's")
    if status.get("observedGeneration") != metadata.get("generation"):
        reasons.append("its latest generation is not observed yet")
    desired = spec.get("replicas", 1)
    counts = [status.get(key, 0) for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")]
    if any(count != desired for count in counts) or status.get("unavailableReplicas"):
        reasons.append(f"not every one of its {desired} replica(s) is updated, ready and available")
    return reasons


def run_unsettled(incidents, ownership, deployment, baseline, *, policy=None, now=None, undelivered=()):
    """Why a new run may not start on this lab; [] when it may (Lab.begin_run).

    incidents and ownership are every row of the Coordinator's incidents and
    target_ownership tables, deployment the live target, baseline its
    recorded baseline, policy the deployed policy.json, now the harness's
    clock and undelivered the incident IDs still waiting in the Observer's
    outbox. The stores outlive each run (one PVC per role), so what an
    earlier run left behind is checked, never reset by hand: an incident
    still open holds this run's incident on the same workload (the
    Coordinator's planning_blocker) and its pending action could be approved
    by this run's operator; an incident the Observer has not delivered yet
    would reach the Coordinator during this run and count as its own; a seal
    still required blocks every incident on the target; and a target off its
    baseline is not the fault this run injects. The baseline's health
    evidence ages from deploy on, and the Guard
    refuses a baseline older than the policy's max_age_seconds, so a run that
    could cross that age part way through is refused before it starts.
    """
    problems = [f"earlier incident {row['incident_id']} is {row['state']}, not terminal: it holds this run's "
                "incident (planning_blocker); let it end, or deploy again into a new run directory"
                for row in incidents if row["state"] not in invariants.TERMINAL]
    problems += [f"the Observer has not yet delivered incident {incident_id} to the Coordinator: it would be "
                 "counted as this run's" for incident_id in undelivered]
    problems += [f"a seal is still required on the target (epoch {row['epoch']})"
                 for row in ownership if row.get("seal_required")]
    problems += ["the target: " + reason for reason in target_unsettled(deployment, baseline)]
    max_age = ((policy or {}).get("baseline_constraints") or {}).get("max_age_seconds")
    healthy = (baseline or {}).get("healthy_observed_at")
    if max_age is not None and healthy and now is not None:
        ends = now + timedelta(seconds=DEADLINE_SECONDS + SETTLE_SECONDS)
        if (ends - datetime.fromisoformat(healthy.replace("Z", "+00:00"))).total_seconds() > max_age:
            problems.append(f"the baseline's health evidence ({healthy}) would be older than the policy's "
                            f"max_age_seconds ({max_age}) before this run ends, and the Guard would refuse it; "
                            "deploy again into a new run directory")
    return problems


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


def private_file(directory, prefix, text):
    """A new file in directory holding text; the caller deletes it.

    mkstemp creates it with mode 0600 on POSIX; on Windows it takes the
    directory's ACL, so the run directory is what keeps it private there.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".json", dir=directory)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)
    return Path(name)


class Lab:
    """The deployed sentinel-dist lab, driven through one Shell."""

    def __init__(self, shell, run_dir, *, clock=utc_now, sleep=time.sleep, monotonic=time.monotonic):
        self.shell, self.run_dir = shell, Path(run_dir)
        self.clock, self.sleep, self.monotonic = clock, sleep, monotonic
        self.tokens = {}
        self._forward = None
        self._forward_lock = threading.Lock()
        self.approved = set()
        self.approvals = []  # {"action_id", "at"} of each approval the operator gave, in order.
        self.prior = frozenset()  # Incidents the store held before this run (begin_run).
        self.read_outages = []  # Each run of transiently failed reads in a wait or read (Lab.wait), in order.

    # Reading the cluster --------------------------------------------------------------------------------

    def nodes(self):
        """{host label: node name} for the three workers."""
        items = self.shell.json("get", "nodes")["items"]
        return {item["metadata"]["labels"]["sentinel.io/host"]: item["metadata"]["name"]
                for item in items if "sentinel.io/host" in item["metadata"].get("labels", {})}

    def pod(self, deployment):
        """The one running Pod of a sentinel-dist Deployment: (name, node).

        TransientRead while it has none (being replaced: every Deployment here
        is Recreate); AcceptanceError when it has more than one.
        """
        items = self.shell.json("get", "pods", "--selector=app=" + deployment, namespace=NAMESPACE)["items"]
        running = [item for item in items if item.get("status", {}).get("phase") == "Running"
                   and not item["metadata"].get("deletionTimestamp")]
        if not running:
            raise TransientRead(f"{deployment} has no running Pod", "no running Pod")
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

    def query(self, sql, parameters=(), *, strict=False, identity="coordinator"):
        """Read-only rows from a database in a Pod. A failed read never reads as no rows.

        identity is "coordinator" (the Coordinator's database, the default) or
        "observer-1" (the Observer's episode store and outbox). While its Pod
        is restarting, not ready or unreachable the read raises TransientRead,
        which a caller polling in Lab.wait or the operator thread asks again;
        with strict it is asked again here (Lab.read) and ends in
        ReadUnavailable. Any other failure, a query error included, raises
        AcceptanceError at once. Until 14 Nov 2026 a failed read here was []
        unless strict, whatever the failure, so a Coordinator being replaced
        and a broken query both read as "no incident yet".
        """
        path = "/data/coordinator.sqlite3" if identity == "coordinator" else "/data/observer.sqlite3"
        owner = "Coordinator" if identity == "coordinator" else "Observer"

        def attempt():
            try:
                result = self.exec_python(self._deployment(identity), self._container(identity), QUERY_HELPER,
                                          path, sql, json.dumps(list(parameters)))
            except (TransientRead, subprocess.TimeoutExpired):
                raise
            except (AcceptanceError, subprocess.SubprocessError) as error:
                raise AcceptanceError(f"The {owner}'s database could not be read: {error}") from None
            try:
                return json.loads(result.stdout or "[]")
            except ValueError:
                raise AcceptanceError(f"The {owner}'s database could not be read: its answer is not JSON rows") \
                    from None
        if not strict:
            return attempt()
        try:
            return self.read(f"the {owner}'s database", attempt)
        except ReadUnavailable as error:
            raise ReadUnavailable(f"The {owner}'s database could not be read: {error}") from None

    def undelivered(self, *, strict=False):
        """IDs of incidents the Observer has opened and not yet delivered to the Coordinator (its outbox).

        The Observer's outbox outlives each run too: an incident still in it
        when a run begins would reach the Coordinator during that run and be
        counted as the run's own.
        """
        rows = self.query("SELECT incident_id FROM observer_outbox WHERE delivered = 0", strict=strict,
                          identity="observer-1")
        return [row["incident_id"] for row in rows]

    def all_incidents(self, *, strict=False):
        """Every incident in the Coordinator's store, earlier runs' included."""
        return self.query("SELECT incident_id, state, active_action_id FROM incidents", strict=strict)

    def incidents(self, *, strict=False):
        """This run's incidents: those the store did not hold when begin_run was called.

        The Coordinator, the Observer and the workers keep their stores across
        runs (their PVCs), so the store still holds every earlier run's
        incidents. Waiting, approving and terminal detection look only at the
        ones this run opened; otherwise an earlier RECOVERED incident makes a
        new run terminal at once (13 Nov 2026,
        docs/distributed-acceptance.md#first-live-attempts).
        """
        return [row for row in self.all_incidents(strict=strict) if row["incident_id"] not in self.prior]

    def ownership(self, *, strict=False):
        """The Coordinator's target_ownership rows (fence epochs and seals), kept across runs."""
        return self.query("SELECT uid, epoch, holder, seal_required, sealed_epoch FROM target_ownership",
                          strict=strict)

    def begin_run(self, baseline, policy=None):
        """Check that the lab is settled for a new run, then remember the incidents already in the store.

        AcceptanceError when it is not (run_unsettled): an earlier incident
        still open holds this run's incident (the Coordinator's
        planning_blocker), and this run's operator must not approve it; an
        incident the Observer has not delivered yet would count as this run's;
        a seal still required blocks the target; a target off its baseline is not the
        fault this run injects; and a baseline about to age past the policy's
        max_age_seconds would be refused by the Guard part way through. Returns
        the earlier incidents' IDs, which incidents() and scope_records leave out.
        """
        rows = self.all_incidents(strict=True)
        problems = run_unsettled(rows, self.ownership(strict=True), self.target(), baseline, policy=policy,
                                 now=self.clock(), undelivered=self.undelivered(strict=True))
        if problems:
            raise AcceptanceError("The lab is not settled for a new run: " + "; ".join(problems))
        self.prior = frozenset(row["incident_id"] for row in rows)
        return self.prior

    def settle(self, baseline, *, timeout=None, quiet=None, interval=2.0, leftovers=None):
        """After the restore: wait until the target has stayed healthy at its baseline and this run's incidents ended.

        The operator's restore (and Sentinel's own rollback) rolls the target
        out with Recreate, and when its Pod-less window outlasts the
        Observer's failure window that rollout opens an incident of its own,
        a little after the rollout. So the target must stay healthy at its
        baseline for quiet seconds (longer than an Observer poll and its
        delivery), every incident this run opened must be terminal, no seal
        may still be required and the Observer's outbox must hold no
        undelivered incident. leftovers, when given, returns what the run's
        faults left behind (kind_acceptance_live.Run.leftovers: a chaos
        object still in the cluster, a node still paused, a fault point still
        held or armed); the lab is not settled while it returns anything, and
        the last answer is reported. Returns what was seen; settled is False
        at the deadline. Run with the operator still approving, so an incident
        the Guard escalated before the restore is decided on live state.
        """
        timeout = SETTLE_SECONDS if timeout is None else timeout
        quiet = SETTLE_QUIET_SECONDS if quiet is None else quiet
        start = self.monotonic()
        healthy_since, reasons, open_incidents, left = None, [], [], []
        while True:
            now = self.monotonic()
            try:
                reasons = target_unsettled(self.target(), baseline)
            except (AcceptanceError, subprocess.SubprocessError, ValueError, KeyError) as error:
                reasons = [f"the target could not be read ({type(error).__name__})"]
            healthy_since = None if reasons else (now if healthy_since is None else healthy_since)
            try:  # One attempt each: this loop is the retry, bounded by timeout; a failed read is never settled.
                open_incidents = [f"{row['incident_id']}: {row['state']}" for row in self.incidents()
                                  if row["state"] not in invariants.TERMINAL]
                open_incidents += [f"a seal is still required (epoch {row['epoch']})"
                                   for row in self.ownership() if row["seal_required"]]
                open_incidents += [f"{incident_id}: not yet delivered by the Observer"
                                   for incident_id in self.undelivered()]
            except (AcceptanceError, subprocess.TimeoutExpired) as error:
                open_incidents = [str(error) if isinstance(error, AcceptanceError)
                                  else "a store could not be read: kubectl did not answer in time"]
            left = list(leftovers()) if leftovers is not None else []
            if healthy_since is not None and now - healthy_since >= quiet and not open_incidents and not left:
                return {"settled": True, "seconds": round(now - start, 1), "target": [], "open": [], "leftovers": []}
            if now - start >= timeout:
                return {"settled": False, "seconds": round(now - start, 1), "target": reasons,
                        "open": open_incidents, "leftovers": left}
            self.sleep(interval)

    def wait(self, check, timeout, what, interval=1.0):
        """check()'s first truthy value, asked every interval seconds; TimeoutError when timeout passes without one.

        A check that fails transiently (TransientRead: its Pod restarting, not
        ready or unreachable; or a kubectl that did not answer within its own
        timeout) is asked again, the pause doubling from interval up to
        READ_BACKOFF_SECONDS while it keeps failing and never past the
        deadline, which is the scenario's own for that step. When the deadline
        comes while the reads are still failing, the wait ends in
        ReadUnavailable, not TimeoutError: a read that never came back is a
        harness error, never the system's answer. Any other failure is raised
        at once. Each run of failed reads is kept in read_outages.
        """
        start = self.monotonic()
        deadline, pause, outage = start + timeout, interval, None
        while self.monotonic() < deadline:
            try:
                value = check()
            except (TransientRead, subprocess.TimeoutExpired) as error:
                outage = self._failed_read(outage, what, error)
                self.sleep(max(0.0, min(pause, deadline - self.monotonic())))
                pause = min(pause * 2, max(interval, READ_BACKOFF_SECONDS))
                continue
            if outage is not None:
                self._end_outage(outage, recovered=True)
                outage, pause = None, interval
            if value:
                return value
            self.sleep(interval)
        if outage is not None:
            entry = self._end_outage(outage, recovered=False)
            raise ReadUnavailable(f"{what}: the reads kept failing for {entry['seconds']:.0f} s, until the "
                                  f"{timeout:.0f} s limit ({entry['attempts']} attempts, last: {entry['reason']})")
        raise TimeoutError(what)

    def read(self, what, attempt, *, within=None):
        """attempt()'s value, asked again with bounded back-off while it fails transiently (Lab.wait).

        within is the bound in seconds (READ_RETRY_SECONDS when None): once it
        passes with the reads still failing, ReadUnavailable names what could
        not be read. A falsy value is returned, not waited past.
        """
        within = READ_RETRY_SECONDS if within is None else within
        try:
            (value,) = self.wait(lambda: (attempt(),), within, what, interval=1.0)
        except TimeoutError:  # Only when within left no time for one attempt.
            raise ReadUnavailable(f"{what}: no time was left to read it") from None
        return value

    def _failed_read(self, outage, what, error):
        now = self.monotonic()
        if outage is None:
            outage = {"what": what, "since": stamp(self.clock()), "started": now, "attempts": 0}
        reason = error.reason if isinstance(error, TransientRead) else "kubectl did not answer in time"
        outage.update(attempts=outage["attempts"] + 1, reason=reason)
        return outage

    def _end_outage(self, outage, *, recovered):
        entry = {key: value for key, value in outage.items() if key != "started"}
        entry.update(seconds=round(self.monotonic() - outage["started"], 1), recovered=recovered)
        self.read_outages.append(entry)
        return entry

    def terminal(self):
        """Whether this run has an incident and every one of them is terminal."""
        states = [row["state"] for row in self.incidents()]
        return bool(states) and all(state in invariants.TERMINAL for state in states)

    def wait_terminal(self, timeout):
        """True once this run has an incident and every one it opened is terminal; False at the deadline."""
        try:
            return bool(self.wait(self.terminal, timeout, "every incident terminal"))
        except TimeoutError:
            return False

    def wait_state(self, states, timeout):
        """The first of this run's incident rows in one of states."""
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
        """The identity whose process reached stem first; the others are disarmed at once.

        A Pod that cannot be read for the moment (TransientRead: S5's
        Coordinator restarts on reaching its point) is asked again on the next
        poll; when none of them could be read, the poll is a failed read that
        Lab.wait asks again with back-off and ends in ReadUnavailable at the
        deadline. Any other failure is raised at once. Until 14 Nov 2026 every
        failure here read as "not reached yet".
        """
        def reached():
            unreadable = []
            for identity in identities:
                try:
                    if self.fault(identity, "reached", stem).stdout.strip():
                        return identity
                except TransientRead as error:
                    unreadable.append(error)
            if unreadable and len(unreadable) == len(identities):
                raise unreadable[-1]
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

    def app_pods(self, app):
        """pod_facts of every Pod labelled app=<app> in sentinel-dist, terminating ones included."""
        items = self.shell.json("get", "pods", "--selector=app=" + app, namespace=NAMESPACE)["items"]
        return [pod_facts(item) for item in items]

    def holder_facts(self, identity):
        """The Pod, UID and container ID of the process of identity that holds a fault point now."""
        running = [pod for pod in self.app_pods(self._deployment(identity))
                   if pod["phase"] == "Running" and not pod["deleting"]]
        if len(running) != 1:
            raise AcceptanceError(f"{self._deployment(identity)} has {len(running)} running Pods")
        container = running[0]["containers"].get(self._container(identity)) or {}
        return {"pod": running[0]["name"], "uid": running[0]["uid"], "container_id": container.get("id")}

    def injectable(self, namespace):
        """Whether namespace carries chaos-mesh.org/inject=enabled (Chaos Mesh runs with enableFilterNamespace)."""
        document = self.shell.json("get", "namespace", namespace)
        return ((document.get("metadata") or {}).get("annotations") or {}).get(INJECT_ANNOTATION) == "enabled"

    def chaos_targets(self, document):
        """{selector key: [pod_facts]}: the running Pods each selector of document matches, read before it is created.

        AcceptanceError when a selector names a namespace other than
        sentinel-dist, the namespace is not annotated for injection (Chaos Mesh
        would select nothing), an app it names has no running Pod, or a
        mode-one fault has more than one candidate (the harness must know
        which Pod it faults).
        """
        targets = {}
        for key, selector in chaos_selectors(document).items():
            if selector.get("namespaces") != [NAMESPACE]:
                raise AcceptanceError("Chaos selects Pods in " + NAMESPACE + " only")
            if not self.injectable(NAMESPACE):
                raise AcceptanceError(f"{NAMESPACE} is not annotated {INJECT_ANNOTATION}=enabled, so Chaos Mesh "
                                      "(enableFilterNamespace) would inject nothing")
            pods = []
            for app in selector_apps(selector):
                running = [pod for pod in self.app_pods(app) if pod["phase"] == "Running" and not pod["deleting"]]
                if not running:
                    raise AcceptanceError(f"{document['kind']} {document['metadata']['name']} selects no running "
                                          f"Pod of {app}")
                pods += running
            targets[key] = pods
        if document["spec"].get("mode") == "one" and len(targets["."]) != 1:
            raise AcceptanceError("A mode-one fault needs exactly one candidate Pod, so the harness knows which it faults")
        return targets

    def chaos_object(self, document):
        """The chaos object as the cluster holds it now, or None when it is gone."""
        text = self.shell.kubectl("get", document["kind"].lower(), document["metadata"]["name"],
                                  "--ignore-not-found=true", "--output=json", namespace=NAMESPACE).stdout.strip()
        return json.loads(text) if text else None

    def chaos_state(self, document, targets):
        """chaos_effect of document now, read from the cluster."""
        pods_now = {}
        if document["kind"] == "PodChaos":
            apps = sorted({app for selector in chaos_selectors(document).values() for app in selector_apps(selector)})
            pods_now = {app: self.app_pods(app) for app in apps}
        return chaos_effect(document, self.chaos_object(document), targets, pods_now)

    def confirm_chaos(self, document, targets, timeout=CHAOS_CONFIRM_SECONDS, interval=1.0):
        """Wait until document is in effect (chaos_effect); its evidence, or AcceptanceError at the deadline."""
        start = self.monotonic()
        while True:
            state = self.chaos_state(document, targets)
            if state["in_effect"]:
                return {**state["evidence"], "confirmed_at": stamp(self.clock()),
                        "waited_seconds": round(self.monotonic() - start, 1)}
            if self.monotonic() - start >= timeout:
                raise AcceptanceError(f"{document['kind']} {document['metadata']['name']} was not confirmed in effect "
                                      f"within {timeout} s: " + "; ".join(state["waiting"]))
            self.sleep(interval)

    def wait_replacement(self, app, gone, timeout=REPLACEMENT_SECONDS):
        """The Ready Pod of app that replaced the UIDs in gone; TimeoutError at the deadline."""
        def ready():
            pods = [pod for pod in self.app_pods(app) if pod["uid"] not in gone and pod["phase"] == "Running"
                    and pod["ready"] and not pod["deleting"]]
            return pods[0] if len(pods) == 1 else None
        pod = self.wait(ready, timeout, f"no Ready replacement Pod of {app} within {timeout} s", interval=2)
        return {"pod": pod["name"], "uid": pod["uid"], "ready_at": stamp(self.clock()), "ready_since": pod["ready_since"]}

    def labelled_chaos(self):
        """Names of chaos objects of any kind this harness labels that are in sentinel-dist now."""
        selector = ",".join(f"{key}={value}" for key, value in CHAOS_LABEL.items())
        result = self.shell.kubectl("get", ",".join(CHAOS_KINDS), "--selector=" + selector, "--ignore-not-found=true",
                                    "--output=name", namespace=NAMESPACE)
        return [line for line in result.stdout.split() if line]

    def node_paused(self, node):
        """Whether docker reports the node container paused."""
        text = self.shell.docker("inspect", "--format", "{{.State.Paused}}", node).stdout.strip()
        if text not in {"true", "false"}:
            raise AcceptanceError("Unreadable paused state of " + node)
        return text == "true"

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
        self.approvals.append({"action_id": action_id, "at": stamp(self.clock())})

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
        # kubectl patch reads --patch-file as a path and has no "-" for standard input (on Windows it
        # looked for a file named "-"), so the patch, which holds no credential, goes to a private file.
        path = private_file(self.run_dir, "restore-patch-", json.dumps(patch))
        try:
            self.shell.kubectl("patch", "deployment", TARGET, "--type=json", "--patch-file=" + str(path),
                               namespace=WORKLOAD)
        finally:
            path.unlink(missing_ok=True)
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
        """A consistent copy of one SQLite file in a Pod, written to destination; False when it is absent.

        Asked again while the Pod is restarting or not ready (Lab.read); a copy
        that still fails at READ_RETRY_SECONDS is ReadUnavailable.
        """
        result = self.read(f"a copy of {path} from the {identity} Pod", lambda: self.exec_python(
            self._deployment(identity), self._container(identity), BACKUP_HELPER, path, binary=True, timeout=120))
        if not result.stdout:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(result.stdout)
        return True

    def log_text(self, identity, since):
        """The current Pod's logs since a UTC stamp: a restarted container's last run first, then the current one."""
        pod, _ = self.read(f"the {identity} Pod", lambda: self.pod(self._deployment(identity)))
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


def scope_records(records, since, prior=()):
    """records narrowed to the incidents this run opened: journal opened at or after since, and not in prior.

    The Coordinator keeps one store for every run of a deployment, so that
    ownership epochs stay monotonic for the admission policy, and its tables
    hold earlier runs too. prior holds the incidents the store already had
    when the run began (Lab.begin_run), so an incident of an earlier run is
    never counted however close the clocks put it to since. Incidents, their
    actions, transitions and outbox rows, and revocations of those actions,
    are kept for this run only. Receipts, worker results, message digests,
    Executor ledgers and the Observer's outbox are left whole, so I4, I5, I6
    and X3 check the store's whole history, which is stricter; X3 therefore
    compares the Observer's acknowledged incidents with every incident the
    Coordinator recorded (recorded_incident_ids), not only this run's.
    """
    start, prior = _seconds(since), set(prior)
    opened = {row["incident_id"] for row in records.get("transitions", [])
              if row.get("from_state") is None and _seconds(row["recorded_at"]) >= start
              and row["incident_id"] not in prior}
    actions = {row["action_id"] for row in records.get("actions", []) if row["incident_id"] in opened}
    scoped = dict(records)
    scoped["recorded_incident_ids"] = sorted({row["incident_id"] for row in records.get("incidents", [])})
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
