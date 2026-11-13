"""Structured Kubernetes REST transport for the Executor (P5.08; optional, kubectl stays the default).

KubernetesRESTDeploymentAPI is KubectlDeploymentAPI with the transport
swapped. It inherits the allowlist, the snapshot checks and the exact JSON
Patches of conditional_restore, conditional_scale and fence, so both transports
send the same patch with the same UID and resourceVersion tests. It replaces
only read, autoscalers and the one-request submit with a small standard-library
HTTPS client.

kubectl folds every HTTP outcome into an exit code and free text, so the
kubectl adapter treats every failed write as unknown. Here the status code is
data, and one request gets one answer:

- 200 with a body that shows exactly the requested change: the write committed.
- 400, 401, 403, 404, 409, 422 or 429: the API server answered this request
  with a definite rejection, so it did not commit: WriteRejected(status, reason).
- The connection (TCP, then TLS) failed before any byte of the request was
  sent: it definitely did not commit: WriteRejected("not_sent").
- Anything else (408, any 5xx, another status, a timeout or a reset after
  sending, a success body that does not show the change): the write may have
  committed: WriteOutcomeUnknown, and reconciliation reads state.

No request is ever retried. Only the HTTP status and a short metav1
StatusReason word from an allowlist reach an exception or a log; the server's
Status.message, response bodies and the bearer token never do. TLS
verification is always on, with the CA from the kubeconfig or else the system
store; plain http is accepted only to a loopback IP address, for tests.
Credentials are a bearer token, a token file (read again for every request, so
a projected ServiceAccount token can rotate) or a client certificate. Exec
plugins, auth providers, basic auth, impersonation, proxies, tls-server-name
and insecure-skip-tls-verify are refused with a configuration error. The
module does not decide authorization; the caller injects fresh Guard
validation and restricted credentials, as for kubectl.
"""

import base64
from copy import deepcopy
from datetime import UTC, datetime
import http.client
import ipaddress
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
from urllib.parse import quote, urlencode, urlsplit

from services.executor.core import ReadUnavailable, TargetAbsent, WriteRejected
from services.executor.kubernetes_adapter import (
    PROCESS_TIMEOUT_SECONDS, REQUEST_TIMEOUT_SECONDS, KubectlDeploymentAPI, LiveAdapterError, WriteOutcomeUnknown,
    _target_shape, _text,
)
from services.executor.store import canonical_json
from services.guard.adapters import MAX_JSON_BYTES, AdapterError, _parse_json
from services.guard.preview import _private_file
from services.guard.scale import autoscalers_for
from shared import log


# A 4xx in this set answers this one request and means it was not persisted. Other
# 4xx codes (408 above all) and every 5xx stay unknown.
DEFINITE_REJECTIONS = frozenset({400, 401, 403, 404, 409, 422, 429})
# metav1.StatusReason words that may reach an exception or a log; anything else is "Unknown".
STATUS_REASONS = frozenset({
    "BadRequest", "Unauthorized", "Forbidden", "NotFound", "Conflict", "AlreadyExists", "Gone", "Invalid",
    "TooManyRequests", "Expired", "MethodNotAllowed", "NotAcceptable", "RequestEntityTooLarge",
    "UnsupportedMediaType", "ServerTimeout", "Timeout", "InternalError", "ServiceUnavailable",
})
FIELD_MANAGER = "sentinel-executor"
_CLUSTER_KEYS = frozenset({"server", "certificate-authority-data", "certificate-authority",
                           "insecure-skip-tls-verify", "extensions"})
_USER_KEYS = frozenset({"token", "tokenFile", "client-certificate-data", "client-key-data",
                        "client-certificate", "client-key", "extensions"})
_MAX_CREDENTIAL = 65536
LOG = log.get_logger("executor")


class _NotSent(Exception):
    """No byte of the request left this process."""


class _Lost(Exception):
    """The request may have been sent, and no complete answer arrived."""


def _endpoint(server):
    """(scheme, host, port) for an https URL, or an http URL to a loopback IP address."""
    try:
        parts = urlsplit(server)
        host, port = parts.hostname, parts.port
    except (TypeError, ValueError, AttributeError):
        raise LiveAdapterError("The API server URL is invalid") from None
    if (not host or parts.username is not None or parts.password is not None or parts.query or parts.fragment
            or parts.path not in ("", "/")):
        raise LiveAdapterError("The API server URL must be scheme, host and port only")
    if parts.scheme == "https":
        return "https", host, port or 443
    if parts.scheme == "http" and _loopback(host):
        return "http", host, port or 80
    raise LiveAdapterError("The API server must use https; plain http is accepted only on a loopback address")


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _header_value(token):
    return (isinstance(token, str) and 0 < len(token) <= _MAX_CREDENTIAL and token.isascii()
            and not any(character.isspace() or ord(character) < 32 for character in token))


