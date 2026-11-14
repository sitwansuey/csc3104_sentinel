"""Named fault points for lab fault-injection runs. Inert in every other run.

A checkpoint does nothing unless both environment variables are set:

- SENTINEL_FAULT_POINTS names an existing run directory, and
- SENTINEL_FAULT_CLUSTER names a cluster on the lab allowlist (LAB_CLUSTERS).

A worker also binds the gate to the cluster its configuration targets
(bind()). With any lab variable set, it starts only when its configured
cluster_id is the gated lab cluster and any configured context is that
cluster's lab context; otherwise it refuses to start. So a worker configured
for any other cluster, the original lab cluster included, never runs a hook,
a proxy or the clock offset. The Coordinator has no cluster in its
configuration, so its one point (coordinator.after_commit_before_reply) is
gated by the two variables alone; it can only stop or exit the Coordinator.

The multi-process harness (experiments/distributed/topology.py) sets both for
the processes it starts. A harness arms a point by writing "<name>.action"
(or "<name>@<qualifier>.action") into the run directory with the word
"exit" or "block". The first process that reaches the point takes the file
(an atomic rename, so an armed point fires once), records "<name>.reached"
with the process name the harness gave it,
and then either exits at once with EXIT_CODE, like a crash, or waits until the
harness creates "<name>.release". Nothing here reads a token, a payload or a
cluster object, and nothing is logged beyond the point name.

The same gate allows a test clock offset (SENTINEL_TEST_CLOCK_OFFSET, in
seconds) so a lab run can give one process a skewed clock. A bound worker
reads its Guard, Executor and Verifier clocks through it, so the offset can
change freshness and expiry decisions; that is what it is for, and why it
applies only to a worker bound to a lab cluster. The same offset shifts the
wall clock its HTTP client uses to measure its offset from the Coordinator.
"""
from datetime import datetime, timedelta
import math
import os
from pathlib import Path
import time

from shared import log

LOG = log.get_logger("fault_points")
ENV_DIRECTORY = "SENTINEL_FAULT_POINTS"
ENV_CLUSTER = "SENTINEL_FAULT_CLUSTER"
ENV_CLOCK_OFFSET = "SENTINEL_TEST_CLOCK_OFFSET"
ENV_PROCESS = "SENTINEL_FAULT_PROCESS"  # The harness's name for this process, recorded when a point fires.
# Clusters a fault run may target: the file-backed fake, the connected kind
# lab and the four-node sentinel-dist lab (P4.01, whose workers run in Pods and
# name its context in their in-Pod kubeconfig), each with the only Kubernetes
# contexts a bound worker may name for it.
FILE_CLUSTER = "file-sentinel-distributed"
LAB_CONTEXTS = {FILE_CLUSTER: frozenset({"file-cluster"}),
                "sentinel-boutique-connected": frozenset({"kind-sentinel-boutique-connected"}),
                "sentinel-dist": frozenset({"kind-sentinel-dist"})}
LAB_CLUSTERS = frozenset(LAB_CONTEXTS)
# Never a fault target, as a cluster_id, a context or the gated cluster, whatever the environment says.
REFUSED = frozenset({"sentinel-lab", "kind-sentinel-lab"})
NAMES = frozenset({
    "planner.mid_inference",
    "executor.after_start_write",
    "executor.before_patch",
    "executor.before_submit",
    "executor.after_patch_before_save",
    "verifier.mid_collection",
    "coordinator.after_commit_before_reply",
})
EXIT_CODE = 73
MAX_BLOCK_SECONDS = 600
MAX_CLOCK_OFFSET_SECONDS = 86400


def directory():
    """The armed run directory, or None when fault points are inert."""
    path, cluster = os.environ.get(ENV_DIRECTORY, ""), os.environ.get(ENV_CLUSTER, "")
    if not path or cluster not in LAB_CLUSTERS:
        return None
    run = Path(path)
    return run if run.is_dir() else None


def enabled():
    return directory() is not None


def requested():
    """True when any lab variable is set, valid or not."""
    return any(os.environ.get(name) for name in (ENV_DIRECTORY, ENV_CLUSTER, ENV_CLOCK_OFFSET))


