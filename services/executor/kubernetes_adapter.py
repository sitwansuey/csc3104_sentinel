"""Restricted Deployment transport for the live executor (ROLLBACK, RESTART and SCALE).

One JSON Patch tests UID/resourceVersion, writes the exact Pod template chosen
by the action handler and writes action evidence together. A RESTART uses this
same patch; its template differs from the baseline only by the restart token. A SCALE (P5.07) is the same
tests, a replace of spec.replicas and the evidence, through the main deployments resource: RBAC needs only the
patch verb it already has, never the deployments/scale subresource. A fence is the same tests plus one metadata
annotation and nothing else; a Coordinator seal adds a second, its epoch. This module does not decide authorization.
The caller must inject fresh Guard validation and a restricted kubeconfig.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import subprocess
import tempfile

from services.executor.core import (
    ACTION_ANNOTATION, ConditionalWriteConflict, ReadUnavailable, TargetAbsent, encode_evidence,
)
from services.executor.reconciliation import (
    FENCE_ANNOTATION, FENCE_EPOCH_ANNOTATION, evidence_fields_valid, valid_fence_marker,
)
from services.executor.store import canonical_json, fingerprint
from services.guard.adapters import AdapterError, _DNS_LABEL, _DNS_SUBDOMAIN, _parse_json
from services.guard.preview import _private_file
from services.guard.scale import autoscalers_for


_TARGET_FIELDS = {"cluster_id", "namespace", "kind", "name", "uid"}
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_NONCE = re.compile(r"[a-f0-9]{32}\Z")
REQUEST_TIMEOUT_SECONDS = 10
PROCESS_TIMEOUT_SECONDS = 15


class LiveAdapterError(ValueError):
    """Invalid configuration or a response that cannot support execution."""


class WriteOutcomeUnknown(RuntimeError):
    """The write may have committed; only read-only reconciliation is safe."""


def _text(value, *, maximum=1024):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
            and not any(ord(character) < 32 for character in value))


def _target_shape(target, cluster_id):
    if not isinstance(target, dict) or set(target) != _TARGET_FIELDS:
        return False
    namespace, name = target["namespace"], target["name"]
    return (
        all(_text(value) for value in target.values())
        and target["cluster_id"] == cluster_id and target["kind"] == "Deployment"
        and len(namespace) <= 63 and _DNS_LABEL.fullmatch(namespace) is not None
        and len(name) <= 253 and _DNS_SUBDOMAIN.fullmatch(name) is not None
        and all(len(label) <= 63 and _DNS_LABEL.fullmatch(label) for label in name.split("."))
    )


def _identity(deployment, cluster_id):
    metadata = deployment["metadata"]
    return {"cluster_id": cluster_id, "kind": deployment["kind"],
            "namespace": metadata["namespace"], "name": metadata["name"], "uid": metadata["uid"]}


class KubectlDeploymentAPI:
    """Explicit-cluster, explicit-credential adapter with a trusted target list.

    The allowlist includes UID; read() returns a recreated object's actual UID so
    the executor can detect the identity conflict. There is no implicit current
    context, global environment mutation, arbitrary resource type or undo action.
    RBAC must separately restrict these credentials to the reviewed Deployment.
    """

    def __init__(self, *, context, cluster_id, kubeconfig, allowed_targets,
                 runner=subprocess.run, clock=None, kubectl="kubectl"):
        if (not _text(context) or not _text(cluster_id)
                or kubeconfig is None or not _text(str(kubeconfig), maximum=32768)
                or not _text(str(kubectl), maximum=32768)
                or not isinstance(allowed_targets, (list, tuple)) or not 1 <= len(allowed_targets) <= 128
                or not all(_target_shape(target, cluster_id) for target in allowed_targets)):
            raise LiveAdapterError("Explicit context, cluster, credentials and valid Deployment targets are required")
        self.context, self.cluster_id = context, cluster_id
        self.kubeconfig, self.kubectl = str(kubeconfig), str(kubectl)
        self.runner = runner
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._allowed = frozenset(canonical_json(target) for target in deepcopy(allowed_targets))
        self.stats = {"read_attempts": 0, "write_attempts": 0, "successful_writes": 0,
                      "fence_attempts": 0, "successful_fences": 0}
        # Lab test hook, None in every other run: a bound lab fault run's wrapper
        # (runtime/fault_points.py::CheckpointCluster) sets it to the fault point
        # executor.before_submit. It runs after the pre-read and local UID/RV check
        # of a template write and before its PATCH is sent; it changes neither.
        self.before_submit = None

    def _target(self, target):
        if not _target_shape(target, self.cluster_id) or canonical_json(target) not in self._allowed:
            raise LiveAdapterError("Target is outside the configured Deployment allowlist")

    def _command(self, target):
        return [self.kubectl, f"--context={self.context}", f"--kubeconfig={self.kubeconfig}",
                f"--namespace={target['namespace']}", f"--request-timeout={REQUEST_TIMEOUT_SECONDS}s"]

    def _snapshot(self, deployment, target):
        try:
            identity = _identity(deployment, self.cluster_id)
            metadata = deployment["metadata"]
            if (deployment["apiVersion"] != "apps/v1"
                    or any(identity[field] != target[field] for field in _TARGET_FIELDS - {"uid"})
                    or not _text(identity["uid"]) or not _text(metadata["resourceVersion"])
                    or not isinstance(deployment["spec"]["template"], dict)):
                raise ValueError("Invalid Deployment response")
            now = self.clock()
            if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
                raise ValueError("Clock must return UTC")
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveAdapterError("Kubernetes returned an invalid Deployment snapshot") from None
        return {"cluster_id": self.cluster_id,
                "captured_at": now.isoformat().replace("+00:00", "Z"), "deployment": deployment}

    def read(self, target):
        """Read only the allowlisted named Deployment, preserving its actual UID."""
        target = deepcopy(target)
        self._target(target)
        command = self._command(target) + ["get", "deployment", target["name"], "--ignore-not-found",
                                           "--output=json"]
        self.stats["read_attempts"] += 1
        try:
            result = self.runner(command, capture_output=True, check=False, timeout=PROCESS_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            raise ReadUnavailable("Live Deployment read is unavailable") from None
        if result.returncode:
            raise ReadUnavailable("Live Deployment read failed")
        # --ignore-not-found turns only NotFound into success with empty output.
        if isinstance(result.stdout, (bytes, str)) and not result.stdout.strip():
            raise TargetAbsent("Deployment does not exist")
        try:
            deployment = _parse_json(result.stdout, "executor_live_state")
        except (AdapterError, TypeError):
            raise LiveAdapterError("Kubernetes returned invalid Deployment JSON") from None
        return self._snapshot(deployment, target)

    def autoscalers(self, target):
        """The HorizontalPodAutoscalers in the target's namespace that scale this Deployment, by name.

        Read-only (list horizontalpodautoscalers.autoscaling); the Guard escalates a
        SCALE when any is found, because it would fight the write. A failed read
        raises ReadUnavailable, so the SCALE is refused with no write.
        """
        target = deepcopy(target)
        self._target(target)
        command = self._command(target) + ["get", "horizontalpodautoscalers.autoscaling", "--output=json"]
        self.stats["read_attempts"] += 1
        try:
            result = self.runner(command, capture_output=True, check=False, timeout=PROCESS_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            raise ReadUnavailable("Autoscaler read is unavailable") from None
        if result.returncode:
            raise ReadUnavailable("Autoscaler read failed")
        try:
            return autoscalers_for(_parse_json(result.stdout, "executor_autoscalers"), target["name"])
        except (AdapterError, TypeError, ValueError):
            raise LiveAdapterError("Kubernetes returned an invalid autoscaler list") from None

    def _evidence(self, target, version, template, evidence):
        try:
            valid = (
                _text(version) and isinstance(template, dict) and isinstance(template.get("spec"), dict)
                and isinstance(template["spec"].get("containers"), list) and bool(template["spec"]["containers"])
                and evidence_fields_valid(evidence)
                and _text(evidence["action_id"])
                and isinstance(evidence["proposal_sha256"], str) and _DIGEST.fullmatch(evidence["proposal_sha256"])
                and evidence["target_uid"] == target["uid"]
                and evidence["template_sha256"] == fingerprint(template)
                and isinstance(evidence["attempt_id"], str) and _NONCE.fullmatch(evidence["attempt_id"])
            )
        except (KeyError, TypeError, ValueError, RecursionError):
            valid = False
        if not valid:
            raise LiveAdapterError("Template, preconditions or action evidence are invalid")

    def conditional_restore(self, *, target, version, template, evidence):
        """Commit only the handler's template and atomic evidence under UID/RV tests.

        A local precondition mismatch is definitely a zero-write conflict. A
        kubectl error after PATCH submission stays unknown because its textual
        diagnostics do not reliably distinguish rejection from a lost response.
        No mutating request is ever retried here.
        """
        target, template, evidence = deepcopy(target), deepcopy(template), deepcopy(evidence)
        self._target(target)
        self._evidence(target, version, template, evidence)
        # Read before preparing the annotation operation. UID/RV tests below
        # still protect the complete patch if state changes after this read.
        try:
            snapshot = self.read(target)
        except TargetAbsent:
            raise ConditionalWriteConflict("Deployment no longer exists before submission") from None
        deployment = snapshot["deployment"]
        metadata = deployment["metadata"]
        if _identity(deployment, self.cluster_id) != target or metadata["resourceVersion"] != version:
            raise ConditionalWriteConflict("Deployment UID or resource version changed before submission")
        annotation = encode_evidence(evidence)
        patch = [{"op": "test", "path": "/metadata/uid", "value": target["uid"]},
                 {"op": "test", "path": "/metadata/resourceVersion", "value": version},
                 {"op": "replace", "path": "/spec/template", "value": template},
                 _annotation_operation(metadata, ACTION_ANNOTATION, annotation)]

        def confirms(admitted):
            return (admitted["metadata"].get("annotations", {}).get(ACTION_ANNOTATION) == annotation
                    and fingerprint(admitted["spec"]["template"]) == evidence["template_sha256"])
        if self.before_submit is not None:
            self.before_submit()  # Lab runs only; the patch above still tests the UID and V0 read here.
        applied = self._submit(target, version, patch, confirms, prefix="sentinel-execution-",
                               counter="write_attempts")
        self.stats["successful_writes"] += 1
        return applied

    def conditional_scale(self, *, target, version, replicas, evidence):
        """Commit only spec.replicas and atomic SCALE evidence under UID/RV tests.

        The evidence must be a SCALE's, name this replica count and the live
        template the read shows; the patch never touches the template. A local
        precondition mismatch is definitely a zero-write conflict; a kubectl error
        after submission stays unknown. No mutating request is ever retried here.
        """
        target, evidence = deepcopy(target), deepcopy(evidence)
        self._target(target)
        try:
            valid = (_text(version) and type(replicas) is int and 1 <= replicas <= 10
                     and evidence_fields_valid(evidence) and evidence.get("action") == "SCALE"
                     and evidence["replicas"] == replicas and _text(evidence["action_id"])
                     and isinstance(evidence["proposal_sha256"], str) and _DIGEST.fullmatch(evidence["proposal_sha256"])
                     and evidence["target_uid"] == target["uid"]
                     and isinstance(evidence["template_sha256"], str) and _DIGEST.fullmatch(evidence["template_sha256"])
                     and isinstance(evidence["attempt_id"], str) and _NONCE.fullmatch(evidence["attempt_id"]))
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise LiveAdapterError("Replica count, preconditions or action evidence are invalid")
        try:
            snapshot = self.read(target)
        except TargetAbsent:
            raise ConditionalWriteConflict("Deployment no longer exists before submission") from None
        deployment = snapshot["deployment"]
        metadata = deployment["metadata"]
        if _identity(deployment, self.cluster_id) != target or metadata["resourceVersion"] != version:
            raise ConditionalWriteConflict("Deployment UID or resource version changed before submission")
        if fingerprint(deployment["spec"]["template"]) != evidence["template_sha256"]:
            # Same object and version as the Guard checked, so the evidence itself is wrong: refuse, never send.
            raise LiveAdapterError("Action evidence names another Pod template than the live one")
        annotation = encode_evidence(evidence)
        patch = [{"op": "test", "path": "/metadata/uid", "value": target["uid"]},
                 {"op": "test", "path": "/metadata/resourceVersion", "value": version},
                 {"op": "replace", "path": "/spec/replicas", "value": replicas},
                 _annotation_operation(metadata, ACTION_ANNOTATION, annotation)]

        def confirms(admitted):
            return (admitted["metadata"].get("annotations", {}).get(ACTION_ANNOTATION) == annotation
                    and admitted["spec"].get("replicas") == replicas
                    and fingerprint(admitted["spec"]["template"]) == evidence["template_sha256"])
        applied = self._submit(target, version, patch, confirms, prefix="sentinel-execution-",
                               counter="write_attempts")
        self.stats["successful_writes"] += 1
        return applied

    def fence(self, *, target, version, marker):
        """Commit only this action's fence marker under the same UID/RV tests.

        The patch never touches spec, so it starts no rollout. If it commits,
        the version has moved past V0 and the action's write, which tests
        V0, can never commit. A local precondition mismatch is a zero-write
        conflict; any other failure is unknown. The fence is never retried here.
        """
        target, marker = deepcopy(target), deepcopy(marker)
        self._target(target)
        if not _text(version) or not valid_fence_marker(marker):
            raise LiveAdapterError("Fence preconditions or marker are invalid")
        try:
            snapshot = self.read(target)
        except TargetAbsent:
            raise ConditionalWriteConflict("Deployment no longer exists before the fence") from None
        metadata = snapshot["deployment"]["metadata"]
        if _identity(snapshot["deployment"], self.cluster_id) != target or metadata["resourceVersion"] != version:
            raise ConditionalWriteConflict("Deployment UID or resource version changed before the fence")
        values = {FENCE_ANNOTATION: canonical_json(marker)}
        if "epoch" in marker:  # A Coordinator seal (P3.04) also records its ownership epoch.
            values[FENCE_EPOCH_ANNOTATION] = str(marker["epoch"])
        patch = [{"op": "test", "path": "/metadata/uid", "value": target["uid"]},
                 {"op": "test", "path": "/metadata/resourceVersion", "value": version},
                 *_annotation_operations(metadata, values)]
        if any(not operation["path"].startswith("/metadata/") for operation in patch):
            raise LiveAdapterError("A fence may change metadata only")

        def confirms(admitted):
            annotations = admitted["metadata"].get("annotations", {})
            return all(annotations.get(key) == value for key, value in values.items())
        fenced = self._submit(target, version, patch, confirms, prefix="sentinel-fence-", counter="fence_attempts")
        self.stats["successful_fences"] += 1
        return fenced

    def _submit(self, target, version, patch, confirms, *, prefix, counter):
        """Send one conditional JSON Patch from a private file, exactly once.

        A kubectl error after submission stays unknown because its textual
        diagnostics do not reliably distinguish rejection from a lost response.
        """
        patch_path = None
        try:
            descriptor, path = tempfile.mkstemp(prefix=prefix, suffix=".json")
            patch_path = Path(path)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                _private_file(patch_path)
                stream.write(canonical_json(patch))
            command = self._command(target) + ["patch", "deployment", target["name"], "--type=json",
                                               "--patch-file", str(patch_path), "--output=json"]
            self.stats[counter] += 1
            try:
                result = self.runner(command, capture_output=True, check=False, timeout=PROCESS_TIMEOUT_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                raise WriteOutcomeUnknown("Live write response is unavailable; reconcile before any new mutation") from None
            if result.returncode:
                raise WriteOutcomeUnknown("Live write returned an error; reconcile its outcome")
            return self._confirmed(result.stdout, target, version, confirms)
        finally:
            if patch_path is not None:
                try:
                    patch_path.unlink(missing_ok=True)
                except OSError:
                    raise WriteOutcomeUnknown("Private execution patch could not be cleaned up") from None

    def _confirmed(self, body, target, version, confirms):
        """The snapshot from a success response, only if it shows exactly the requested change.

        Shared by every transport: anything else is WriteOutcomeUnknown, so a fresh read decides.
        """
        try:
            admitted = _parse_json(body, "executor_write_response")
            applied = self._snapshot(admitted, target)
            valid = (_identity(admitted, self.cluster_id) == target
                     and admitted["metadata"]["resourceVersion"] != version and confirms(admitted))
        except (AdapterError, LiveAdapterError, AttributeError, KeyError, TypeError, ValueError, RecursionError):
            raise WriteOutcomeUnknown("Live write response cannot confirm the exact requested state") from None
        if not valid:
            raise WriteOutcomeUnknown("Live write response does not match the requested change")
        return applied


def _annotation_operation(metadata, key, value):
    """Add one annotation, keeping every other annotation on the object."""
    annotations = metadata.get("annotations")
    if annotations is None:
        return {"op": "add", "path": "/metadata/annotations", "value": {key: value}}
    if not isinstance(annotations, dict):
        raise LiveAdapterError("Deployment annotations are invalid")
    return {"op": "add", "path": "/metadata/annotations/" + key.replace("~", "~0").replace("/", "~1"), "value": value}


def _annotation_operations(metadata, values):
    """Add several annotations in one patch, keeping every other annotation on the object."""
    if metadata.get("annotations") is None:
        return [{"op": "add", "path": "/metadata/annotations", "value": dict(values)}]
    return [_annotation_operation(metadata, key, value) for key, value in values.items()]
