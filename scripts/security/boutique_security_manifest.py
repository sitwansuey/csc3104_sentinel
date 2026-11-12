"""Pure opt-in overlay: shipping inspection for all real Boutique callers."""

from copy import deepcopy
import json

from scripts.security.security_lab import envoy_config, tls_context


NAMESPACE = "sentinel-boutique-security"
CALLERS = ("frontend", "checkoutservice")
# The shipping route: callers' sidecars dial the gateway, which dials
# shippingservice's own TLS sidecar. scripts/security/network_policy.py
# rewrites the graph's shipping edges to this chain.
GATEWAY = ("security-gateway", 8443)
UPSTREAM_PORT = 8444


def _address(cluster, host, port):
    cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]["socket_address"].update(
        address=host, port_value=port)


def caller_config(identity):
    config = envoy_config(upstream=True)
    listener = config["static_resources"]["listeners"][0]
    listener["address"]["socket_address"].update(address="127.0.0.1", port_value=50052)
    listener["filter_chains"][0].pop("transport_socket")
    cluster = config["static_resources"]["clusters"][0]
    _address(cluster, "security-gateway", 8443)
    cluster["transport_socket"] = {"name": "envoy.transport_sockets.tls",
                                  "typed_config": tls_context(identity, client=True, server_name="gateway.internal")}
    return config


def proxy_config(upstream=False):
    config = envoy_config(upstream=upstream)
    _address(config["static_resources"]["clusters"][0], "127.0.0.1" if upstream else "security-upstream",
             50051 if upstream else 8444)
    validation = config["static_resources"]["listeners"][0]["filter_chains"][0]["transport_socket"]["typed_config"]["common_tls_context"]["validation_context"]
    identities = ["proxy-client.internal"] if upstream else ["frontend-client.internal", "checkoutservice-client.internal", "client.internal"]
    validation["match_typed_subject_alt_names"] = [{"san_type": "DNS", "matcher": {"exact": name}} for name in identities]
    return config


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
    def envoy(name, identities, port):
        return {"name": "envoy", "image": envoy_image, "imagePullPolicy": "Never", "command": ["envoy"],
                "args": ["-c", "/config/envoy.json", "--concurrency", "2", "--log-level", "warn"],
                "ports": [{"containerPort": port}],
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
            matches = [entry for entry in pod["containers"][0].get("env", []) if entry["name"] == "SHIPPING_SERVICE_ADDR"]
            if len(matches) != 1:
                raise ValueError("Reviewed shipping caller wiring changed")
            matches[0]["value"] = "127.0.0.1:50052"
            identity = name + "-client"
            config = "security-" + name + "-egress"
            sidecar = envoy(config, [identity], 50052)
            sidecar["name"] = "shipping-egress"
            pod["containers"].append(sidecar)
            pod.setdefault("volumes", []).extend([config_volume(config), key_volume(identity)])
            pod.setdefault("securityContext", {})["fsGroup"] = 10001
    for caller in CALLERS:
        configmap("security-" + caller + "-egress", caller_config(caller + "-client"))
    shipping = next(item for item in items if item["kind"] == "Deployment" and item["metadata"]["name"] == "shippingservice")
    for name, identity, port in (("security-upstream", "upstream", 8444), ("security-gateway", "gateway", 8443)):
        configmap(name, proxy_config(upstream=identity == "upstream"))
        identities = [identity] + (["proxy-client"] if identity == "gateway" else [])
        pod = {"automountServiceAccountToken": False, "terminationGracePeriodSeconds": 5,
               "securityContext": {"fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
               "containers": [envoy(name, identities, port)],
               "volumes": [config_volume(name)] + [key_volume(key) for key in identities]}
        pod["containers"][0]["readinessProbe"] = {"tcpSocket": {"port": port}, "periodSeconds": 3}
        if identity == "gateway":
            pod["volumes"].append({"name": "audit", "persistentVolumeClaim": {"claimName": "security-audit"}})
            pod["containers"].append({"name": "inspector", "image": inspector_image, "imagePullPolicy": "Never",
                "args": ["--journal", "/data/telemetry.sqlite3", "--stdout-events"],
                "volumeMounts": [{"name": "audit", "mountPath": "/data"}],
                "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "runAsNonRoot": True,
                                    "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"cpu": "200m", "memory": "96Mi"}}})
        if identity == "upstream":
            # Terminate the upstream TLS connection in the workload's own Pod.
            # Cleartext is confined to loopback, never a Pod-to-Pod connection.
            workload = shipping["spec"]["template"]["spec"]
            ingress = pod["containers"][0]
            ingress["name"] = "shipping-ingress"
            workload["containers"].append(ingress)
            workload.setdefault("volumes", []).extend(pod["volumes"])
            workload.setdefault("securityContext", {})["fsGroup"] = 10001
            selected_app = "shippingservice"
        else:
            items.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": NAMESPACE},
                "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app": name}},
                         "template": {"metadata": {"labels": {"app": name}}, "spec": pod}}})
            selected_app = name
        items.append(resource("Service", name, spec={"selector": {"app": selected_app},
                     "ports": [{"name": "grpc-tls", "port": port, "targetPort": port}]}))
    items.append(resource("PersistentVolumeClaim", "security-audit", spec={"accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "1Gi"}}}))
    for target, sources, port in (("shippingservice", [GATEWAY[0]], UPSTREAM_PORT),
                                  (GATEWAY[0], [*CALLERS, "security-test"], GATEWAY[1])):
        items.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "protect-" + target, "namespace": NAMESPACE},
            "spec": {"podSelector": {"matchLabels": {"app": target}}, "policyTypes": ["Ingress"],
                     "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": source}}} for source in sources],
                                  "ports": [{"protocol": "TCP", "port": port}]}]}})
    return result