def _pem(value, field):
    if not isinstance(value, str) or not 0 < len(value) <= _MAX_CREDENTIAL or not value.isascii():
        raise LiveAdapterError("The " + field + " is invalid")
    return value


def _tls_context(certificate_authority, client_certificate, client_key):
    """A verifying client context: CERT_REQUIRED, hostname checks and TLS 1.2 or newer."""
    try:
        if certificate_authority is not None:
            context = ssl.create_default_context(cadata=_pem(certificate_authority, "cluster CA"))
        else:
            context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    except (OSError, ValueError):
        raise LiveAdapterError("The cluster CA is invalid") from None
    if client_certificate is not None:
        _load_client_certificate(context, _pem(client_certificate, "client certificate"),
                                 _pem(client_key, "client key"))
    return context


def _load_client_certificate(context, certificate, key):
    """Load the pair through private temporary files that are gone again when this returns, also on error."""
    paths = []
    try:
        for prefix, text in (("sentinel-client-cert-", certificate), ("sentinel-client-key-", key)):
            descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".pem")
            paths.append(Path(name))
            with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
                _private_file(paths[-1])
                stream.write(text)
        context.load_cert_chain(str(paths[0]), str(paths[1]))
    except (OSError, ValueError):
        raise LiveAdapterError("The client certificate or key is invalid") from None
    finally:
        try:
            for path in paths:
                path.unlink(missing_ok=True)
        except OSError:
            raise LiveAdapterError("A private credential file could not be removed") from None


def _named(entries, name, field):
    found = [entry.get(field) for entry in entries if isinstance(entry, dict) and entry.get("name") == name] \
        if isinstance(entries, list) and _text(name) else []
    if len(found) != 1 or not isinstance(found[0], dict):
        raise LiveAdapterError("The kubeconfig has no single " + field + " for the selected context")
    return found[0]


def _data(value, field):
    try:
        return base64.b64decode(value, validate=True).decode("ascii")
    except (TypeError, ValueError):
        raise LiveAdapterError("The kubeconfig " + field + " is invalid") from None


def _file(path, field):
    try:
        with open(path, encoding="ascii") as stream:
            text = stream.read(_MAX_CREDENTIAL + 1)
    except (OSError, TypeError, ValueError):
        raise LiveAdapterError("The kubeconfig " + field + " file could not be read") from None
    return _pem(text, field)


def kubeconfig_credentials(config, context):
    """Server, CA and credentials of one named context in a parsed (flattened) kubeconfig.

    Fails closed with LiveAdapterError on anything this transport does not
    implement, rather than connecting with less than the kubeconfig asks for.
    """
    if not isinstance(config, dict):
        raise LiveAdapterError("The kubeconfig is invalid")
    selected = _named(config.get("contexts"), context, "context")
    cluster = _named(config.get("clusters"), selected.get("cluster"), "cluster")
    user = _named(config.get("users"), selected.get("user"), "user")
    if set(cluster) - _CLUSTER_KEYS or cluster.get("insecure-skip-tls-verify", False) is not False:
        raise LiveAdapterError("Only a verified https cluster entry (server and CA) is supported")
    if set(user) - _USER_KEYS:
        raise LiveAdapterError("Only a bearer token, a token file or a client certificate is supported")
    settings = {"server": cluster.get("server")}
    if "certificate-authority-data" in cluster:
        settings["certificate_authority"] = _data(cluster["certificate-authority-data"], "cluster CA")
    elif "certificate-authority" in cluster:
        settings["certificate_authority"] = _file(cluster["certificate-authority"], "cluster CA")
    for field, data_key, file_key in (("client_certificate", "client-certificate-data", "client-certificate"),
                                      ("client_key", "client-key-data", "client-key")):
        if data_key in user:
            settings[field] = _data(user[data_key], data_key)
        elif file_key in user:
            settings[field] = _file(user[file_key], file_key)
    if "token" in user:
        settings["token"] = user["token"]
    if "tokenFile" in user:
        settings["token_file"] = user["tokenFile"]
    return settings


def _status(data):
    """A metav1.Status body as a dict, or None. Only reason and details are ever read from it."""
    try:
        status = _parse_json(data, "kubernetes_status")
    except (AdapterError, TypeError):
        return None
    return status if isinstance(status, dict) and status.get("kind") == "Status" else None


