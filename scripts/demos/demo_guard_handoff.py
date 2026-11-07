"""Exercise the real Guard against a fake consumer and conditional-write API.

This is an executable integration example, not the production Executor. It has
no Kubernetes/network access, persistent ledger, distributed lock or retries.
"""

import argparse
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.demos.demo_guard import DEFAULT_NOW, load_fixtures, utc_timestamp
from services.guard.core import assert_decision_usable, evaluate, proposal_fingerprint, template_sha256
from shared.contracts import ContractError


SCENARIOS = (
    "allowed", "rejected", "missing-approval", "invalid-state",
    "state-changed-before-recheck", "state-changed-after-recheck",
    "approval-expired-before-recheck", "approval-revoked-before-recheck",
    "dependency-blocked-before-recheck", "resource-limit-exceeded",
)


class FakeCluster:
    """Only successful compare-and-swap operations count as writes."""

    def __init__(self, snapshot):
        self.snapshot = deepcopy(snapshot)
        self.write_attempts = []
        self.writes = []

    def read(self):
        return deepcopy(self.snapshot)

    def external_change(self):
        self.snapshot["deployment"]["metadata"]["resourceVersion"] = "fake-external-change"

    def conditional_restore(self, *, target, version, template):
        self.write_attempts.append({
            "target": deepcopy(target), "version": version,
            "template": deepcopy(template),
        })
        deployment = self.snapshot["deployment"]
        metadata = deployment["metadata"]
        actual_target = {
            "cluster_id": self.snapshot["cluster_id"], "kind": deployment["kind"],
            "namespace": metadata["namespace"], "name": metadata["name"],
            "uid": metadata["uid"],
        }
        if actual_target != target or metadata["resourceVersion"] != version:
            return False
        deployment["spec"]["template"] = deepcopy(template)
        metadata["resourceVersion"] = "fake-after-write"
        self.writes.append(deepcopy(template))
        return True


def run_scenario(name, fixtures=None):
    if name not in SCENARIOS:
        raise ValueError(f"Unknown scenario: {name}")
    inputs = deepcopy(load_fixtures() if fixtures is None else fixtures)
    now = utc_timestamp(DEFAULT_NOW)
    proposal = inputs["proposal"]
    cluster = FakeCluster(inputs.pop("live_state"))
    inputs["approval"] = {
        "action_id": proposal["action_id"],
        "proposal_sha256": proposal_fingerprint(proposal),
        "policy_version": inputs["policy"]["policy_version"],
        "operator_id": "fabricated-demo-operator",
        "decision": "APPROVED",
        "approved_at": (now - timedelta(seconds=1)).isoformat(),
        "expires_at": (now + timedelta(seconds=1)).isoformat(),
    }
    if name == "rejected":
        inputs["policy"]["targets"] = []
    elif name == "missing-approval":
        inputs["approval"] = None
    elif name == "invalid-state":
        del cluster.snapshot["deployment"]["metadata"]["resourceVersion"]
    elif name == "resource-limit-exceeded":
        inputs["policy"]["resource_constraints"] = {
            "max_container_limits": {"cpu": "100m"},
        }

    summary = {"status": "NOT_ATTEMPTED", "decisions": [], "write_attempts": 0, "writes": 0}

    def check():
        # Fresh provider lookup on each call; never reuse an old ALLOW result.
        current = deepcopy(inputs)
        current["live_state"] = cluster.read()
        decision = evaluate(**current, now=now)
        summary["decisions"].append(decision)
        return decision, current

    try:
        initial, _ = check()
        if initial["verdict"] != "ALLOW":
            return summary
        if name == "state-changed-before-recheck":
            cluster.external_change()
        elif name == "approval-expired-before-recheck":
            now += timedelta(seconds=1)
        elif name == "approval-revoked-before-recheck":
            inputs["approval"] = None
        elif name == "dependency-blocked-before-recheck":
            inputs["dependencies"]["status"] = "BLOCKED"
            inputs["dependencies"]["blockers"] = ["dependency changed after planning"]
        final, checked_inputs = check()
        if final["verdict"] != "ALLOW":
            return summary
        if name == "state-changed-after-recheck":
            cluster.external_change()
        assert_decision_usable(final, **checked_inputs, now=now)
        applied = cluster.conditional_restore(
            target=checked_inputs["proposal"]["target"],
            version=final["validated_resource_version"],
            template=checked_inputs["baseline"]["pod_template"],
        )
        summary["status"] = "APPLIED" if applied else "CONFLICT"
        if applied:
            summary["applied_template_sha256"] = template_sha256(cluster.writes[0])
    except ContractError:
        summary["status"] = "INPUT_ERROR"
    finally:
        # Count actual API calls even on an early return or input-error path.
        summary["write_attempts"] = len(cluster.write_attempts)
        summary["writes"] = len(cluster.writes)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    args = parser.parse_args()
    names = SCENARIOS if args.scenario == "all" else (args.scenario,)
    print(json.dumps({name: run_scenario(name) for name in names}, indent=2))


if __name__ == "__main__":
    main()