def bind(config):
    """Bind the lab gate to the cluster a worker configuration targets.

    False when no lab variable is set: an ordinary run, with no hook, proxy,
    file cluster or clock offset. True only when the gate is on, the configured
    cluster_id is the gated lab cluster and any configured context is that
    cluster's lab context. Every other case raises ValueError, so a worker
    configured for another cluster refuses to start rather than run a hook.
    """
    if not requested():
        return False
    cluster_id, context = config.get("cluster_id"), config.get("context")
    gated = os.environ.get(ENV_CLUSTER, "")
    if {value for value in (cluster_id, context, gated) if isinstance(value, str)} & REFUSED:
        raise ValueError("This cluster is never a fault-injection target")
    if not enabled():
        raise ValueError("Lab variables are set but the fault gate is incomplete")
    if cluster_id != gated:
        raise ValueError("The worker's configured cluster is not the gated lab cluster")
    if context is not None and context not in LAB_CONTEXTS[cluster_id]:
        raise ValueError("The worker's configured context is not the lab cluster's context")
    return True


def _take(run, name, qualifier):
    """Atomically claim an armed action file; return its action or None."""
    candidates = ([f"{name}@{qualifier}"] if qualifier else []) + [name]
    for stem in candidates:
        armed = run / (stem + ".action")
        taken = run / f"{stem}.taken-{os.getpid()}-{time.monotonic_ns()}"
        try:
            os.replace(armed, taken)
        except FileNotFoundError:
            continue
        action = taken.read_text(encoding="utf-8").strip()
        if action not in {"exit", "block"}:
            raise ValueError("A fault point action must be exit or block")
        return stem, action
    return None


def checkpoint(name, *, qualifier=None, sleep=time.sleep):
    """Run the armed action for this point, if any. Unknown names always fail."""
    if name not in NAMES:
        raise ValueError("Unknown fault point")
    run = directory()
    if run is None:
        return None
    taken = _take(run, name, qualifier)
    if taken is None:
        return None
    stem, action = taken
    (run / (stem + ".reached")).write_text(os.environ.get(ENV_PROCESS) or str(os.getpid()), encoding="utf-8")
    log.emit(LOG, "fault_point.reached", level=log.WARNING, outcome=action, operation=name)
    if action == "exit":
        os._exit(EXIT_CODE)  # A crash: no cleanup, no flushed buffers, no reply.
    release = run / (stem + ".release")
    deadline = time.monotonic() + MAX_BLOCK_SECONDS
    while not release.exists() and time.monotonic() < deadline:
        sleep(0.05)
    log.emit(LOG, "fault_point.released", level=log.WARNING, operation=name)
    return action


def clock_offset():
    """Seconds to add to this process's wall clock; always 0 when inert."""
    raw = os.environ.get(ENV_CLOCK_OFFSET, "")
    if not raw or not enabled():
        return 0.0
    value = float(raw)
    if not math.isfinite(value) or abs(value) > MAX_CLOCK_OFFSET_SECONDS:
        raise ValueError("Test clock offset must be finite and at most one day")
    return value


def offset_clock(clock):
    """Wrap a datetime clock with the gated test offset, or return it unchanged."""
    offset = clock_offset()
    if not offset:
        return clock

    def shifted() -> datetime:
        return clock() + timedelta(seconds=offset)
    return shifted


def offset_seconds_clock(clock):
    """Wrap a clock in epoch seconds (like time.time) with the gated test offset."""
    offset = clock_offset()
    if not offset:
        return clock

    def shifted() -> float:
        return clock() + offset
    return shifted


class CheckpointLedger:
    """Executor ledger proxy that stops at executor.after_start_write."""
    def __init__(self, ledger):
        self._ledger = ledger

    def __getattr__(self, name):
        return getattr(self._ledger, name)

    def start_write(self, *args, **kwargs):
        result = self._ledger.start_write(*args, **kwargs)
        checkpoint("executor.after_start_write")
        return result


class CheckpointCluster:
    """Deployment API proxy that stops just before and just after the patch.

    executor.before_patch stops before the adapter's conditional_restore, so
    before its pre-read. executor.before_submit stops inside it, after the
    pre-read and local UID/resourceVersion check have passed and before the
    PATCH is sent (the adapter's before_submit hook, which this proxy sets):
    a process held there and released after the target moved still sends
    its PATCH with the version it read, so the API server's own test decides.
    """
    def __init__(self, cluster):
        self._cluster = cluster
        if hasattr(cluster, "before_submit"):  # The live adapter (services/executor/kubernetes_adapter.py).
            cluster.before_submit = lambda: checkpoint("executor.before_submit")

    def __getattr__(self, name):
        return getattr(self._cluster, name)

    def conditional_restore(self, **kwargs):
        checkpoint("executor.before_patch")
        applied = self._cluster.conditional_restore(**kwargs)
        checkpoint("executor.after_patch_before_save")
        return applied
