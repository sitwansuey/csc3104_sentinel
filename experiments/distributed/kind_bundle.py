"""Prepare the kind lab for run_scenarios --mode kind, and inject and restore its fault per run.

python -m experiments.distributed.kind_bundle prepare --context kind-sentinel-boutique-connected \
    --storefront http://127.0.0.1:18631 [--scenario R1] [--bundle DIR] [--token-duration 2h]
python -m experiments.distributed.kind_bundle restore --context kind-sentinel-boutique-connected --bundle DIR

The operator runs prepare once per session, with a port-forward to the
storefront (kubectl port-forward service/frontend 18631:80) already open, then
passes the printed bundle directory to run_scenarios --kind-bundle. Kind mode
then calls KindLab.inject before each run and KindLab.restore after it, so
--repeat and several scenarios in one invocation each get a fresh fault and a
verified baseline in between.

Only an allowlisted lab context is accepted (LABS, which must also be in
topology.KIND_CONTEXTS; P4.01 adds its multi-node lab there), and any context
naming sentinel-lab is refused first. The catalog scenario (R1 by default:
shippingservice PORT=invalid-port) must list that context's scope, and its
fault must be a template change (set_env or set_image_digest).

What prepare reads, with the operator's own kubeconfig and --context (never
another context):
  - deployment/<target> and deployment/frontend in the lab namespace: the
    target must be Available, pinned by digest to the catalog repository, and
    keep one template over a healthy window (4 reads 10 s apart, each also
    needing HTTP 200 from <storefront>/cart);
  - kubectl config view --minify --flatten --raw for that context: only the
    API server URL (which must be HTTPS on loopback, as kind's is) and the
    cluster CA are kept; the operator's own client credentials are never
    written anywhere;
  - kubectl auth can-i with the new restricted kubeconfig (below).
What it creates on the cluster: one TokenRequest (kubectl create token) for
the existing ServiceAccount sentinel-executor, valid for --token-duration (at
most 8 h). No Deployment, Role, binding or namespace is created or changed.
What it writes, into a new directory outside the repository (a fresh
temporary directory unless --bundle names another new one):
  baseline.json, baseline-health.json, policy.json, allowed-targets.json,
  verifier-inventory.json, lab.json and rbac-checks.json, which hold no
  credential, and kubeconfig: that token, the CA and the server, with one
  context named after the lab context, readable by the owner only. Delete the
  directory when the session ends; the token also expires by itself.
  lab.json also keeps the target's annotations from the last window read,
  which the restore returns Sentinel's annotations to.
The printed summary names the files and the expiry, never the token.

KindLab, used by run_scenarios, writes with the operator's kubeconfig (an
operator action, never the restricted Executor identity): the catalog
injection before a run, and after a run a JSON patch that puts the baseline
template back (tests on uid and resourceVersion) when Sentinel did not. Each
is recorded in the run's summary.json as an operator action, never as a
Sentinel write, and it waits until the target is Available with the baseline
template and the storefront answers 200 before the next run. Once the
injection has been sent, any failure (a kubectl read or timeout, a target
that changed identity, a fault that never shows, an interrupt) is followed
by a restore, and the error says whether that restore reached a verified
healthy baseline; when it did not, the fault may still be in place and the
error names the restore command.

After the template, the restore returns Sentinel's own annotations on the
target (sentinel.io/execution, sentinel.io/fence, sentinel.io/fence-epoch;
SENTINEL_ANNOTATIONS) to what prepare recorded in lab.json: one metadata-only
JSON patch that tests the uid and resourceVersion it read, and no write when
nothing differs. It never touches another annotation: if any other one
(except the controller's deployment.kubernetes.io/revision) changed since
prepare, it writes nothing to the annotations, records the refusal with the
keys that changed, and the restore is not healthy. Resetting fence-epoch is a
lab reset between runs only, never part of normal operation.

restore reads lab.json and baseline.json from the bundle and does what
KindLab.restore does after a run, with the operator's kubeconfig (the
bundle's token is never used, so an expired bundle can still restore). It
prints the record and exits 0 only when the target is back at a healthy
baseline.
"""
import argparse
from datetime import UTC, datetime, timedelta
from http.client import HTTPException
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

from experiments.distributed.topology import KIND_CONTEXTS, OBSERVER_FAILURE_SECONDS, ROOT
from services.executor.reconciliation import ACTION_ANNOTATION, FENCE_ANNOTATION, FENCE_EPOCH_ANNOTATION

