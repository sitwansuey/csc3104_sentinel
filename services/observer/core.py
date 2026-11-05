"""Owner 2: turn recorded observations into contract-valid incident messages."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from copy import deepcopy
import hashlib
import json

from shared.contracts import ContractError, validate_message


DEFAULT_FAILURE_DURATION_SECONDS = 30
TARGET_FIELDS = ("cluster_id", "namespace", "kind", "name", "uid")


class ObservationError(ContractError):
    """An observation is not usable as incident evidence."""


@dataclass(frozen=True)
class ObservationSnapshot:
    observation: dict
    captured_at: datetime
    source: str
    target: dict
    resource_version: str
    generation: int
    observed_generation: int
    desired_replicas: int
    ready_replicas: int
    available_replicas: int


@dataclass
class FailureEpisode:
    correlation_key: tuple
    first_seen_at: datetime
    last_seen_at: datetime
    first_snapshot: ObservationSnapshot
    last_snapshot: ObservationSnapshot
    emitted: bool = False


def detect(observations: list[dict], policy: dict) -> dict | None:
    """Return the first incident from an ordered observation batch, if any."""
    incidents = detect_all(observations, policy)
    return incidents[0] if incidents else None


def detect_all(observations: list[dict], policy: dict) -> list[dict]:
    """Return all newly detected incidents from an ordered observation batch."""
    observer = Observer(policy)
    incidents = []
    for observation in _observation_batch(observations):
        incident = observer.observe(observation)
        if incident is not None:
            incidents.append(incident)
    return incidents


class Observer:
    """Stateful detection with checkpointable failure episodes and bounded history."""

    def __init__(self, policy: dict):
        self.failure_duration = _failure_duration(policy)
        detection = policy.get("detection") or {}
        self.max_sample_gap = detection.get("max_sample_gap_seconds")
        if self.max_sample_gap is not None:
            _integer(self.max_sample_gap, "policy.detection.max_sample_gap_seconds", minimum=1)
        self._latest_snapshots: dict[tuple, ObservationSnapshot] = {}
        self._episodes_by_target: dict[tuple, FailureEpisode] = {}

    def observe(self, observation: dict) -> dict | None:
        """Process one observation and return a new incident only when warranted."""
        snapshot = validate_observation(observation)
        signals = _failure_signals(snapshot)
        target_key = _target_key(snapshot.target)
        previous = self._latest_snapshots.get(target_key)
        if previous is not None and (snapshot.captured_at < previous.captured_at or (
                snapshot.captured_at == previous.captured_at and snapshot.generation <= previous.generation)):
            return None
        for old_key in list(self._latest_snapshots):
            if old_key[:-1] == target_key[:-1] and old_key != target_key:
                self._latest_snapshots.pop(old_key)
                self._episodes_by_target.pop(old_key, None)
        if target_key not in self._latest_snapshots and len(self._latest_snapshots) >= 1024:
            raise ObservationError("Observer target budget exceeded")
        self._latest_snapshots[target_key] = snapshot
        if not signals:
            self._episodes_by_target.pop(target_key, None)
            return None

        correlation_key = _correlation_key(snapshot)
        episode = self._episodes_by_target.get(target_key)
        if (episode is not None and not episode.emitted and self.max_sample_gap is not None
                and (snapshot.captured_at - episode.last_seen_at).total_seconds() > self.max_sample_gap):
            episode = None
        if episode is None or episode.correlation_key != correlation_key:
            episode = FailureEpisode(
                correlation_key=correlation_key,
                first_seen_at=snapshot.captured_at,
                last_seen_at=snapshot.captured_at,
                first_snapshot=snapshot,
                last_snapshot=snapshot,
            )
            self._episodes_by_target[target_key] = episode
        else:
            episode.last_seen_at = snapshot.captured_at
            episode.last_snapshot = snapshot

        if episode.emitted:
            return None
        if episode.last_seen_at - episode.first_seen_at < self.failure_duration:
            return None

        incident = _incident_from_episode(episode, signals)
        validate_message(incident)
        episode.emitted = True
        return incident


    def collection_gap(self):
        """Unknown collection intervals cannot prove a continuously failing window."""
        self._episodes_by_target = {key: value for key, value in self._episodes_by_target.items() if value.emitted}

    def export_state(self):
        return {
            "version": 1,
            "failure_duration_seconds": self.failure_duration.total_seconds(),
            "max_sample_gap_seconds": self.max_sample_gap,
            "latest": [deepcopy(snapshot.observation) for snapshot in self._latest_snapshots.values()],
            "episodes": [{"first": deepcopy(episode.first_snapshot.observation),
                          "last": deepcopy(episode.last_snapshot.observation), "emitted": episode.emitted}
                         for episode in self._episodes_by_target.values()],
        }

    @classmethod
    def from_state(cls, policy, state):
        observer = cls(policy)
        if (state.get("version") != 1
                or state.get("failure_duration_seconds") != observer.failure_duration.total_seconds()
                or state.get("max_sample_gap_seconds") != observer.max_sample_gap):
            raise ObservationError("Incompatible Observer checkpoint")
        if len(state["latest"]) > 1024 or len(state["episodes"]) > 1024:
            raise ObservationError("Observer checkpoint exceeds target budget")
        for observation in state["latest"]:
            snapshot = validate_observation(observation)
            key = _target_key(snapshot.target)
            if key in observer._latest_snapshots:
                raise ObservationError("Repeated checkpoint target")
            observer._latest_snapshots[key] = snapshot
        for item in state["episodes"]:
            first, last = validate_observation(item["first"]), validate_observation(item["last"])
            key = _target_key(last.target)
            latest = observer._latest_snapshots.get(key)
            if (type(item["emitted"]) is not bool or _correlation_key(first) != _correlation_key(last)
                    or first.captured_at > last.captured_at or latest is None
                    or latest.observation != last.observation or not _failure_signals(first)
                    or not _failure_signals(last) or key in observer._episodes_by_target):
                raise ObservationError("Invalid Observer failure checkpoint")
            observer._episodes_by_target[key] = FailureEpisode(
                correlation_key=_correlation_key(first), first_seen_at=first.captured_at,
                last_seen_at=last.captured_at, first_snapshot=first, last_snapshot=last,
                emitted=item["emitted"],
            )
        return observer


def validate_observation(observation: dict) -> ObservationSnapshot:
    """Validate the fixture-shaped Kubernetes observation used by the observer."""
    observation = _mapping(observation, "observation")
    source = _string(observation.get("source"), "observation.source")
    captured_at = _utc_timestamp(observation.get("captured_at"), "observation.captured_at")
    cluster_id = _string(observation.get("cluster_id"), "observation.cluster_id")

    deployment = _mapping(observation.get("deployment"), "observation.deployment")
    kind = _string(deployment.get("kind"), "observation.deployment.kind")
    if kind != "Deployment":
        raise ObservationError("observation.deployment.kind: only Deployment is supported")
    metadata = _mapping(deployment.get("metadata"), "observation.deployment.metadata")
    for key in ("namespace", "name", "uid", "resourceVersion"):
        _string(metadata.get(key), f"observation.deployment.metadata.{key}")
    generation = _integer(
        metadata.get("generation"), "observation.deployment.metadata.generation", minimum=1
    )

    spec = _mapping(deployment.get("spec"), "observation.deployment.spec")
    desired_replicas = _integer(
        spec.get("replicas"), "observation.deployment.spec.replicas", minimum=0
    )
    status = _mapping(deployment.get("status"), "observation.deployment.status")
    observed_generation = _optional_integer(
        status.get("observedGeneration"),
        "observation.deployment.status.observedGeneration",
        default=generation,
        minimum=0,
    )
    ready_replicas = _optional_integer(
        status.get("readyReplicas"),
        "observation.deployment.status.readyReplicas",
        default=0,
        minimum=0,
    )
    available_replicas = _optional_integer(
        status.get("availableReplicas"),
        "observation.deployment.status.availableReplicas",
        default=0,
        minimum=0,
    )

    _validate_pods(observation.get("pods"))
    _validate_events(observation.get("events"))
    _validate_logs(observation.get("logs"))

    target = {
        "cluster_id": cluster_id,
        "namespace": metadata["namespace"],
        "kind": kind,
        "name": metadata["name"],
        "uid": metadata["uid"],
    }
    return ObservationSnapshot(
        observation=observation,
        captured_at=captured_at,
        source=source,
        target=target,
        resource_version=metadata["resourceVersion"],
        generation=generation,
        observed_generation=observed_generation,
        desired_replicas=desired_replicas,
        ready_replicas=ready_replicas,
        available_replicas=available_replicas,
    )


def _failure_duration(policy: dict) -> timedelta:
    policy = _mapping(policy, "policy")
    detection = policy.get("detection", {})
    if detection is None:
        detection = {}
    detection = _mapping(detection, "policy.detection")
    seconds = detection.get("failure_duration_seconds", DEFAULT_FAILURE_DURATION_SECONDS)
    return timedelta(
        seconds=_integer(seconds, "policy.detection.failure_duration_seconds", minimum=0)
    )


def _observation_batch(observations):
    if not isinstance(observations, list):
        raise ObservationError("observations: expected a list")
    return observations


def _mapping(value, path):
    if not isinstance(value, dict):
        raise ObservationError(f"{path}: expected an object")
    return value


def _string(value, path):
    if not isinstance(value, str) or not value.strip():
        raise ObservationError(f"{path}: expected a nonempty string")
    return value


def _integer(value, path, minimum=0):
    if type(value) is not int or value < minimum:
        raise ObservationError(f"{path}: expected an integer >= {minimum}")
    return value


def _optional_integer(value, path, *, default, minimum=0):
    if value is None:
        return default
    return _integer(value, path, minimum=minimum)


def _utc_timestamp(value, path):
    _string(value, path)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObservationError(f"{path}: invalid ISO timestamp") from exc
    if result.utcoffset() != timedelta(0):
        raise ObservationError(f"{path}: expected a timezone-aware UTC timestamp")
    return result


def _validate_pods(value):
    if not isinstance(value, list):
        raise ObservationError("observation.pods: expected a list")
    for index, pod in enumerate(value):
        pod_path = f"observation.pods[{index}]"
        pod = _mapping(pod, pod_path)
        metadata = _mapping(pod.get("metadata"), f"{pod_path}.metadata")
        _string(metadata.get("name"), f"{pod_path}.metadata.name")
        status = _mapping(pod.get("status"), f"{pod_path}.status")
        _string(status.get("phase"), f"{pod_path}.status.phase")
        for field in ("containerStatuses", "initContainerStatuses"):
            container_statuses = status.get(field, [])
            if not isinstance(container_statuses, list):
                raise ObservationError(f"{pod_path}.status.{field}: expected a list")
            for container_index, container in enumerate(container_statuses):
                container_path = f"{pod_path}.status.{field}[{container_index}]"
                container = _mapping(container, container_path)
                _string(container.get("name"), f"{container_path}.name")
                if type(container.get("ready")) is not bool:
                    raise ObservationError(f"{container_path}.ready: expected a boolean")
                _integer(container.get("restartCount"), f"{container_path}.restartCount")
                _mapping(container.get("state"), f"{container_path}.state")
                _mapping(container.get("lastState", {}), f"{container_path}.lastState")


def _validate_events(value):
    if not isinstance(value, list):
        raise ObservationError("observation.events: expected a list")
    for index, event in enumerate(value):
        event_path = f"observation.events[{index}]"
        event = _mapping(event, event_path)
        for key in ("reason", "type", "message", "lastTimestamp"):
            _string(event.get(key), f"{event_path}.{key}")
        _utc_timestamp(event["lastTimestamp"], f"{event_path}.lastTimestamp")


def _validate_logs(value):
    if not isinstance(value, list):
        raise ObservationError("observation.logs: expected a list")
    for index, entry in enumerate(value):
        log_path = f"observation.logs[{index}]"
        entry = _mapping(entry, log_path)
        for key in ("pod", "container", "text", "at"):
            _string(entry.get(key), f"{log_path}.{key}")
        _utc_timestamp(entry["at"], f"{log_path}.at")


def _failure_signals(snapshot: ObservationSnapshot) -> list[dict]:
    signals = []
    if snapshot.observed_generation < snapshot.generation:
        signals.append(_signal(
            snapshot,
            "deployment_status",
            "RolloutNotObserved",
            (
                f"Deployment generation {snapshot.generation} has not been fully "
                f"observed; status observedGeneration is {snapshot.observed_generation}"
            ),
            snapshot.captured_at,
        ))
    if (
        snapshot.ready_replicas < snapshot.desired_replicas
        or snapshot.available_replicas < snapshot.desired_replicas
    ):
        signals.append(_signal(
            snapshot,
            "deployment_status",
            "UnavailableReplicas",
            (
                f"Deployment {snapshot.target['namespace']}/{snapshot.target['name']} "
                f"desired {snapshot.desired_replicas}, ready {snapshot.ready_replicas}, "
                f"available {snapshot.available_replicas}; resourceVersion "
                f"{snapshot.resource_version}, generation {snapshot.generation}"
            ),
            snapshot.captured_at,
        ))

    for pod in snapshot.observation["pods"]:
        pod_name = pod["metadata"]["name"]
        pod_status = pod["status"]
        pod_phase = pod_status["phase"]
        containers = list(pod_status.get("containerStatuses", []))
        containers.extend(container for container in pod_status.get("initContainerStatuses", [])
                          if container.get("state", {}).get("terminated", {}).get("exitCode") != 0)
        for container in containers:
            reason, detail = _container_failure_detail(pod_name, pod_phase, container)
            if reason is None:
                continue
            signals.append(_signal(snapshot, "pod_status", reason, detail, snapshot.captured_at))

    if not signals and snapshot.desired_replicas == 0:
        return []
    if not signals and not _containers_are_ready(snapshot):
        signals.append(_signal(
            snapshot,
            "pod_status",
            "ContainerNotReady",
            "One or more desired containers are not ready",
            snapshot.captured_at,
        ))
    if not signals:
        return []

    for event in snapshot.observation["events"]:
        if event["type"] != "Warning":
            continue
        signals.append(_signal(
            snapshot,
            "event",
            event["reason"],
            event["message"],
            _utc_timestamp(event["lastTimestamp"], "event.lastTimestamp"),
        ))

    for entry in snapshot.observation["logs"]:
        signals.append(_signal(
            snapshot,
            "container_log",
            _log_reason(entry["text"]),
            entry["text"],
            _utc_timestamp(entry["at"], "log.at"),
        ))

    return signals


def _containers_are_ready(snapshot: ObservationSnapshot) -> bool:
    expected_containers = 0
    ready_containers = 0
    for pod in snapshot.observation["pods"]:
        for container in pod["status"].get("containerStatuses", []):
            expected_containers += 1
            if container["ready"]:
                ready_containers += 1
    if snapshot.desired_replicas == 0:
        return True
    if expected_containers == 0:
        return (
            snapshot.observed_generation >= snapshot.generation
            and snapshot.ready_replicas >= snapshot.desired_replicas
            and snapshot.available_replicas >= snapshot.desired_replicas
        )
    return expected_containers > 0 and ready_containers == expected_containers


def _container_failure_detail(pod_name, pod_phase, container):
    if container["ready"]:
        return None, None
    state = container["state"]
    waiting = state.get("waiting", {})
    terminated = state.get("terminated", {})
    last_terminated = container.get("lastState", {}).get("terminated", {})
    reason = (
        waiting.get("reason")
        or terminated.get("reason")
        or last_terminated.get("reason")
        or "ContainerNotReady"
    )
    parts = [
        f"Pod {pod_name} container {container['name']} is not ready",
        f"phase {pod_phase}",
        f"restartCount {container['restartCount']}",
    ]
    if waiting.get("message"):
        parts.append(waiting["message"])
    if "exitCode" in terminated:
        parts.append(f"current exitCode {terminated['exitCode']}")
    if "exitCode" in last_terminated:
        parts.append(f"last exitCode {last_terminated['exitCode']}")
    return reason, "; ".join(parts)


def _log_reason(text):
    return "StartupError" if "startup" in text.lower() else "ContainerLog"


def _signal(snapshot, source, reason, detail, at):
    return {
        "evidence_id": _stable_id(
            "E",
            snapshot.target,
            source,
            reason,
            detail,
            _format_timestamp(at),
        ),
        "source": source,
        "reason": reason,
        "detail": detail,
        "at": _format_timestamp(at),
    }


def _incident_from_episode(episode: FailureEpisode, signals: list[dict]) -> dict:
    snapshot = episode.last_snapshot
    incident_id = _stable_id(
        "INC",
        snapshot.target,
        episode.first_seen_at.isoformat(),
        episode.correlation_key,
    )
    return {
        "schema_version": "1.0",
        "incident_id": incident_id,
        "target": deepcopy(snapshot.target),
        "kind": "incident",
        "observed_at": _format_timestamp(snapshot.captured_at),
        "observed_resource_version": snapshot.resource_version,
        "observed_generation": snapshot.generation,
        "signals": _unique_signals(_failure_signals(episode.first_snapshot) + signals),
    }


def _unique_signals(signals: list[dict]) -> list[dict]:
    unique = []
    seen = set()
    for signal in signals:
        fingerprint = (signal["source"], signal["reason"], signal["detail"], signal["at"])
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(signal)
    return unique


def _correlation_key(snapshot: ObservationSnapshot) -> tuple:
    # Kubernetes conditions evolve during the same failed rollout. Preserve the
    # initial timer and emitted flag until health returns or a new generation starts.
    return _target_key(snapshot.target) + (snapshot.generation,)


def _target_key(target: dict) -> tuple:
    return tuple(target[field] for field in TARGET_FIELDS)


def _stable_id(prefix, *parts) -> str:
    return f"{prefix}-{_json_sha256(parts)[:12].upper()}"


def _json_sha256(value) -> str:
    try:
        canonical = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ObservationError("observation: expected JSON-serializable content") from exc
    return hashlib.sha256(canonical).hexdigest()


def _format_timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
