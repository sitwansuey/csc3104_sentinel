"""Translate Kubernetes Deployment state into Observer observations.

The adapter owns Kubernetes shape handling only. Incident decisions remain in
services.observer.
"""

from datetime import datetime, timedelta, timezone
from copy import deepcopy
import time

from services.observer import ObservationError, validate_observation


DEFAULT_SOURCE = "kubernetes_api"
FAILED_CONDITION_PRIORITY = {
    ("Progressing", "False"): 0,
    ("ReplicaFailure", "True"): 1,
    ("Available", "False"): 2,
}


class KubernetesObservationError(ObservationError):
    """Kubernetes state cannot be normalized into an Observer observation."""


class DeploymentObservationMonitor:
    """Poll one Deployment and feed normalized observations to an Observer."""

    def __init__(
        self,
        observer,
        apps_api,
        *,
        namespace: str,
        name: str,
        cluster_id: str,
        interval_seconds: float = 15.0,
        max_consecutive_errors: int = 3,
        max_backoff_seconds: float = 30.0,
        source: str = DEFAULT_SOURCE,
        clock=None,
        sleep=None,
        on_observation=None,
        on_incident=None,
        on_error=None,
    ):
        self.observer = observer
        self.apps_api = apps_api
        self.namespace = _string(namespace, "namespace")
        self.name = _string(name, "name")
        self.cluster_id = _string(cluster_id, "cluster_id")
        self.interval_seconds = _nonnegative_number(interval_seconds, "interval_seconds")
        self.max_consecutive_errors = _integer(
            max_consecutive_errors, "max_consecutive_errors", minimum=0
        )
        self.max_backoff_seconds = _nonnegative_number(
            max_backoff_seconds, "max_backoff_seconds"
        )
        self.source = _string(source, "source")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or time.sleep
        self.on_observation = on_observation
        self.on_incident = on_incident
        self.on_error = on_error
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self, *, max_iterations: int | None = None) -> dict:
        """Run until stopped, exhausted, or too many consecutive read errors."""
        if max_iterations is not None:
            _integer(max_iterations, "max_iterations", minimum=0)
        summary = {
            "observations": 0,
            "incidents": [],
            "errors": 0,
            "stopped": False,
        }
        consecutive_errors = 0
        iterations = 0

        while not self._stopped and (
            max_iterations is None or iterations < max_iterations
        ):
            should_continue = True
            try:
                observation = read_deployment_observation(
                    self.apps_api,
                    namespace=self.namespace,
                    name=self.name,
                    cluster_id=self.cluster_id,
                    observed_at=self.clock(),
                    source=self.source,
                )
                consecutive_errors = 0
                summary["observations"] += 1
                if self.on_observation is not None:
                    self.on_observation(observation)
                incident = self.observer.observe(observation)
                if incident is not None:
                    summary["incidents"].append(incident)
                    if self.on_incident is not None:
                        self.on_incident(incident)
            except Exception as exc:
                summary["errors"] += 1
                consecutive_errors += 1
                if self.on_error is not None:
                    self.on_error(exc)
                if consecutive_errors > self.max_consecutive_errors:
                    should_continue = False

            iterations += 1
            if not should_continue:
                break
            if self._should_sleep(max_iterations, iterations):
                self.sleep(self._sleep_seconds(consecutive_errors))

        summary["stopped"] = self._stopped
        return summary

    def _should_sleep(self, max_iterations, iterations):
        return not self._stopped and (max_iterations is None or iterations < max_iterations)

    def _sleep_seconds(self, consecutive_errors):
        if consecutive_errors <= 0:
            return self.interval_seconds
        backoff = self.interval_seconds * consecutive_errors
        return min(backoff, self.max_backoff_seconds)