# Allowlisted lab contexts and their cluster and namespace. An entry is needed
# here and in topology.KIND_CONTEXTS, and the catalog needs a scope for it.
LABS = {
    "kind-sentinel-boutique-connected": {"cluster_id": "sentinel-boutique-connected",
                                         "namespace": "sentinel-boutique-connected"},
}
FORBIDDEN = "sentinel-lab"
EXECUTOR_ACCOUNT = "sentinel-executor"
SCHEMA = "sentinel-kind-bundle-v1"
# The strict live profile that passed 12 of 12 kind-mode runs on 12 Nov 2026. Its first
# version (3 samples, a 4 s stable window, a 10 s gap, a 60 s timeout) failed I3: the
# Coordinator's retry backoff spaced the live Verifier's one-sample passes beyond 10 s.
# The Verifier now samples within one pass every min(gap / 2, window / (samples - 1)) s,
# 10 s here (services/verifier/store.py::sampling_interval), which leaves 10 s of the 20 s
# gap for a slow reading or a quick Verifier restart; a target healthy by about +40 s is
# then confirmed near +50 s, inside the 60 s grace, so the values below are unchanged.
POLICY_VERSION = "distributed-kind-lab-2"
HEALTH = {"protocol": "http", "required_http_status": 200, "minimum_samples": 2, "stable_window_seconds": 10,
          "max_sample_gap_seconds": 20, "require_target_identity": True, "require_template_match": True,
          "require_available_replicas": True, "convergence_grace_seconds": 60, "verification_timeout_seconds": 90}
TEMPLATE_FAULTS = ("set_env", "set_image_digest")
HEALTH_PATH = "/cart"  # Calls cartservice and shippingservice; the catalog's R1 and C1 impact says it fails.
MAX_TOKEN_MINUTES = 480
# A bundle whose token expires sooner than this is refused for a new run.
MIN_TOKEN_LEFT = timedelta(minutes=10)
RESTORE_HINT = ("restore it with python -m experiments.distributed.kind_bundle restore --context {context} "
                "--bundle <the bundle directory>")
RBAC_CHECKS = (("get", "deployment/{target}", "yes"), ("patch", "deployment/{target}", "yes"),
               ("delete", "deployment/{target}", "no"), ("patch", "deployment/paymentservice", "no"),
               ("get", "secrets", "no"), ("list", "pods", "no"))
# The annotations Sentinel writes on a target (the Executor's evidence, its fence and a seal's epoch).
# The restore puts these, and only these, back to their prepared values.
SENTINEL_ANNOTATIONS = (ACTION_ANNOTATION, FENCE_ANNOTATION, FENCE_EPOCH_ANNOTATION)
# Set by the Deployment controller on every template rollout, so it is expected to differ after a run.
CONTROLLER_ANNOTATIONS = frozenset({"deployment.kubernetes.io/revision"})


def annotation_path(key):
    """The JSON pointer to one metadata annotation (RFC 6901: ~ is ~0, / is ~1)."""
    return "/metadata/annotations/" + key.replace("~", "~0").replace("/", "~1")


class LabError(RuntimeError):
    """The lab, the bundle or the workload is not in the state a kind-mode run needs."""


def utc_now():
    return datetime.now(UTC)


def stamp(moment):
    return moment.isoformat().replace("+00:00", "Z")


def lab_scope(context):
    """The allowlisted lab for context; LabError for sentinel-lab and anything not allowlisted."""
    if not isinstance(context, str) or FORBIDDEN in context:
        raise LabError("The kind lab tooling never targets " + FORBIDDEN)
    if context not in LABS or context not in KIND_CONTEXTS:
        raise LabError("Kind mode runs only against an allowlisted lab context; this context is refused")
    return {"context": context, **LABS[context]}


def storefront_origin(url):
    """A loopback http(s) origin without credentials, path or query (the operator's port-forward)."""
    parts = urlsplit(url if isinstance(url, str) else "")
    if (parts.scheme not in {"http", "https"} or parts.hostname not in {"127.0.0.1", "localhost"}
            or parts.username is not None or parts.password is not None or parts.path not in {"", "/"}
            or parts.query or parts.fragment or parts.port is None):
        raise LabError("The storefront must be a loopback origin with a port, such as http://127.0.0.1:18631")
    return f"{parts.scheme}://{parts.hostname}:{parts.port}"


