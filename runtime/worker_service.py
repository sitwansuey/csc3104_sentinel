"""Run an HTTP-bound component: python -m runtime.worker_service --help."""
import argparse
from contextlib import ExitStack
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import ssl
import threading
import time

from runtime import fault_points
from runtime.remote_adapters import (
    AgentPlannerAdapter, DeterministicProvider, ExecutorAdapter, GuardAdapter, OllamaProvider, PlannerAdapter,
    VerifierAdapter, utc_now,
)
from runtime.remote_worker import RemoteWorker, WorkerJournal
from runtime.worker_health import PreflightFailed, WorkerHealth, serve_health
from runtime.worker_http import StoreWatch, WorkerClient
from shared import log

LOG = log.get_logger("worker_service")
ROLES = ("planner", "guard", "executor", "verifier")
PLANNER_MODES = ("provider", "agent")
# Configuration keys that would hold a secret value rather than name its variable.
SECRET_KEY = re.compile(r"(?:^|[_-])(?:tokens?|secrets?|password|passwd|api_?key|authorization|bearer|"
                        r"credentials?)(?:[_-]|$)", re.IGNORECASE)
DNS_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")
# The longest wait between polling passes after failures (P3.06; it was 60 s).
BACKOFF_CAP_SECONDS = 15
# The longest gap between lease renewals while a claimed request is processed
# (runtime/remote_worker.py::Lease renews at this interval or faster).
RENEWAL_SECONDS = 5


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def credential(name):
    token = os.environ.get(name, "")
    if len(token) < 32 or not token.isascii() or any(char.isspace() for char in token):
        raise ValueError("Missing or invalid configured service credential")
    return token


def reader(path):
    return lambda _message: read_json(path)


def client(config, clock=time.time, store=None, answered=None):
    """A Coordinator client; answered (WorkerHealth.contacted) runs after each successful exchange."""
    context = ssl.create_default_context(cafile=config["ca"]) if config.get("ca") else None
    return WorkerClient(config["url"], config["identity"], credential(config["token_env"]),
                        timeout=config.get("timeout", 15), tls_context=context, clock=clock, store=store,
                        answered=answered)


def backoff_seconds(poll_seconds, failures, random=random.random):
    """Seconds to wait before the next polling pass.

    After a pass that completed, poll_seconds. After failed passes in a row
    (including a failed preflight, or a request the Coordinator answered
    with an error), a wait drawn uniformly between 0 and
    min(BACKOFF_CAP_SECONDS, poll_seconds * 2 ** min(failures, 5)) ("full
    jitter"): workers that lost the Coordinator together do not all retry
    at once, and none waits more than the cap once it is back.
    """
    if failures <= 0:
        return poll_seconds
    return random() * min(BACKOFF_CAP_SECONDS, poll_seconds * 2 ** min(failures, 5))


def skew_bound(config):
    """The configured clock skew bound in seconds, or None to measure without enforcing."""
    value = config.get("max_clock_skew_seconds")
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 3600:
        raise ValueError("The clock skew bound must be positive and at most one hour")
    return value


def lab_cluster(config, stack, lab):
    """The file-backed lab cluster named by config["cluster"], or None for a real one.

    Only a worker bound to the file lab cluster (runtime/fault_points.py::bind)
    may use it, so a production worker can never be pointed at a fake by its
    configuration alone.
    """
    name = config.get("cluster")
    if name is None:
        return None
    if (not lab or config.get("cluster_id") != fault_points.FILE_CLUSTER
            or not isinstance(name, str) or not name.startswith("file:")):
        raise ValueError("A file cluster is accepted only in a lab fault run")
    from tests.support.file_cluster import FileCluster
    cluster = FileCluster(name.removeprefix("file:"))
    stack.callback(cluster.close)
    return cluster


