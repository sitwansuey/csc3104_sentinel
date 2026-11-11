"""Run an HTTP-bound component: python -m runtime.worker_service --help."""
import argparse
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import signal
import ssl
import threading

from runtime.remote_adapters import ExecutorAdapter, GuardAdapter, OllamaProvider, PlannerAdapter, VerifierAdapter
from runtime.remote_worker import RemoteWorker, WorkerJournal
from runtime.worker_http import WorkerClient
from shared import log

LOG = log.get_logger("worker_service")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def credential(name):
    token = os.environ.get(name, "")
    if len(token) < 32 or not token.isascii() or any(char.isspace() for char in token):
        raise ValueError("Missing or invalid configured service credential")
    return token


def reader(path):
    return lambda _message: read_json(path)


def client(config):
    context = ssl.create_default_context(cafile=config["ca"]) if config.get("ca") else None
    return WorkerClient(config["url"], config["identity"], credential(config["token_env"]),
                        timeout=config.get("timeout", 15), tls_context=context)


def adapter(config, directory, stack):
    role = config["role"]
    if role == "planner":
        from services.planner.store import PlannerStore
        journal = PlannerStore(directory / "planner.db")
        stack.callback(journal.close)
        model = read_json(config["model_config"])
        timeout = model.get("timeout", 300)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError("Model timeout must be positive, finite and at most one hour")
        baselines = reader(config["baselines"])
        def baseline_list(incident):
            value = baselines(incident)
            return value if isinstance(value, list) else [value]
        return PlannerAdapter(journal, baseline_reader=baseline_list,
            provider=OllamaProvider(model, progress=lambda update: print(
                json.dumps({"component": "planner", "event": "progress", **update}), flush=True)),
            lease_seconds=2 * timeout + 30,
            max_attempts=config.get("max_attempts", 3), retry_seconds=config.get("retry_seconds", 5))
    if role == "verifier":
        from services.observer.kubernetes import InClusterReader, KubectlReader
        from services.verifier.live import LiveEvidenceCollector
        from services.verifier.store import VerificationStore
        journal = VerificationStore(directory / "verifier.db")
        stack.callback(journal.close)
        kubernetes = InClusterReader() if config.get("in_cluster") else KubectlReader(config["context"])
        collector = LiveEvidenceCollector(read_json(config["evidence"]), kubernetes)
        return VerifierAdapter(journal, policy_reader=reader(config["policy"]), sample_reader=collector.samples,
                               dependency_reader=collector.dependencies)
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
        live = build_adapters(**options, allowed_targets=config["allowed_targets"],
                              ledger=directory / "executor.db", locks=directory / "locks",
                              fence_after_seconds=config.get("fence_after_seconds"))
        stack.callback(live.ledger.close)
        if config.get("remote_dependencies", True):
            connection = client(config)
            live.authorizer.dependency_reader = lambda p: connection.post("dependencies", {"proposal": p})["dependencies"]
        return ExecutorAdapter(live)
    authorizer = LiveGuardAuthorizer(**options, approval=None)
    if config.get("remote_dependencies", True):
        connection = client(config)
        authorizer.dependency_reader = lambda p: connection.post("dependencies", {"proposal": p})["dependencies"]
    return GuardAdapter(state_reader=lambda p: read_deployment(p["target"], context=config["context"],
        cluster_id=config["cluster_id"], kubeconfig=config["kubeconfig"]),
        policy_reader=reader(config["policy"]), baseline_reader=reader(config["baseline"]),
        dependency_reader=authorizer.dependency_reader, approval_reader=approval, authorizer=authorizer)


def report(status, error=None):
    """Log one polling pass. Idle passes are DEBUG to keep 2 s polling quiet."""
    level = log.WARNING if error is not None else log.DEBUG if status["status"] == "idle" else log.INFO
    log.emit(LOG, "worker.status", level=level, status=status["status"], replay=status.get("replay"),
             error_type=type(error).__name__ if error is not None else None,
             **{key: status.get(key) for key in ("delivery_id", "event_id", "topic", "action_id", "incident_id")})


@log.entrypoint("worker_service")
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Trusted JSON; secret values stay in environment")
    parser.add_argument("--once", action="store_true", help="One bounded polling pass")
    parser.add_argument("--poll-seconds", type=float, default=2)
    args = parser.parse_args(argv)
    if not math.isfinite(args.poll_seconds) or not 0 < args.poll_seconds <= 60:
        parser.error("Poll interval must be positive and at most 60 seconds")
    stopping = threading.Event()
    previous = {}
    try:
        config = read_json(args.config)
        connection = client(config)
        directory = Path(config["state_directory"])
        directory.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            component = adapter(config, directory, stack)
            journal = WorkerJournal(directory / "transport.db")
            stack.callback(journal.close)
            worker = RemoteWorker(connection, journal, component)
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGINT, signal.SIGTERM):
                    previous[sig] = signal.signal(sig, lambda *_: stopping.set())
            failures = 0
            stack.enter_context(log.bind(identity=config.get("identity"), role=config.get("role")))
            while not stopping.is_set():
                error = None
                try:
                    status = worker.once()
                    failures = 0
                except Exception as caught:  # noqa: BLE001 - a failed pass is retried with backoff; report() logs it
                    error = caught
                    failures += 1
                    status = {"status": "retry", "error_code": type(error).__name__}
                print(json.dumps(status), flush=True)
                report({**worker.current, **status}, error)
                if args.once:
                    return int(bool(failures))
                stopping.wait(min(60, args.poll_seconds * 2 ** min(failures, 5)))
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
