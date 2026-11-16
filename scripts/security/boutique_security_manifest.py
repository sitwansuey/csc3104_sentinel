"""Pure opt-in overlay: shipping and cart inspection for all real Boutique callers."""

from copy import deepcopy
import json

from scripts.security.security_lab import CONFIG_DIR, ROUTES, TRUST_DIR, config_files, envoy_config, tls_context


NAMESPACE = "sentinel-boutique-security"
CALLERS = ("frontend", "checkoutservice")
# Each protected route (security_lab.ROUTES): callers' sidecars dial the
# gateway, which dials the service's own TLS sidecar.
# scripts/security/network_policy.py rewrites the graph's edges to each
# protected service into this chain.
GATEWAY = ("security-gateway", 8443)
GATEWAY_SERVICES = {"shipping": "security-gateway", "cart": "security-cart-gateway"}
UPSTREAM_SERVICES = {"shipping": "security-upstream", "cart": "security-cart-upstream"}
INGRESS_CONTAINERS = {"shipping": "shipping-ingress", "cart": "cart-ingress"}
# The inspector's capacity in the gateway Pod (runtime/security_inspector.py).
INSPECTOR_MAX_INFLIGHT = 16
INSPECTOR_MAX_RPS = 200
# Every Envoy container the overlay adds runs exactly this; the Guard pins it for the reviewed sidecars.
ENVOY_COMMAND = ["envoy"]
ENVOY_ARGS = ["-c", "/config/envoy.json", "--concurrency", "2", "--log-level", "warn"]
ENVOY_RESOURCES = {"requests": {"cpu": "20m", "memory": "48Mi"}, "limits": {"cpu": "300m", "memory": "192Mi"}}
# P5.05: the Verifier's own client identity. Its SAN is accepted at the gateway
# only (never at a service's ingress sidecar), and its probe Pods may dial
# only the gateway's route ports, so an authenticated health probe travels the
# same inspected path as the real callers.
VERIFIER_IDENTITY = "sentinel-verifier-client"
VERIFIER_PROBE_APP = "sentinel-verifier-probe"
PROBE_PROTO_CONFIGMAP = "security-test-proto"
GATEWAY_SERVER_NAME = "gateway.internal"
# P5.06: the public trust bundle (the lab CA, or the old and new CA during a
# rollover) that every Envoy mounts at TRUST_DIR; keys stay in each identity's
# cert-manager Secret, security-<identity>.
TRUST_BUNDLE = "security-trust-bundle"
DNS_RULE = {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}


def _address(cluster, host, port):
    cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]["socket_address"].update(
        address=host, port_value=port)


def caller_config(identity):
    """The caller's egress sidecar: one plaintext loopback listener per route, each dialling its gateway port."""
    listeners, clusters = [], []
    for route, spec in ROUTES.items():
        config = envoy_config(upstream=True, route=route)
        listener = config["static_resources"]["listeners"][0]
        listener["address"]["socket_address"].update(address="127.0.0.1", port_value=spec["egress_port"])
        listener["filter_chains"][0].pop("transport_socket")
        cluster = config["static_resources"]["clusters"][0]
        _address(cluster, GATEWAY_SERVICES[route], spec["gateway_port"])
        cluster["transport_socket"] = {"name": "envoy.transport_sockets.tls",
                                      "typed_config": tls_context(identity, client=True, server_name="gateway.internal")}
        listeners.append(listener)
        clusters.append(cluster)
    return {"static_resources": {"listeners": listeners, "clusters": clusters}}


def proxy_config(upstream=False, route="shipping"):
    spec = ROUTES[route]
    config = envoy_config(upstream=upstream, route=route)
    _address(config["static_resources"]["clusters"][0], "127.0.0.1" if upstream else UPSTREAM_SERVICES[route],
             spec["port"] if upstream else spec["upstream_port"])
    validation = (config["static_resources"]["listeners"][0]["filter_chains"][0]["transport_socket"]["typed_config"]
                  ["common_tls_context"]["combined_validation_context"]["default_validation_context"])
    identities = ["proxy-client.internal"] if upstream else [
        "frontend-client.internal", "checkoutservice-client.internal", "client.internal", VERIFIER_IDENTITY + ".internal"]
    validation["match_typed_subject_alt_names"] = [{"san_type": "DNS", "matcher": {"exact": name}} for name in identities]
    return config


