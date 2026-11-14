"""Opt-in Boutique recovery experiment; real recovery and Role 6 verification.

Uses a dedicated kind cluster. --agent enables real RCA and model planning.
--target picks a catalog recovery target (experiments/boutique/catalog.json):
shippingservice (default) or cartservice, which needs --connected.
"""

import argparse
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from experiments import lab_lock
from experiments.recovery.live import Experiment, crash_worker, now, require, save, utc
from experiments.boutique.harness import scenarios, targets
from runtime.approval_session import MODES, operator_identity, require_attended
from runtime.observer_pipeline import LocalCoordinatorSink, ObserverStore
from runtime.verifier_pipeline import process_verifier_requests
from services.verifier.core import inspect_dependencies
from services.verifier.store import VerificationStore, observation_seconds
from services.executor.store import fingerprint
from services.observer.kubernetes import CollectionError, KubernetesEvidenceCollector, KubectlReader
from shared import log
from shared.provenance import tool_versions

LOG = log.get_logger("experiment")

# A Deployment read through kubectl can fail transiently (non-zero exit, timeout, bad JSON).
READ_ERRORS = (RuntimeError, ValueError, OSError, subprocess.SubprocessError)


# The isolated single-service lab deploys only shipping's reviewed manifest (shipping_manifest below).
SHIPPING_IMAGE = targets.profile("shippingservice").image
CLIENT_IMAGE = "fullstorydev/grpcurl:v1.9.3"
# ConfigMap holding a target's vendored proto for probes against servers without reflection.
PROBE_PROTO_CONFIGMAP = "sentinel-probe-proto"
# A real catalog product id; the cart service stores any id without checking the catalog.
PROBE_PRODUCT = "OLJCESPC7Z"


def prepared_environment(path):
    """Only the host bootstrap can supply reviewed image/scope configuration."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    require(set(data) == {"cluster_id", "context", "node_name", "shipping", "client"}, "Invalid prepared environment")
    require(data["cluster_id"] == "sentinel-boutique-package"
            and data["context"] == "kind-sentinel-boutique-package"
            and data["node_name"] == "sentinel-boutique-package-control-plane", "Wrong packaged demo scope")
    for key, image in (("shipping", SHIPPING_IMAGE), ("client", CLIENT_IMAGE)):
        repository = image.rsplit(":", 1)[0]
        if key == "client":
            repository = "docker.io/" + repository
        require(isinstance(data[key], str) and re.fullmatch(re.escape(repository) + r"@sha256:[a-f0-9]{64}", data[key]),
                "Prepared image must be the selected repository's immutable manifest")
    return data


def command(arguments, timeout=300):
    result = subprocess.run(arguments, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout, check=False)
    require(result.returncode == 0, "Command failed: " + " ".join(arguments[:3]) + "\n" + result.stderr[-1500:])
    return result.stdout


def repository_digest(image, inspection):
    """Accept the selected repository's manifest digest, never the config ID."""
    repository = image.rsplit(":", 1)[0]
    values = (value.removeprefix("docker.io/") if repository.startswith("fullstorydev/") else value
              for item in inspection for value in item.get("RepoDigests", []))
    matches = {value for value in values
               if re.fullmatch(re.escape(repository) + r"@sha256:[a-f0-9]{64}", value)}
    require(len(matches) == 1, "Cannot resolve an unambiguous manifest digest for " + repository)
    return matches.pop()


def shipping_manifest(image):
    """Small lab deployment of the actual upstream application, not a substitute."""
    namespace = BoutiqueExperiment.namespace
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}},
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": "shippingservice", "namespace": namespace}, "automountServiceAccountToken": False},
        {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": "shippingservice", "namespace": namespace},
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "minReadySeconds": 2,
                  "selector": {"matchLabels": {"app": "shippingservice"}},
                  "template": {"metadata": {"labels": {"app": "shippingservice"}}, "spec": {
                      "serviceAccountName": "shippingservice", "automountServiceAccountToken": False,
                      "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                                          "seccompProfile": {"type": "RuntimeDefault"}},
                      "containers": [{"name": "server", "image": image, "imagePullPolicy": "Never",
                          "ports": [{"containerPort": 50051}],
                          "env": [{"name": "PORT", "value": "50051"},
                                  {"name": "DISABLE_PROFILER", "value": "1"},
                                  {"name": "DISABLE_TRACING", "value": "1"}],
                          "resources": {"requests": {"cpu": "100m", "memory": "64Mi"},
                                        "limits": {"cpu": "200m", "memory": "128Mi"}},
                          "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                              "capabilities": {"drop": ["ALL"]}},
                          "readinessProbe": {"grpc": {"port": 50051}, "periodSeconds": 3},
                          "livenessProbe": {"grpc": {"port": 50051}, "periodSeconds": 5,
                                            "initialDelaySeconds": 5}}]}}}},
        {"apiVersion": "v1", "kind": "Service",
         "metadata": {"name": "shippingservice", "namespace": namespace},
         "spec": {"selector": {"app": "shippingservice"},
                  "ports": [{"name": "grpc", "port": 50051, "targetPort": 50051}]}}
    ]}


def validate_quote(response):
    cost = response.get("costUsd", {})
    require(cost.get("currencyCode") == "USD", "Shipping request did not return a USD quote")
    units, nanos = int(cost.get("units", 0)), int(cost.get("nanos", 0))
    require(units >= 0 and 0 <= nanos < 1_000_000_000 and units + nanos > 0,
            "Shipping quote must be positive and valid")
    return cost


def validate_cart(response):
    """The cart round trip stored and returned exactly the probe's one item for its own user."""
    cart = response.get("cart", {})
    require(isinstance(response.get("user_id"), str) and response["user_id"].startswith("sentinel-probe-")
            and cart.get("userId") == response["user_id"], "Cart request returned another user's cart")
    require(cart.get("items") == [{"productId": PROBE_PRODUCT, "quantity": 1}],
            "Cart round trip did not return exactly the added item")
    require(response.get("emptied") is True, "Cart probe did not empty its cart")
    return cart


def validate_redis(response):
    """A Redis PING (redis-cart, P4.05) is healthy only when it answers exactly PONG."""
    require(response == "PONG", "Redis PING did not answer PONG")
    return response


# Code-owned application checks; the catalog names one per target. Value: (sample key, validator).
PROBES = {"shipping_quote": ("quote", validate_quote), "cart_round_trip": ("cart", validate_cart),
          "redis_ping": ("redis_reply", validate_redis)}
# The longest Redis reply a probe keeps; anything longer is not a PING answer.
REDIS_REPLY_LIMIT = 64


