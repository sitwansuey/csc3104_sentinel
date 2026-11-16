"""Synthetic inputs for a file-mode run, rebuilt from the committed samples.

The committed samples (data/sentinel/samples) use fixed 2026-10-10 times and
the original lab's cluster name. A multi-process run uses real wall clocks, so
every timestamp is moved by one offset to end just before the run starts, and
the cluster name is replaced with the file cluster's lab name. The policy is
the strict live profile the Guard and Executor workers require, with a short
health window so that verification takes seconds. Everything here is labelled
synthetic: no value comes from a cluster.
"""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re

from services.guard.core import template_sha256

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "data" / "sentinel" / "samples"
CLUSTER_ID = "file-sentinel-distributed"
SAMPLE_CLUSTER = "sentinel-lab"
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")
SYNTHETIC = "synthetic_development_fixture"


def _sample(name):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _text(moment):
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def rebase(value, offset):
    """Copy JSON data with every UTC timestamp moved and the sample cluster renamed."""
    if isinstance(value, dict):
        return {key: rebase(item, offset) for key, item in value.items()}
    if isinstance(value, list):
        return [rebase(item, offset) for item in value]
    if isinstance(value, str):
        if STAMP.fullmatch(value):
            return _text(_utc(value) + offset)
        if value == SAMPLE_CLUSTER:
            return CLUSTER_ID
    return value


def live_policy(policy):
    """The strict live profile (as in tests/test_live_executor.py) with a seconds-long health window."""
    policy = deepcopy(policy)
    policy.update({
        "profile": "live", "decision_ttl_seconds": 300,
        "max_approval_lifetime_seconds": 300,
        "max_state_age_seconds": 30, "max_dependency_age_seconds": 300, "max_clock_skew_seconds": 2,
        "baseline_constraints": {
            "max_age_seconds": 3600, "min_healthy_duration_seconds": 30,
            "trusted_sources": ["synthetic-verifier"], "require_health_evidence": True,
            "require_compatible_references": True, "require_image_digests": True,
        },
        "security_constraints": {
            "deny_privileged": True, "deny_host_access": True, "forbid_service_account_change": True,
            "allowed_image_registries": ["docker.io"], "allowed_service_accounts": ["default"],
        },
        "resource_constraints": {
            "require_requests_and_limits": True,
            "max_container_requests": {"cpu": "100m", "memory": "64Mi"},
            "max_container_limits": {"cpu": "500m", "memory": "128Mi"},
        },
        "workload_constraints": {
            "max_pod_requests": {"cpu": "100m", "memory": "64Mi"},
            "max_rollout_requests": {"cpu": "1", "memory": "1Gi"},
            "max_replicas": 3, "max_surge_replicas": 1,
        },
    })
    policy["policy_version"] = "distributed-file-policy-v1"
    policy["health"] = {"stable_window_seconds": 2, "minimum_samples": 3, "max_sample_gap_seconds": 2,
                        "required_http_status": 200}
    policy["note"] = "Synthetic live-profile policy for single-host multi-process fault runs."
    return policy


def build(now=None):
    """Return the inputs of one file-mode run, ending two seconds before now."""
    now = now or datetime.now(UTC)
    series = _sample("observations.json")["failed-rollout"]
    offset = now - timedelta(seconds=2) - _utc(series[-1]["captured_at"])
    observations = rebase(series, offset)
    baseline = rebase(_sample("baseline.json"), offset)
    target = deepcopy(baseline["target"])
    baseline["pod_template"]["spec"]["containers"][0]["image"] = "demo/sentinel@sha256:" + "a" * 64
    digest = template_sha256(baseline["pod_template"])
    baseline.update(template_sha256=digest, reference_versions=[], health_evidence={
        "source": "synthetic-verifier", "evidence_id": "synthetic-window", "status": "HEALTHY",
        "target": deepcopy(target), "template_sha256": digest,
        "window_start": _text(_utc(baseline["healthy_observed_at"]) - timedelta(seconds=60)),
        "window_end": baseline["healthy_observed_at"]})
    policy = live_policy(rebase(_sample("policy.json"), offset))
    graph = rebase(_sample("dependencies.json"), offset)
    return {"observations": observations, "snapshot": deepcopy(observations[-1]), "policy": policy,
            "baseline": baseline, "graph": graph, "target": target,
            "detection_policy": rebase(_sample("policy.json"), offset), "source": SYNTHETIC}
