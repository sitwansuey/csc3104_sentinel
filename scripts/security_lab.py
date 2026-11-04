"""Isolated mTLS gRPC inspection lab; no existing Kubernetes cluster is changed."""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.grpc_inspection import MAX_BODY, METHODS

# Reviewed release manifests: Envoy 1.40.2, Boutique 0.10.7, grpcurl 1.9.3.
ENVOY = "envoyproxy/envoy@sha256:8de97e654644827d62afdbf7e1d3dc9fee01a8690d410dd1c63a30dc6b6cecbb"
SHIPPING = "us-central1-docker.pkg.dev/online-boutique-ci/microservices-demo/shippingservice@sha256:3984397422ef1b45bd8a7c6736f935f4b6a41a1724ea39f9563b26733ed633e4"
CLIENT = "fullstorydev/grpcurl@sha256:085e183ca334eb4e81ca81ee12cbb2b2737505d1d77f5e33dabc5d066593d998"
INSPECTOR = "sentinel-security-inspector:0.1.0"
PROJECT = "sentinel-security-lab"


def run(args, *, timeout=300, required=True):
    result = subprocess.run([str(arg) for arg in args], capture_output=True, text=True,
                            timeout=timeout, check=False)
    if required and result.returncode:
        # These commands contain no certificate contents, credentials or bodies.
        raise RuntimeError(f"{args[0]} failed: {result.stderr[-2000:]}")
    return result


