"""Exercise Guard v2 using fabricated inputs only; never contact Kubernetes."""

import argparse
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.demo_guard import DEFAULT_NOW, load_fixtures
from services.guard import evaluate_with_report, proposal_fingerprint, template_sha256


SCENARIOS = {
    "allowed": ("ALLOW", "VALIDATION_PASSED"),
    "expired-baseline": ("REJECT", "BASELINE_AGE"),
    "untrusted-health": ("REJECT", "BASELINE_HEALTH_SOURCE"),
    "privileged-template": ("REJECT", "SECURITY_PRIVILEGED"),
    "config-changed": ("REJECT", "BASELINE_REFERENCE_CHANGED"),
    "pod-budget": ("REJECT", "WORKLOAD_POD_REQUEST_EXCEEDS_POLICY"),
    "rollout-budget": ("REJECT", "WORKLOAD_ROLLOUT_REQUEST_EXCEEDS_POLICY"),
    "missing-preview": ("ESCALATE", "SERVER_DRY_RUN_REQUIRED"),
    "admission-mutation": ("REJECT", "SERVER_DRY_RUN_REJECTED"),
}


def _utc(value):
    return value.isoformat().replace("+00:00", "Z")


def _bind(inputs):
    """Fabricate mutually bound records after changing a scenario candidate.

    This simulates trusted stores for the offline demonstration only. A live
    planner must never create its own approval, health evidence or API preview.
    """
    proposal, baseline, now = inputs["proposal"], inputs["baseline"], inputs["now"]
    digest = template_sha256(baseline["pod_template"])
    baseline["template_sha256"] = digest
    baseline["health_evidence"]["template_sha256"] = digest
    proposal["parameters"]["template_sha256"] = digest
    fingerprint = proposal_fingerprint(proposal)
    inputs["approval"] = {
        "action_id": proposal["action_id"], "proposal_sha256": fingerprint,
        "policy_version": inputs["policy"]["policy_version"],
        "operator_id": "SYNTHETIC-DEMO-OPERATOR", "decision": "APPROVED",
        "approved_at": _utc(now - timedelta(seconds=5)),
        "expires_at": _utc(now + timedelta(seconds=295)),
    }
    inputs["preview"] = {
        "status": "PASSED", "proposal_sha256": fingerprint,
        "target": deepcopy(proposal["target"]),
        "resource_version": inputs["live_state"]["deployment"]["metadata"]["resourceVersion"],
        "template_sha256": digest, "checked_at": _utc(now - timedelta(seconds=1)),
        "details_code": "SYNTHETIC_DEMO_PREVIEW_NOT_A_SERVER_RESULT",
        "admitted_template_sha256": digest,
    }


def build_inputs():
    """Return deterministic synthetic inputs with the full live-policy shape."""
    inputs = deepcopy(load_fixtures())
    inputs["now"] = datetime.fromisoformat(DEFAULT_NOW.replace("Z", "+00:00"))
    baseline = inputs["baseline"]
    baseline["pod_template"]["spec"]["containers"][0]["image"] = (
        "registry.example.test/sentinel/demo@sha256:" + "1" * 64
    )
    baseline["reference_versions"] = []
    inputs["live_state"]["reference_versions"] = []
    baseline["health_evidence"] = {
        "source": "synthetic-demo-verifier", "evidence_id": "SYNTHETIC-HEALTH-001",
        "status": "HEALTHY", "target": deepcopy(inputs["proposal"]["target"]),
        "template_sha256": "pending", "window_start": "2026-10-10T11:58:30Z",
        "window_end": baseline["healthy_observed_at"],
    }
    inputs["policy"].update({
        "profile": "live", "policy_id": "synthetic-guard-v2-demo",
        "policy_version": "synthetic-guard-v2-demo-1",
        "max_state_age_seconds": 15, "max_dependency_age_seconds": 30,
        "decision_ttl_seconds": 10, "max_approval_lifetime_seconds": 300,
        "require_server_dry_run": True, "max_server_dry_run_age_seconds": 10,
        "resource_constraints": {
            "require_requests_and_limits": True,
            "max_container_requests": {"cpu": "100m", "memory": "64Mi"},
            "max_container_limits": {"cpu": "500m", "memory": "128Mi"},
        },
        "workload_constraints": {
            "max_pod_requests": {"cpu": "250m", "memory": "128Mi"},
            "max_rollout_requests": {"cpu": "1", "memory": "512Mi"},
            "max_replicas": 3, "max_surge_replicas": 1,
        },
        "baseline_constraints": {
            "max_age_seconds": 300, "min_healthy_duration_seconds": 30,
            "trusted_sources": ["synthetic-demo-verifier"],
            "require_health_evidence": True, "require_compatible_references": True,
            "require_image_digests": True,
        },
        "security_constraints": {
            "deny_privileged": True, "deny_host_access": True,
            "allowed_image_registries": ["registry.example.test"],
            "allowed_service_accounts": ["default"],
            "forbid_service_account_change": True,
        },
        "note": "FABRICATED INPUTS ONLY. Live-shaped policy exercises validation; these identities, evidence, approval and preview are not real authorization.",
    })
    _bind(inputs)
    return inputs


