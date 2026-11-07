"""Read a live Deployment and feed it through the Observer pipeline.

This smoke script uses kubectl so the repository does not need a Python
Kubernetes dependency for local development. It performs no recovery action.
"""

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from services.observer.kubernetes_adapter import DeploymentObservationMonitor
from services.observer.core import Observer
from shared.contracts import validate_message
from scripts.validation.observer_live_checks import assess_report


DEFAULT_POLICY = ROOT / "data/sentinel/samples" / "policy.json"


class KubectlAppsApi:
    """Small AppsV1-like reader backed by kubectl get deployment -o json."""

    def __init__(self, *, kubectl="kubectl", context=None, kubeconfig=None):
        self.kubectl = kubectl
        self.context = context
        self.kubeconfig = kubeconfig

    def read_namespaced_deployment(self, *, name, namespace):
        command = [self.kubectl]
        if self.kubeconfig:
            command.extend(["--kubeconfig", self.kubeconfig])
        if self.context:
            command.extend(["--context", self.context])
        command.extend([
            "--request-timeout=10s",
            "--namespace",
            namespace,
            "get",
            "deployment",
            name,
            "-o",
            "json",
        ])
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=15)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "kubectl failed"
            raise RuntimeError(detail)
        return json.loads(result.stdout)


def load_policy(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def observation_summary(observation):
    deployment = observation["deployment"]
    metadata = deployment["metadata"]
    status = deployment["status"]
    return {
        "captured_at": observation["captured_at"],
        "uid": metadata["uid"],
        "resourceVersion": metadata["resourceVersion"],
        "namespace": metadata["namespace"],
        "deployment": metadata["name"],
        "generation": metadata["generation"],
        "observedGeneration": status["observedGeneration"],
        "desiredReplicas": deployment["spec"]["replicas"],
        "replicas": status["replicas"],
        "updatedReplicas": status["updatedReplicas"],
        "readyReplicas": status["readyReplicas"],
        "availableReplicas": status["availableReplicas"],
        "unavailableReplicas": status["unavailableReplicas"],
        "conditions": status["conditions"],
        "events": observation["events"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="sentinel-demo")
    parser.add_argument("--deployment", default="demo-api")
    parser.add_argument("--cluster-id", default="sentinel-lab")
    parser.add_argument("--context")
    parser.add_argument("--kubeconfig")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--poll-count", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    parser.add_argument("--max-consecutive-errors", type=int, default=3)
    parser.add_argument("--include-observations", action="store_true")
    parser.add_argument("--expect", choices=("healthy", "persistent-failure"))
    parser.add_argument("--minimum-window-seconds", type=float, default=60)
    parser.add_argument("--output", type=Path, help="Save JSON evidence even when acceptance checks fail")
    args = parser.parse_args(argv)
    if args.poll_count < 1:
        parser.error("--poll-count must be positive")
    if not math.isfinite(args.interval_seconds) or args.interval_seconds <= 0:
        parser.error("--interval-seconds must be finite and positive")
    if not math.isfinite(args.minimum_window_seconds) or args.minimum_window_seconds < 60:
        parser.error("--minimum-window-seconds must be finite and at least 60")

    policy = load_policy(args.policy)
    observer = Observer(policy)
    apps_api = KubectlAppsApi(
        kubectl=args.kubectl,
        context=args.context,
        kubeconfig=args.kubeconfig,
    )
    observations = []
    errors = []
    emissions = []

    def on_observation(observation):
        observations.append(observation)

    def on_incident(incident):
        emissions.append({
            "poll_index": len(observations) - 1,
            "incident_id": incident["incident_id"],
        })

    def on_error(error):
        errors.append(str(error))

    monitor = DeploymentObservationMonitor(
        observer,
        apps_api,
        namespace=args.namespace,
        name=args.deployment,
        cluster_id=args.cluster_id,
        interval_seconds=args.interval_seconds,
        max_consecutive_errors=args.max_consecutive_errors,
        on_observation=on_observation,
        on_incident=on_incident,
        on_error=on_error,
    )
    result = monitor.run(max_iterations=args.poll_count)
    incidents = result["incidents"]
    for incident in incidents:
        validate_message(incident)

    report = {
        "namespace": args.namespace,
        "deployment": args.deployment,
        "cluster_id": args.cluster_id,
        "poll_count": args.poll_count,
        "interval_seconds": args.interval_seconds,
        "failure_duration_seconds": observer.failure_duration.total_seconds(),
        "polls": [observation_summary(item) for item in observations],
        "emissions": emissions,
        "observations_read": result["observations"],
        "errors": errors,
        "incident_count": len(incidents),
        "incidents": incidents,
        "latest_observation": observation_summary(observations[-1]) if observations else None,
    }
    if args.include_observations:
        report["observations"] = observations
    if args.expect:
        report["validation"] = assess_report(
            report, args.expect, window_seconds=args.minimum_window_seconds,
        )
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    failed = bool(errors) or result["observations"] != args.poll_count
    if args.expect:
        failed = failed or not report["validation"]["passed"]
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