def token_minutes(duration):
    """Minutes in a kubectl --duration such as 90m or 2h; at most eight hours."""
    match = re.fullmatch(r"([1-9][0-9]{0,2})([mh])", duration or "")
    minutes = match and int(match[1]) * (60 if match[2] == "h" else 1)
    if not minutes or minutes > MAX_TOKEN_MINUTES:
        raise LabError("The token duration must be like 90m or 2h and at most 8h")
    return minutes


def http_status(url, timeout=8):
    """The HTTP status of a GET, or None when nothing answered. Never through a proxy."""
    try:
        with build_opener(ProxyHandler({})).open(url, timeout=timeout) as response:
            return response.status
    except HTTPError as error:
        return error.code
    except (URLError, OSError, HTTPException):  # HTTPException: a reply that is not HTTP (port-forward hiccup).
        return None


def kubectl(runner, scope, *args):
    """kubectl with the operator's kubeconfig, pinned to the lab's --context and namespace."""
    command = ["kubectl", "--context=" + scope["context"], "--namespace=" + scope["namespace"],
               "--request-timeout=30s", *args]
    try:
        result = runner(command, capture_output=True, text=True, timeout=60, check=False)
    except (subprocess.SubprocessError, OSError) as error:  # A timeout or a missing kubectl.
        raise LabError(f"kubectl {args[0]} did not complete ({type(error).__name__})") from error
    if result.returncode:
        raise LabError(f"kubectl {args[0]} failed")  # Its error text may echo cluster details; not kept.
    return result


def catalog_fault(scenario_id, scope):
    """The catalog scenario, checked to be a template fault allowed in this lab's scope."""
    from experiments.boutique.harness import scenarios
    try:
        item = scenarios.scenario(scenario_id)
        scenarios.require_scope(item, cluster_id=scope["cluster_id"], context=scope["context"],
                                namespace=scope["namespace"])
    except scenarios.CatalogError as error:
        raise LabError(str(error)) from error
    if item["injection"]["kind"] not in TEMPLATE_FAULTS or item["restore"]["kind"] != "template_baseline":
        raise LabError(f"Scenario {scenario_id} is not a template fault; kind mode injects set_env or "
                       "set_image_digest faults only")
    return item