def refuse_inline_secrets(value, path="config"):
    """ValueError when trusted configuration holds a secret value itself.

    A worker takes every secret from an environment variable that its
    configuration names with a key ending in _env (token_env, for example), so
    no token reaches a configuration file, argv or a mounted ConfigMap.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and not key.lower().endswith("_env") and SECRET_KEY.search(key):
                raise ValueError(f"{path}.{key} looks like a secret; name an environment variable with a *_env key")
            refuse_inline_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            refuse_inline_secrets(item, f"{path}[{index}]")


def agent_model_config(config):
    """The RCA agent configuration: the sample defaults, then model_config, then model_endpoint.

    The endpoint and model come from trusted configuration. The Ollama client
    sends no credential, and a secret in either file is refused.
    """
    from services.rca.core import DEFAULT_CONFIG
    model = read_json(DEFAULT_CONFIG)
    if config.get("model_config") is not None:
        override = read_json(config["model_config"])
        if not isinstance(override, dict):
            raise ValueError("model_config must hold a JSON object")
        refuse_inline_secrets(override, "model_config")
        model.update(override)
    if "model_endpoint" in config:
        model["base_url"] = config["model_endpoint"]
    model["allow_abstention"] = True  # An abstention is a saved ESCALATE, never a stopped worker.
    return model


def observer_telemetry(directory):
    """Export one incident's window from read-only Observer snapshots (*-observation.json)."""
    path = Path(directory)
    if not path.is_dir():
        raise ValueError("telemetry_directory must be an existing directory of Observer snapshots")

    def export(destination, incident):
        from experiments.boutique.harness.agent_live import export_telemetry
        export_telemetry(path, incident["target"], destination, incident=incident)
    return export


def agent_target_reader(config, cluster):
    """A read-only Deployment read for the agent's target checks and rebinding.

    The lab file cluster in a bound lab run; otherwise the projected in-cluster
    identity (in_cluster: true) or an explicit kubectl context. The incident
    target must name the configured cluster_id.
    """
    cluster_id = config.get("cluster_id")
    if not isinstance(cluster_id, str) or not cluster_id:
        raise ValueError("An agent Planner needs the cluster_id of the cluster it reads")
    if cluster is not None:
        def read(target):
            return cluster.snapshot(target)["deployment"]
    elif config.get("in_cluster"):
        from services.observer.kubernetes import InClusterReader
        api = InClusterReader()

        def read(target):
            if target.get("kind") != "Deployment" or not all(
                    isinstance(target.get(key), str) and DNS_NAME.fullmatch(target[key]) for key in ("namespace", "name")):
                raise ValueError("Expected a valid Deployment target")
            return api.get(f"/apis/apps/v1/namespaces/{target['namespace']}/deployments/{target['name']}")
    else:
        from services.guard.adapters import read_deployment
        context = config.get("context")
        if not isinstance(context, str) or not context:
            raise ValueError("An agent Planner needs in_cluster or an explicit kubectl context")

        def read(target):
            return read_deployment(target, context=context, cluster_id=cluster_id,
                                   kubeconfig=config.get("kubeconfig"))["deployment"]

    def deployment(target):
        if target.get("cluster_id") != cluster_id:
            raise ValueError("The incident target is in another cluster")
        return read(target)
    return deployment


def agent_planner(config, journal, directory, cluster):
    """The remote agent Planner: AgentPlanner hooks, the durable journal and a model preflight."""
    if "provider" in config:
        raise ValueError("Agent mode plans with the RCA agent; remove provider from its configuration")
    baseline = read_json(config["baselines"])
    if isinstance(baseline, list):
        if len(baseline) != 1:
            raise ValueError("Agent mode plans with exactly one reviewed baseline")
        baseline = baseline[0]
    return AgentPlannerAdapter(journal, config=agent_model_config(config), folder=directory / "agent",
                               baseline=baseline, telemetry=observer_telemetry(config["telemetry_directory"]),
                               deployment_reader=agent_target_reader(config, cluster),
                               max_attempts=config.get("max_attempts", 3), retry_seconds=config.get("retry_seconds", 5),
                               preflight_seconds=config.get("preflight_seconds", 30))


