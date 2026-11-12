"""Run the Coordinator and every worker as separate OS processes on one host.

The Coordinator (runtime.coordinator_server) and each worker
(runtime.worker_service: planner-1, guard-1, executor-a, executor-b and
verifier-1, plus the Observer feed) run as their own Python process, with
their own state directory, their own generated credentials and their own TCP
connections. Each worker reaches the Coordinator through its own fault proxy
(experiments/distributed/faultproxy.py), so a harness can cut, slow or break
one worker's network path without touching the others.

Credentials are generated per run and given to each child only in its
environment, never in argv or configuration files; invariant X2 checks that
no log holds one either. Every worker gets only its own credentials. The
Coordinator, which must check them, gets every delivery and approval
credential, and the harness keeps the operator credential in memory.
A throwaway CA and server certificate (openssl) give the Coordinator HTTPS on
loopback when openssl is available; otherwise the run records tls false.

These are processes on one host. They share one kernel, one clock and one disk.
They are not separate machines. Each worker measures its clock offset from the
Coordinator (Cristian's algorithm, runtime/worker_http.py::ClockEstimator) and
the Coordinator records it, but on one host the only skew it can find is the
gated test offset. The file mode backs Kubernetes with tests/support/file_cluster.py; the
kind mode targets only the connected kind lab (KIND_CONTEXTS) and refuses every
other context. Both modes count a template write by its effect on the pod
template, never by metadata.generation, so a metadata-only Executor fence, or
a Coordinator seal (a fence that also writes sentinel.io/fence-epoch), is not
a write in either (observed_writes, kind_writes).
"""
from contextlib import suppress
from datetime import UTC, datetime, timedelta
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time

from experiments.distributed import fixtures
from experiments.distributed.faultproxy import FaultProxy, ProxyGroup
from runtime import fault_points

ROOT = Path(__file__).resolve().parents[2]
LOOPBACK = "127.0.0.1"
KIND_CONTEXTS = frozenset({"kind-sentinel-boutique-connected"})
KIND_CLUSTER_ID = "sentinel-boutique-connected"
WORKERS = (("planner-1", "planner"), ("guard-1", "guard"), ("executor-a", "executor"),
           ("executor-b", "executor"), ("verifier-1", "verifier"))
OBSERVER = ("observer-1", "observer")
APPROVERS = (("operator-1", "operator"), ("guard-1", "guard"), ("executor-a", "executor"), ("executor-b", "executor"))
TERMINAL = frozenset({"RECOVERED", "FAILED", "NEEDS_REVIEW", "REJECTED"})
OPENSSL_CANDIDATES = (r"C:\Program Files\Git\usr\bin\openssl.exe", r"C:\Program Files\Git\mingw64\bin\openssl.exe")


class SuspendUnsupported(RuntimeError):
    """Process suspension needs SIGSTOP; Windows has no supported equivalent in the standard library."""


def check_kind_context(context):
    """Allow only the connected kind lab. The original lab cluster is always refused."""
    if context not in KIND_CONTEXTS:
        raise ValueError("Kind mode runs only against an allowlisted lab context; this context is refused")
    return context


def observation(deployment):
    """What a template-write count needs from one read of the Deployment.

    template_sha256 fingerprints the whole spec.template, restart annotation
    included, the way the Executor and the file cluster do
    (services/executor/store.py::fingerprint); evidence and fence are the raw
    sentinel.io/execution and sentinel.io/fence annotations, and fence_epoch
    the raw sentinel.io/fence-epoch a seal writes with its fence marker in
    the same patch (P3.04), so a seal is one fence, not also a change of the
    other annotations. other_annotations and other_spec fingerprint the rest
    of the annotations and of the spec, and generation is
    metadata.generation (None when absent): together they
    let observed_writes tell when more changes were committed between two
    reads than the reads show.
    """
    from services.executor.core import ACTION_ANNOTATION
    from services.executor.reconciliation import FENCE_ANNOTATION, FENCE_EPOCH_ANNOTATION
    from services.executor.store import fingerprint
    from shared.canonical import sha256_hex
    metadata = deployment.get("metadata") or {}
    annotations = dict(metadata.get("annotations") or {})
    spec = dict(deployment.get("spec") or {})
    template = spec.pop("template", None)
    generation = metadata.get("generation")
    return {"template_sha256": fingerprint(template) if isinstance(template, dict) else None,
            "evidence": annotations.pop(ACTION_ANNOTATION, None), "fence": annotations.pop(FENCE_ANNOTATION, None),
            "fence_epoch": annotations.pop(FENCE_EPOCH_ANNOTATION, None),
            "other_annotations": sha256_hex(annotations), "other_spec": sha256_hex(spec),
            "generation": generation if type(generation) is int else None,
            "resource_version": metadata.get("resourceVersion"), "observed_at": time.time()}