class KindLab:
    """The operator's side of one lab target: read it, inject the catalog fault, restore the baseline.

    Commands run with the operator's kubeconfig and the allowlisted --context
    and namespace only; runner, status, sleep and monotonic are injectable so
    tests drive it offline.
    """

    def __init__(self, context, target, baseline_template, fault, storefront, *, prepared_annotations=None,
                 runner=subprocess.run, status=http_status, sleep=time.sleep, monotonic=time.monotonic):
        """prepared_annotations: the target's annotations when prepare read it (None: not recorded)."""
        self.scope = lab_scope(context)
        if target.get("cluster_id") != self.scope["cluster_id"] or target.get("namespace") != self.scope["namespace"]:
            raise LabError("The bundle target is not in the allowlisted lab")
        if fault["target"] != target.get("name"):
            raise LabError("The catalog scenario targets another workload")
        if prepared_annotations is not None and not (
                isinstance(prepared_annotations, dict)
                and all(isinstance(key, str) and isinstance(value, str)
                        for key, value in prepared_annotations.items())):
            raise LabError("The bundle's prepared annotations are not a map of strings")
        self.target, self.baseline_template, self.fault = dict(target), baseline_template, fault
        self.prepared_annotations = None if prepared_annotations is None else dict(prepared_annotations)
        self.health_url = storefront_origin(storefront) + HEALTH_PATH
        self.runner, self.status, self.sleep, self.monotonic = runner, status, sleep, monotonic

    @classmethod
    def from_bundle(cls, bundle, context, *, now=utc_now, check_token=True, **options):
        """The lab a prepared bundle names; LabError for another context, an old bundle or an expiring token.

        check_token False (the restore command) accepts an expiring token: the
        operator side writes with the operator's kubeconfig, never with it.
        """
        bundle = Path(bundle)
        lab_scope(context)
        try:
            lab = json.loads((bundle / "lab.json").read_text(encoding="utf-8"))
            baseline = json.loads((bundle / "baseline.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise LabError("The bundle needs lab.json and baseline.json; prepare it with "
                           "python -m experiments.distributed.kind_bundle prepare") from error
        if lab.get("schema") != SCHEMA or lab.get("context") != context:
            raise LabError("The bundle was prepared for another context or by another tool")
        if baseline.get("target") != lab.get("target"):
            raise LabError("The bundle's baseline and lab target differ")
        try:
            expires = datetime.fromisoformat(str(lab["kubeconfig_expires_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError) as error:
            raise LabError("The bundle does not record when its token expires") from error
        if check_token and expires - now() < MIN_TOKEN_LEFT:
            raise LabError("The bundle's Executor token expires within 10 minutes; prepare a new bundle")
        fault = catalog_fault(lab["fault_scenario"], lab_scope(context))
        return cls(context, lab["target"], baseline["pod_template"], fault, lab["storefront"],
                   prepared_annotations=lab.get("target_annotations"), **options)

    # Reads ----------------------------------------------------------------

    def kubectl(self, *args):
        return kubectl(self.runner, self.scope, *args)

    def deployment(self):
        try:
            item = json.loads(self.kubectl("get", "deployment", self.target["name"], "--output=json").stdout)
            uid, _ = item["metadata"]["uid"], item["spec"]["template"]
        except (ValueError, KeyError, TypeError) as error:
            raise LabError("kubectl get returned no Deployment") from error
        if uid != self.target["uid"]:
            raise LabError("The lab Deployment is not the bundle's target (deleted or recreated)")
        return item

    def state(self, item):
        """What a run record needs from one read: identity-free counters and the template check."""
        from services.executor.store import fingerprint
        metadata, status = item["metadata"], item.get("status") or {}
        return {"generation": metadata.get("generation"), "observed_generation": status.get("observedGeneration"),
                "resource_version": metadata.get("resourceVersion"),
                "desired": item["spec"].get("replicas", 1), "available": status.get("availableReplicas", 0),
                "updated": status.get("updatedReplicas", 0),
                "template_is_baseline": item["spec"]["template"] == self.baseline_template,
                "template_sha256": fingerprint(item["spec"]["template"])}

    def _healthy(self, item):
        state = self.state(item)
        settled = (state["observed_generation"] or 0) >= (state["generation"] or 0)
        return (state["template_is_baseline"] and settled and state["available"] >= state["desired"]
                and state["updated"] >= state["desired"] and self.status(self.health_url) == 200)

    def _until(self, check, timeout, interval=2):
        deadline = self.monotonic() + timeout
        while True:
            item = self.deployment()
            if check(item):
                return item, True
            if self.monotonic() >= deadline:
                return item, False
            self.sleep(interval)

    # Operator writes --------------------------------------------------------

    def off_baseline(self, what):
        """The error for a target that may be left off its baseline: it says so and names the restore command."""
        return LabError(f"{what}; the target is not at a verified healthy baseline and the fault may still be "
                        "in place: " + RESTORE_HINT.format(context=self.scope["context"]))

    def restore_or_record(self, **options):
        """restore(), but a read or write that fails becomes a record with healthy False and the error."""
        try:
            return self.restore(**options)
        except LabError as error:
            return {"operator_restore": None, "healthy": False, "error": str(error)}

    def inject(self, *, healthy_timeout=120, visible_timeout=90):
        """Inject the catalog fault at a healthy baseline and wait until it shows; LabError otherwise.

        From the moment the injection is sent, a fault that does not show
        within visible_timeout and any failure (a kubectl read or timeout,
        another uid, an interrupt) are followed by a restore, so no run
        starts on a workload in an unknown state. The error says whether the
        restore reached a verified healthy baseline (off_baseline when not).
        """
        _, healthy = self._until(self._healthy, healthy_timeout)
        if not healthy:
            raise LabError("The target is not at its healthy baseline before injection; restore it first")
        from experiments.boutique.harness import scenarios
        injection = self.fault["injection"]
        if injection["kind"] == "set_env":
            argv = scenarios.set_env_argv(self.target["name"], injection["container"], injection["env"],
                                          injection["value"])
        else:
            repository = self.fault["target_profile"]["image_repository"]
            argv = scenarios.set_image_argv(self.target["name"], injection["container"],
                                            scenarios.pinned_image(repository, injection["digest"]), repository)
        started, at = self.monotonic(), stamp(utc_now())
        record = {"scenario": self.fault["id"], "injection": dict(injection), "injected_at": at}

        def visible(item):
            state = self.state(item)
            record["health_http_status"] = self.status(self.health_url)
            return (not state["template_is_baseline"] and state["available"] == 0
                    and (state["observed_generation"] or 0) >= (state["generation"] or 0)
                    and record["health_http_status"] not in (200, None))
        try:
            self.kubectl(*argv)  # A timeout here may still have written: the restore below covers that too.
            item, shown = self._until(visible, visible_timeout, interval=1)
            record.update(fault_visible=shown, visible_after_seconds=round(self.monotonic() - started, 1),
                          state=self.state(item))
        except BaseException as caught:
            cause = str(caught) if isinstance(caught, LabError) else type(caught).__name__
            if self.restore_or_record()["healthy"]:
                raise LabError(f"The fault injection stopped ({cause}); the baseline was restored and verified "
                               "healthy") from caught
            raise self.off_baseline(f"The fault injection stopped ({cause}) and the restore did not reach a "
                                    "healthy baseline") from caught
        if not shown:
            record["restore"] = self.restore_or_record()
            if record["restore"]["healthy"]:
                raise LabError("The injected fault did not show within the timeout; the baseline was restored and "
                               "verified healthy")
            raise self.off_baseline("The injected fault did not show within the timeout and the restore did not "
                                    "reach a healthy baseline")
        return record

    def restore(self, *, timeout=240, attempts=3):
        """Put the baseline template back if Sentinel did not, then wait until healthy.

        The patch tests the uid and resourceVersion it read, so it never
        overwrites a change it did not see; a refused test reads again.
        Returns the record: operator_restore says whether this wrote.
        """
        started = self.monotonic()
        item = self.deployment()
        record = {"before": self.state(item), "operator_restore": False}
        for _ in range(attempts):
            if item["spec"]["template"] == self.baseline_template:
                break
            patch = [{"op": "test", "path": "/metadata/uid", "value": item["metadata"]["uid"]},
                     {"op": "test", "path": "/metadata/resourceVersion", "value": item["metadata"]["resourceVersion"]},
                     {"op": "replace", "path": "/spec/template", "value": self.baseline_template}]
            try:
                self.kubectl("patch", "deployment", self.target["name"], "--type=json", "--patch=" + json.dumps(patch))
            except LabError:
                item = self.deployment()
                continue
            record.update(operator_restore=True, restored_at=stamp(utc_now()))
            break
        record["annotations"] = self.restore_annotations(attempts=attempts)
        item, healthy = self._until(self._healthy, timeout)
        settled = record["annotations"]["status"] in {"unchanged", "reset", "not recorded"}
        record.update(healthy=healthy and settled, seconds=round(self.monotonic() - started, 1),
                      after=self.state(item))
        if not settled:
            record["error"] = record["annotations"]["error"]
        return record

    def annotation_changes(self, item):
        """What returns Sentinel's annotations on item to their prepared values.

        Returns (operations, changes, foreign): the JSON patch operations, the
        record of what they remove, reset or add (with the values), and the
        sorted keys of any other annotation that differs from prepare (the
        controller's revision excepted), in which case nothing may be written.
        """
        prepared = self.prepared_annotations or {}
        current = item["metadata"].get("annotations") or {}
        foreign = sorted(key for key in set(current) | set(prepared)
                         if key not in SENTINEL_ANNOTATIONS and key not in CONTROLLER_ANNOTATIONS
                         and current.get(key) != prepared.get(key))
        operations, changes = [], {"removed": {}, "reset": {}, "added": {}}
        for key in SENTINEL_ANNOTATIONS:
            was, now = prepared.get(key), current.get(key)
            if was == now:
                continue
            if was is None:
                changes["removed"][key] = now
                operations.append({"op": "remove", "path": annotation_path(key)})
            elif now is None:
                changes["added"][key] = was
                operations.append({"op": "add", "path": annotation_path(key), "value": was})
            else:
                changes["reset"][key] = {"from": now, "to": was}
                operations.append({"op": "replace", "path": annotation_path(key), "value": was})
        if operations and not current:  # No annotations map to add a key to: add the map itself.
            operations = [{"op": "add", "path": "/metadata/annotations", "value": dict(changes["added"])}]
        return operations, changes, foreign

    def restore_annotations(self, *, attempts=3):
        """Return Sentinel's annotations to their prepared values in one metadata-only patch; the record.

        The patch tests the uid and resourceVersion it read, so it never
        overwrites a change it did not see; a refused patch (a 409 or a failed
        test) reads again, up to attempts times. Nothing is written when
        nothing differs, or when an annotation that is not Sentinel's changed
        since prepare (status refused). This is a lab reset between runs,
        never part of normal operation: lowering sentinel.io/fence-epoch
        undoes a seal.
        """
        if self.prepared_annotations is None:
            return {"status": "not recorded", "note": "The bundle does not record the target's annotations "
                    "(prepared before the annotation reset); Sentinel's annotations were left as they are"}
        for _ in range(attempts):
            item = self.deployment()
            operations, changes, foreign = self.annotation_changes(item)
            if foreign:
                return {"status": "refused", "foreign_changed": foreign,
                        "error": "Annotations that are not Sentinel's changed since prepare (" + ", ".join(foreign)
                                 + "); the restore writes no annotation then. Inspect the target and prepare a "
                                   "new bundle"}
            if not operations:
                return {"status": "unchanged"}
            patch = [{"op": "test", "path": "/metadata/uid", "value": item["metadata"]["uid"]},
                     {"op": "test", "path": "/metadata/resourceVersion", "value": item["metadata"]["resourceVersion"]},
                     *operations]
            try:
                self.kubectl("patch", "deployment", self.target["name"], "--type=json", "--patch=" + json.dumps(patch))
            except LabError:
                continue  # A conflict or a failed test: someone committed since the read. Read again.
            return {"status": "reset", "reset_at": stamp(utc_now()),
                    **{name: values for name, values in changes.items() if values}}
        return {"status": "failed", "error": f"The conditional annotation patch was refused {attempts} times; "
                                             "Sentinel's annotations are not at their prepared values"}


# Preparation --------------------------------------------------------------

def bundle_directory(path):
    """A new directory outside the repository: path, or a fresh one in the temporary directory.

    The temporary directory is checked too: TMP or TEMP inside the checkout is refused.
    """
    fresh = path is None
    path = Path(tempfile.gettempdir() if fresh else path).resolve()
    if path == ROOT or ROOT in path.parents:
        raise LabError("The bundle holds a credential; write it outside the repository")
    if fresh:
        return Path(tempfile.mkdtemp(prefix="sentinel-kind-bundle-", dir=path))
    path.mkdir(parents=True, exist_ok=False)
    return path


def _private_json(path, value):
    from services.guard.preview import _private_file
    with path.open("x", encoding="utf-8") as stream:
        _private_file(path)  # Owner-only before the token is written.
        json.dump(value, stream, indent=2)


def _json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def policy(target, profile):
    """The strict live profile for one lab target (as experiments/recovery/live.py builds it)."""
    from experiments.recovery.live import policy_ceilings
    return {"policy_id": "distributed-kind-lab", "policy_version": POLICY_VERSION, "profile": "live",
            "targets": [target], "allowed_actions": ["ROLLBACK"], "require_operator_approval": True,
            "unknown_dependency_result": "ESCALATE", "max_state_age_seconds": 30,
            "max_dependency_age_seconds": 300, "decision_ttl_seconds": 300, "max_approval_lifetime_seconds": 300,
            "require_server_dry_run": False,
            "baseline_constraints": {"max_age_seconds": 14400, "min_healthy_duration_seconds": 30,
                                     "trusted_sources": [target["cluster_id"] + "-storefront-http"],
                                     "require_health_evidence": True, "require_compatible_references": True,
                                     "require_image_digests": True},
            "security_constraints": {"deny_privileged": True, "deny_host_access": True,
                                     "allowed_image_registries": [profile.registry],
                                     "allowed_service_accounts": [profile.service_account],
                                     "forbid_service_account_change": True},
            **policy_ceilings(profile.ceilings),
            # The harness gives the Observer this window too (topology.OBSERVER_FAILURE_SECONDS).
            "detection": {"failure_duration_seconds": OBSERVER_FAILURE_SECONDS},
            "health": dict(HEALTH),
            "note": "Kind-mode lab bundle from experiments/distributed/kind_bundle.py. Health is the storefront "
                    "page over a loopback port-forward plus replica readiness."}


def prepare(context, storefront, *, scenario_id="R1", bundle=None, token_duration="2h", runner=subprocess.run,
            status=http_status, sleep=time.sleep, now=utc_now, samples=4, spacing=10):
    """Write a kind bundle (module docstring); return the printable summary, which holds no credential."""
    from experiments.boutique.harness import scenarios, targets
    from services.executor.store import fingerprint
    scope = lab_scope(context)
    origin = storefront_origin(storefront)
    minutes = token_minutes(token_duration)
    fault = catalog_fault(scenario_id, scope)
    profile = targets.profile(fault["target"])

    def read(name):
        return json.loads(kubectl(runner, scope, "get", "deployment", name, "--output=json").stdout)

    first = read(profile.name)
    target = {"cluster_id": scope["cluster_id"], "namespace": scope["namespace"], "kind": "Deployment",
              "name": profile.name, "uid": first["metadata"]["uid"]}
    template = first["spec"]["template"]
    try:
        scenarios.image_digest(scenarios.container_image(template, profile.container), profile.image_repository)
    except scenarios.CatalogError as error:
        raise LabError("The target image must be the catalog repository pinned by digest") from error
    window = []
    for index in range(samples):
        current = first if index == 0 else read(profile.name)
        state = current.get("status") or {}
        desired = current["spec"].get("replicas", 1)
        sample = {"at": stamp(now()), "template_sha256": fingerprint(current["spec"]["template"]),
                  "ready_replicas": state.get("readyReplicas", 0),
                  "available_replicas": state.get("availableReplicas", 0), "desired_replicas": desired,
                  "health_http_status": status(origin + HEALTH_PATH),
                  "resource_version": current["metadata"]["resourceVersion"]}
        window.append(sample)
        if (current["spec"]["template"] != template or current["metadata"]["uid"] != target["uid"]
                or sample["ready_replicas"] != desired or sample["available_replicas"] != desired
                or sample["health_http_status"] != 200):
            raise LabError("The target was not healthy and unchanged during the baseline window")
        if index < samples - 1:
            sleep(spacing)
    view = json.loads(kubectl(runner, scope, "config", "view", "--minify", "--flatten", "--raw",
                              "--output=json").stdout)
    cluster = view["clusters"][0]["cluster"]  # Only the server and CA are read; the user entry is ignored.
    server = urlsplit(cluster.get("server", ""))
    if server.scheme != "https" or server.hostname not in {"127.0.0.1", "localhost"} \
            or "certificate-authority-data" not in cluster:
        raise LabError("The lab API server must be HTTPS on loopback with its CA in the kubeconfig")
    frontend = read("frontend")
    annotations = dict(current["metadata"].get("annotations") or {})
    directory = bundle_directory(bundle)
    digest = fingerprint(template)
    baseline = {"baseline_id": "BASE-KIND-" + uuid4().hex[:12], "target": target,
                "healthy_observed_at": window[-1]["at"], "pod_template": template, "template_sha256": digest,
                "reference_versions": [],
                "health_evidence": {"source": target["cluster_id"] + "-storefront-http",
                                    "evidence_id": "HEALTH-" + uuid4().hex, "status": "HEALTHY", "target": target,
                                    "template_sha256": digest, "window_start": window[0]["at"],
                                    "window_end": window[-1]["at"]}}
    frontend_target = dict(target, name="frontend", uid=frontend["metadata"]["uid"])
    expires = now() + timedelta(minutes=minutes)
    files = {"baseline.json": baseline, "baseline-health.json": window, "policy.json": policy(target, profile),
             "allowed-targets.json": [target],
             "verifier-inventory.json": {
                 "cluster_id": target["cluster_id"],
                 "services": {target["name"]: {"target": target, "health_url": origin + HEALTH_PATH},
                              "frontend": {"target": frontend_target, "health_url": origin + "/_healthz"}},
                 "declared_edges": [{"from": "frontend", "to": target["name"]}],
                 "dependency_inventory_complete": True, "require_traces": False, "max_collection_seconds": 20,
                 "max_health_age_seconds": 10, "max_trace_age_seconds": 300},
             "lab.json": {"schema": SCHEMA, "context": context, "cluster_id": target["cluster_id"],
                          "namespace": target["namespace"], "target": target, "fault_scenario": fault["id"],
                          "storefront": origin, "health_path": HEALTH_PATH, "prepared_at": stamp(now()),
                          "executor_service_account": EXECUTOR_ACCOUNT, "kubeconfig_expires_at": stamp(expires),
                          "observer_failure_window_seconds": OBSERVER_FAILURE_SECONDS,
                          # The restore returns Sentinel's annotations to these values (the last window read).
                          "target_annotations": annotations}}
    for name, value in files.items():
        _json(directory / name, value)
    kubeconfig = directory / "kubeconfig"
    try:
        token = kubectl(runner, scope, "create", "token", EXECUTOR_ACCOUNT,
                        "--duration=" + token_duration).stdout.strip()
        if not token or any(char.isspace() for char in token):
            raise LabError("The token request returned no token")
        _private_json(kubeconfig, {
            "apiVersion": "v1", "kind": "Config",
            "clusters": [{"name": target["cluster_id"], "cluster": {
                "server": cluster["server"], "certificate-authority-data": cluster["certificate-authority-data"]}}],
            "users": [{"name": EXECUTOR_ACCOUNT, "user": {"token": token}}],
            "contexts": [{"name": context, "context": {"cluster": target["cluster_id"], "user": EXECUTOR_ACCOUNT,
                                                       "namespace": target["namespace"]}}],
            "current-context": context})
        checks = []
        for verb, resource, expected in RBAC_CHECKS:
            resource = resource.format(target=target["name"])
            answer = runner(["kubectl", "--context=" + context, "--kubeconfig=" + str(kubeconfig),
                             "--namespace=" + target["namespace"], "--request-timeout=30s", "auth", "can-i", verb,
                             resource], capture_output=True, text=True, timeout=60, check=False).stdout.strip()
            checks.append({"verb": verb, "resource": resource, "answer": answer, "expected": expected})
            if answer != expected:
                raise LabError(f"The restricted identity may {verb} {resource}: answered {answer!r}, expected "
                               f"{expected!r}")
    except BaseException:
        kubeconfig.unlink(missing_ok=True)  # Never leave a credential behind a failed preparation.
        raise
    _json(directory / "rbac-checks.json", checks)
    return {"bundle": str(directory), "context": context, "target": target, "fault_scenario": fault["id"],
            "template_sha256": digest, "baseline_id": baseline["baseline_id"], "policy_version": POLICY_VERSION,
            "kubeconfig_expires_at": stamp(expires), "rbac": checks,
            "sentinel_annotations_at_prepare": sorted(key for key in SENTINEL_ANNOTATIONS if key in annotations),
            "files": sorted([*files, "kubeconfig", "rbac-checks.json"])}


def restore_command(args, lab_options):
    """The restore subcommand: put the bundle's baseline back; 0 only when it is verified healthy."""
    try:
        lab = KindLab.from_bundle(args.bundle, args.context, check_token=False, **lab_options)
    except LabError as error:
        print(json.dumps({"status": "failed", "error": str(error)}), flush=True)
        return 1
    record = lab.restore_or_record()
    print(json.dumps({"status": "healthy" if record["healthy"] else "not healthy", **record}, indent=1), flush=True)
    return 0 if record["healthy"] else 1


def main(argv=None, lab_options=None):
    """lab_options: KindLab runner, status, sleep and monotonic for the restore command (tests only)."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("prepare", help="Measure the baseline and write a bundle with a short-lived token")
    make.add_argument("--context", required=True, help="An allowlisted lab context")
    make.add_argument("--storefront", required=True, help="Loopback origin of a port-forward to service/frontend")
    make.add_argument("--scenario", default="R1", help="Catalog fault injected before each run (default R1)")
    make.add_argument("--bundle", type=Path, help="A new directory outside the repository (default: a temporary one)")
    make.add_argument("--token-duration", default="2h", help="Executor token lifetime, such as 90m or 2h (at most 8h)")
    fix = commands.add_parser("restore", help="Put the bundle's baseline template back with your kubeconfig and "
                                              "wait until the target is healthy")
    fix.add_argument("--context", required=True, help="The bundle's allowlisted lab context")
    fix.add_argument("--bundle", required=True, type=Path, help="The bundle directory prepare printed")
    args = parser.parse_args(argv)
    try:
        lab_scope(args.context)
        if args.command == "prepare":
            storefront_origin(args.storefront)
            token_minutes(args.token_duration)
    except LabError as error:
        parser.error(str(error))
    if args.command == "restore":
        return restore_command(args, lab_options or {})
    try:
        summary = prepare(args.context, args.storefront, scenario_id=args.scenario, bundle=args.bundle,
                          token_duration=args.token_duration)
    except (LabError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__,
                          "error": str(error) if isinstance(error, LabError) else None}), flush=True)
        return 1
    print(json.dumps(summary, indent=1), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