def gateway_config():
    """The gateway's Envoy: each route's inspecting listener and upstream cluster, and one inspector cluster."""
    listeners, clusters = [], []
    for route in ROUTES:
        resources = proxy_config(route=route)["static_resources"]
        listeners.extend(resources["listeners"])
        clusters.extend(cluster for cluster in resources["clusters"]
                        if cluster["name"] != "inspector" or not any(seen["name"] == "inspector" for seen in clusters))
    return {"static_resources": {"listeners": listeners, "clusters": clusters}}


def overlay(base, *, envoy_image, inspector_image):
    result = deepcopy(base)
    items = result["items"]
    def resource(kind, name, **fields):
        return {"apiVersion": "v1", "kind": kind, "metadata": {"name": name, "namespace": NAMESPACE}, **fields}
    def configmap(name, config):
        # envoy.json and the SDS files it subscribes to; they name key paths, never keys (P5.06).
        items.append(resource("ConfigMap", name, data=config_files(config, node=name)))
    def config_volume(name):
        return {"name": name, "configMap": {"name": name}}
    def key_volume(identity):
        return {"name": identity, "secret": {"secretName": "security-" + identity, "defaultMode": 288}}
    def envoy(name, identities, ports):
        return {"name": "envoy", "image": envoy_image, "imagePullPolicy": "Never", "command": list(ENVOY_COMMAND),
                "args": list(ENVOY_ARGS),
                "ports": [{"containerPort": port} for port in ports],
                "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "runAsNonRoot": True,
                                    "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": deepcopy(ENVOY_RESOURCES),
                "volumeMounts": [{"name": name, "mountPath": CONFIG_DIR, "readOnly": True},
                                 {"name": TRUST_BUNDLE, "mountPath": TRUST_DIR, "readOnly": True}] + [
                    {"name": identity, "mountPath": "/pki/" + identity, "readOnly": True} for identity in identities]}
    for item in items:
        name = item.get("metadata", {}).get("name")
        if item["kind"] == "Deployment" and name in CALLERS:
            pod = item["spec"]["template"]["spec"]
            # The upstream clients speak plaintext only to their own loopback
            # sidecar; both network legs use authenticated TLS.
            for route, spec in ROUTES.items():
                matches = [entry for entry in pod["containers"][0].get("env", []) if entry["name"] == spec["env"]]
                if len(matches) != 1 or matches[0].get("value") != f"{spec['service']}:{spec['port']}":
                    raise ValueError("Reviewed " + route + " caller wiring changed")
                matches[0]["value"] = f"127.0.0.1:{spec['egress_port']}"
            identity = name + "-client"
            config = "security-" + name + "-egress"
            sidecar = envoy(config, [identity], [spec["egress_port"] for spec in ROUTES.values()])
            sidecar["name"] = "protected-egress"
            pod["containers"].append(sidecar)
            pod.setdefault("volumes", []).extend([config_volume(config), config_volume(TRUST_BUNDLE), key_volume(identity)])
            pod.setdefault("securityContext", {})["fsGroup"] = 10001
    for caller in CALLERS:
        configmap("security-" + caller + "-egress", caller_config(caller + "-client"))
    deployments = {item["metadata"]["name"]: item for item in items if item["kind"] == "Deployment"}
    for route, spec in ROUTES.items():
        # Terminate the upstream TLS connection in the workload's own Pod.
        # Cleartext is confined to loopback, never a Pod-to-Pod connection.
        name, identity, port = UPSTREAM_SERVICES[route], spec["upstream_identity"], spec["upstream_port"]
        configmap(name, proxy_config(upstream=True, route=route))
        ingress = envoy(name, [identity], [port])
        ingress["name"] = INGRESS_CONTAINERS[route]
        ingress["readinessProbe"] = {"tcpSocket": {"port": port}, "periodSeconds": 3}
        if spec["service"] not in deployments:
            raise ValueError("Reviewed " + route + " service is missing")
        workload = deployments[spec["service"]]["spec"]["template"]["spec"]
        workload["containers"].append(ingress)
        workload.setdefault("volumes", []).extend([config_volume(name), config_volume(TRUST_BUNDLE), key_volume(identity)])
        workload.setdefault("securityContext", {})["fsGroup"] = 10001
        items.append(resource("Service", name, spec={"selector": {"app": spec["service"]},
                     "ports": [{"name": "grpc-tls", "port": port, "targetPort": port}]}))
    name, identities = GATEWAY[0], ["gateway", "proxy-client"]
    configmap(name, gateway_config())
    gateway = envoy(name, identities, [spec["gateway_port"] for spec in ROUTES.values()])
    gateway["readinessProbe"] = {"tcpSocket": {"port": GATEWAY[1]}, "periodSeconds": 3}
    # The inspector serves only 127.0.0.1, so the kubelet cannot reach it with
    # httpGet; an exec probe fetches /readyz inside the container. While it
    # fails, the Pod leaves both gateway Services and callers are refused.
    readyz = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9001/readyz', timeout=2)"
    inspector = {"name": "inspector", "image": inspector_image, "imagePullPolicy": "Never",
        "args": ["--journal", "/data/telemetry.sqlite3", "--stdout-events",
                 "--max-inflight", str(INSPECTOR_MAX_INFLIGHT), "--max-rps", str(INSPECTOR_MAX_RPS)],
        "readinessProbe": {"exec": {"command": ["python", "-c", readyz]}, "periodSeconds": 5,
                           "timeoutSeconds": 3, "failureThreshold": 2},
        "volumeMounts": [{"name": "audit", "mountPath": "/data"}],
        "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "runAsNonRoot": True,
                            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"cpu": "200m", "memory": "96Mi"}}}
    pod = {"automountServiceAccountToken": False, "terminationGracePeriodSeconds": 5,
           "securityContext": {"fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
           "containers": [gateway, inspector],
           "volumes": [config_volume(name), config_volume(TRUST_BUNDLE)] + [key_volume(key) for key in identities]
                      + [{"name": "audit", "persistentVolumeClaim": {"claimName": "security-audit"}}]}
    items.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name}}, "spec": pod}}})
    # One Service per route port, so each matrix destination has one port.
    for route, spec in ROUTES.items():
        items.append(resource("Service", GATEWAY_SERVICES[route], spec={"selector": {"app": name},
                     "ports": [{"name": "grpc-tls", "port": spec["gateway_port"], "targetPort": spec["gateway_port"]}]}))
    items.append(resource("PersistentVolumeClaim", "security-audit", spec={"accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "1Gi"}}}))
    items.extend(protect_policies())
    return result


def protect_policies():
    """The reviewed ingress chain per route: callers -> gateway -> the service's TLS sidecar.

    Each protected service accepts only the gateway, on its ingress sidecar's
    port, so its plaintext port is closed to every Pod; the gateway accepts
    the callers and the lab's test identity on its route ports.
    """
    gateway_ports = [spec["gateway_port"] for spec in ROUTES.values()]
    chains = [(spec["service"], [GATEWAY[0]], [spec["upstream_port"]]) for spec in ROUTES.values()]
    chains.append((GATEWAY[0], [*CALLERS, "security-test", VERIFIER_PROBE_APP], gateway_ports))
    policies = [{"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                 "metadata": {"name": "protect-" + target, "namespace": NAMESPACE},
                 "spec": {"podSelector": {"matchLabels": {"app": target}}, "policyTypes": ["Ingress"],
                          "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": source}}} for source in sources],
                                       "ports": [{"protocol": "TCP", "port": port} for port in ports]}]}}
                for target, sources, ports in chains]
    # P5.05: the Verifier's probe Pods may send only DNS and the gateway's route ports, so an
    # authenticated probe can never reach a protected service, its sidecar or any other workload.
    policies.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                     "metadata": {"name": "protect-" + VERIFIER_PROBE_APP + "-egress", "namespace": NAMESPACE},
                     "spec": {"podSelector": {"matchLabels": {"app": VERIFIER_PROBE_APP}}, "policyTypes": ["Egress"],
                              "egress": [deepcopy(DNS_RULE),
                                         {"to": [{"podSelector": {"matchLabels": {"app": GATEWAY[0]}}}],
                                          "ports": [{"protocol": "TCP", "port": port} for port in gateway_ports]}]}})
    return policies