def _evidence_template(value):
    """The template_sha256 an evidence annotation names, or None when it is malformed."""
    try:
        found = json.loads(value).get("template_sha256")
    except (TypeError, ValueError, AttributeError, RecursionError):
        return None
    return found if isinstance(found, str) else None


def observed_writes(previous, current):
    """The rows for what was committed between two successive observations.

    A write is counted by its effect, as the file cluster counts a patch
    (tests/support/file_cluster.py::patch_action): the pod template's
    fingerprint changed (a RESTART changes only the restart annotation inside
    the template, and that counts), or a new sentinel.io/execution evidence
    annotation appeared whose template_sha256 is the template now in place (a
    Sentinel write that put back the same template, so the fingerprint alone
    cannot show it). Anything else changes metadata only and is not a write:
    an Executor fence, a Coordinator seal (the fence marker with its
    sentinel.io/fence-epoch), a label, another annotation, or the Deployment
    controller's own bookkeeping, even though the API server raises
    metadata.generation for some of them. A counted write is labelled by the
    annotation it brought, as in file mode: new evidence names its action, or
    "unknown" when the evidence is malformed or names a template other than
    the changed one; else a new fence marker makes it a fence that changed the
    template, when one commit made both changes; else it is unannotated. With
    the template unchanged, new evidence that names another template yields
    no write row of its own.

    Reads are polled, so several commits can fall between two of them. The
    apps/v1 Deployment strategy raises metadata.generation once for every
    commit that changes the spec or the annotations, and each such commit
    changes at least one of four parts: the template or its evidence, the
    fence marker (with a seal's epoch), the other annotations, or the rest
    of the spec. When generation rose by more than the number of parts that
    differ, some commit is not accounted for and could be a hidden template
    write (two writes, or a write and its revert). That window adds an
    "unobserved" row with action_id "unknown", on which I1 fails, so a
    kind-mode I1 pass is not bought by polling too slowly. Harmless commits
    can raise it too (two fences or seals between reads), which fails safe. One commit that changes several
    parts lets one more commit go unflagged, and without a generation in
    both reads no window is flagged.
    """
    from tests.support.file_cluster import annotation_action_id
    template_changed = current["template_sha256"] != previous["template_sha256"]
    evidence = current["evidence"] if current["evidence"] != previous["evidence"] else None
    fence = current["fence"] if current["fence"] != previous["fence"] else None
    fenced = fence is not None or current.get("fence_epoch") != previous.get("fence_epoch")
    matches = evidence is not None and _evidence_template(evidence) == current["template_sha256"]
    commits = (current["generation"] - previous["generation"]
               if None not in (current["generation"], previous["generation"]) else None)
    parts = sum((template_changed or evidence is not None, fenced,
                 current["other_annotations"] != previous["other_annotations"],
                 current["other_spec"] != previous["other_spec"]))

    def row(marker, action_id, changed, **extra):
        return {"host": "unknown", "action_id": action_id, "kind": "write", "marker": marker,
                "template_changed": int(changed), "state": "committed",
                "resource_version": current["resource_version"], "template_sha256": current["template_sha256"],
                "decided_at": current["observed_at"], **extra}
    rows = []
    if template_changed or matches:
        if evidence is not None:
            marker, action_id = "write", annotation_action_id(evidence) if matches else "unknown"
        elif fence is not None and (commits is None or commits <= 1):
            marker, action_id = "fence", annotation_action_id(fence)
        else:
            marker, action_id = "none", "unannotated"
        rows.append(row(marker, action_id, template_changed))
    if commits is not None and commits > parts:
        rows.append(row("unobserved", "unknown", False, unaccounted=commits - parts))
    return rows


def free_port():
    """A loopback port free now. Another process could take it before a child binds it
    (a small race); the child then fails to start and the readiness wait times out."""
    with socket.socket() as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


def env_name(prefix, identity):
    return prefix + identity.upper().replace("-", "_").replace(".", "_")