def scenario_inputs(name):
    if name not in SCENARIOS:
        raise ValueError("Unknown demonstration scenario")
    inputs = build_inputs()
    baseline, policy, live = inputs["baseline"], inputs["policy"], inputs["live_state"]
    spec = baseline["pod_template"]["spec"]
    if name == "expired-baseline":
        end = inputs["now"] - timedelta(seconds=600)
        baseline["healthy_observed_at"] = _utc(end)
        baseline["health_evidence"].update(window_start=_utc(end - timedelta(seconds=60)), window_end=_utc(end))
    elif name == "untrusted-health":
        baseline["health_evidence"]["source"] = "untrusted-planner"
    elif name == "privileged-template":
        spec["containers"][0]["securityContext"]["privileged"] = True
    elif name == "config-changed":
        spec["containers"][0]["envFrom"] = [{"configMapRef": {"name": "demo-settings"}}]
        record = {"kind": "ConfigMap", "namespace": inputs["proposal"]["target"]["namespace"], "name": "demo-settings", "uid": "synthetic-config-uid", "resource_version": "4"}
        baseline["reference_versions"] = [record]
        live["reference_versions"] = [dict(record, resource_version="5")]
    elif name == "pod-budget":
        template = deepcopy(spec["containers"][0])
        template["resources"]["requests"]["cpu"] = "75m"
        spec["containers"] = [dict(deepcopy(template), name=f"synthetic-app-{index}") for index in range(4)]
    elif name == "rollout-budget":
        live["deployment"]["spec"].update(replicas=3, strategy={"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}})
        policy["workload_constraints"]["max_rollout_requests"] = {"cpu": "100m", "memory": "64Mi"}
    # Rebind hashes/approval/preview after mutations: each experiment exercises
    # its intended rule rather than failing accidentally on a stale hash.
    _bind(inputs)
    if name == "missing-preview":
        inputs["preview"] = None
    elif name == "admission-mutation":
        inputs["preview"]["admitted_template_sha256"] = "0" * 64
    return inputs


def run_scenario(name):
    decision, report = evaluate_with_report(**scenario_inputs(name))
    expected, reason = SCENARIOS[name]
    if decision["verdict"] != expected or reason not in decision["reason_codes"]:
        raise RuntimeError(f"Scenario {name} did not reach its intended rule")
    return {"expected_verdict": expected, "decision": decision, "report": report}


def export_inputs(directory, scenarios):
    """Create a new fixture directory exclusively; never overwrite a file."""
    directory.mkdir(parents=True, exist_ok=False)
    notice = {
        "source": "synthetic_development_demonstration",
        "warning": "Approvals, health evidence and admission previews are fabricated. No real cluster was read or modified. Never use these files to authorize live writes.",
        "fixture_evaluation_time": DEFAULT_NOW,
    }
    with (directory / "NOTICE.json").open("x", encoding="utf-8") as stream:
        json.dump(notice, stream, indent=2)
    for name in scenarios:
        location = directory / name
        location.mkdir()
        for key, value in scenario_inputs(name).items():
            if key == "now":
                continue
            with (location / f"{key.replace('_', '-')}.json").open("x", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2, allow_nan=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    parser.add_argument("--write-inputs", type=Path, metavar="NEW_DIRECTORY", help="Export fabricated JSON inputs into a new directory; existing directories are rejected")
    args = parser.parse_args()
    scenarios = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    result = {name: run_scenario(name) for name in scenarios}
    if args.write_inputs is not None:
        try:
            export_inputs(args.write_inputs, scenarios)
        except OSError:
            parser.error("Cannot create fixture export; choose a writable directory that does not exist")
    print(json.dumps({
        "source": "synthetic_development_demonstration",
        "execution": {"mode": "evaluation_only", "persistent_writes_attempted": 0, "cluster_contacted": False},
        "notice": "Health evidence, operator approvals and server preview results are fabricated for offline testing.",
        "scenarios": result,
    }, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
