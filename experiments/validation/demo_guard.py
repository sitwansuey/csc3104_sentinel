"""Demonstrate offline Guard decisions using copied synthetic fixtures."""

import argparse
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from services.guard.core import evaluate, proposal_fingerprint
from shared.canonical import sha256_hex


DEFAULT_NOW = "2026-10-10T12:00:40Z"
SCENARIOS = (
    "valid-approved",
    "missing-approval",
    "forbidden-target",
    "forbidden-action",
    "stale-state",
    "tampered-baseline",
    "blocked-dependencies",
    "unknown-dependencies",
)


def utc_timestamp(value: str) -> datetime:
    """Parse the CLI clock without accepting naive or non-UTC timestamps."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an ISO UTC timestamp") from exc
    if parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError("expected a timezone-aware UTC timestamp")
    return parsed


def load_fixtures() -> dict:
    def read(relative_path: str) -> dict:
        return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))

    return {
        "proposal": read("data/sentinel/samples/messages/proposal.json"),
        "live_state": read("data/sentinel/samples/observations.json")["failed-rollout"][-1],
        "policy": read("data/sentinel/samples/policy.json"),
        "baseline": read("data/sentinel/samples/baseline.json"),
        "dependencies": read("data/sentinel/samples/messages/dependency-result.json"),
    }


def scale_proposal(fixtures: dict) -> dict:
    """A well-formed SCALE (P5.07 shape) for an action the demo policy does not allow."""
    proposal = deepcopy(fixtures["proposal"])
    proposal["action"] = "SCALE"
    proposal["parameters"] = {
        "baseline_id": fixtures["proposal"]["parameters"]["baseline_id"],
        "desired_replicas": 2,
        "template_sha256": sha256_hex(fixtures["live_state"]["deployment"]["spec"]["template"]),
    }
    return proposal


def run_scenario(name: str, fixtures: dict, now: datetime) -> dict:
    """Modify only copies; every approval is a fabricated development record."""
    inputs = deepcopy(fixtures)
    proposal = inputs["proposal"]
    policy = inputs["policy"]
    dependencies = inputs["dependencies"]
    if name == "forbidden-target":
        policy["targets"][0]["name"] = "other-deployment"
        policy["policy_version"] = "demo-policy-forbidden-target-v1"
    elif name == "forbidden-action":
        proposal = inputs["proposal"] = scale_proposal(inputs)
    elif name == "stale-state":
        inputs["live_state"]["deployment"]["metadata"]["resourceVersion"] = "103"
    elif name == "tampered-baseline":
        containers = inputs["baseline"]["pod_template"]["spec"]["containers"]
        containers[0]["image"] = "sentinel-demo:tampered"
    elif name == "blocked-dependencies":
        dependencies["status"] = "BLOCKED"
        dependencies["blockers"] = ["mock-db recovery would be disrupted"]
    elif name == "unknown-dependencies":
        dependencies["status"] = "UNKNOWN"
        dependencies["unknowns"] = ["mock-db health could not be determined"]
    elif name not in {"valid-approved", "missing-approval"}:
        raise ValueError(f"Unknown scenario: {name}")

    approval = None
    if name != "missing-approval":
        approval = {
            "action_id": proposal["action_id"],
            "proposal_sha256": proposal_fingerprint(proposal),
            "policy_version": policy["policy_version"],
            "operator_id": "demo-operator",
            "decision": "APPROVED",
            "approved_at": "2026-10-10T12:00:35Z",
        }
    return evaluate(**inputs, approval=approval, now=now)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    parser.add_argument(
        "--now", type=utc_timestamp, default=utc_timestamp(DEFAULT_NOW),
        help=f"UTC evaluation time; fixed fixture default: {DEFAULT_NOW}",
    )
    args = parser.parse_args()
    fixtures = load_fixtures()
    if args.scenario == "all":
        result = {name: run_scenario(name, fixtures, args.now) for name in SCENARIOS}
    else:
        result = run_scenario(args.scenario, fixtures, args.now)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
