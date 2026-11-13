"""Pure opt-in overlay: shipping and cart inspection for all real Boutique callers."""

from copy import deepcopy
import json

from scripts.security.security_lab import ROUTES, envoy_config, tls_context


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
    validation = config["static_resources"]["listeners"][0]["filter_chains"][0]["transport_socket"]["typed_config"]["common_tls_context"]["validation_context"]
    identities = ["proxy-client.internal"] if upstream else ["frontend-client.internal", "checkoutservice-client.internal", "client.internal"]
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
        items.append(resource("ConfigMap", name, data={"envoy.json": json.dumps(config)}))
    def config_volume(name):
        return {"name": name, "configMap": {"name": name}}
    def key_volume(identity):
        return {"name": identity, "secret": {"secretName": "security-" + identity, "defaultMode": 288}}
    def envoy(name, identities, ports):
        return {"name": "envoy", "image": envoy_image, "imagePullPolicy": "Never", "command": ["envoy"],
                "args": ["-c", "/config/envoy.json", "--concurrency", "2", "--log-level", "warn"],
                "ports": [{"containerPort": port} for port in ports],
                "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "runAsNonRoot": True,
                                    "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "20m", "memory": "48Mi"}, "limits": {"cpu": "300m", "memory": "192Mi"}},
                "volumeMounts": [{"name": name, "mountPath": "/config", "readOnly": True}] + [
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
            pod.setdefault("volumes", []).extend([config_volume(config), key_volume(identity)])
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
        workload.setdefault("volumes", []).extend([config_volume(name), key_volume(identity)])
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
           "volumes": [config_volume(name)] + [key_volume(key) for key in identities]
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
    chains = [(spec["service"], [GATEWAY[0]], [spec["upstream_port"]]) for spec in ROUTES.values()]
    chains.append((GATEWAY[0], [*CALLERS, "security-test"], [spec["gateway_port"] for spec in ROUTES.values()]))
    return [{"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
             "metadata": {"name": "protect-" + target, "namespace": NAMESPACE},
             "spec": {"podSelector": {"matchLabels": {"app": target}}, "policyTypes": ["Ingress"],
                      "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": source}}} for source in sources],
                                   "ports": [{"protocol": "TCP", "port": port} for port in ports]}]}}
            for target, sources, ports in chains]
