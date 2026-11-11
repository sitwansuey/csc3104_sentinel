"""Non-persisting Kubernetes admission preview for a bound rollback candidate.

Every Kubernetes patch in this module includes ``--dry-run=server``. A passing
Deployment preview establishes API acceptance of that exact template only; it
does not establish future Pod admission/quota, scheduling, image availability or
application health. The caller must supply a separately trusted context/cluster
mapping and must still obtain fresh Guard validation before actual execution.
"""

from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import tempfile

from services.guard.core import GuardInputError, proposal_fingerprint, template_sha256
from services.guard.adapters import (
    AdapterError, KUBECTL_TIMEOUT_SECONDS, _DNS_LABEL, _DNS_SUBDOMAIN, _parse_json,
)
from shared.canonical import canonical_json
from shared.contracts import ContractError


def _private_file(path):
    """Restrict the new empty patch file before putting template data in it."""
    os.chmod(path, 0o600)
    if os.name == "nt":
        # Windows chmod does not enforce a Unix-style owner-only read ACL.
        # Install a protected DACL granting full access to the object's owner.
        # OWNER RIGHTS (OW) refers to the owner of this newly created file.
        import ctypes
        from ctypes import wintypes

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        descriptor = ctypes.c_void_p()
        convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                            ctypes.POINTER(ctypes.c_void_p),
                            ctypes.POINTER(wintypes.DWORD)]
        convert.restype = wintypes.BOOL
        set_security = advapi.SetFileSecurityW
        set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        set_security.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        if not convert("D:P(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None):
            raise OSError("Could not restrict preview temporary file")
        try:
            # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION.
            if not set_security(str(path), 0x80000004, descriptor):
                raise OSError("Could not restrict preview temporary file")
        finally:
            kernel.LocalFree(descriptor)


def server_dry_run(proposal: dict, live_state: dict, baseline: dict, *,
                   context: str, cluster_id: str,
                   kubeconfig: str | Path | None = None) -> dict:
    """Preview an exact rollback via a UID/version-conditional JSON Patch.

    Invalid proposal/context inputs raise a safe AdapterError. Inconsistent
    snapshots, unavailable API access and ambiguous kubectl errors return
    UNKNOWN, never a positive authorization. Raw server output is not exposed.
    Admission mutations return REJECTED: an altered candidate needs its own
    policy evaluation, proposal fingerprint and approval.
    """
    try:
        fingerprint = proposal_fingerprint(proposal)
    except (ContractError, TypeError, ValueError, RecursionError) as exc:
        raise AdapterError("INVALID_PREVIEW_INPUT", "server_preview",
                           "Expected a schema-valid recovery proposal") from exc
    target = proposal["target"]
    namespace, name = target["namespace"], target["name"]
    if (
        not isinstance(context, str) or not context.strip()
        or not isinstance(cluster_id, str) or not cluster_id.strip()
        or (kubeconfig is not None and not str(kubeconfig).strip())
        or len(namespace) > 63 or not _DNS_LABEL.fullmatch(namespace)
        or len(name) > 253 or not _DNS_SUBDOMAIN.fullmatch(name)
        or any(not _DNS_LABEL.fullmatch(label) for label in name.split("."))
        or target["kind"] != "Deployment"
    ):
        raise AdapterError("INVALID_PREVIEW_TARGET", "server_preview",
                           "Expected explicit context, cluster ID and valid Deployment target")

    snapshot = {
        "status": "UNKNOWN",
        "proposal_sha256": fingerprint,
        "target": deepcopy(target),
        "resource_version": proposal["based_on_resource_version"],
        "template_sha256": proposal["parameters"].get("template_sha256"),
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "details_code": "PREVIEW_NOT_COMPLETED",
        "admitted_template_sha256": None,
    }

    def finish(status, details, admitted=None):
        snapshot.update(status=status, details_code=details,
                        admitted_template_sha256=admitted,
                        checked_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        return snapshot

    if proposal["action"] != "ROLLBACK":
        return finish("UNKNOWN", "UNSUPPORTED_PREVIEW_ACTION")
    if not isinstance(live_state, dict) or live_state.get("cluster_id") != cluster_id:
        return finish("UNKNOWN", "LIVE_PRECONDITION_FAILED")
    deployment = live_state.get("deployment")
    if not _same_deployment(deployment, target, proposal["based_on_resource_version"]) or target["cluster_id"] != cluster_id:
        return finish("UNKNOWN", "LIVE_PRECONDITION_FAILED")
    if not isinstance(baseline, dict) or (
        baseline.get("target") != target
        or baseline.get("baseline_id") != proposal["parameters"]["baseline_id"]
    ):
        return finish("UNKNOWN", "BASELINE_PRECONDITION_FAILED")
    template = baseline.get("pod_template")
    try:
        digest = template_sha256(template)
    except (GuardInputError, TypeError, ValueError, RecursionError):
        return finish("UNKNOWN", "BASELINE_PRECONDITION_FAILED")
    if digest != baseline.get("template_sha256") or digest != snapshot["template_sha256"]:
        return finish("UNKNOWN", "BASELINE_PRECONDITION_FAILED")
    if (not isinstance(template.get("spec"), dict)
            or not isinstance(template["spec"].get("containers"), list)
            or not template["spec"]["containers"]):
        return finish("UNKNOWN", "BASELINE_PRECONDITION_FAILED")

    patch = [
        {"op": "test", "path": "/metadata/uid", "value": target["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": snapshot["resource_version"]},
        {"op": "replace", "path": "/spec/template", "value": template},
    ]
    command = ["kubectl", f"--context={context}", f"--namespace={namespace}",
               "--request-timeout=10s"]
    if kubeconfig is not None:
        command.append(f"--kubeconfig={kubeconfig}")
    patch_path = None
    try:
        # mkstemp creates an exclusive new file. Close it before kubectl opens
        # it; keeping a NamedTemporaryFile open breaks reads on Windows.
        descriptor, path = tempfile.mkstemp(prefix="sentinel-preview-", suffix=".json")
        patch_path = Path(path)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            _private_file(patch_path)
            stream.write(canonical_json(patch))
        command.extend(["patch", "deployment", name, "--type=json", "--patch-file",
                        str(patch_path), "--dry-run=server", "--output=json"])
        result = subprocess.run(command, capture_output=True, check=False,
                                timeout=KUBECTL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return finish("UNKNOWN", "SERVER_DRY_RUN_TIMEOUT")
    except (OSError, ValueError, TypeError, RecursionError):
        return finish("UNKNOWN", "SERVER_DRY_RUN_UNAVAILABLE")
    finally:
        if patch_path is not None:
            try:
                patch_path.unlink(missing_ok=True)
            except OSError as exc:
                # The file remains owner-restricted. Do not expose its path or
                # contents in an OS error, or issue a passing preview result.
                raise AdapterError("PREVIEW_CLEANUP_FAILED", "server_preview",
                                   "Preview temporary file could not be removed") from exc
    if result.returncode != 0:
        # kubectl normally returns textual stderr. It does not reliably
        # distinguish auth/connectivity failure from policy rejection.
        return finish("UNKNOWN", "SERVER_DRY_RUN_FAILED")
    try:
        admitted = _parse_json(result.stdout, "server_preview")
    except (AdapterError, TypeError):
        return finish("UNKNOWN", "INVALID_SERVER_RESPONSE")
    if not _same_deployment(admitted, target, snapshot["resource_version"]):
        return finish("UNKNOWN", "SERVER_RESPONSE_IDENTITY_MISMATCH")
    if not isinstance(admitted.get("spec"), dict) or not isinstance(admitted["spec"].get("template"), dict):
        return finish("UNKNOWN", "INVALID_SERVER_RESPONSE")
    try:
        admitted_digest = template_sha256(admitted["spec"]["template"])
    except (GuardInputError, TypeError, ValueError, RecursionError):
        return finish("UNKNOWN", "INVALID_SERVER_RESPONSE")
    if admitted_digest != digest:
        return finish("REJECTED", "ADMISSION_TEMPLATE_MUTATED", admitted_digest)
    return finish("PASSED", "SERVER_DRY_RUN_PASSED", admitted_digest)


def _same_deployment(deployment, target, version):
    if not isinstance(deployment, dict):
        return False
    metadata = deployment.get("metadata")
    return (
        deployment.get("apiVersion") == "apps/v1"
        and deployment.get("kind") == "Deployment"
        and isinstance(metadata, dict)
        and all(metadata.get(field) == target[field] for field in ("namespace", "name", "uid"))
        and metadata.get("resourceVersion") == version
    )