def reviewed_sidecars(envoy_image):
    """The Guard's allowed_sidecars for the protected services: each route's TLS ingress sidecar (P5.05).

    One entry per protected service, exactly as overlay() adds it: the
    route's ingress container, the reviewed Envoy digest, its command and
    arguments, and its three
    read-only mounts (its Envoy configuration ConfigMap, the trust bundle
    ConfigMap and its identity's cert-manager Secret, P5.06).
    tests/test_protected_recovery.py checks that this matches the overlay's
    container, so a changed overlay needs a new review.
    """
    entries = []
    for route, spec in ROUTES.items():
        config, identity = UPSTREAM_SERVICES[route], spec["upstream_identity"]
        entries.append({"target": spec["service"], "name": INGRESS_CONTAINERS[route], "image": envoy_image,
                        "command": list(ENVOY_COMMAND), "args": list(ENVOY_ARGS),
                        "mounts": [{"volume": config, "mount_path": CONFIG_DIR,
                                    "source": {"kind": "ConfigMap", "name": config}},
                                   {"volume": TRUST_BUNDLE, "mount_path": TRUST_DIR,
                                    "source": {"kind": "ConfigMap", "name": TRUST_BUNDLE}},
                                   {"volume": identity, "mount_path": "/pki/" + identity,
                                    "source": {"kind": "Secret", "name": "security-" + identity}}]})
    return entries