def _reason(data):
    status = _status(data)
    reason = status.get("reason") if status is not None else None
    return reason if reason in STATUS_REASONS else "Unknown"


class KubernetesRESTDeploymentAPI(KubectlDeploymentAPI):
    """The Executor's Deployment adapter over HTTPS, with the kubectl adapter's patches and checks.

    Explicit server, cluster and credentials, and a trusted target list that
    includes the UID, as for kubectl. Only GET and PATCH on the allowlisted
    Deployment, and a list of HorizontalPodAutoscalers in its namespace for a
    SCALE, are ever sent. Every request uses a new connection.
    """

    def __init__(self, *, server, cluster_id, allowed_targets, token=None, token_file=None,
                 client_certificate=None, client_key=None, certificate_authority=None, clock=None,
                 timeout=REQUEST_TIMEOUT_SECONDS):
        if (not _text(cluster_id)
                or not isinstance(allowed_targets, (list, tuple)) or not 1 <= len(allowed_targets) <= 128
                or not all(_target_shape(target, cluster_id) for target in allowed_targets)):
            raise LiveAdapterError("Explicit cluster, credentials and valid Deployment targets are required")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise LiveAdapterError("The request timeout must be more than 0 and at most 60 seconds")
        if token is not None and not _header_value(token):
            raise LiveAdapterError("The bearer token is invalid")
        if token_file is not None and not _text(str(token_file), maximum=4096):
            raise LiveAdapterError("The token file path is invalid")
        if (client_certificate is None) != (client_key is None):
            raise LiveAdapterError("A client certificate needs its key, and a key its certificate")
        if token is None and token_file is None and client_certificate is None:
            raise LiveAdapterError("A bearer token, a token file or a client certificate is required")
        self._scheme, self._host, self._port = _endpoint(server)
        self._tls = (_tls_context(certificate_authority, client_certificate, client_key)
                     if self._scheme == "https" else None)
        self._token, self._token_file = token, token_file
        self.cluster_id, self.timeout = cluster_id, timeout
        self.clock = clock or (lambda: datetime.now(UTC))
        self._allowed = frozenset(canonical_json(target) for target in deepcopy(allowed_targets))
        self.stats = {"read_attempts": 0, "write_attempts": 0, "successful_writes": 0,
                      "fence_attempts": 0, "successful_fences": 0, "rejected_writes": 0}

    @classmethod
    def from_kubeconfig_data(cls, config, *, context, cluster_id, allowed_targets, **options):
        """Build from an already parsed kubeconfig (for example kubectl config view --flatten output)."""
        return cls(**kubeconfig_credentials(config, context), cluster_id=cluster_id,
                   allowed_targets=allowed_targets, **options)

    @classmethod
    def from_kubeconfig(cls, *, context, kubeconfig, cluster_id, allowed_targets, runner=subprocess.run,
                        kubectl="kubectl", **options):
        """Read one context with a single read-only kubectl config view, then use REST only.

        kubectl resolves the kubeconfig format here and is never run again by
        this adapter. The raw output holds credentials, so it is parsed in
        memory and never written, logged or echoed.
        """
        if (not _text(context) or kubeconfig is None or not _text(str(kubeconfig), maximum=32768)
                or not _text(str(kubectl), maximum=32768)):
            raise LiveAdapterError("Explicit context and credentials are required")
        command = [str(kubectl), "config", "view", "--raw", "--minify", "--flatten", "--output=json",
                   f"--context={context}", f"--kubeconfig={kubeconfig}"]
        try:
            result = runner(command, capture_output=True, check=False, timeout=PROCESS_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            raise LiveAdapterError("The kubeconfig could not be read") from None
        if result.returncode:
            raise LiveAdapterError("The kubeconfig could not be read")
        try:
            config = _parse_json(result.stdout, "executor_kubeconfig")
        except (AdapterError, TypeError):
            raise LiveAdapterError("The kubeconfig view is invalid") from None
        return cls.from_kubeconfig_data(config, context=context, cluster_id=cluster_id,
                                        allowed_targets=allowed_targets, **options)

    # Transport -----------------------------------------------------------------

    def _command(self, target):
        raise LiveAdapterError("The REST transport runs no kubectl command")

    def _connection(self, method):
        """A new connection for one request. method is unused here; a test may fail one kind of request."""
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._host, self._port, timeout=self.timeout, context=self._tls)
        return http.client.HTTPConnection(self._host, self._port, timeout=self.timeout)

    def _headers(self):
        token = self._token
        if self._token_file is not None:
            with open(self._token_file, encoding="ascii") as stream:
                token = stream.read(_MAX_CREDENTIAL + 1).strip()
            if not _header_value(token):
                raise ValueError("The token file holds no usable token")
        headers = {"Accept": "application/json", "User-Agent": FIELD_MANAGER}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        return headers

    def _exchange(self, method, path, body=None, content_type=None):
        """One request on a new connection: (status, body). _NotSent or _Lost otherwise; never retried."""
        try:
            headers = self._headers()
        except (OSError, ValueError):
            raise _NotSent() from None
        if body is not None:
            headers["Content-Type"] = content_type
        connection = self._connection(method)
        try:
            try:
                # TCP and TLS complete here, before any byte of the request is written.
                connection.connect()
            except (OSError, ValueError):
                raise _NotSent() from None
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                data = response.read(MAX_JSON_BYTES + 1)
            except (OSError, ValueError, http.client.HTTPException):
                raise _Lost() from None
            if len(data) > MAX_JSON_BYTES:
                raise _Lost()
            return response.status, data
        finally:
            connection.close()

    @staticmethod
    def _path(target):
        return (f"/apis/apps/v1/namespaces/{quote(target['namespace'], safe='')}"
                f"/deployments/{quote(target['name'], safe='')}")

    def read(self, target):
        """GET only the allowlisted named Deployment, preserving its actual UID."""
        target = deepcopy(target)
        self._target(target)
        self.stats["read_attempts"] += 1
        try:
            status, data = self._exchange("GET", self._path(target))
        except (_NotSent, _Lost):
            raise ReadUnavailable("Live Deployment read is unavailable") from None
        if status == 404:
            found = _status(data)
            details = found.get("details") if found is not None else None
            # Only the API server's own NotFound for this Deployment means absent, never a bare 404.
            if (found is not None and found.get("reason") == "NotFound" and isinstance(details, dict)
                    and details.get("name") == target["name"] and details.get("kind") == "deployments"):
                raise TargetAbsent("Deployment does not exist")
        if status != 200:
            raise ReadUnavailable("Live Deployment read failed")
        try:
            deployment = _parse_json(data, "executor_live_state")
        except (AdapterError, TypeError):
            raise LiveAdapterError("Kubernetes returned invalid Deployment JSON") from None
        return self._snapshot(deployment, target)

    def autoscalers(self, target):
        """The HorizontalPodAutoscalers in the target's namespace that scale this Deployment, by name.

        Read-only (list horizontalpodautoscalers in autoscaling/v2). A failed
        read raises ReadUnavailable, so the SCALE is refused with no write.
        """
        target = deepcopy(target)
        self._target(target)
        self.stats["read_attempts"] += 1
        path = f"/apis/autoscaling/v2/namespaces/{quote(target['namespace'], safe='')}/horizontalpodautoscalers"
        try:
            status, data = self._exchange("GET", path)
        except (_NotSent, _Lost):
            raise ReadUnavailable("Autoscaler read is unavailable") from None
        if status != 200:
            raise ReadUnavailable("Autoscaler read failed")
        try:
            return autoscalers_for(_parse_json(data, "executor_autoscalers"), target["name"])
        except (AdapterError, TypeError, ValueError):
            raise LiveAdapterError("Kubernetes returned an invalid autoscaler list") from None

    def _submit(self, target, version, patch, confirms, *, prefix, counter):
        """Send one conditional JSON Patch, exactly once, and classify its one answer."""
        del prefix  # The kubectl transport names its private patch file with it; nothing is written here.
        body = canonical_json(patch).encode("utf-8")
        query = urlencode({"fieldManager": FIELD_MANAGER, "timeout": f"{REQUEST_TIMEOUT_SECONDS}s"})
        self.stats[counter] += 1
        try:
            status, data = self._exchange("PATCH", self._path(target) + "?" + query, body,
                                          "application/json-patch+json")
        except _NotSent:
            self.stats["rejected_writes"] += 1
            log.emit(LOG, "executor.rest.rejected", level=log.WARNING, operation=counter, http_status="not_sent",
                     reason="NotSent")
            raise WriteRejected("not_sent", "NotSent") from None
        except _Lost:
            raise WriteOutcomeUnknown("Live write response is unavailable; reconcile before any new mutation") from None
        if status in DEFINITE_REJECTIONS:
            self.stats["rejected_writes"] += 1
            reason = _reason(data)
            # Status and an allowlisted reason word only; never the Status message or the body.
            log.emit(LOG, "executor.rest.rejected", level=log.WARNING, operation=counter, http_status=status,
                     reason=reason)
            raise WriteRejected(status, reason)
        if status != 200:
            raise WriteOutcomeUnknown("Live write returned an error; reconcile its outcome")
        return self._confirmed(data, target, version, confirms)