def find_openssl():
    found = shutil.which("openssl")
    if found:
        return found
    return next((path for path in OPENSSL_CANDIDATES if Path(path).is_file()), None)


def make_certificates(directory, openssl=None):
    """A throwaway CA and a 127.0.0.1 server certificate; None without openssl.

    The CA key is deleted once the server certificate is signed.
    """
    openssl = openssl or find_openssl()
    if openssl is None:
        return None
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ca, ca_key = directory / "ca.crt", directory / "ca.key"
    cert, key, request = directory / "server.crt", directory / "server.key", directory / "server.csr"
    extensions = directory / "server.ext"
    extensions.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                          "extendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1,DNS:localhost\n"
                          "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n", encoding="ascii")
    # A minimal configuration of our own, so a host's OPENSSL_CONF cannot change or break the run.
    settings = directory / "openssl.cnf"
    settings.write_text("[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = unused\n", encoding="ascii")
    env = dict(os.environ, OPENSSL_CONF=str(settings))
    curve = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    commands = [
        [openssl, "req", "-x509", *curve, "-keyout", str(ca_key), "-out", str(ca), "-days", "2",
         "-subj", "/CN=Sentinel throwaway test CA", "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext", "subjectKeyIdentifier=hash"],
        [openssl, "req", *curve, "-keyout", str(key), "-out", str(request), "-subj", "/CN=127.0.0.1"],
        [openssl, "x509", "-req", "-in", str(request), "-CA", str(ca), "-CAkey", str(ca_key),
         "-set_serial", "0x" + secrets.token_hex(8), "-out", str(cert), "-days", "2", "-extfile", str(extensions)],
    ]
    try:
        for command in commands:
            subprocess.run(command, check=True, capture_output=True, timeout=60, env=env)
    finally:
        for path in (ca_key, request, settings):
            path.unlink(missing_ok=True)
    return {"ca": ca, "cert": cert, "key": key}


class Process:
    """One child process with its own log files."""

    def __init__(self, name, command, env, logs):
        self.name, self.command, self.env = name, command, env
        self.stdout_path, self.stderr_path = logs / f"{name}.stdout.log", logs / f"{name}.stderr.log"
        self.popen = None
        self.starts = 0
        self._files = []

    def start(self):
        if self.running:
            raise RuntimeError("Process is already running")
        self._close_files()
        out, err = open(self.stdout_path, "ab"), open(self.stderr_path, "ab")  # Closed by kill().
        self._files = [out, err]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.popen = subprocess.Popen(self.command, cwd=ROOT, env=self.env, stdout=out, stderr=err,
                                      stdin=subprocess.DEVNULL, creationflags=flags)
        self.starts += 1
        return self

    @property
    def running(self):
        return self.popen is not None and self.popen.poll() is None

    @property
    def pid(self):
        return self.popen.pid if self.popen else None

    def kill(self):
        """SIGKILL on POSIX, TerminateProcess on Windows: no cleanup runs."""
        if self.running:
            self.popen.kill()
        if self.popen is not None:
            with suppress(subprocess.TimeoutExpired):
                self.popen.wait(10)
        self._close_files()

    def wait(self, timeout):
        return self.popen.wait(timeout)

    def suspend(self):
        if os.name == "nt":
            raise SuspendUnsupported("Suspend is not supported on Windows without psutil")
        os.kill(self.pid, signal.SIGSTOP)

    def resume(self):
        if os.name == "nt":
            raise SuspendUnsupported("Resume is not supported on Windows without psutil")
        os.kill(self.pid, signal.SIGCONT)

    def _close_files(self):
        for stream in self._files:
            stream.close()
        self._files = []

    def stdout_records(self):
        """JSON status lines the process printed on stdout."""
        records = []
        if self.stdout_path.is_file():
            for line in self.stdout_path.read_text(encoding="utf-8", errors="replace").splitlines():
                with suppress(ValueError):
                    value = json.loads(line)
                    if isinstance(value, dict):
                        records.append(value)
        return records