def planner_provider(config, lab=False):
    """(provider, attempt lease seconds). Ollama is the default.

    The deterministic provider is accepted only in a bound lab fault run. Its
    rationale and its journal usage both say it is synthetic, but it is a
    stand-in for planning, not planning, so no other run may serve it.
    """
    name = config.get("provider", "ollama")
    if name == "deterministic":
        if not lab:
            raise ValueError("The deterministic planner is accepted only in a lab fault run")
        lease = config.get("attempt_lease_seconds", 30)
        if type(lease) not in (int, float) or not math.isfinite(lease) or not 0 < lease <= 3600:
            raise ValueError("Attempt lease must be positive, finite and at most one hour")
        return DeterministicProvider(checkpoint=lambda: fault_points.checkpoint("planner.mid_inference")), lease
    if name != "ollama":
        raise ValueError("Planner provider must be ollama or deterministic")
    model = read_json(config["model_config"])
    refuse_inline_secrets(model, "model_config")
    timeout = model.get("timeout", 300)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValueError("Model timeout must be positive, finite and at most one hour")
    return OllamaProvider(model, progress=lambda update: print(
        json.dumps({"component": "planner", "event": "progress", **update}), flush=True)), 2 * timeout + 30


def adapter(config, directory, stack, store=None, answered=None, stopping=None):
    """The role's component; stopping (the shutdown event) ends a Verifier pass early."""
    role = config["role"]
    refuse_inline_secrets(config)
    lab = fault_points.bind(config)  # Raises when lab variables are set for a worker aimed elsewhere.
    cluster = lab_cluster(config, stack, lab)
    clock = fault_points.offset_clock(utc_now) if lab else utc_now
    if role == "planner":
        from services.planner.store import PlannerStore
        mode = config.get("mode", "provider")
        if mode not in PLANNER_MODES:
            raise ValueError("Planner mode must be provider or agent")
        journal = PlannerStore(directory / "planner.db")
        stack.callback(journal.close)
        if mode == "agent":
            return agent_planner(config, journal, directory, cluster)
        provider, lease_seconds = planner_provider(config, lab)
        baselines = reader(config["baselines"])
        def baseline_list(incident):
            value = baselines(incident)
            return value if isinstance(value, list) else [value]
        return PlannerAdapter(journal, baseline_reader=baseline_list, provider=provider, lease_seconds=lease_seconds,
            max_attempts=config.get("max_attempts", 3), retry_seconds=config.get("retry_seconds", 5))
    if role == "verifier":
        from services.verifier.store import VerificationStore
        journal = VerificationStore(directory / "verifier.db")
        stack.callback(journal.close)
        if cluster is not None:
            samples, dependencies = cluster.verifier_readers(config, clock=clock)
        else:
            from services.observer.kubernetes import InClusterReader, KubectlReader
            from services.verifier.live import LiveEvidenceCollector
            kubernetes = InClusterReader() if config.get("in_cluster") else KubectlReader(config["context"])
            collector = LiveEvidenceCollector(read_json(config["evidence"]), kubernetes)
            samples, dependencies = collector.samples, collector.dependencies
        # A pass samples within its claim (L-VERIFIER-RETRY-BACKOFF); the shutdown event cuts its waits short.
        return VerifierAdapter(journal, policy_reader=reader(config["policy"]), sample_reader=samples,
                               dependency_reader=dependencies, clock=clock, stopping=stopping,
                               sleep=stopping.wait if stopping is not None else time.sleep)
    if role not in {"guard", "executor"}:
        raise ValueError("Worker role must be planner, guard, executor or verifier")
    from runtime.approval_http import ApprovalClient
    from runtime.live_executor import LiveGuardAuthorizer, build_adapters
    from services.guard.adapters import read_deployment
    approval_config = config["approvals"]
    tls = ssl.create_default_context(cafile=approval_config["ca"]) if approval_config.get("ca") else None
    approval = ApprovalClient(approval_config["url"], approval_config["identity"],
        credential(approval_config["token_env"]), tls_context=tls)
    options = {key: config[key] for key in ("context", "cluster_id", "kubeconfig", "policy", "baseline")}
    options["dependencies"] = config.get("dependencies", directory / "remote-dependencies.json")
    options.update(approval_reader=approval, server_dry_run=config.get("server_dry_run", False))
    if role == "executor":
        runner = {"runner": cluster.kubectl(config["identity"])} if cluster is not None else {}
        # P5.08: "transport": "rest" swaps the Executor's Deployment client; kubectl stays the default.
        transport = config.get("transport", "kubectl")
        if cluster is not None and transport != "kubectl":
            raise ValueError("A lab file cluster is driven only through the kubectl transport")
        live = build_adapters(**options, allowed_targets=config["allowed_targets"],
                              ledger=directory / "executor.db", locks=directory / "locks",
                              fence_after_seconds=config.get("fence_after_seconds"), clock=clock,
                              transport=transport, **runner)
        stack.callback(live.ledger.close)
        if lab:  # Bound lab fault runs only: named stop points. clock above carries the test offset.
            live = replace(live, ledger=fault_points.CheckpointLedger(live.ledger),
                           cluster=fault_points.CheckpointCluster(live.cluster))
        if config.get("remote_dependencies", True):
            connection = client(config, store=store, answered=answered)
            live.authorizer.dependency_reader = lambda p: connection.post("dependencies", {"proposal": p})["dependencies"]
        return ExecutorAdapter(live)
    authorizer = LiveGuardAuthorizer(**options, approval=None, clock=clock)
    if config.get("remote_dependencies", True):
        connection = client(config, store=store, answered=answered)
        authorizer.dependency_reader = lambda p: connection.post("dependencies", {"proposal": p})["dependencies"]
    state_reader = ((lambda p: cluster.snapshot(p["target"])) if cluster is not None else
                    lambda p: read_deployment(p["target"], context=config["context"],
                                              cluster_id=config["cluster_id"], kubeconfig=config["kubeconfig"]))
    return GuardAdapter(state_reader=state_reader,
        policy_reader=reader(config["policy"]), baseline_reader=reader(config["baseline"]),
        dependency_reader=authorizer.dependency_reader, approval_reader=approval, authorizer=authorizer, clock=clock)