class BoutiqueExperiment(Experiment):
    cluster_id = "sentinel-boutique-lab"
    context = "kind-sentinel-boutique-lab"
    namespace = "sentinel-boutique"
    # The catalog's default recovery target; select_target() picks another before prepare().
    name = targets.default_target()
    restricted_context = "sentinel-executor-boutique"
    entrypoint = ROOT / "scripts/test_boutique_live.py"
    health_source = "sentinel-boutique-lab-grpc"
    health_description = "gRPC quote and replica health"
    dashboard = None
    lab_control = None
    approval_relay = None  # Dashboard approval relay; set only for --approval ui.
    pause_before_injection = False
    prepared = None
    publish_dependency_results = True
    health_outcome_description = " after executor rollback; Role 6 published RECOVERED"
    # The catalog scenario this run injects (experiments/boutique/catalog.json). None means the
    # target's primary scenario: R1, the original PORT fault, for shippingservice.
    scenario_id = None
    # The image this run removed from the kind node (scenario N1), until it is pulled back.
    evicted_image = None

    @property
    def profile(self):
        """The catalog TargetProfile of this run's target."""
        if getattr(self, "_profile", None) is None or self._profile.name != self.name:
            self._profile = targets.profile(self.name)
        return self._profile

    @property
    def service_account(self):
        return self.profile.service_account

    @property
    def allowed_registries(self):
        return [self.profile.registry]

    @property
    def ceilings(self):
        return self.profile.ceilings

    def select_target(self, name):
        """Choose the catalog recovery target before anything is prepared."""
        require(self.target is None and self.baseline is None, "The recovery target cannot change after preparation")
        self.name = targets.profile(name).name
        self.scenario_id = None
        self.summary["recovery_target"] = self.name

    @property
    def scenario(self):
        scenario_id = self.scenario_id or self.profile.primary_scenario
        if getattr(self, "_scenario", None) is None or self._scenario["id"] != scenario_id:
            self._scenario = scenarios.scenario(scenario_id)
        return self._scenario

    def scope(self):
        """This run's catalog scope name; CatalogError when the cluster is not a catalog lab."""
        return targets.scope_name(self.cluster_id, self.context, self.namespace)

    def executor_names(self):
        """Every target enabled in this scope; the Role and allowlist name exactly these."""
        names = targets.enabled_targets(self.scope())
        require(self.name in names, "The recovery target is not offered in this lab scope")
        return list(names)

    def denied_permissions(self):
        # A real Boutique Deployment that is never a recovery target.
        return [*super().denied_permissions(), ("patch", "deployment/paymentservice")]

    def execution_scope(self):
        """The independent Executor allowlist: the measured identity of every target enabled in this scope."""
        identities = []
        for name in self.executor_names():
            if name == self.name:
                identities.append(self.target)
                continue
            metadata = json.loads(self.kubectl("get", "deployment", name, "-o", "json").stdout)["metadata"]
            identities.append({"cluster_id": self.cluster_id, "namespace": self.namespace,
                               "kind": "Deployment", "name": name, "uid": metadata["uid"]})
        return identities

    def baseline_inputs(self):
        # Keyed by target name: the Executor refuses a baseline measured for another target.
        return {"baseline": None, "baselines": self.directory / "baselines"}

    def write_executor_rbac(self):
        self.rbac_path = self.directory / "executor-rbac.yaml"
        self.rbac_path.write_text(targets.executor_rbac(self.namespace, self.executor_names()), encoding="utf-8")

    def after_recovery_scenarios(self):
        """Finish the default walkthrough by refusing a redundant third repair."""
        if self.provider_script is None:
            from experiments.boutique.harness.rejection import run_unnecessary_rollback
            run_unnecessary_rollback(self)

    def close(self):
        """Workload-specific child processes may override this teardown hook."""

    def request_approval(self, proposal, decision):
        if self.approval_mode in {"interactive", "ui"}:
            self.event(4, "status", "Guard needs operator approval: " + (
                "type the action ID at the harness terminal" if self.approval_mode == "interactive" else
                "type the action ID in the dashboard approval panel and approve"),
                       target=proposal["target"], incident_id=proposal["incident_id"], action_id=proposal["action_id"])
        record = super().request_approval(proposal, decision)
        self.event(4, "status", ("Approval recorded through the approval API by " + record["operator_id"]
                                 + (" (automated lab approval)" if self.approval_mode == "auto-lab" else ""))
                   if record else "Approval refused; no Executor write", target=proposal["target"],
                   incident_id=proposal["incident_id"], action_id=proposal["action_id"])
        return record

    provider_script = None

    def adapters(self, folder, *, coordinator=False, reconcile_only=False):
        adapters = super().adapters(folder, coordinator=coordinator, reconcile_only=reconcile_only)
        if not reconcile_only:
            # Initial routing evidence may age while the action waits. The
            # Executor obtains a new Role 6 finding under its target lock.
            adapters.authorizer.dependency_reader = self.dependency_result
        return adapters

    def recover_incident(self, incident):
        if self.provider_script is None:
            return super().recover_incident(incident)
        from scripts.boutique_provider import run_provider_case
        return run_provider_case(self, incident)

    def prepare(self):
        super().prepare()
        self.policy["health"] = self.health_rules()
        save(self.directory / "policy.json", self.policy)

    def health_rules(self):
        """The Verifier health policy for this run's target, in its probe's protocol."""
        rules = {
            "protocol": "grpc", "required_grpc_status": 0,
            "minimum_samples": 3, "stable_window_seconds": 10,
            "max_sample_gap_seconds": 45, "required_dependencies": [],
            "require_target_identity": True, "require_template_match": True,
            "require_available_replicas": True,
            # Callers' gRPC reconnect backoff can outlive Pod readiness; sustained health is still required afterwards.
            "convergence_grace_seconds": 150, "verification_timeout_seconds": 300,
        }
        if self.profile.probe["kind"] == "redis":
            # redis-cart (P4.05): a PING must answer exactly PONG.
            del rules["required_grpc_status"]
            rules.update(protocol="redis", required_redis_reply="PONG")
        return rules

    def dependency_result(self, proposal):
        # Reviewed single-service topology, not automatic dependency discovery.
        graph = json.loads((ROOT / "deploy/boutique/dependencies.json").read_text(encoding="utf-8"))
        state = {"cluster_id": self.cluster_id, "captured_at": utc(), "deployment": self.deployment()}
        result = inspect_dependencies(proposal, graph, state, now=now())
        save(self.directory / "dependency-graph.json", graph)
        self.event(6, "status", f"Role 6 assessed the isolated {self.profile.label} dependency graph: " + result["status"],
                   target=proposal["target"], incident_id=proposal["incident_id"], action_id=proposal["action_id"])
        return result

    def verification_samples(self, execution):
        """Collect protocol-native evidence; collection failures stay unknown.

        The durable worker closes this reader as soon as the ledger freezes an
        outcome. Its own limit only ensures one reading is offered after the fixed
        deadline, so a pass that collects without raising ends with exactly one
        outcome. A probe that cannot report is missing evidence, never a known
        failure: a target that never answers ends UNKNOWN at that deadline, not
        NOT_RECOVERED after the grace. A failed cluster read skips that reading;
        the Verifier's maximum sample gap still bounds the hole it leaves. Three
        consecutive failed reads raise and leave the saved job pending for a later pass.
        """
        limit = time.monotonic() + observation_seconds(self.policy["health"]) + 5
        index = failures = 0

        def skip(error):
            nonlocal failures
            failures += 1
            self.event(6, "error", "Role 6 skipped a verification reading; cluster read failed: "
                       + type(error).__name__, target=execution["target"],
                       incident_id=execution["incident_id"], action_id=execution["action_id"])
            if failures >= 3:
                raise error
            time.sleep(5)

        while True:
            last = time.monotonic() >= limit  # Decided before collecting this reading.
            try:
                before = self.deployment()
            except READ_ERRORS as error:
                skip(error)
                continue
            key, validator = PROBES[self.profile.probe["check"]]
            if self.profile.probe["kind"] == "redis":
                # The reply itself is the evidence; the Verifier accepts only the exact PONG.
                sample = {"service": self.name, "protocol": "redis", "dependencies": {}, "redis_reply": None}
                try:
                    sample["redis_reply"] = self.probe_response()
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    sample["collection_error"] = type(error).__name__
            else:
                sample = {"service": self.name, "protocol": "grpc", "dependencies": {},
                          "grpc_status": None, "application_check_passed": None}
                try:
                    response = self.probe_response()
                    sample.update(grpc_status=0, **{key: response})
                    try:
                        validator(response)
                    except (AttributeError, TypeError, ValueError, RuntimeError):
                        sample["application_check_passed"] = False
                    else:
                        sample["application_check_passed"] = True
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    # Do not turn a timeout or unavailable probe into an invented gRPC status.
                    sample["collection_error"] = type(error).__name__
            try:
                after = self.deployment()
            except READ_ERRORS as error:
                skip(error)
                continue
            failures = 0
            metadata, status = after["metadata"], after.get("status", {})
            sample.update(
                at=utc(), target={"cluster_id": self.cluster_id, "namespace": self.namespace,
                                  "kind": "Deployment", "name": self.name, "uid": metadata["uid"]},
                ready_replicas=status.get("readyReplicas", 0),
                available_replicas=status.get("availableReplicas", 0),
                desired_replicas=after["spec"].get("replicas", 1),
                template_sha256=fingerprint(after["spec"]["template"]),
                resource_version=metadata["resourceVersion"],
                configuration_stable=(before["metadata"]["uid"] == metadata["uid"]
                                      and before["spec"]["template"] == after["spec"]["template"]
                                      and status.get("observedGeneration", 0) >= metadata["generation"]),
            )
            self.event(6, "status", f"Role 6 collected {self.profile.label} verification sample " + str(index + 1),
                       target=execution["target"], incident_id=execution["incident_id"], action_id=execution["action_id"])
            try:
                self.observe("role6-" + execution["action_id"] + "-" + str(index))
            except CollectionError:
                self.event(2, "error", "Dashboard observation unavailable during Role 6 measurement")
            yield sample
            if last:
                return
            index += 1
            time.sleep(5)

    def complete_recovery(self, folder, proposal, execution, *, clock=None):
        # Readiness waiting is bounded and never itself establishes recovery.
        self.kubectl("rollout", "status", "deployment/" + self.name, "--timeout=120s",
                     timeout=160, allow_failure=True)

        # The sampler runs until the ledger freezes one outcome, not for a fixed count.
        def collect(current):
            return self.verification_samples(current)

        ledger = VerificationStore(self.directory / "verifier.sqlite3")
        try:
            outcomes = process_verifier_requests(self.store, self.bus, ledger=ledger, sample_reader=collect,
                                                 policy_reader=lambda current: self.policy, clock=clock)
            job = ledger.get(execution["action_id"])
            if job:
                save(folder / "post-health.json", job["samples"])
        finally:
            ledger.close()
        require(len(outcomes) == 1 and outcomes[0]["action_id"] == proposal["action_id"],
                "Expected one Role 6 outcome for the current recovery")
        outcome = outcomes[0]
        save(folder / "outcome.json", outcome)
        self.phase("outcome_recorded", folder.name)
        disclosed = next((check["detail"] for check in outcome["checks"]
                          if check["name"] == "convergence" and check["detail"].startswith("Excluded ")), None)
        failed = ", ".join(check["name"] for check in outcome["checks"] if not check["passed"])
        self.event(6, "status", "Role 6 published " + outcome["status"], target=proposal["target"],
                   incident_id=proposal["incident_id"], action_id=proposal["action_id"])
        if disclosed:
            self.event(6, "status", "Role 6 disclosed convergence: " + disclosed, target=proposal["target"],
                       incident_id=proposal["incident_id"], action_id=proposal["action_id"])
        require(outcome["status"] == "RECOVERED", f"Role 6 could not verify {self.profile.label} recovery: "
                + outcome["status"]
                + (" (failed: " + failed + ")" if failed else ""))
        require(self.store.incident(proposal["incident_id"])["state"] == "RECOVERED",
                "Coordinator did not close the verified incident")
        self.check("Role 6 verified gRPC, readiness and template; Coordinator RECOVERED",
                   action_id=proposal["action_id"], outcome="RECOVERED",
                   **({"convergence": disclosed} if disclosed else {}))
        if self.lab_control:
            self.lab_control.complete_scenario("RECOVERED", incident_id=proposal["incident_id"],
                                               action_id=proposal["action_id"])

    def event(self, component, event_type, summary, **details):
        if self.dashboard:
            self.dashboard.event(component, event_type, summary, **details)

    def check(self, name, **details):
        super().check(name, **details)
        self.event(1, "status", "Live harness assertion passed: " + name)

    def load_image(self, image):
        print("Preparing image: " + image, flush=True)
        command(["docker", "pull", image], timeout=600)
        digest = repository_digest(image, json.loads(command(["docker", "image", "inspect", image])))
        # Pull the immutable manifest through the node runtime. Docker Desktop
        # exports can omit other architectures referenced by a multiarch index;
        # kind's --all-platforms importer then fails on those absent layers.
        destination = "docker.io/" + digest if image == CLIENT_IMAGE else digest
        command(["docker", "exec", self.cluster_id + "-control-plane", "crictl", "pull", destination], timeout=600)
        return destination

    def prepare_image(self):
        if self.prepared:
            node = json.loads(self.kubectl("get", "node", self.prepared["node_name"], "-o", "json").stdout)
            require(node["metadata"]["name"] == self.prepared["node_name"], "Wrong prepared cluster node")
        else:
            nodes = command(["kind", "get", "nodes", "--name", self.cluster_id]).split()
            require(nodes == [self.cluster_id + "-control-plane"], "Requires the dedicated single-node Boutique lab")
        require(self.profile.isolated_lab, self.name + " has no isolated lab manifest; use --connected")
        self.summary.update(workload=f"Online Boutique {self.name} {self.profile.release}",
                            planning="operator-authored proposal; no model call",
                            dependencies="Role 6 evaluates reviewed isolated-service graph against live identity; no automatic discovery",
                            verification="Role 6 consumes real gRPC, readiness and template evidence and publishes outcomes",
                            scope="single-node kind; Roles 1/2/4/5/6 integrated; Planner proposal supplied by harness")
        image = self.prepared["shipping"] if self.prepared else self.load_image(SHIPPING_IMAGE)
        self.client_image = self.prepared["client"] if self.prepared else self.load_image(CLIENT_IMAGE)
        save(self.directory / "images.json", {"shipping": image, "client": self.client_image})
        manifest = self.directory / "shippingservice.json"
        save(manifest, shipping_manifest(image))
        self.kubectl("apply", "-f", str(manifest))
        self.kubectl("rollout", "status", "deployment/" + self.name, "--timeout=120s", timeout=160)
        self.observer_collector = KubernetesEvidenceCollector(
            KubectlReader(self.context), namespace=self.namespace, name=self.name,
            cluster_id=self.cluster_id, collect_logs=True)
        # Reuse the same narrow Role semantics, adapting only this lab's scope and enabled targets.
        self.write_executor_rbac()
        return image

    def observer_policy(self):
        """Role 2's detection policy; it must stay the same for the whole run (the checkpoint is bound to it)."""
        return {"detection": {"failure_duration_seconds": 5, "max_sample_gap_seconds": 45}}

    def observe(self, label):
        """Use Role 2's full collector, durable checkpoint/outbox and Role 1 sink."""
        observation = self.observer_collector.collect()
        policy = self.observer_policy()
        with ObserverStore(self.directory / "observer.sqlite", policy, self.observer_collector.target) as store:
            incident = store.observe(observation)
            delivery = store.flush(LocalCoordinatorSink(self.directory / "coordinator.sqlite"))
            require(delivery["error"] is None, "Observer could not deliver its persisted incident")
            observer_status = store.status()
        save(self.directory / (label + "-observation.json"), observation)
        save(self.directory / "observer-status.json", observer_status)
        deployment = observation["deployment"]
        self.event(2, "observation", "Role 2 collected actual Deployment, owned Pods, warnings and bounded logs",
                   target={**self.observer_collector.target, "uid": deployment["metadata"]["uid"]},
                   metrics={"desired": deployment["spec"].get("replicas", 1),
                            "ready": deployment.get("status", {}).get("readyReplicas", 0),
                            "available": deployment.get("status", {}).get("availableReplicas", 0)})
        if incident:
            save(self.directory / (label + "-incident.json"), incident)
            self.event(2, "status", "Role 2 incident durably delivered to Coordinator", target=incident["target"],
                       incident_id=incident["incident_id"])
        return incident

    def probe_pod(self, name, method, request):
        """The unprivileged grpcurl Pod for one call; a vendored proto replaces server reflection."""
        proto = self.profile.probe["proto_file"]
        schema = ["-import-path", "/proto", "-proto", Path(proto).name] if proto else []
        container = {"name": "probe", "image": self.client_image, "imagePullPolicy": "Never",
                     "args": ["-plaintext", "-max-time", "5", *schema, "-d", json.dumps(request),
                              self.profile.address, method],
                     "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                                   "limits": {"cpu": "100m", "memory": "64Mi"}},
                     "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                         "capabilities": {"drop": ["ALL"]}}}
        spec = {"restartPolicy": "Never", "activeDeadlineSeconds": 45,
                "automountServiceAccountToken": False,
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [container]}
        if proto:
            container["volumeMounts"] = [{"name": "proto", "mountPath": "/proto", "readOnly": True}]
            spec["volumes"] = [{"name": "proto", "configMap": {"name": PROBE_PROTO_CONFIGMAP}}]
        # The labels let a graph-derived NetworkPolicy admit this probe to its
        # target's port only (scripts/security/network_policy.py).
        labels = {"app": "sentinel-probe", "sentinel.io/probe-target": self.name}
        return {"apiVersion": "v1", "kind": "Pod",
                "metadata": {"name": name, "namespace": self.namespace, "labels": labels}, "spec": spec}

    def install_probe_schema(self):
        """Publish the target's vendored proto for its probe Pods (administrator context); no-op with reflection."""
        proto = self.profile.probe["proto_file"]
        if not proto:
            return
        configmap = {"apiVersion": "v1", "kind": "ConfigMap",
                     "metadata": {"name": PROBE_PROTO_CONFIGMAP, "namespace": self.namespace},
                     "data": {Path(proto).name: (ROOT / proto).read_text(encoding="utf-8")}}
        path = self.directory / "probe-proto.json"
        save(path, configmap)
        self.kubectl("apply", "-f", str(path))

    def probe_response(self):
        """The target's catalog application check, called through cluster DNS by an unprivileged client."""
        check = self.profile.probe["check"]
        if check == "redis_ping":
            return self.redis_response()
        return self.quote_response() if check == "shipping_quote" else self.cart_response()

    def redis_probe_pod(self, name):
        """An unprivileged redis-cli Pod that sends one PING to the target's Service.

        It runs the target's own image as pinned in the measured baseline (or,
        before the baseline exists, the live template), so nothing new is
        pulled: imagePullPolicy stays Never.
        """
        template = (self.baseline or {}).get("pod_template") or self.deployment()["spec"]["template"]
        image = scenarios.container_image(template, self.profile.container)
        require("@sha256:" in image, "The Redis probe image must be pinned by digest")
        container = {"name": "probe", "image": image, "imagePullPolicy": "Never",
                     "command": ["redis-cli", "-h", self.name, "-p", str(self.profile.port), "PING"],
                     "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                                   "limits": {"cpu": "100m", "memory": "64Mi"}},
                     "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                         "capabilities": {"drop": ["ALL"]}}}
        spec = {"restartPolicy": "Never", "activeDeadlineSeconds": 45, "automountServiceAccountToken": False,
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [container]}
        # The grpcurl probe's labels, so a graph-derived NetworkPolicy admits this Pod to redis-cart's port only
        # (scripts/security/network_policy.py).
        labels = {"app": "sentinel-probe", "sentinel.io/probe-target": self.name}
        return {"apiVersion": "v1", "kind": "Pod",
                "metadata": {"name": name, "namespace": self.namespace, "labels": labels}, "spec": spec}

    def redis_response(self):
        """The PING reply text, bounded; a longer or empty reply is reported as it is, never as PONG."""
        name = self.profile.label + "-probe-" + uuid4().hex[:10]
        return self.run_probe_pod(name, self.redis_probe_pod(name),
                                  lambda text: text.strip()[:REDIS_REPLY_LIMIT + 1])

    def probe(self):
        response = self.probe_response()
        PROBES[self.profile.probe["check"]][1](response)
        return response

    def cart_response(self):
        """AddItem, GetCart and EmptyCart for a fresh probe user, so the call reaches Redis."""
        user = "sentinel-probe-" + uuid4().hex[:12]
        service = "hipstershop.CartService/"
        self.grpc_call(service + "AddItem", {"userId": user, "item": {"productId": PROBE_PRODUCT, "quantity": 1}})
        cart = self.grpc_call(service + "GetCart", {"userId": user})
        self.grpc_call(service + "EmptyCart", {"userId": user})
        return {"user_id": user, "cart": cart, "emptied": True}

    def quote_response(self):
        """Independent unprivileged client calls the service through cluster DNS."""
        return self.grpc_call("hipstershop.ShippingService/GetQuote",
                              {"items": [{"productId": "demo-item", "quantity": 1}]})

    def grpc_call(self, method, request):
        name = self.profile.label + "-probe-" + uuid4().hex[:10]
        return self.run_probe_pod(name, self.probe_pod(name, method, request), json.loads)

    def run_probe_pod(self, name, pod, parse):
        """Create one probe Pod, wait for it to finish, and parse its log; the Pod is always deleted."""
        path = self.directory / (name + ".json")
        save(path, pod)
        try:
            self.kubectl("create", "-f", str(path))
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = json.loads(self.kubectl("get", "pod", name, "-o", "json").stdout)
                phase = status.get("status", {}).get("phase")
                if phase in {"Succeeded", "Failed"}:
                    logs = self.kubectl("logs", name, allow_failure=True)
                    require(phase == "Succeeded" and logs.returncode == 0,
                            self.profile.label.capitalize() + " RPC failed: " + logs.stdout[-500:])
                    return parse(logs.stdout)
                time.sleep(1)
            raise RuntimeError(self.profile.label.capitalize() + " RPC probe timed out")
        finally:
            self.kubectl("delete", "pod", name, "--ignore-not-found", "--wait=false", allow_failure=True)

    def quote(self):
        response = self.quote_response()
        validate_quote(response)
        return response

    def health_window(self, expected_template):
        samples = []
        for index in range(3):
            deployment = self.deployment()
            require(deployment["spec"]["template"] == expected_template, "Template changed during health measurement")
            status = deployment.get("status", {})
            require(status.get("readyReplicas") == 1 and status.get("availableReplicas") == 1,
                    self.profile.label.capitalize() + " service is not ready/available")
            response = self.probe()
            after = self.deployment()
            require(after["metadata"]["uid"] == deployment["metadata"]["uid"]
                    and after["spec"]["template"] == expected_template, "Target changed during RPC")
            samples.append({"at": utc(), "ready_replicas": 1, PROBES[self.profile.probe["check"]][0]: response,
                            "resource_version": after["metadata"]["resourceVersion"]})
            self.observe("healthy-" + str(index))
            if index < 2:
                time.sleep(5)
        return samples

    def inject_fault(self):
        """Inject the catalog scenario's fault, only inside one of its catalog scopes."""
        item = self.scenario
        scope = scenarios.require_scope(item, cluster_id=self.cluster_id, context=self.context,
                                        namespace=self.namespace)
        injection, profile = item["injection"], item["target_profile"]
        require(item["target"] == self.name, "The scenario targets another workload")
        if injection["kind"] == "set_env":
            # Invalid PORT (R1) is a real application startup failure, not a fake API.
            self.kubectl(*scenarios.set_env_argv(self.name, injection["container"], injection["env"],
                                                 injection["value"]))
            message = f"Operator injected {injection['env']}={injection['value']} into {self.name}"
        elif injection["kind"] == "set_image_digest":
            # R2: a rollout to a well-formed digest that is not on the node. imagePullPolicy
            # stays Never, so kubelet reports ErrImageNeverPull and never contacts a registry.
            image = scenarios.pinned_image(profile["image_repository"], injection["digest"])
            self.kubectl(*scenarios.set_image_argv(self.name, injection["container"], image,
                                                   profile["image_repository"]))
            message = f"Operator rolled {self.name} out to an image digest that is not on the node"
        else:
            self.evict_node_image(scope, item)
            message = (f"Operator removed the {self.name} image from the kind node and deleted its Pod; "
                       "the Deployment template is unchanged")
        # When the API server accepted the bad rollout. Latency reports measure
        # detection from here; older runs only have the first failing snapshot.
        # The first injection is kept; later scenarios are in the timeline.
        self.summary.setdefault("fault_injected_at", utc())
        self.event(1, "fault", message, target=self.target)

    def evict_node_image(self, scope, item):
        """N1: remove the target's image from the kind node, then delete its Pod; the template stays.

        Refuses unless the live template equals the measured baseline, the
        image is the catalog repository pinned by digest, and no other Pod
        references that digest. The removal is confirmed before the Pod is
        deleted, so a failed removal leaves the running Pod in place.
        """
        repository = item["target_profile"]["image_repository"]
        template = self.deployment()["spec"]["template"]
        require(self.baseline is not None and template == self.baseline["pod_template"],
                "Node image eviction needs the live template to equal the healthy baseline")
        image = scenarios.container_image(template, item["injection"]["container"])
        digest = scenarios.image_digest(image, repository)
        pods = json.loads(self.kubectl("get", "pods", "--all-namespaces", "-o", "json").stdout)
        users = scenarios.other_image_users(pods, digest, namespace=self.namespace, owner=self.name)
        require(not users, "Node image eviction refused; other Pods use the image: " + ", ".join(users[:5]))
        self.evicted_image = image
        command(scenarios.node_argv(scope, "rmi", image, repository), timeout=120)
        check = subprocess.run(scenarios.node_argv(scope, "inspecti", image, repository), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
        require(check.returncode != 0, "The image is still on the node after removal; no Pod was deleted")
        self.kubectl("delete", "pod", "-l", "app=" + self.name, "--wait=false")
        self.summary["node_image_evicted"] = {"node": scope["node"], "image": image}

    def emergency_restore(self):
        """Also pull a removed node image back (N1), the same way setup loads it, and replace the Pod."""
        restored = super().emergency_restore()
        if self.evicted_image is None:
            return restored
        try:
            scope = scenarios.require_scope(self.scenario, cluster_id=self.cluster_id, context=self.context,
                                            namespace=self.namespace)
            command(scenarios.node_argv(scope, "pull", self.evicted_image,
                                        self.scenario["target_profile"]["image_repository"]), timeout=600)
            # kubelet retries ErrImageNeverPull only after its back-off; a new Pod starts at once.
            self.kubectl("delete", "pod", "-l", "app=" + self.name, "--wait=false")
            self.evicted_image = None
            self.summary["node_image_restored"] = True
        except Exception as restore_error:  # noqa: BLE001 - recorded; a failed run re-raises its original error
            log.failure(LOG, "boutique.node_image_restore.failed", restore_error, level=log.ERROR)
            self.summary["node_image_restored"] = False
            return False
        return restored

    def fault(self, label):
        if self.lab_control:
            print("Baseline ready. Inject the " + label + " scenario from the dashboard.", flush=True)
            self.lab_control.wait_for_injection(label)
        elif self.pause_before_injection:
            input("Baseline ready. Press Enter to inject the " + label + " bad rollout (Ctrl+C aborts): ")
        self.inject_fault()
        self.phase("fault_injected", label)
        deadline = time.monotonic() + 130
        index = 0
        consecutive_errors = 0
        while time.monotonic() < deadline:
            try:
                incident = self.observe(label + "-" + str(index))
                consecutive_errors = 0
            except CollectionError as error:
                # A rollout can change while multiple objects are being read.
                # Role 2 correctly rejects that sample; reset its timer and retry
                # bounded read-only collection, never the recovery mutation.
                consecutive_errors += 1
                policy = self.observer_policy()
                with ObserverStore(self.directory / "observer.sqlite", policy, self.observer_collector.target) as store:
                    store.collection_failed(error)
                self.event(2, "error", "Role 2 rejected an inconsistent rollout sample; retrying collection")
                if consecutive_errors >= 3:
                    raise
                incident = None
            if incident:
                # Keep the existing evidence filenames used by the original lab.
                save(self.directory / (label + "-incident.json"), incident)
                self.phase("incident_observed", label)
                return incident
            index += 1
            time.sleep(2)
        # A scored run records NO_INCIDENT first. Only a scenario whose label allows
        # no incident continues (T1, and N3 as acceptable); for the others this stays a failure.
        before = self.summary.get("scenario_result")
        if not self.no_incident(label):
            graded = self.summary.get("scenario_result")
            if graded is not None and graded is not before and graded.get("actual") == "NO_INCIDENT":
                proof = graded.get("true_negative")
                raise RuntimeError(
                    f"No incident opened in the detection window: scenario {graded['scenario']} scored NO_INCIDENT "
                    f"{graded['score']}"
                    + ("" if proof is None else "; true-negative proof " + ("passed" if proof.get("passed")
                                                                             else "failed"))
                    + "; the run does not pass on it")
            raise RuntimeError("Role 2 did not detect the Boutique rollout failure")
        return None

    def no_incident(self, label):
        """Record that no incident appeared; True only when the scenario's label allows that."""
        return False


def chaos_pass_line(scenario_id, expect, summary):
    """The PASS line of a Chaos Mesh run: the decision actually scored, not only the outcome the run expected."""
    graded = summary.get("scenario_result")
    if graded and graded.get("score") != "correct":
        outcome = f"ended in {graded['actual']}, which its label scores {graded['score']} (run expected {expect})"
    elif graded:
        outcome = f"reached its expected outcome ({expect}) with {graded['actual']}"
    else:
        outcome = f"reached its expected outcome ({expect})"
    refused = len(summary.get("forced_refusals", []))
    return (f"PASS: Chaos Mesh scenario {scenario_id} {outcome}; "
            + (f"{refused} forced wrong action(s) refused with zero writes; " if refused else "")
            + "chaos cleanup confirmed")


def main():
    """The live run; the shared heavy-lab lock a Chaos Mesh scenario takes is released however it ends."""
    held = []
    try:
        return _main(held)
    finally:
        for lock in held:
            lock.release()


def _main(held):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--agent", action="store_true", help="Use live RCA and model planning for one guarded recovery")
    parser.add_argument("--agent-config", type=Path, help="JSON overrides for the live agent's Ollama settings")
    parser.add_argument("--expect", choices=["recovered", "escalated"],
                        help="Agent mode: the outcome that passes (default: the scenario's pre-registered one; "
                             "recovered for R1 and R2). 'escalated' passes only when planning ends "
                             "in a saved ESCALATE and NEEDS_REVIEW; the administrator then removes the fault")
    catalog_ids = scenarios.scenario_ids()
    parser.add_argument("--target", choices=targets.target_names(),
                        help="Catalog recovery target (default " + targets.default_target()
                             + "). cartservice needs --connected")
    parser.add_argument("--scenario", choices=catalog_ids,
                        help="Catalog scenario to inject (experiments/boutique/catalog.json). The default is the "
                             "target's primary scenario: R1, the invalid PORT, for shippingservice and C1 for "
                             "cartservice. A target's fallback scenario (C2) also runs the operator sequence; "
                             "R2 and N1 need --agent --connected; the Chaos Mesh scenarios (N2, N3, T1) need "
                             "--connected and plan with the code rule provider unless --agent is given. Every "
                             "one is scored against its pre-registered expected outcome")
    parser.add_argument("--prepared-environment", type=Path, help="Trusted host-prepared packaged cluster/images; no Docker access in runtime")
    parser.add_argument("--dashboard", action="store_true", help="Serve the real UI and collect live cluster telemetry")
    parser.add_argument("--dashboard-port", type=int, default=8765, help="Loopback UI port; 0 selects a free port")
    parser.add_argument("--dashboard-bind", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--exit-after-test", action="store_true", help="Stop the dashboard after validation instead of waiting for Ctrl+C")
    parser.add_argument("--pause-before-injection", action="store_true",
                        help="Wait for Enter before each bad rollout and the final unnecessary rollback proposal")
    parser.add_argument("--ui-controls", action="store_true",
                        help="Opt in to local dashboard buttons for the fixed lab injection sequence")
    parser.add_argument("--connected", action="store_true", help="Deploy the full connected Boutique storefront")
    parser.add_argument("--storefront-port", type=int, default=8080, help="Loopback storefront port in connected mode")
    parser.add_argument("--guard-rejection", action="store_true",
                        help="Run only the final unnecessary-rollback rejection scenario, skipping both recovery rounds")
    parser.add_argument("--crash-worker", help=argparse.SUPPRESS)
    parser.add_argument("--provider-script", type=Path, help="Opt in to a trusted Planner provider adapter (JSON stdin/stdout)")
    parser.add_argument("--provider-policy", type=Path, help="Trusted policy override for provider recovery")
    parser.add_argument("--approval", choices=MODES, default="interactive",
                        help="interactive (default): a person types each action ID at this terminal; auto-lab: explicit "
                             "opt-in for unattended lab runs, recorded as automated approval; reject: refuse every "
                             "approval; ui: type the action ID and approve in the dashboard (needs --ui-controls)")
    parser.add_argument("--operator", help="Operator identity for interactive or ui approval "
                                           "(default lab-operator; dashboard-operator for ui)")
    parser.add_argument("--pair", choices=scenarios.pair_ids(),
                        help="Inject a catalog scenario pair together on the connected lab and verify each target "
                             "on its own (P4.05); needs --connected and plans with the code rule provider")
    parser.add_argument("--executor-kill", action="store_true",
                        help="With --pair: kill the Executor worker right after the configuration fault's repair "
                             "write and reconcile it without writing again")
    parser.add_argument(lab_lock.BREAK_OPTION, action="store_true",
                        help="With a Chaos Mesh scenario or --pair: remove a heavy-lab lock whose process is no "
                             "longer running, then take it (experiments/lab_lock.py)")
    parser.add_argument("--image-scans", type=Path,
                        help="With --connected: turn on the CVE gate (deploy/security/cve-gate.json) and read its "
                             "scans from this directory of scan summaries (P5.02)")
    parser.add_argument("--cve-exceptions", type=Path, help="With --image-scans: the reviewed CVE exceptions file")
    parser.add_argument("--protected", action="store_true",
                        help="P5.05: recover the security lab's protected shippingservice (R1) with the sidecar-aware "
                             "Guard, the split Guard-reader identity and the authenticated Verifier probe; start the "
                             "lab with scripts/security/boutique_security.py first. Holds the heavy-lab lock "
                             "(docs/security.md, Protected recovery)")
    parser.add_argument("--security-run", type=Path, help="With --protected: the security lab's run folder")
    parser.add_argument("--protected-stage", choices=("baseline", "full"), default="full",
                        help="With --protected: baseline stops after the protected baseline, the RBAC matrix and the "
                             "before-repair bypass probes; full (default) injects R1 and recovers it with one write")
    args = parser.parse_args()
    if (args.security_run or args.protected_stage != "full") and not args.protected:
        parser.error("--security-run and --protected-stage require --protected")
    if args.protected:
        if (args.pair or args.connected or args.agent or args.guard_rejection or args.provider_script or args.ui_controls
                or args.image_scans or args.prepared_environment or args.dashboard):
            parser.error("--protected runs its own staged recovery; drop --connected, --agent, --pair, "
                         "--guard-rejection, --provider-script, --ui-controls, --dashboard, --image-scans and "
                         "--prepared-environment")
        if not args.security_run:
            parser.error("--protected requires --security-run (the boutique_security.py run folder)")
        if args.target not in (None, "shippingservice") or args.scenario not in (None, "R1"):
            parser.error("--protected recovers shippingservice from scenario R1 only")
    if args.executor_kill and not args.pair:
        parser.error("--executor-kill requires --pair")
    if (args.image_scans or args.cve_exceptions) and (args.pair or not args.connected):
        parser.error("--image-scans applies to one connected scenario, not --pair or the isolated lab")
    if args.cve_exceptions and not args.image_scans:
        parser.error("--cve-exceptions requires --image-scans")
    if args.pair:
        return pair_main(parser, args)
    args.target = args.target or targets.default_target()
    if args.agent_config and not args.agent:
        parser.error("--agent-config requires --agent")
    if args.expect == "escalated" and not args.agent:
        parser.error("--expect escalated requires --agent")
    profile = targets.profile(args.target)
    args.scenario = args.scenario or profile.primary_scenario
    scenario = scenarios.scenario(args.scenario)
    if scenario["target"] != args.target:
        parser.error(f"--scenario {args.scenario} targets {scenario['target']}, not --target {args.target}")
    mode_scope = ("protected" if args.protected else "connected" if args.connected else
                  "package" if args.prepared_environment else "lab")
    if mode_scope not in profile.scopes:
        parser.error(f"--target {args.target} is offered only in the " + ", ".join(profile.scopes)
                     + " scope; add --connected")
    chaos_scenario = scenario["injection"]["kind"] == "chaos"
    # P5.07: a scale fault (S1) plans with the code rule provider like a chaos run; it takes the same lock.
    scale_scenario = scenario["injection"]["kind"] == "scale"
    if args.scenario not in profile.operator_scenarios:
        # The other scenarios are scored decisions on the connected lab, outside the fixed UI sequence.
        if (chaos_scenario or scale_scenario) and not args.connected:
            parser.error("--scenario " + args.scenario + (" (Chaos Mesh)" if chaos_scenario else " (scale)")
                         + " requires --connected")
        if scale_scenario and args.agent:
            parser.error("--scenario " + args.scenario + " (scale) plans with the code rule provider; drop --agent")
        if not (chaos_scenario or scale_scenario) and not (args.agent and args.connected):
            parser.error("--scenario " + args.scenario + " requires --agent --connected")
    if scale_scenario and (args.guard_rejection or args.provider_script or args.dashboard or args.expect):
        parser.error("A scale scenario runs its own planning, refusal and checks; drop --guard-rejection, "
                     "--provider-script, --dashboard and --expect")
    if chaos_scenario and (args.guard_rejection or args.provider_script or args.dashboard):
        # The dashboard's end-of-run check verifies the operator walkthrough or an agent case, not a chaos run.
        parser.error("A Chaos Mesh scenario runs its own planning, refusals and checks; drop --guard-rejection, "
                     "--provider-script and --dashboard")
    if args.ui_controls and (args.scenario != profile.primary_scenario or args.target != targets.default_target()):
        # The dashboard's fixed lab buttons name the default target's primary fault (an invalid PORT).
        parser.error("--ui-controls runs the fixed three-step lab sequence; use the default target and scenario R1")
    expect = args.expect or scenarios.expect(scenario)
    # A Chaos Mesh agent variant may expect an escalation that its label accepts (N2: RESTART correct,
    # ESCALATE acceptable); the score is still recorded but does not decide the run.
    accepted_escalation = (chaos_scenario and args.expect == "escalated"
                           and "ESCALATE" in scenario["expected"]["acceptable"])
    # A chaos scenario takes its outcome from the label, with or without --agent: an --expect recovered on N3 or
    # T1 would turn a pre-registered no-repair trial into an approved repair.
    if (chaos_scenario and args.expect and args.expect != scenarios.expect(scenario)
            and not accepted_escalation):
        parser.error(f"--expect {args.expect} contradicts scenario {args.scenario}'s pre-registered outcome "
                     f"({scenario['expected']['terminal_state']})")
    if (args.agent and args.expect and args.expect != scenarios.expect(scenario)
            and args.scenario != profile.primary_scenario and not accepted_escalation):
        parser.error(f"--expect {args.expect} contradicts scenario {args.scenario}'s pre-registered outcome "
                     f"({scenario['expected']['terminal_state']})")
    if args.agent and args.guard_rejection:
        parser.error("--agent and --guard-rejection are separate scenarios")
    if args.agent and args.provider_script:
        parser.error("--agent and --provider-script are separate planning modes")
    if args.provider_script and not args.provider_script.is_file():
        parser.error("--provider-script must identify an existing adapter")
    if args.provider_script and args.dashboard:
        parser.error("The dashboard acceptance sequence uses the original operator lab mode")
    if args.provider_script and args.guard_rejection:
        parser.error("Provider recovery and the harness-only rejection scenario are separate modes")
    if args.provider_policy and not args.provider_script:
        parser.error("--provider-policy requires --provider-script")
    if args.crash_worker:
        return crash_worker(args.crash_worker)
    try:
        operator_identity(args.approval, args.operator)
        # The rejection scenario, an expected agent escalation and an expected chaos no-repair never ask.
        no_repair = expect in ("escalated", "no-incident") and (args.agent or chaos_scenario)
        if not (args.guard_rejection or no_repair):
            require_attended(args.approval)
    except ValueError as error:
        parser.error(str(error))
    if not 0 <= args.dashboard_port <= 65535:
        parser.error("--dashboard-port must be between 0 and 65535")
    if args.exit_after_test and not args.dashboard:
        parser.error("--exit-after-test requires --dashboard")
    if args.ui_controls and (not args.dashboard or args.pause_before_injection):
        parser.error("--ui-controls requires --dashboard and replaces --pause-before-injection")
    if args.approval == "ui" and not args.ui_controls:
        parser.error("--approval ui requires --dashboard --ui-controls: the approval routes use the lab session token")
    if args.connected and args.prepared_environment:
        parser.error("Connected mode requires its host lab; the old packaged bootstrap is single-service")
    if not 1 <= args.storefront_port <= 65535 or ((args.connected or args.protected) and args.dashboard
                                                 and args.storefront_port == args.dashboard_port):
        parser.error("Storefront port must be valid and separate from the dashboard port")
    if args.break_stale_lock and not (chaos_scenario or scale_scenario or args.protected):
        parser.error(lab_lock.BREAK_OPTION + " applies only to a Chaos Mesh or scale scenario, --protected or --pair")
    if chaos_scenario or scale_scenario or args.protected:
        # P4.03: the whole live run holds the heavy-lab lock, so P4.01 and P4.05 never run alongside it.
        # P5.05: the protected security lab shares Docker's memory with them, so it takes the same lock.
        lock = lab_lock.HeavyLabLock(holder=("P5.05 protected recovery " if args.protected else
                                             "P4.03 chaos scenario " if chaos_scenario else "P5.07 scale scenario ")
                                     + args.scenario, break_stale=args.break_stale_lock)
        try:
            lock.acquire()
        except lab_lock.LabLockError as error:
            print("Refused: " + str(error), file=sys.stderr, flush=True)
            return 2
        held.append(lock)
        if lock.broken is not None:
            print("Removed a stale heavy-lab lock (" + lab_lock.describe(lock.broken) + ")", file=sys.stderr,
                  flush=True)
    directory = args.run_dir or ROOT / "data/experimentation/runs" / ("boutique-live-" + now().strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:6])
    if args.protected:
        from experiments.boutique.harness.protected import ProtectedBoutiqueExperiment
        experiment_type = ProtectedBoutiqueExperiment
    elif args.connected:
        from experiments.boutique.harness.connected import ConnectedBoutiqueExperiment
        experiment_type = ConnectedBoutiqueExperiment
    else:
        experiment_type = BoutiqueExperiment
    if args.agent:
        from experiments.boutique.harness.agent_live import AgentPlanningMixin
        experiment_type = type("AgentBoutiqueExperiment", (AgentPlanningMixin, experiment_type), {})
    if chaos_scenario:
        from experiments.boutique.harness.chaos_recovery import ChaosRecoveryMixin
        experiment_type = type("ChaosBoutiqueExperiment", (ChaosRecoveryMixin, experiment_type), {})
    if scale_scenario:
        from experiments.boutique.harness.scale_recovery import ScaleRecoveryMixin
        experiment_type = type("ScaleBoutiqueExperiment", (ScaleRecoveryMixin, experiment_type), {})
    options = {"storefront_port": args.storefront_port} if args.connected or args.protected else {}
    if args.protected:
        options.update(security_run=args.security_run, stage=args.protected_stage)
    experiment = experiment_type(directory, **options)
    if args.protected:
        experiment.held_lock = held[-1]
    experiment.select_target(args.target)
    if args.image_scans:  # P5.02: the run's policy gets the CVE gate; Guard and Executor read these scans
        experiment.image_scans = args.image_scans.resolve()
        experiment.cve_exceptions = args.cve_exceptions.resolve() if args.cve_exceptions else None
    if chaos_scenario:
        experiment.agent_mode = args.agent
        experiment.expect = expect
    if args.agent:
        experiment.agent_config = args.agent_config
        experiment.expect = expect
        # An explicit --expect that differs from the label is a planning-failure check (primary scenario only):
        # the score is still recorded, but it does not decide whether the run passes.
        experiment.scenario_gates_pass = args.expect in (None, scenarios.expect(scenario))
    experiment.scenario_id = args.scenario
    if not args.guard_rejection:  # The rejection-only check injects no fault.
        experiment.summary["scenario"] = {"id": scenario["id"], "name": scenario["name"],
                                          "class": scenario["class"], "labels": scenario["labels"]["status"]}
    experiment.summary["provenance"]["tools"] = tool_versions(
        ("kubectl", "kind", "docker", "helm") + (("ollama",) if args.agent else ()))
    experiment.provider_script = args.provider_script.resolve() if args.provider_script else None
    experiment.provider_policy = args.provider_policy
    experiment.configure_approval(args.approval, args.operator)
    if args.guard_rejection:
        experiment.approval_not_requested(
            "no approval was requested: the Guard-only refusal check uses a fixed file-backed lab record "
            "that never reaches the Coordinator and proves a refusal only")
    if args.expect == "escalated":
        experiment.approval_not_requested(
            "no approval was requested: --expect escalated passes only when agent planning ends in a saved "
            "ESCALATE and NEEDS_REVIEW, which never reaches Guard approval; a proposed repair is refused unasked")
    elif args.agent and expect == "escalated":
        experiment.approval_not_requested(
            f"no approval was requested: scenario {args.scenario}'s pre-registered correct outcome is a saved "
            "ESCALATE and NEEDS_REVIEW, which never reaches Guard approval; a proposed repair is refused unasked")
    elif no_repair:
        experiment.approval_not_requested(
            f"no approval was requested: scenario {args.scenario}'s pre-registered correct outcome is "
            + ("a saved ESCALATE and NEEDS_REVIEW" if expect == "escalated" else "no incident")
            + "; a proposed repair is refused unasked")
    if args.prepared_environment:
        experiment.prepared = prepared_environment(args.prepared_environment)
        experiment.cluster_id = experiment.prepared["cluster_id"]
        experiment.context = experiment.prepared["context"]
    experiment.pause_before_injection = args.pause_before_injection
    if args.ui_controls:
        from runtime.lab_control import LabControl
        experiment.lab_control = LabControl(directory, cluster_id=experiment.cluster_id, context=experiment.context,
            namespace=experiment.namespace, deployment=experiment.name, rejection_only=args.guard_rejection)
    if args.approval == "ui" and not args.guard_rejection:
        from runtime.approval_relay import ApprovalRelay
        experiment.approval_relay = ApprovalRelay()
    print("Boutique evidence: " + str(experiment.directory), flush=True)
    dashboard = None
    # Docker stop sends SIGTERM. Reuse the normal interruption/finally cleanup.
    def stop_requested(_signum, _frame):
        raise KeyboardInterrupt
    previous_term = signal.signal(signal.SIGTERM, stop_requested)
    try:
        if args.dashboard:
            from experiments.boutique.harness.dashboard import BoutiqueDashboard
            dashboard = BoutiqueDashboard(experiment.directory, context=experiment.context,
                cluster_id=experiment.cluster_id, namespace=experiment.namespace, target=experiment.name,
                port=args.dashboard_port,
                bind=args.dashboard_bind, lab_control=experiment.lab_control,
                approval_relay=experiment.approval_relay,
                application_collector=getattr(experiment, "application_inventory", None))
            experiment.dashboard = dashboard
            dashboard.start()
        if args.guard_rejection:
            from experiments.boutique.harness.rejection import run_unnecessary_rollback
            experiment.prepare()
            experiment.permissions()
            run_unnecessary_rollback(experiment)
            experiment.summary.update(status="PASS", finished_at=utc())
            experiment.credentials.unlink(missing_ok=True)
            experiment.summary["private_executor_credentials_removed"] = not experiment.credentials.exists()
            save(experiment.directory / "summary.json", experiment.summary)
        else:
            experiment.run()
        if dashboard:
            identity = experiment.summary["operator_approval"]["identity"]
            experiment.summary["dashboard"] = (dashboard.verify_agent(expect, operator_id=identity) if args.agent else
                                                dashboard.verify_rejection() if args.guard_rejection else
                                                dashboard.verify(operator_id=identity))
            print("PASS: live rejection dashboard, Guard findings and zero-write evidence"
                  if args.guard_rejection else
                  "PASS: live dashboard HTTP, Guard reports, execution records and cluster telemetry", flush=True)
            if experiment.lab_control:
                experiment.lab_control.finish("COMPLETED")
            if not args.exit_after_test:
                print("Tests complete. Dashboard stays live for inspection; press Ctrl+C to stop it.", flush=True)
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
    except KeyboardInterrupt:
        experiment.summary.update(status="INTERRUPTED")
        if experiment.lab_control:
            experiment.lab_control.finish("STOPPED")
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level boundary: record FAILED, stop the lab and exit 1
        log.failure(LOG, "experiment.failed", exc, level=log.ERROR)
        experiment.summary.update(status="FAILED", integration_error=type(exc).__name__)
        if experiment.lab_control:
            experiment.lab_control.finish("FAILED")
        print("FAIL: Boutique experiment: " + str(exc), file=sys.stderr)
        return 1
    finally:
        try:
            if dashboard:
                dashboard.close()
                save(experiment.directory / "dashboard-cleanup.json", {"server_stopped": True, "collector_stopped": True})
        finally:
            try:
                experiment.close()
            finally:
                experiment.close_approval_authority()
                experiment.store.close()
                experiment.credentials.unlink(missing_ok=True)
                save(experiment.directory / "summary.json", experiment.summary)
                signal.signal(signal.SIGTERM, previous_term)
    if scale_scenario:
        from experiments.boutique.harness.scale_recovery import scale_pass_line
    print(("PASS: protected baseline captured; Executor and Guard-reader RBAC matrix and bypass probes passed; no fault"
           if args.protected_stage == "baseline" else
           "PASS: protected template recovered with one approved write; authenticated Verifier RECOVERED; bypass "
           "probes blocked before, during and after repair")
          if args.protected else
          chaos_pass_line(args.scenario, expect, experiment.summary)
          if chaos_scenario else
          scale_pass_line(args.scenario, experiment.summary)
          if scale_scenario else
          "PASS: agent planning ended in a saved ESCALATE; incident NEEDS_REVIEW with no repair"
          if args.agent and expect == "escalated" else
          "PASS: live agent diagnosed the fault, proposed rollback, and Coordinator verified RECOVERED"
          if args.agent else
          "PASS: configured provider recovery completed with an independent Verifier outcome"
          if args.provider_script else
          "PASS: Guard rejected unnecessary rollback; service unchanged and zero Executor patch attempts"
          if args.guard_rejection else
          "PASS: two Boutique recoveries verified; final unnecessary rollback REJECTED with zero Executor patch attempts", flush=True)
    graded = experiment.summary.get("scenario_result")
    if graded:
        print(f"Scenario {graded['scenario']} ({graded['name']}): decision {graded['actual']}, scored {graded['score']}; "
              f"labels {graded['labels']['status']}", flush=True)
        if graded.get("labels_pending"):
            print("WARNING: the scenario labels are not signed off; do not report this trial as scored", flush=True)
    return 0


def pair_main(parser, args):
    """Validate the --pair options and run the pair (experiments/boutique/harness/multi_fault.py)."""
    if not args.connected:
        parser.error("--pair requires --connected")
    given = [option for option, value in (
        ("--target", args.target), ("--scenario", args.scenario), ("--agent", args.agent),
        ("--agent-config", args.agent_config), ("--expect", args.expect),
        ("--prepared-environment", args.prepared_environment), ("--dashboard", args.dashboard),
        ("--ui-controls", args.ui_controls), ("--pause-before-injection", args.pause_before_injection),
        ("--guard-rejection", args.guard_rejection), ("--provider-script", args.provider_script),
        ("--provider-policy", args.provider_policy), ("--crash-worker", args.crash_worker)) if value]
    if given:
        # The pair names its own targets and scenarios, plans by rule and has no dashboard end-of-run check.
        parser.error("--pair runs its own targets, scenarios and checks; drop " + ", ".join(given))
    try:
        operator_identity(args.approval, args.operator)
        require_attended(args.approval)  # Each of the two repairs needs an approval.
    except ValueError as error:
        parser.error(str(error))
    if args.approval == "ui":
        parser.error("--approval ui requires --dashboard --ui-controls, which a pair run does not use")
    if not 1 <= args.storefront_port <= 65535:
        parser.error("Storefront port must be valid")
    from experiments.boutique.harness.multi_fault import run_pair
    directory = args.run_dir or ROOT / "data/experimentation/runs" / (
        "boutique-pair-" + now().strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:6])
    return run_pair(args, directory, tools=tool_versions(("kubectl", "kind", "docker", "helm")))


if __name__ == "__main__":
    # connected, chaos_recovery and multi_fault import this module by name; run with -m, only that module runs,
    # so BoutiqueExperiment and everything else are one class each (as scripts/test_boutique_live.py does).
    from experiments.boutique.harness import live as _canonical
    raise SystemExit(_canonical.main())