def deployment_to_observation(
    deployment,
    *,
    cluster_id: str,
    observed_at,
    source: str = DEFAULT_SOURCE,
) -> dict:
    """Normalize one Kubernetes Deployment object or dict for services.observer."""
    captured_at = _format_timestamp(_utc_datetime(observed_at, "observed_at"))
    cluster_id = _string(cluster_id, "cluster_id")
    source = _string(source, "source")
    deployment = _normalize_deployment(deployment)

    observation = {
        "source": source,
        "captured_at": captured_at,
        "cluster_id": cluster_id,
        "deployment": deployment,
        "pods": [],
        "events": _condition_events(deployment, captured_at),
        "logs": [],
    }
    validate_observation(observation)
    return observation


def read_deployment_observation(
    apps_api,
    *,
    namespace: str,
    name: str,
    cluster_id: str,
    observed_at=None,
    source: str = DEFAULT_SOURCE,
) -> dict:
    """Read a Deployment through an injected AppsV1-like API client."""
    observed_at = datetime.now(timezone.utc) if observed_at is None else observed_at
    deployment = apps_api.read_namespaced_deployment(name=name, namespace=namespace)
    return deployment_to_observation(
        deployment,
        cluster_id=cluster_id,
        observed_at=observed_at,
        source=source,
    )


def observe_deployment(
    observer,
    apps_api,
    *,
    namespace: str,
    name: str,
    cluster_id: str,
    observed_at=None,
    source: str = DEFAULT_SOURCE,
) -> dict | None:
    """Read, normalize, and pass one Deployment observation to an Observer."""
    observation = read_deployment_observation(
        apps_api,
        namespace=namespace,
        name=name,
        cluster_id=cluster_id,
        observed_at=observed_at,
        source=source,
    )
    return observer.observe(observation)


def _normalize_deployment(deployment) -> dict:
    deployment = _objectish(deployment, "deployment")
    metadata = _objectish(_field(deployment, "metadata"), "deployment.metadata")
    spec = _objectish(_field(deployment, "spec"), "deployment.spec")
    status = _objectish(_field(deployment, "status"), "deployment.status")

    normalized = {
        "apiVersion": _optional_string(
            _field(deployment, "apiVersion", "api_version"), default="apps/v1"
        ),
        "kind": _optional_string(_field(deployment, "kind"), default="Deployment"),
        "metadata": {
            "name": _string(_field(metadata, "name"), "deployment.metadata.name"),
            "namespace": _string(_field(metadata, "namespace"), "deployment.metadata.namespace"),
            "labels": _copy_mapping(_field(metadata, "labels"), default={}),
            "uid": _string(_field(metadata, "uid"), "deployment.metadata.uid"),
            "resourceVersion": _string(
                _field(metadata, "resourceVersion", "resource_version"),
                "deployment.metadata.resourceVersion",
            ),
            "generation": _integer(
                _field(metadata, "generation"), "deployment.metadata.generation", minimum=1
            ),
        },
        "spec": {
            "replicas": _optional_integer(
                _field(spec, "replicas"), "deployment.spec.replicas", default=1, minimum=0
            )
        },
        "status": {
            "observedGeneration": _optional_integer(
                _field(status, "observedGeneration", "observed_generation"),
                "deployment.status.observedGeneration",
                default=0,
                minimum=0,
            ),
            "replicas": _optional_integer(
                _field(status, "replicas"), "deployment.status.replicas", default=0, minimum=0
            ),
            "updatedReplicas": _optional_integer(
                _field(status, "updatedReplicas", "updated_replicas"),
                "deployment.status.updatedReplicas",
                default=0,
                minimum=0,
            ),
            "readyReplicas": _optional_integer(
                _field(status, "readyReplicas", "ready_replicas"),
                "deployment.status.readyReplicas",
                default=0,
                minimum=0,
            ),
            "availableReplicas": _optional_integer(
                _field(status, "availableReplicas", "available_replicas"),
                "deployment.status.availableReplicas",
                default=0,
                minimum=0,
            ),
            "unavailableReplicas": _optional_integer(
                _field(status, "unavailableReplicas", "unavailable_replicas"),
                "deployment.status.unavailableReplicas",
                default=0,
                minimum=0,
            ),
            "conditions": _conditions(_field(status, "conditions")),
        },
    }
    if normalized["kind"] != "Deployment":
        raise KubernetesObservationError("deployment.kind: only Deployment is supported")
    return normalized