def save(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def tls_context(identity, *, client=False, server_name=None):
    common = {"tls_params": {"tls_minimum_protocol_version": "TLSv1_2"},
              "tls_certificates": [{"certificate_chain": {"filename": f"/pki/{identity}/tls.crt"},
                                    "private_key": {"filename": f"/pki/{identity}/tls.key"}}],
              "validation_context": {"trusted_ca": {"filename": f"/pki/{identity}/ca.crt"}},
              "alpn_protocols": ["h2"]}
    if client:
        common["validation_context"]["match_typed_subject_alt_names"] = [
            {"san_type": "DNS", "matcher": {"exact": server_name}}]
        return {"@type": "type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.UpstreamTlsContext",
                "sni": server_name, "common_tls_context": common}
    return {"@type": "type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.DownstreamTlsContext",
            "require_client_certificate": True, "common_tls_context": common}


def envoy_config(*, upstream=False):
    name, port = ("upstream", 8444) if upstream else ("gateway", 8443)
    cluster = {"name": "shipping", "type": "STRICT_DNS", "connect_timeout": "2s",
               "typed_extension_protocol_options": {"envoy.extensions.upstreams.http.v3.HttpProtocolOptions": {
                   "@type": "type.googleapis.com/envoy.extensions.upstreams.http.v3.HttpProtocolOptions",
                   "explicit_http_config": {"http2_protocol_options": {}}}},
               "load_assignment": {"cluster_name": "shipping", "endpoints": [{"lb_endpoints": [{"endpoint": {
                   "address": {"socket_address": {"address": "127.0.0.1" if upstream else "upstream",
                                                   "port_value": 50051 if upstream else 8444}}}}]}]}}
    if not upstream:
        cluster["transport_socket"] = {"name": "envoy.transport_sockets.tls",
                                        "typed_config": tls_context("proxy-client", client=True, server_name="upstream.internal")}
    routes = [{"name": "shipping-rpc", "match": {"path": method, "headers": [
                   {"name": ":method", "string_match": {"exact": "POST"}},
                   {"name": "content-type", "string_match": {"safe_regex": {
                       "regex": r"(?i)application/grpc(?:\+proto)?(?:;.*)?"}}}]},
               "route": {"cluster": "shipping", "timeout": "5s"}} for method in sorted(METHODS)]
    filters = []
    clusters = [cluster]
    if not upstream:
        clusters.append({"name": "inspector", "type": "STATIC", "connect_timeout": "1s",
                         "load_assignment": {"cluster_name": "inspector", "endpoints": [{"lb_endpoints": [{
                             "endpoint": {"address": {"socket_address": {"address": "127.0.0.1", "port_value": 9001}}}}]}]}})
        filters.append({"name": "envoy.filters.http.ext_authz", "typed_config": {
            "@type": "type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthz",
            "failure_mode_allow": False, "clear_route_cache": False,
            "status_on_error": {"code": "ServiceUnavailable"},
            "allowed_headers": {"patterns": [{"exact": "grpc-encoding"}]},
            "with_request_body": {"max_request_bytes": MAX_BODY, "allow_partial_message": False},
            "http_service": {"server_uri": {"uri": "http://127.0.0.1:9001", "cluster": "inspector", "timeout": "2s"},
                             # Never label the HTTP authorization transport as
                             # gRPC. Otherwise a local connection error can be
                             # translated into HTTP 200 + grpc-status:14, which
                             # HTTP ext_authz interprets as permission to pass.
                             # Original client content-type is enforced above.
                             "authorization_request": {"headers_to_add": [
                                 {"key": "content-type", "value": "application/octet-stream"},
                                 {"key": "x-sentinel-body-format", "value": "grpc"}]}}}})
    filters.append({"name": "envoy.filters.http.router", "typed_config": {
        "@type": "type.googleapis.com/envoy.extensions.filters.http.router.v3.Router"}})
    manager = {"@type": "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager",
               "stat_prefix": name, "codec_type": "HTTP2", "stream_idle_timeout": "10s",
               "request_timeout": "10s", "max_request_headers_kb": 16,
               "route_config": {"name": name, "virtual_hosts": [{"name": "shipping", "domains": ["*"],
                   "routes": routes + [{"name": "denied", "match": {"prefix": "/"}, "direct_response": {"status": 403}}]}]},
               "http_filters": filters,
               "access_log": [{"name": "envoy.access_loggers.stdout", "typed_config": {
                   "@type": "type.googleapis.com/envoy.extensions.access_loggers.stream.v3.StdoutAccessLog",
                   "log_format": {"json_format": {"route": "%ROUTE_NAME%", "status": "%RESPONSE_CODE%",
                       "grpc_status": "%GRPC_STATUS%", "flags": "%RESPONSE_FLAGS%", "duration_ms": "%DURATION%"}}}}]}
    return {"static_resources": {"listeners": [{"name": name, "address": {"socket_address": {
        "address": "0.0.0.0", "port_value": port}}, "filter_chains": [{
            "transport_socket": {"name": "envoy.transport_sockets.tls", "typed_config": tls_context(name)},
            "filters": [{"name": "envoy.filters.network.http_connection_manager", "typed_config": manager}]}]}],
        "clusters": clusters}}


def certificates(directory, *, extra_clients=()):
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError("OpenSSL is required on PATH for short-lived local test certificates")
    ca = directory / "pki-ca"
    ca.mkdir(mode=0o700)
    run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=Sentinel lab CA",
         "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
         "-addext", "subjectKeyIdentifier=hash", "-keyout", ca / "ca.key", "-out", ca / "ca.crt"])
    identities = [("gateway", "gateway.internal", False), ("upstream", "upstream.internal", False),
                  ("proxy-client", "proxy-client.internal", True), ("client", "client.internal", True)]
    identities += [(identity, identity + ".internal", True) for identity in extra_clients]
    for identity, host, client in identities:
        folder = directory / identity
        folder.mkdir(mode=0o700)
        run([openssl, "req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={host}",
             "-keyout", folder / "tls.key", "-out", ca / f"{identity}.csr"])
        extension = ca / f"{identity}.ext"
        extension.write_text(f"basicConstraints=critical,CA:FALSE\nsubjectAltName=DNS:{host}\n"
                             "keyUsage=critical,digitalSignature,keyEncipherment\n"
                             "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n"
                             f"extendedKeyUsage={'clientAuth' if client else 'serverAuth'}\n", encoding="utf-8")
        run([openssl, "x509", "-req", "-in", ca / f"{identity}.csr", "-CA", ca / "ca.crt", "-CAkey", ca / "ca.key",
             "-CAcreateserial", "-days", "2", "-extfile", extension, "-out", folder / "tls.crt"])
        shutil.copyfile(ca / "ca.crt", folder / "ca.crt")
        # Parent is private; only these identity-specific directories are mounted.
        # Containers run as non-root and need read access to their mounted keys.
        folder.chmod(0o755)
        for file in folder.iterdir():
            file.chmod(0o444)
    (ca / "ca.key").chmod(0o600)


