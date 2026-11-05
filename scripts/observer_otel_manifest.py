"""Generate the fixed-scope Buddy 2 connected-telemetry lab manifest as JSON."""

import json

NAMESPACE = "sentinel-observer-otel"
OBSERVER_IMAGE = "sentinel-observer:0.3.0"
WORKLOAD_IMAGE = "sentinel-otel-validation:0.1.0"
COLLECTOR_IMAGE = "otel/opentelemetry-collector:0.163.2"


def manifest():
    def metadata(name):
        return {"name": name, "namespace": NAMESPACE}

    def service(name, port):
        return {"apiVersion": "v1", "kind": "Service", "metadata": metadata(name),
                "spec": {"selector": {"app": name}, "ports": [{"port": port, "targetPort": port}]}}

    def deployment(name, containers, *, volumes=(), service_account="default"):
        for container in containers:
            container.setdefault("resources", {"requests": {"cpu": "50m", "memory": "64Mi"},
                                               "limits": {"cpu": "500m", "memory": "256Mi"}})
            container["securityContext"] = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                             "capabilities": {"drop": ["ALL"]}}
        return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata(name), "spec": {
            "replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": {"app": name}}, "spec": {
                "serviceAccountName": service_account, "automountServiceAccountToken": service_account != "default",
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                    "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": containers, "volumes": list(volumes)}}}}

    targets = [{"namespace": NAMESPACE, "deployment": name, "service_names": [name]}
               for name in ("otel-caller", "otel-callee")]
    policy = {"detection": {"failure_duration_seconds": 10, "max_sample_gap_seconds": 45},
              "telemetry": {"max_age_seconds": 15, "metric_thresholds": [
                  {"name": "sentinel.request.failed", "unit": "1", "above": 0}]}}
    collector = {"receivers": {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}},
                 "exporters": {"otlp_http/observer": {"endpoint": "http://sentinel-observer-otel:4319",
                    "encoding": "json", "compression": "gzip", "headers": {
                        "Authorization": "Bearer ${env:SENTINEL_OTLP_TOKEN}"},
                    "retry_on_failure": {"enabled": True}, "sending_queue": {"enabled": True, "queue_size": 100}}},
                 "service": {"pipelines": {signal: {"receivers": ["otlp"], "exporters": ["otlp_http/observer"]}
                                           for signal in ("traces", "metrics", "logs")}}}
    items = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": metadata("sentinel-observer")},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": metadata("sentinel-observer"),
         "rules": [{"apiGroups": ["apps"], "resources": ["deployments"],
                    "resourceNames": ["otel-caller", "otel-callee"], "verbs": ["get"]},
                   {"apiGroups": ["apps"], "resources": ["replicasets"], "verbs": ["list"]},
                   {"apiGroups": [""], "resources": ["pods", "events"], "verbs": ["list"]},
                   {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": metadata("sentinel-observer"),
         "subjects": [{"kind": "ServiceAccount", "name": "sentinel-observer", "namespace": NAMESPACE}],
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "sentinel-observer"}},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": metadata("observer-data"),
         "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "256Mi"}}}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata("observer-config"),
         "data": {"targets.json": json.dumps(targets), "policy.json": json.dumps(policy)}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata("otel-config"),
         "data": {"collector.yaml": json.dumps(collector)}},
    ]
    items.append(deployment("sentinel-observer-otel", [
        {"name": "worker", "image": OBSERVER_IMAGE, "imagePullPolicy": "Never",
         "args": ["run", "--in-cluster", "--cluster-id=sentinel-observer-otel", "--targets=/config/targets.json",
                  "--state=/data/observer.sqlite3", "--coordinator-db=/data/coordinator.sqlite3",
                  "--telemetry-db=/data/telemetry.sqlite3", "--policy=/config/policy.json", "--interval-seconds=2", "--collect-logs"],
         "volumeMounts": [{"name": "data", "mountPath": "/data"}, {"name": "config", "mountPath": "/config", "readOnly": True}],
         "readinessProbe": {"exec": {"command": ["python", "-m", "services.observer_cli", "fleet-health",
             "--state=/data/observer.sqlite3", "--targets=/config/targets.json", "--cluster-id=sentinel-observer-otel"]},
             "initialDelaySeconds": 5, "periodSeconds": 5, "timeoutSeconds": 5}},
        {"name": "receiver", "image": OBSERVER_IMAGE, "imagePullPolicy": "Never",
         "command": ["python", "-m", "runtime.otlp_receiver"],
         "args": ["--db=/data/telemetry.sqlite3", "--bind=0.0.0.0", "--port=4319", "--token-file=/auth/token"],
         "ports": [{"containerPort": 4319}],
         "volumeMounts": [{"name": "data", "mountPath": "/data"}, {"name": "auth", "mountPath": "/auth", "readOnly": True}],
         "readinessProbe": {"httpGet": {"path": "/healthz", "port": 4319}, "periodSeconds": 5}}
    ], volumes=[{"name": "data", "persistentVolumeClaim": {"claimName": "observer-data"}},
                {"name": "config", "configMap": {"name": "observer-config"}},
                {"name": "auth", "secret": {"secretName": "observer-otlp-token", "defaultMode": 288}}],
        service_account="sentinel-observer"))
    receiver_service = service("sentinel-observer-otel", 4319)
    # Ingestion must remain reachable while the worker is waiting for evidence.
    receiver_service["spec"]["publishNotReadyAddresses"] = True
    items.append(receiver_service)
    items.append(deployment("otel-collector", [{"name": "collector", "image": COLLECTOR_IMAGE,
        "args": ["--config=/config/collector.yaml"], "ports": [{"containerPort": 4318}],
        "env": [{"name": "SENTINEL_OTLP_TOKEN", "valueFrom": {"secretKeyRef": {"name": "observer-otlp-token", "key": "token"}}}],
        "volumeMounts": [{"name": "config", "mountPath": "/config", "readOnly": True}],
        "readinessProbe": {"tcpSocket": {"port": 4318}, "periodSeconds": 5}}],
        volumes=[{"name": "config", "configMap": {"name": "otel-config"}}]))
    items.append(service("otel-collector", 4318))
    for name in ("otel-callee", "otel-caller"):
        env = [{"name": "SERVICE_NAME", "value": name},
               {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
               {"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
               {"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": "http://otel-collector:4318"}]
        if name == "otel-caller":
            env.append({"name": "UPSTREAM_URL", "value": "http://otel-callee:8080/work"})
        items.append(deployment(name, [{"name": "app", "image": WORKLOAD_IMAGE, "imagePullPolicy": "Never", "env": env,
            "ports": [{"containerPort": 8080}], "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 2}}]))
        items.append(service(name, 8080))
    items.append(deployment("otel-load", [{"name": "load", "image": WORKLOAD_IMAGE, "imagePullPolicy": "Never",
                                           "command": ["python", "load.py"]}]))
    return {"apiVersion": "v1", "kind": "List", "items": items}


if __name__ == "__main__":
    print(json.dumps(manifest(), indent=2))