class Topology:
    """A Coordinator, five workers and an Observer feed as separate processes on one host."""

    def __init__(self, run_dir, *, mode="file", tls=True, lease_seconds=4, poll_seconds=0.2,
                 worker_timeout=3, fence_after_seconds=2, clock_offsets=None, auto_approve=True,
                 context=None, kind_bundle=None, python=None, bind=LOOPBACK, certificates=None,
                 max_clock_skew=None, kind_poll_seconds=0.5):
        if mode not in {"file", "kind"}:
            raise ValueError("Mode must be file or kind")
        if bind != LOOPBACK:
            raise ValueError("This harness binds loopback only; processes on one host are not separate hosts")
        self.mode, self.run_dir = mode, Path(run_dir)
        if mode == "kind":
            check_kind_context(context)
            if kind_bundle is None or not Path(kind_bundle).is_dir():
                raise ValueError("Kind mode needs a prepared bundle directory (see experiments/distributed/README.md)")
        self.context, self.kind_bundle = context, kind_bundle
        self.cluster_id = fixtures.CLUSTER_ID if mode == "file" else KIND_CLUSTER_ID
        self.tls_requested, self.lease_seconds, self.poll_seconds = tls, lease_seconds, poll_seconds
        self.worker_timeout, self.fence_after_seconds = worker_timeout, fence_after_seconds
        self.clock_offsets = dict(clock_offsets or {})
        # The skew bound in seconds for every worker and the Coordinator; None measures without enforcing.
        self.max_clock_skew = max_clock_skew
        self.auto_approve = auto_approve
        self.python = python or sys.executable
        self.paths = {name: self.run_dir / name for name in ("config", "state", "logs", "faults", "coordinator")}
        self.tokens, self.approval_tokens, self.ports = {}, {}, {}
        self.processes, self.links, self.health_ports = {}, {}, {}
        self.certificates = certificates
        self.inputs = None
        self.started_at = None
        # Kind mode: the latest observation of the Deployment and the writes seen so far.
        self.kind_poll_seconds = kind_poll_seconds
        self.kind_last, self.kind_rows = None, []
        self._kind_lock = threading.Lock()
        self._kind_watcher = None
        self.approved = set()
        self._stopping = threading.Event()
        self._operator = None

    # Configuration -------------------------------------------------------

    def _write(self, relative, value):
        path = self.paths["config"] / relative
        path.write_text(json.dumps(value, indent=2), encoding="utf-8")
        return path

    def _generate(self):
        for path in self.paths.values():
            path.mkdir(parents=True, exist_ok=True)
        identities = dict([*WORKERS, OBSERVER])
        self.tokens = {name: secrets.token_urlsafe(32) for name in identities}
        self.approval_tokens = {name: secrets.token_urlsafe(32) for name, _ in APPROVERS}
        self._write("identities.json", {name: {"role": role, "token_env": env_name("SENTINEL_DELIVERY_TOKEN_", name)}
                                        for name, role in identities.items()})
        self._write("approval-identities.json", {
            name: {"role": role, "token_env": env_name("SENTINEL_APPROVAL_TOKEN_", name)} for name, role in APPROVERS})
        if self.mode == "file":
            self.inputs = fixtures.build()
            from tests.support.file_cluster import FileCluster
            FileCluster.create(self.run_dir / "cluster.db", self.inputs["snapshot"]).close()
            for name in ("policy", "baseline", "graph", "observations", "detection_policy"):
                self._write(name + ".json", self.inputs[name])
            self._write("allowed-targets.json", [self.inputs["target"]])
        else:
            self.inputs = self._kind_inputs()
        target = self.inputs["target"]
        self._write("approval-permissions.json", {
            name: [{"target": target, "operations": ["create", "read", "revoke", "history"]
                    if role == "operator" else ["read"]}] for name, role in APPROVERS})
        if self.tls_requested and self.certificates is None:
            self.certificates = make_certificates(self.paths["config"] / "tls")
        self.ports = {"delivery": free_port(), "approvals": free_port()}

    def _kind_inputs(self):
        bundle = Path(self.kind_bundle)
        required = ("policy.json", "baseline.json", "allowed-targets.json", "kubeconfig", "verifier-inventory.json")
        missing = [name for name in required if not (bundle / name).is_file()]
        if missing:
            raise ValueError("Kind bundle is missing: " + ", ".join(missing))
        baseline = json.loads((bundle / "baseline.json").read_text(encoding="utf-8"))
        return {"target": baseline["target"], "baseline": baseline, "bundle": bundle, "source": "kind"}

    def expects_clock_skew(self, name):
        """True when this worker's simulated offset is beyond the skew bound, so it must stay unready."""
        return self.max_clock_skew is not None and abs(self.clock_offsets.get(name, 0)) > self.max_clock_skew

    @property
    def tls(self):
        return self.certificates is not None

    @property
    def scheme(self):
        return "https" if self.tls else "http"

    def _worker_config(self, name, role, link):
        config = {"role": role, "identity": name, "url": f"{self.scheme}://{LOOPBACK}:{link['delivery'].port}",
                  "token_env": env_name("SENTINEL_DELIVERY_TOKEN_", name),
                  "state_directory": str(self.paths["state"] / name), "timeout": self.worker_timeout,
                  # Every worker names its cluster, so runtime/fault_points.py::bind can tie the lab gate to it.
                  "cluster_id": self.cluster_id}
        if self.max_clock_skew is not None:
            config["max_clock_skew_seconds"] = self.max_clock_skew
        if self.tls:
            config["ca"] = str(self.certificates["ca"])
        config_dir = self.paths["config"]
        if self.mode == "kind":
            bundle = Path(self.kind_bundle)
            cluster = {"context": self.context, "cluster_id": self.cluster_id, "kubeconfig": str(bundle / "kubeconfig")}
            policy, baseline = str(bundle / "policy.json"), str(bundle / "baseline.json")
            allowed = str(bundle / "allowed-targets.json")
        else:
            cluster = {"cluster": "file:" + str(self.run_dir / "cluster.db"), "context": "file-cluster",
                       "cluster_id": self.cluster_id, "kubeconfig": str(self.run_dir / "cluster.db")}
            policy, baseline = str(config_dir / "policy.json"), str(config_dir / "baseline.json")
            allowed = str(config_dir / "allowed-targets.json")
        if role == "planner":
            config.update(provider="deterministic", baselines=baseline, attempt_lease_seconds=5,
                          max_attempts=3, retry_seconds=1)
        elif role == "verifier":
            config.update(policy=policy, baseline=baseline)
            if self.mode == "file":
                config.update(cluster=cluster["cluster"], dependency_graph=str(config_dir / "graph.json"),
                              samples_per_pass=3, sample_spacing_seconds=1.05)
            else:
                config.update(context=self.context, evidence=str(Path(self.kind_bundle) / "verifier-inventory.json"))
        else:
            config.update(cluster, policy=policy, baseline=baseline, approvals={
                "url": f"{self.scheme}://{LOOPBACK}:{link['approvals'].port}", "identity": name,
                "token_env": env_name("SENTINEL_APPROVAL_TOKEN_", name), "timeout": self.worker_timeout})
            if self.tls:
                config["approvals"]["ca"] = str(self.certificates["ca"])
            if role == "executor":
                config.update(allowed_targets=allowed, fence_after_seconds=self.fence_after_seconds)
        if self.mode == "kind":
            config.pop("cluster", None)
        return self._write(name + ".json", config)

    def _env(self, name, *, level="INFO"):
        env = {key: value for key, value in os.environ.items() if not key.startswith("SENTINEL_")}
        env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", SENTINEL_LOG_LEVEL=level,
                   **{fault_points.ENV_DIRECTORY: str(self.paths["faults"]),
                      fault_points.ENV_CLUSTER: self.cluster_id, fault_points.ENV_PROCESS: name})
        if name == "coordinator":
            env.update({env_name("SENTINEL_DELIVERY_TOKEN_", n): t for n, t in self.tokens.items()})
            env.update({env_name("SENTINEL_APPROVAL_TOKEN_", n): t for n, t in self.approval_tokens.items()})
        else:
            env[env_name("SENTINEL_DELIVERY_TOKEN_", name)] = self.tokens[name]
            if name in self.approval_tokens:
                env[env_name("SENTINEL_APPROVAL_TOKEN_", name)] = self.approval_tokens[name]
        if name in self.clock_offsets:
            env[fault_points.ENV_CLOCK_OFFSET] = str(float(self.clock_offsets[name]))
        return env

    # Lifecycle -----------------------------------------------------------

    def prepare(self):
        """Generate credentials, inputs, configurations, proxies and commands; start nothing."""
        self._generate()
        config = self.paths["config"]
        command = [self.python, "-m", "runtime.coordinator_server",
                   "--db", str(self.paths["coordinator"] / "coordinator.db"),
                   "--identities", str(config / "identities.json"),
                   "--approval-identities", str(config / "approval-identities.json"),
                   "--approval-permissions", str(config / "approval-permissions.json"),
                   "--bind", LOOPBACK, "--port", str(self.ports["delivery"]),
                   "--approval-port", str(self.ports["approvals"]),
                   "--lease-seconds", str(self.lease_seconds), "--request-timeout", "5"]
        if self.tls:
            command += ["--cert", str(self.certificates["cert"]), "--key", str(self.certificates["key"])]
        if self.max_clock_skew is not None:
            command += ["--max-clock-skew-seconds", str(self.max_clock_skew)]
        self.processes["coordinator"] = Process("coordinator", command, self._env("coordinator", level="DEBUG"),
                                                self.paths["logs"])
        for name, role in WORKERS:
            link = ProxyGroup({route: FaultProxy((LOOPBACK, self.ports[route]), tls=self.tls).start()
                               for route in ("delivery", "approvals")})
            self.links[name] = link
            path = self._worker_config(name, role, link)
            self.health_ports[name] = free_port()
            command = [self.python, "-m", "runtime.worker_service", "--config", str(path),
                       "--poll-seconds", str(self.poll_seconds), "--health-port", str(self.health_ports[name]),
                       "--ready-seconds", "30"]
            self.processes[name] = Process(name, command, self._env(name), self.paths["logs"])
        return self

    def start(self, *, observer=True, timeout=60):
        """Start the Coordinator, then every worker, and wait until each is ready."""
        self.started_at = time.time()
        self.prepare()
        if self.mode == "kind":
            self.kind_begin()
            self._kind_watcher = threading.Thread(target=self._kind_watch, daemon=True)
            self._kind_watcher.start()
        self.processes["coordinator"].start()
        self.wait_coordinator_ready(timeout)
        for name, _ in WORKERS:
            self.processes[name].start()
        for name, _ in WORKERS:
            if self.expects_clock_skew(name):  # Fails closed by design: alive, never ready.
                self._wait(lambda name=name: self.worker_probe(name, "/healthz")[0] == 200, timeout, name)
            else:
                self.wait_worker_ready(name, timeout)
        if self.auto_approve:
            self._operator = threading.Thread(target=self._operate, daemon=True)
            self._operator.start()
        if observer:
            self.start_observer()
        return self

    def start_observer(self):
        name, _ = OBSERVER
        link = ProxyGroup({"delivery": FaultProxy((LOOPBACK, self.ports["delivery"]), tls=self.tls).start()})
        self.links[name] = link
        if self.mode == "kind":
            command = self._kind_observer(name, link)
            self.processes[name] = Process(name, command, self._env(name), self.paths["logs"]).start()
            return
        config = {"identity": name, "url": f"{self.scheme}://{LOOPBACK}:{link['delivery'].port}",
                  "token_env": env_name("SENTINEL_DELIVERY_TOKEN_", name),
                  "state_directory": str(self.paths["state"] / name), "timeout": self.worker_timeout,
                  "policy": str(self.paths["config"] / "detection_policy.json"),
                  "observations": str(self.paths["config"] / "observations.json"), "target": self.inputs["target"]}
        if self.tls:
            config["ca"] = str(self.certificates["ca"])
        path = self._write(name + ".json", config)
        command = [self.python, "-m", "experiments.distributed.observer_feed", "--config", str(path)]
        self.processes[name] = Process(name, command, self._env(name), self.paths["logs"]).start()

    def _kind_observer(self, name, link):
        """The real Observer CLI against the lab context, delivering over the same proxied HTTP(S)."""
        target, bundle = self.inputs["target"], Path(self.kind_bundle)
        command = [self.python, "-m", "services.observer.cli", "run", "--context", check_kind_context(self.context),
                   "--cluster-id", self.cluster_id, "--namespace", target["namespace"], "--deployment", target["name"],
                   "--state", str(self.paths["state"] / name / "observer.sqlite3"),
                   "--policy", str(bundle / "policy.json"),
                   "--coordinator-url", f"{self.scheme}://{LOOPBACK}:{link['delivery'].port}",
                   "--coordinator-identity", name,
                   "--coordinator-token-env", env_name("SENTINEL_DELIVERY_TOKEN_", name), "--interval-seconds", "2"]
        if self.tls:
            command += ["--coordinator-ca", str(self.certificates["ca"])]
        return command

    def kind_deployment(self, runner=subprocess.run):
        """Read the lab Deployment with the bundle's restricted kubeconfig and the allowlisted context."""
        target, bundle = self.inputs["target"], Path(self.kind_bundle)
        command = ["kubectl", f"--context={check_kind_context(self.context)}", f"--kubeconfig={bundle / 'kubeconfig'}",
                   f"--namespace={target['namespace']}", "--request-timeout=10s", "get", "deployment",
                   target["name"], "--output=json"]
        result = runner(command, capture_output=True, check=False, timeout=20)
        if result.returncode:
            raise RuntimeError("The lab Deployment could not be read")
        deployment = json.loads(result.stdout)
        if deployment["metadata"]["uid"] != target["uid"]:
            raise RuntimeError("The lab Deployment is not the bundle's target")
        return deployment

    def kind_begin(self, runner=subprocess.run):
        """Take the starting observation of the lab Deployment; writes are counted from here."""
        first = observation(self.kind_deployment(runner))
        with self._kind_lock:
            self.kind_last, self.kind_rows = first, []
        return first

    def kind_observe(self, runner=subprocess.run):
        """Read the Deployment once and record the rows for what changed since the last read."""
        with self._kind_lock:
            if self.kind_last is None:
                raise RuntimeError("Kind writes are counted only after kind_begin")
            current = observation(self.kind_deployment(runner))
            rows = observed_writes(self.kind_last, current)
            self.kind_last = current
            self.kind_rows.extend(rows)
            return rows

    def kind_writes(self, runner=subprocess.run):
        """Committed template writes since the start, counted from the Deployment itself.

        Independent of Sentinel's records: each read of the Deployment is
        compared with the one before (observed_writes), so a write is a change
        of the pod template, or a new evidence annotation for the template in
        place, and never a metadata-only change such as an Executor fence,
        even though a real API server raises metadata.generation for that.
        Rows have the shape file mode records, with host "unknown" (the
        Deployment does not name the writer) and decided_at the time the write
        was first seen, up to kind_poll_seconds after it committed, so I2 can
        place a write that committed just before a revocation after it.
        During a run a watcher reads every kind_poll_seconds. Two writes
        between one read and the next are seen as at most one, but the
        generation check in observed_writes then adds an "unobserved" row on
        which I1 fails, so too slow a poll fails I1 instead of hiding a
        write. Any writer other than Sentinel is counted too, so nothing else
        may change the template during a run.
        """
        self.kind_observe(runner)
        with self._kind_lock:
            return [dict(row) for row in self.kind_rows]

    def _kind_watch(self, runner=subprocess.run):
        while not self._stopping.wait(self.kind_poll_seconds):
            # A failed read is retried at the next poll; kind_writes raises it to the harness.
            with suppress(OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
                self.kind_observe(runner)

    def stop(self):
        self._stopping.set()
        for thread in (self._operator, self._kind_watcher):
            if thread is not None:
                thread.join(5)
        for process in self.processes.values():
            process.kill()
        for link in self.links.values():
            link.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()

    def kill(self, name):
        self.processes[name].kill()

    def restart(self, name, timeout=60):
        process = self.processes[name]
        process.kill()
        process.start()
        if name == "coordinator":
            self.wait_coordinator_ready(timeout)
        elif name in self.health_ports:
            self.wait_worker_ready(name, timeout)

    def restart_coordinator(self, timeout=60):
        self.restart("coordinator", timeout)

    def suspend(self, name):
        self.processes[name].suspend()

    def resume(self, name):
        self.processes[name].resume()

    # Probes --------------------------------------------------------------

    def _tls_context(self):
        return ssl.create_default_context(cafile=str(self.certificates["ca"])) if self.tls else None

    def _get(self, port, path, *, tls):
        if tls:
            connection = http.client.HTTPSConnection(LOOPBACK, port, timeout=2, context=self._tls_context())
        else:
            connection = http.client.HTTPConnection(LOOPBACK, port, timeout=2)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def _wait(self, check, timeout, what):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with suppress(OSError, ValueError, http.client.HTTPException):
                if check():
                    return True
            time.sleep(0.1)
        raise TimeoutError(what + " did not become ready")

    def wait_coordinator_ready(self, timeout=60):
        process = self.processes["coordinator"]

        def ready():
            if not process.running:
                raise RuntimeError("The Coordinator exited during start-up")
            return self._get(self.ports["delivery"], "/readyz", tls=self.tls)[0] == 200
        return self._wait(ready, timeout, "Coordinator")

    def worker_probe(self, name, path="/readyz"):
        return self._get(self.health_ports[name], path, tls=False)

    def wait_worker_ready(self, name, timeout=60):
        process = self.processes[name]

        def ready():
            if not process.running:
                raise RuntimeError(f"{name} exited during start-up")
            return self.worker_probe(name)[0] == 200
        return self._wait(ready, timeout, name)

    # Faults ----------------------------------------------------------------

    def arm(self, point, action, *, qualifier=None):
        if point not in fault_points.NAMES or action not in {"exit", "block"}:
            raise ValueError("Unknown fault point or action")
        stem = f"{point}@{qualifier}" if qualifier else point
        (self.paths["faults"] / (stem + ".action")).write_text(action, encoding="utf-8")
        return stem

    def reached(self, stem):
        return (self.paths["faults"] / (stem + ".reached")).is_file()

    def wait_reached(self, stem, timeout=60):
        return self._wait(lambda: self.reached(stem), timeout, "Fault point " + stem)

    def reached_by(self, stem):
        """Name of the process that took an armed point, as it recorded it."""
        name = (self.paths["faults"] / (stem + ".reached")).read_text(encoding="utf-8").strip()
        if name not in self.processes:
            raise ValueError("A fault point was taken by an unknown process")
        return name

    def release(self, stem):
        (self.paths["faults"] / (stem + ".release")).write_text("release", encoding="utf-8")

    # Coordinator state (read-only) ------------------------------------------

    def coordinator_db(self):
        uri = (self.paths["coordinator"] / "coordinator.db").resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def query(self, sql, parameters=()):
        try:
            connection = self.coordinator_db()
        except sqlite3.OperationalError:
            return []
        try:
            return [dict(row) for row in connection.execute(sql, parameters)]
        except sqlite3.OperationalError:
            return []
        finally:
            connection.close()

    def incidents(self):
        return self.query("SELECT incident_id, state, active_action_id FROM incidents")

    def states(self):
        return [row["state"] for row in self.incidents()]

    def wait_state(self, states, timeout=60):
        states = set(states)
        return self._wait(lambda: any(state in states for state in self.states()), timeout,
                          "Incident state " + "/".join(sorted(states)))

    def wait_terminal(self, timeout=60):
        """True once every incident is terminal; False at the deadline."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.states()
            if current and all(state in TERMINAL for state in current):
                return True
            time.sleep(0.2)
        return False

    # The operator ------------------------------------------------------------

    def approval_client(self):
        from runtime.approval_http import ApprovalClient
        return ApprovalClient(f"{self.scheme}://{LOOPBACK}:{self.ports['approvals']}", "operator-1",
                              self.approval_tokens["operator-1"], timeout=5, tls_context=self._tls_context())

    def approve(self, action_id):
        from runtime.worker_transport import digest
        (row,) = self.query("SELECT proposal FROM actions WHERE action_id=?", (action_id,))
        proposal = json.loads(row["proposal"])
        policy_version = (self.inputs.get("policy") or {}).get("policy_version") or json.loads(
            (Path(self.kind_bundle) / "policy.json").read_text(encoding="utf-8"))["policy_version"]
        expires = datetime.now(UTC) + timedelta(seconds=240)
        self.approval_client().post("create", {
            "action_id": action_id, "proposal_sha256": digest(proposal), "policy_version": policy_version,
            "expires_at": expires.isoformat()})
        self.approved.add(action_id)

    def revoke(self, action_id, reason="withdrawn by the harness"):
        from runtime.worker_transport import digest
        (row,) = self.query("SELECT proposal FROM actions WHERE action_id=?", (action_id,))
        (latest,) = self.query("SELECT id FROM approvals WHERE action_id=? ORDER BY id DESC LIMIT 1", (action_id,))
        self.approval_client().post("revoke", {
            "action_id": action_id, "proposal_sha256": digest(json.loads(row["proposal"])),
            "approval_id": latest["id"], "reason": reason})

    def _operate(self):
        """Approve each action once when it waits for approval, like an attentive operator."""
        while not self._stopping.wait(0.1):
            for row in self.incidents():
                action_id = row["active_action_id"]
                if row["state"] == "AWAITING_APPROVAL" and action_id and action_id not in self.approved:
                    with suppress(OSError, ValueError, http.client.HTTPException):
                        self.approve(action_id)