def _condition_events(deployment: dict, captured_at: str) -> list[dict]:
    failed = []
    for condition in deployment["status"]["conditions"]:
        key = (condition["type"], condition["status"])
        if key in FAILED_CONDITION_PRIORITY:
            failed.append(condition)
    failed.sort(key=lambda item: (
        FAILED_CONDITION_PRIORITY[(item["type"], item["status"])],
        item["type"],
        item["reason"],
        item["message"],
    ))
    events = []
    for condition in failed:
        message = condition["message"] or (
            f"Deployment condition {condition['type']} is {condition['status']}"
        )
        events.append({
            "reason": condition["reason"],
            "type": "Warning",
            "message": message,
            "lastTimestamp": condition["lastTransitionTime"] or captured_at,
        })
    return events


def _conditions(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise KubernetesObservationError("deployment.status.conditions: expected a list")
    conditions = []
    for index, condition in enumerate(value):
        condition = _objectish(condition, f"deployment.status.conditions[{index}]")
        conditions.append({
            "type": _string(
                _field(condition, "type"), f"deployment.status.conditions[{index}].type"
            ),
            "status": _string(
                _field(condition, "status"), f"deployment.status.conditions[{index}].status"
            ),
            "reason": _optional_string(
                _field(condition, "reason"), default="DeploymentCondition"
            ),
            "message": _optional_string(_field(condition, "message"), default=""),
            "lastTransitionTime": _optional_timestamp(
                _field(condition, "lastTransitionTime", "last_transition_time")
            ),
        })
    return conditions


def _field(value, *names):
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if not isinstance(value, dict) and hasattr(value, name):
            return getattr(value, name)
    return None


def _objectish(value, path):
    if value is None:
        raise KubernetesObservationError(f"{path}: expected an object")
    if hasattr(value, "to_dict") and not isinstance(value, dict):
        value = value.to_dict()
    if not isinstance(value, dict):
        return value
    return value


def _copy_mapping(value, *, default):
    if value is None:
        return deepcopy(default)
    if hasattr(value, "to_dict") and not isinstance(value, dict):
        value = value.to_dict()
    if not isinstance(value, dict):
        raise KubernetesObservationError("deployment.metadata.labels: expected an object")
    return deepcopy(value)


def _string(value, path):
    if not isinstance(value, str) or not value.strip():
        raise KubernetesObservationError(f"{path}: expected a nonempty string")
    return value


def _optional_string(value, *, default):
    if value is None:
        return default
    if not isinstance(value, str):
        raise KubernetesObservationError("optional string field: expected a string")
    return value


def _integer(value, path, minimum=0):
    if type(value) is not int or value < minimum:
        raise KubernetesObservationError(f"{path}: expected an integer >= {minimum}")
    return value


def _optional_integer(value, path, *, default, minimum=0):
    if value is None:
        return default
    return _integer(value, path, minimum=minimum)


def _nonnegative_number(value, path):
    if type(value) not in (int, float) or value < 0:
        raise KubernetesObservationError(f"{path}: expected a nonnegative number")
    return float(value)


def _optional_timestamp(value):
    if value is None:
        return ""
    return _format_timestamp(_utc_datetime(value, "condition.lastTransitionTime"))


def _utc_datetime(value, path):
    if isinstance(value, datetime):
        result = value
    else:
        _string(value, path)
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise KubernetesObservationError(f"{path}: invalid ISO timestamp") from exc
    if result.utcoffset() != timedelta(0):
        raise KubernetesObservationError(f"{path}: expected a timezone-aware UTC timestamp")
    return result


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
