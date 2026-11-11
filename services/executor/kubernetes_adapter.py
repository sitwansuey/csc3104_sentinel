"""Restricted Deployment transport for the live rollback executor.

One JSON Patch tests UID/resourceVersion, restores the exact Pod template and
writes action evidence together. This module does not decide authorization.
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
from services.executor.store import canonical_json, fingerprint
from services.guard.adapters import AdapterError, _DNS_LABEL, _DNS_SUBDOMAIN, _parse_json
from services.guard.preview import _private_file


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
        self.stats = {"read_attempts": 0, "write_attempts": 0, "successful_writes": 0}

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

    def _evidence(self, target, version, template, evidence):
        required = {"action_id", "proposal_sha256", "target_uid", "template_sha256", "attempt_id"}
        try:
            valid = (
                _text(version) and isinstance(template, dict) and isinstance(template.get("spec"), dict)
                and isinstance(template["spec"].get("containers"), list) and bool(template["spec"]["containers"])
                and isinstance(evidence, dict) and set(evidence) == required
                and _text(evidence["action_id"])
                and isinstance(evidence["proposal_sha256"], str) and _DIGEST.fullmatch(evidence["proposal_sha256"])
                and evidence["target_uid"] == target["uid"]
                and evidence["template_sha256"] == fingerprint(template)
                and isinstance(evidence["attempt_id"], str) and _NONCE.fullmatch(evidence["attempt_id"])
            )
        except (KeyError, TypeError, ValueError, RecursionError):
            valid = False
        if not valid:
            raise LiveAdapterError("Rollback template, preconditions or action evidence are invalid")

    def conditional_restore(self, *, target, version, template, evidence):
        """Commit only the baseline template and atomic evidence under UID/RV tests.

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
        annotations = metadata.get("annotations")
        if annotations is not None and not isinstance(annotations, dict):
            raise LiveAdapterError("Deployment annotations are invalid")
        evidence_operation = (
            {"op": "add", "path": "/metadata/annotations", "value": {ACTION_ANNOTATION: annotation}}
            if annotations is None else
            {"op": "add", "path": "/metadata/annotations/" + ACTION_ANNOTATION.replace("~", "~0").replace("/", "~1"),
             "value": annotation}
        )
        patch = [{"op": "test", "path": "/metadata/uid", "value": target["uid"]},
                 {"op": "test", "path": "/metadata/resourceVersion", "value": version},
                 {"op": "replace", "path": "/spec/template", "value": template}, evidence_operation]
        patch_path = None
        try:
            descriptor, path = tempfile.mkstemp(prefix="sentinel-execution-", suffix=".json")
            patch_path = Path(path)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                _private_file(patch_path)
                stream.write(canonical_json(patch))
            command = self._command(target) + ["patch", "deployment", target["name"], "--type=json",
                                               "--patch-file", str(patch_path), "--output=json"]
            self.stats["write_attempts"] += 1
            try:
                result = self.runner(command, capture_output=True, check=False, timeout=PROCESS_TIMEOUT_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                raise WriteOutcomeUnknown("Live write response is unavailable; reconcile before any new mutation") from None
            if result.returncode:
                raise WriteOutcomeUnknown("Live write returned an error; reconcile its outcome")
            try:
                admitted = _parse_json(result.stdout, "executor_write_response")
                applied = self._snapshot(admitted, target)
                valid = (
                    _identity(admitted, self.cluster_id) == target
                    and admitted["metadata"]["resourceVersion"] != version
                    and admitted["metadata"].get("annotations", {}).get(ACTION_ANNOTATION) == annotation
                    and fingerprint(admitted["spec"]["template"]) == evidence["template_sha256"]
                )
            except (AdapterError, LiveAdapterError, AttributeError, KeyError, TypeError, ValueError, RecursionError):
                raise WriteOutcomeUnknown("Live write response cannot confirm the exact requested state") from None
            if not valid:
                raise WriteOutcomeUnknown("Live write response does not match the requested template and evidence")
            self.stats["successful_writes"] += 1
            return applied
        finally:
            if patch_path is not None:
                try:
                    patch_path.unlink(missing_ok=True)
                except OSError:
                    raise WriteOutcomeUnknown("Private execution patch could not be cleaned up") from None
