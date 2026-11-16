"""Read-only input adapters for the Recovery Guard.

File records are supplied by a trusted caller, not authenticated by this module.
No adapter performs Kubernetes writes or creates an operator approval.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re
import subprocess


MAX_JSON_BYTES = 16 * 1024 * 1024
KUBECTL_TIMEOUT_SECONDS = 15
_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?\Z")
_DNS_SUBDOMAIN = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?\Z")


class AdapterError(ValueError):
    """A safe, structured error that never includes file or command contents."""

    def __init__(self, code: str, source: str, message: str):
        super().__init__(message)
        self.code = code
        self.source = source


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("nonfinite JSON number")


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("nonfinite JSON number")
    # Preserve the decimal value written by the producer. Silent float rounding
    # could otherwise turn an over-limit quantity into an allowed boundary (or
    # underflow a nonzero quantity to zero) before the Guard can check it.
    try:
        if len(value) > 256 or Decimal(value) != Decimal(str(parsed)):
            raise ValueError("JSON decimal loses precision; encode exact quantities as strings")
    except InvalidOperation as exc:
        raise ValueError("invalid JSON decimal") from exc
    return parsed


def _parse_json(data: bytes, source: str):
    if len(data) > MAX_JSON_BYTES:
        raise AdapterError("INPUT_TOO_LARGE", source, "JSON input exceeds the size limit")
    try:
        # Accept a UTF-8 BOM, including files created with Windows PowerShell.
        return json.loads(
            data.decode("utf-8-sig"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant, parse_float=_finite_float,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AdapterError("INVALID_JSON", source, "Input must contain valid UTF-8 JSON") from exc


def load_json(path: str | Path, source: str):
    """Read bounded, strict JSON; schema/safety validation remains in the Guard."""
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_JSON_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise AdapterError("INPUT_UNAVAILABLE", source, "Input file could not be read") from exc
    return _parse_json(data, source)


def read_deployment(target: dict, *, context: str, cluster_id: str,
                    kubeconfig: str | Path | None = None) -> dict:
    """Read one Deployment using an explicit context and a bounded kubectl call.

    The caller validates the proposal before using this adapter. Cluster identity
    is an independently supplied operator label: the caller must ensure context
    selects cluster_id. It is not copied from the untrusted proposal. The current
    kubectl context is never used implicitly.
    """
    namespace, name = target["namespace"], target["name"]
    if (
        not isinstance(context, str) or not context.strip()
        or not isinstance(cluster_id, str) or not cluster_id.strip()
        or (kubeconfig is not None and not str(kubeconfig).strip())
        or not isinstance(namespace, str) or len(namespace) > 63
        or not _DNS_LABEL.fullmatch(namespace)
        or not isinstance(name, str) or len(name) > 253
        or not _DNS_SUBDOMAIN.fullmatch(name)
        or any(not _DNS_LABEL.fullmatch(label) for label in name.split("."))
        or target["kind"] != "Deployment"
    ):
        raise AdapterError("INVALID_LIVE_TARGET", "live_state", "Expected explicit context, cluster ID and valid Deployment target")
    command = [
        "kubectl", f"--context={context}", f"--namespace={namespace}",
        "--request-timeout=10s",
    ]
    if kubeconfig is not None:
        command.append(f"--kubeconfig={kubeconfig}")
    command.extend(["get", "deployment", name, "--output=json"])
    try:
        result = subprocess.run(
            command, capture_output=True, check=False,
            timeout=KUBECTL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdapterError("LIVE_STATE_TIMEOUT", "live_state", "Live Deployment read timed out") from exc
    except OSError as exc:
        raise AdapterError("LIVE_STATE_UNAVAILABLE", "live_state", "Live Deployment read could not be started") from exc
    if result.returncode != 0:
        # kubectl stderr can contain credentials, endpoint details or server data.
        raise AdapterError("LIVE_STATE_UNAVAILABLE", "live_state", "Live Deployment read failed")
    deployment = _parse_json(result.stdout, "live_state")
    return {
        "cluster_id": cluster_id,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "deployment": deployment,
    }


def read_reference_versions(target, references, *, context, cluster_id, kubeconfig=None):
    """Read referenced object UID/version only; never return configuration values.

    kubectl selects two metadata fields for stdout. Kubernetes RBAC nevertheless
    requires get on the referenced ConfigMaps/Secrets; scope that identity to the
    reviewed namespace/resourceNames. Secret values never enter Python or reports.
    """
    if (
        not isinstance(context, str) or not context.strip()
        or not isinstance(cluster_id, str) or not cluster_id.strip()
        or target.get("cluster_id") != cluster_id
        or (kubeconfig is not None and not str(kubeconfig).strip())
        or not isinstance(references, list) or len(references) > 64
    ):
        raise AdapterError("INVALID_REFERENCE_TARGET", "reference_state", "Expected explicit trusted context, cluster and bounded references")
    namespace = target.get("namespace")
    if not isinstance(namespace, str) or len(namespace) > 63 or not _DNS_LABEL.fullmatch(namespace):
        raise AdapterError("INVALID_REFERENCE_TARGET", "reference_state", "Invalid reference namespace")
    selected = []
    for reference in references:
        if not isinstance(reference, dict) or set(reference) != {"kind", "namespace", "name"}:
            raise AdapterError("INVALID_REFERENCE_TARGET", "reference_state", "Malformed reference identity")
        kind, name = reference["kind"], reference["name"]
        if (
            kind not in ("ConfigMap", "Secret") or reference["namespace"] != namespace
            or not isinstance(name, str) or len(name) > 253
            or not _DNS_SUBDOMAIN.fullmatch(name)
            or any(len(label) > 63 or not _DNS_LABEL.fullmatch(label) for label in name.split("."))
        ):
            raise AdapterError("INVALID_REFERENCE_TARGET", "reference_state", "Only same-namespace ConfigMap and Secret metadata is supported")
        selected.append((kind, name))
    output = []
    for kind, name in sorted(set(selected)):
        command = ["kubectl", f"--context={context}", f"--namespace={namespace}", "--request-timeout=10s"]
        if kubeconfig is not None:
            command.append(f"--kubeconfig={kubeconfig}")
        command += ["get", kind.lower(), name,
                    '--output=jsonpath={.metadata.uid}{"\\t"}{.metadata.resourceVersion}']
        try:
            result = subprocess.run(command, capture_output=True, check=False, timeout=KUBECTL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise AdapterError("REFERENCE_STATE_TIMEOUT", "reference_state", "Reference metadata read timed out") from exc
        except OSError as exc:
            raise AdapterError("REFERENCE_STATE_UNAVAILABLE", "reference_state", "Reference metadata read could not be started") from exc
        if result.returncode != 0:
            raise AdapterError("REFERENCE_STATE_UNAVAILABLE", "reference_state", "Reference metadata is unavailable")
        try:
            if len(result.stdout) > 2048:
                raise ValueError("oversized metadata")
            uid, version = result.stdout.decode("utf-8-sig").strip().split("\t")
            if not uid.strip() or not version.strip() or any(char in uid + version for char in "\r\n"):
                raise ValueError("invalid metadata")
        except (ValueError, UnicodeError) as exc:
            raise AdapterError("REFERENCE_STATE_INVALID", "reference_state", "Invalid reference metadata response") from exc
        output.append({"kind": kind, "namespace": namespace, "name": name,
                       "uid": uid, "resource_version": version})
    return output


# P5.06: a Certificate is read as a bounded JSON object and reduced to these
# fields at once. A cert-manager Certificate holds no key material: the key
# lives in the Secret it names, which this reader never requests.
MAX_CERTIFICATE_BYTES = 64 * 1024


def certificate_record(certificate, namespace):
    """The Guard's record of one cert-manager Certificate: identity, spec and status fields only.

    Raises AdapterError when the object is not that namespace's Certificate.
    Optional status fields that cert-manager has not set yet are None, which
    the Guard treats as unknown, never as valid.
    """
    try:
        metadata, spec = certificate["metadata"], certificate["spec"]
        status = certificate.get("status") or {}
        if (certificate.get("apiVersion") != "cert-manager.io/v1" or certificate.get("kind") != "Certificate"
                or metadata.get("namespace") != namespace or not isinstance(status, dict)):
            raise ValueError("not this namespace's Certificate")
        issuer = spec["issuerRef"]
        ready = [item for item in status.get("conditions") or [] if isinstance(item, dict) and item.get("type") == "Ready"]
        condition = ready[0] if len(ready) == 1 else {}
        generation = metadata["generation"]
        observed = condition.get("observedGeneration")
        revision = status.get("revision")
        names = spec.get("dnsNames")
        if names is not None and not isinstance(names, list):
            # A string would otherwise become a list of its characters.
            raise ValueError("dnsNames is not a list")
        record = {
            "kind": "Certificate", "namespace": namespace, "name": metadata["name"], "uid": metadata["uid"],
            "generation": generation, "secret_name": spec["secretName"],
            "dns_names": list(names or []),
            "issuer": {"group": issuer.get("group") or "cert-manager.io", "kind": issuer.get("kind") or "Issuer",
                       "name": issuer["name"]},
            "ready": condition.get("status") if condition.get("status") in ("True", "False") else "Unknown",
            "ready_observed_generation": observed if type(observed) is int else None,
            "not_before": status.get("notBefore"), "not_after": status.get("notAfter"),
            "revision": revision if type(revision) is int else None,
        }
        if (type(generation) is not int or any(not isinstance(record[key], str) or not record[key]
                                               for key in ("name", "uid", "secret_name"))
                or not isinstance(record["issuer"]["name"], str)
                or any(not isinstance(name, str) for name in record["dns_names"])
                or any(value is not None and not isinstance(value, str)
                       for value in (record["not_before"], record["not_after"]))):
            raise ValueError("malformed Certificate fields")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise AdapterError("CERTIFICATE_STATE_INVALID", "certificate_state", "Invalid Certificate status response") from exc
    return record


def read_certificate_status(target, names, *, context, cluster_id, kubeconfig=None):
    """Read named cert-manager Certificates in the target's namespace; never a Secret (P5.06).

    The Guard-reader identity needs get on exactly these
    certificates.cert-manager.io names, and no Secret permission at all.
    """
    if (
        not isinstance(context, str) or not context.strip()
        or not isinstance(cluster_id, str) or not cluster_id.strip()
        or target.get("cluster_id") != cluster_id
        or (kubeconfig is not None and not str(kubeconfig).strip())
        or not isinstance(names, list) or len(names) > 64
    ):
        raise AdapterError("INVALID_CERTIFICATE_TARGET", "certificate_state", "Expected explicit trusted context, cluster and bounded names")
    namespace = target.get("namespace")
    if not isinstance(namespace, str) or len(namespace) > 63 or not _DNS_LABEL.fullmatch(namespace):
        raise AdapterError("INVALID_CERTIFICATE_TARGET", "certificate_state", "Invalid Certificate namespace")
    for name in names:
        if (not isinstance(name, str) or len(name) > 253 or not _DNS_SUBDOMAIN.fullmatch(name)
                or any(len(label) > 63 or not _DNS_LABEL.fullmatch(label) for label in name.split("."))):
            raise AdapterError("INVALID_CERTIFICATE_TARGET", "certificate_state", "Invalid Certificate name")
    output = []
    for name in sorted(set(names)):
        command = ["kubectl", f"--context={context}", f"--namespace={namespace}", "--request-timeout=10s"]
        if kubeconfig is not None:
            command.append(f"--kubeconfig={kubeconfig}")
        command += ["get", "certificates.cert-manager.io", name, "--output=json"]
        try:
            result = subprocess.run(command, capture_output=True, check=False, timeout=KUBECTL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise AdapterError("CERTIFICATE_STATE_TIMEOUT", "certificate_state", "Certificate status read timed out") from exc
        except OSError as exc:
            raise AdapterError("CERTIFICATE_STATE_UNAVAILABLE", "certificate_state", "Certificate status read could not be started") from exc
        if result.returncode != 0:
            raise AdapterError("CERTIFICATE_STATE_UNAVAILABLE", "certificate_state", "Certificate status is unavailable")
        if len(result.stdout) > MAX_CERTIFICATE_BYTES:
            raise AdapterError("CERTIFICATE_STATE_INVALID", "certificate_state", "Invalid Certificate status response")
        try:
            certificate = _parse_json(result.stdout, "certificate_state")
        except AdapterError as exc:
            raise AdapterError("CERTIFICATE_STATE_INVALID", "certificate_state", "Invalid Certificate status response") from exc
        record = certificate_record(certificate, namespace)
        if record["name"] != name:
            raise AdapterError("CERTIFICATE_STATE_INVALID", "certificate_state", "Invalid Certificate status response")
        output.append(record)
    return output