def report(status, error=None):
    """Log one polling pass. Idle passes are DEBUG to keep 2 s polling quiet."""
    level = (log.WARNING if error is not None or status["status"] == "store_changed"
             else log.DEBUG if status["status"] == "idle" else log.INFO)
    log.emit(LOG, "worker.status", level=level, status=status["status"], replay=status.get("replay"),
             error_type=type(error).__name__ if error is not None else None,
             # A failed preflight: its fixed code and the class of its cause, never the cause text.
             reason=error.code if isinstance(error, PreflightFailed) else status.get("reason"),
             cause=type(error.__cause__).__name__ if isinstance(error, PreflightFailed) else None,
             **{key: status.get(key) for key in ("delivery_id", "event_id", "topic", "action_id", "incident_id")})


def poll(worker, health, stopping, poll_seconds, *, once=False, wait=None, random=random.random):
    """Run polling passes until stopping is set; with once, one pass. Return the exit code.

    wait(seconds) sleeps between passes (stopping.wait by default); random
    draws the jitter. Tests pass both to follow the back-off without waiting.
    """
    wait = wait or stopping.wait
    failures = 0
    while not stopping.is_set():
        error = None
        health.heartbeat()
        try:
            status = worker.once()
            health.contacted()  # once() returned, so the Coordinator answered this pass.
            health.clock_checked(worker.clock_ready())  # Out of bound: not ready, no claims.
            health.store_checked(worker.store_ready())  # Replaced or rolled-back database: not ready, no claims.
            failures = 0
        except PreflightFailed as caught:
            # The store and skew checks run before the preflight, so this pass found both
            # passing: record them, and /readyz names the preflight alone as the reason.
            health.clock_checked(worker.clock_ready())
            health.store_checked(worker.store_ready())
            error = caught
            failures += 1
            status = {"status": "not_ready", "error_code": caught.code}
        except Exception as caught:  # noqa: BLE001 - a failed pass is retried with backoff; report() logs it
            error = caught
            failures += 1
            status = {"status": "retry", "error_code": type(error).__name__}
        print(json.dumps(status), flush=True)
        report({**worker.current, **status}, error)
        if once:
            return int(bool(failures))
        wait(backoff_seconds(poll_seconds, failures, random))
    return 0


def accept_store(config):
    """Clear a recorded Coordinator store change in this worker's journal; the running worker resumes."""
    journal = WorkerJournal(Path(config["state_directory"]) / "transport.db")
    try:
        change = journal.accept_store()
    finally:
        journal.close()
    log.emit(LOG, "worker.store_change_cleared", identity=config.get("identity"),
             reason=change["reason"] if change else None)
    print(json.dumps({"status": "store_accepted", "reason": change["reason"] if change else None}), flush=True)
    return 0