def verifier_probe_pod(name, *, image, route, method, request, schema):
    """The Verifier's authenticated grpcurl Pod for one call through the gateway (P5.05).

    It mounts only the Verifier's client Secret and the probe schema, both
    read-only, presents the client certificate, checks the gateway's server
    identity and dials the route's gateway Service: no plaintext and no direct
    service address. Its label admits it at the gateway and nowhere else
    (protect_policies).
    """
    if route not in ROUTES or "@sha256:" not in image:
        raise ValueError("The Verifier probe needs a protected route and a digest-pinned client image")
    args = ["-max-time", "5", "-import-path", "/proto", "-proto", schema,
            "-cacert", "/client/ca.crt", "-cert", "/client/tls.crt", "-key", "/client/tls.key",
            "-servername", GATEWAY_SERVER_NAME, "-d", json.dumps(request),
            f"{GATEWAY_SERVICES[route]}:{ROUTES[route]['gateway_port']}", method]
    container = {"name": "probe", "image": image, "imagePullPolicy": "Never", "args": args,
                 "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
                 "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                     "capabilities": {"drop": ["ALL"]}},
                 "volumeMounts": [{"name": "client", "mountPath": "/client", "readOnly": True},
                                  {"name": "proto", "mountPath": "/proto", "readOnly": True}]}
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": NAMESPACE, "labels": {"app": VERIFIER_PROBE_APP}},
            "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": 45, "automountServiceAccountToken": False,
                     "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "fsGroup": 10001,
                                         "seccompProfile": {"type": "RuntimeDefault"}},
                     "volumes": [{"name": "client", "secret": {"secretName": "security-" + VERIFIER_IDENTITY,
                                                               "defaultMode": 288}},
                                 {"name": "proto", "configMap": {"name": PROBE_PROTO_CONFIGMAP}}],
                     "containers": [container]}}
