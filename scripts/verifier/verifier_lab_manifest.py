"""Disposable connected-service lab; no production deployment or mutation agent."""

IMAGE = "sentinel-verifier-lab:0.1.0"
COLLECTOR = "otel/opentelemetry-collector-contrib:0.163.2"
COLLECTOR_CONFIG = """receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
exporters:
  file:
    path: /evidence/traces.json
    format: json
    flush_interval: 1s
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [file]
"""


def manifest(namespace):
    items = [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "collector"},
              "data": {"config.yaml": COLLECTOR_CONFIG}}]
    for name in ("checkout", "payment", "collector"):
        container = {"name": name, "image": IMAGE, "imagePullPolicy": "Never",
                     "env": [{"name": "ROLE", "value": name}, {"name": "NAMESPACE", "value": namespace}],
                     "resources": {"requests": {"cpu": "20m", "memory": "32Mi"},
                                   "limits": {"cpu": "200m", "memory": "128Mi"}},
                     "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                         "capabilities": {"drop": ["ALL"]}},
                     "volumeMounts": [{"name": "temporary", "mountPath": "/tmp"}]}
        volumes = [{"name": "temporary", "emptyDir": {}}]
        if name == "collector":
            container["env"][0]["value"] = "bridge"
            container["volumeMounts"].append({"name": "evidence", "mountPath": "/evidence", "readOnly": True})
            containers = [container, {"name": "otel", "image": COLLECTOR, "imagePullPolicy": "Never",
                "args": ["--config=/configuration/config.yaml"],
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                              "limits": {"cpu": "500m", "memory": "256Mi"}},
                "volumeMounts": [{"name": "evidence", "mountPath": "/evidence"},
                                 {"name": "configuration", "mountPath": "/configuration", "readOnly": True}]}]
            volumes.extend([{"name": "evidence", "emptyDir": {"sizeLimit": "8Mi"}},
                            {"name": "configuration", "configMap": {"name": "collector"}}])
        else:
            container["readinessProbe"] = {"httpGet": {"path": "/readyz", "port": 8080},
                                           "periodSeconds": 2, "failureThreshold": 1}
            containers = [container]
        items.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}}, "spec": {
                         "automountServiceAccountToken": False,
                         "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                             "runAsGroup": 10001, "fsGroup": 10001,
                                             "seccompProfile": {"type": "RuntimeDefault"}},
                         "containers": containers, "volumes": volumes}}}})
        ports = [{"name": "http", "port": 8080, "targetPort": 8080}]
        if name == "collector":
            ports.append({"name": "otlp", "port": 4318, "targetPort": 4318})
        items.append({"apiVersion": "v1", "kind": "Service", "metadata": {"name": name},
                      "spec": {"selector": {"app": name}, "ports": ports}})
    for identity, verbs, resources in (("verifier", ["get"], ["checkout", "payment"]),
                                       ("executor", ["get", "patch"], ["checkout"])):
        items.extend([
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": identity}},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": identity},
             "rules": [{"apiGroups": ["apps"], "resources": ["deployments"],
                        "resourceNames": resources, "verbs": verbs}]},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": identity},
             "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": identity},
             "subjects": [{"kind": "ServiceAccount", "name": identity, "namespace": namespace}]}])
    return {"apiVersion": "v1", "kind": "List", "items": items}