@log.entrypoint("worker_service")
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Trusted JSON; secret values stay in environment")
    parser.add_argument("--once", action="store_true", help="One bounded polling pass")
    parser.add_argument("--role", choices=ROLES,
                        help="Refuse to start unless the configuration is for this role (a container image argument)")
    parser.add_argument("--poll-seconds", type=float, default=2)
    parser.add_argument("--health-port", type=int, help="Serve /healthz and /readyz on 127.0.0.1 at this port")
    parser.add_argument("--ready-seconds", type=float, default=60,
                        help="/readyz fails when the last successful Coordinator exchange (a claim, lease "
                             f"renewal, result or clock measurement) is older than this; above {RENEWAL_SECONDS}")
    parser.add_argument("--live-seconds", type=float, default=900,
                        help="/healthz fails when the loop started no pass for this long; set it above the longest pass")
    parser.add_argument("--accept-store", action="store_true",
                        help="After a restore check, clear this worker's recorded Coordinator store change and exit; "
                             "the running worker then resumes claiming")
    args = parser.parse_args(argv)
    if not math.isfinite(args.poll_seconds) or not 0 < args.poll_seconds <= 60:
        parser.error("Poll interval must be positive and at most 60 seconds")
    if args.health_port is not None and not 0 <= args.health_port <= 65535:
        parser.error("Health port must be from 0 to 65535")
    if not math.isfinite(args.ready_seconds) or not 0 < args.ready_seconds <= 3600:
        parser.error("Readiness window must be positive and at most one hour")
    if args.ready_seconds <= RENEWAL_SECONDS:
        # Every successful exchange counts as contact, and during a long pass the lease
        # renewals are the exchanges, so a shorter window would flap between renewals.
        parser.error(f"Readiness window must be longer than the {RENEWAL_SECONDS} s lease renewal interval")
    if not math.isfinite(args.live_seconds) or not 0 < args.live_seconds <= 86400:
        parser.error("Liveness window must be positive and at most one day")
    stopping = threading.Event()
    health = WorkerHealth(ready_seconds=args.ready_seconds, live_seconds=args.live_seconds)
    previous = {}
    try:
        config = read_json(args.config)
        if args.role is not None and config.get("role") != args.role:
            raise ValueError("The configuration is for another worker role")
        if args.accept_store:
            return accept_store(config)
        bound = skew_bound(config)
        # In a bound lab run the test offset shifts this wall clock too, so the
        # measured offset (Cristian's algorithm) sees the skew it simulates.
        wall = fault_points.offset_seconds_clock(time.time) if fault_points.bind(config) else time.time
        watch = StoreWatch()  # Shared by every client of this worker.
        connection = client(config, wall, watch, answered=health.contacted)
        directory = Path(config["state_directory"])
        directory.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            if args.health_port is not None:
                stack.enter_context(serve_health(health, args.health_port, stopping))
            component = adapter(config, directory, stack, store=watch, answered=health.contacted, stopping=stopping)
            journal = WorkerJournal(directory / "transport.db")
            stack.callback(journal.close)
            # A component with its own prerequisite (the agent Planner's model) checks it just
            # before a claim. While it fails the worker claims no new work and /readyz stays 503,
            # but saved results are still re-sent: delivering one needs no model.
            preflight = getattr(component, "preflight", None)

            def before_claim():
                try:
                    preflight()
                except PreflightFailed:
                    health.prerequisites(False)
                    raise
                health.prerequisites(True)
            worker = RemoteWorker(connection, journal, component, max_clock_skew=bound,
                                  renewal_interval=RENEWAL_SECONDS,
                                  before_claim=before_claim if preflight is not None else None)
            health.configured()
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGINT, signal.SIGTERM):
                    previous[sig] = signal.signal(sig, lambda *_: stopping.set())
            stack.enter_context(log.bind(identity=config.get("identity"), role=config.get("role")))
            return poll(worker, health, stopping, args.poll_seconds, once=args.once)
    except Exception as error:  # noqa: BLE001 - top-level boundary: log the error class and exit 1
        print(json.dumps({"status": "configuration_error", "error_code": type(error).__name__}), flush=True)
        log.emit(LOG, "worker.configuration_error", level=log.ERROR, error_type=type(error).__name__)
        return 1
    finally:
        for sig, value in previous.items():
            signal.signal(sig, value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