def resolve(image):
    run(["docker", "pull", image])
    data = json.loads(run(["docker", "image", "inspect", image]).stdout)[0]
    repository = image.split("@", 1)[0]
    values = [value for value in data.get("RepoDigests", []) if value.split("@")[0] in {repository, "docker.io/" + repository}]
    if len(values) != 1:
        raise RuntimeError("Cannot resolve an exact repository manifest for " + image)
    return values[0]


def compose(directory, *args, **kwargs):
    return run(["docker", "compose", "--project-name", PROJECT, "--file", directory / "compose.json", *args], **kwargs)


def prepare(directory, port):
    if directory.exists():
        raise ValueError("Use a new run directory; existing certificates/evidence are preserved")
    directory.mkdir(parents=True, mode=0o700)
    certificates(directory)
    images = {"envoy": resolve(ENVOY), "shipping": resolve(SHIPPING), "client": resolve(CLIENT)}
    run(["docker", "build", "-f", ROOT / "infra/security-inspector.Dockerfile", "-t", INSPECTOR, ROOT], timeout=300)
    images["inspector"] = json.loads(run(["docker", "image", "inspect", INSPECTOR]).stdout)[0]["Id"]
    save(directory / "images.json", images)
    for side in ("gateway", "upstream"):
        save(directory / f"{side}.json", envoy_config(upstream=side == "upstream"))
    data = directory / "data"
    data.mkdir()
    if os.name != "nt":
        # Only UID 10001's private evidence mount; never relax repository ACLs.
        run(["docker", "run", "--rm", "--network", "none", "--user", "0:0", "--entrypoint", "python",
             "--mount", f"type=bind,source={data},target=/data", images["inspector"],
             "-c", "import os; os.chown('/data',10001,10001); os.chmod('/data',0o700)"])
    def mount(source, target, readonly=True):
        return {"type": "bind", "source": str(source), "target": target, "read_only": readonly}
    base = {"read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "pids_limit": 128, "mem_limit": "256m", "cpus": 1, "tmpfs": ["/tmp:rw,noexec,nosuid,size=16m"]}
    services = {}
    for side in ("gateway", "upstream"):
        services[side] = {**base, "image": images["envoy"], "user": "10001:10001", "entrypoint": ["envoy"],
                          "command": ["-c", f"/etc/envoy/{side}.json", "--concurrency", "2", "--log-level", "warn"],
                          "networks": ["front", "backend"] if side == "gateway" else ["backend"],
                          "volumes": [mount(directory / f"{side}.json", f"/etc/envoy/{side}.json"),
                                      mount(directory / side, f"/pki/{side}")]}
    services["gateway"]["volumes"].append(mount(directory / "proxy-client", "/pki/proxy-client"))
    services["gateway"]["ports"] = [f"127.0.0.1:{port}:8443"]
    services["shipping"] = {**base, "image": images["shipping"], "user": "1000:1000",
                             "network_mode": "service:upstream", "depends_on": ["upstream"],
                             "environment": {"PORT": "50051", "DISABLE_TRACING": "1", "DISABLE_PROFILER": "1"}}
    services["inspector"] = {**base, "image": images["inspector"], "network_mode": "service:gateway",
                              "depends_on": ["gateway"], "command": ["--journal", "/data/telemetry.sqlite3"],
                              "volumes": [mount(data, "/data", False)]}
    services["client"] = {**base, "image": images["client"], "profiles": ["validation"], "networks": ["front"],
                           "volumes": [mount(directory / "client", "/client"), mount(ROOT / "demo/security", "/proto")]}
    save(directory / "compose.json", {"services": services, "networks": {"front": {}, "backend": {"internal": True}}})


def rpc(directory, quantity=1, *, certificate=True, address="gateway:8443", tls=True,
        server_name="gateway.internal", method="hipstershop.ShippingService/GetQuote"):
    args = ["run", "--rm", "--no-deps", "client", "-max-time", "5"]
    if tls:
        args += ["-cacert", "/client/ca.crt", "-servername", server_name]
        if certificate:
            args += ["-cert", "/client/tls.crt", "-key", "/client/tls.key"]
    else:
        args += ["-plaintext"]
    args += ["-import-path", "/proto", "-proto", "shipping.proto", "-d",
             json.dumps({"items": [{"productId": "OLJCESPC7Z", "quantity": quantity}]}),
             address, method]
    return compose(directory, *args, required=False, timeout=30)


def validate(directory):
    checks = {}
    for _ in range(20):
        healthy = rpc(directory)
        if healthy.returncode == 0:
            break
        time.sleep(1)
    checks["valid_quote_over_two_mtls_connections"] = healthy.returncode == 0 and "USD" in healthy.stdout
    checks["invalid_quantity_denied"] = rpc(directory, 101).returncode != 0
    checks["client_certificate_required"] = rpc(directory, certificate=False).returncode != 0
    checks["server_identity_verified"] = rpc(directory, server_name="wrong.internal").returncode != 0
    checks["direct_backend_unreachable_from_client_network"] = rpc(directory, address="shipping:50051", tls=False).returncode != 0
    compose(directory, "stop", "inspector")
    try:
        checks["inspector_failure_closed"] = rpc(directory).returncode != 0
    finally:
        compose(directory, "start", "inspector")
    configuration = json.loads((directory / "compose.json").read_text())
    host_port = int(configuration["services"]["gateway"]["ports"][0].split(":")[1])
    context = ssl.create_default_context(cafile=str(directory / "client/ca.crt"))
    context.load_cert_chain(directory / "client/tls.crt", directory / "client/tls.key")
    context.set_alpn_protocols(["h2"])
    with socket.create_connection(("127.0.0.1", host_port), timeout=3) as connection:
        with context.wrap_socket(connection, server_hostname="gateway.internal") as secure:
            checks["host_loopback_mtls_listener"] = secure.selected_alpn_protocol() == "h2"
    with sqlite3.connect(directory / "data/telemetry.sqlite3") as db:
        evidence = [json.loads(row[0]) for row in db.execute("SELECT payload FROM security_events WHERE kind='inspection'")]
    checks["actual_inspection_decisions_recorded"] = any(e["rule"] == "QUANTITY" and e["verdict"] == "DENY" for e in evidence)
    save(directory / "validation.json", checks)
    if not all(checks.values()):
        raise RuntimeError("Security checks failed: " + json.dumps(checks) + "\n" + healthy.stderr[-1500:])
    print(json.dumps(checks, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "test", "stop"))
    parser.add_argument("--directory", type=Path, default=ROOT / "runs/security-lab")
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.action == "start":
        prepare(directory, args.port)
        compose(directory, "up", "-d")
        validate(directory)
        print(f"Lab running on 127.0.0.1:{args.port}; evidence: {directory / 'data/telemetry.sqlite3'}")
    elif args.action == "test":
        validate(directory)
    else:
        compose(directory, "down", "--remove-orphans")


if __name__ == "__main__":
    main()
