"""Code-owned decision context for agent planning: vocabulary, reason taxonomy and rollback eligibility.

The live diagnosis may name only a component and a reason that this module
supplies. Components come from the reviewed topology (for Boutique,
deploy/boutique/dependencies.json) plus the incident target; reasons are the
fixed taxonomy below. Nothing here is read from telemetry or model output.

rollback_eligibility() decides from code facts alone whether ROLLBACK may be
proposed: the diagnosed reason is one a rollback can repair, the component is
the target itself, the incident signals show a rollout, startup, crash,
back-off or image pull symptom, the live Pod template differs from the healthy baseline
(services/planner/change.py), and that change is recent. The decision is
recorded with decided_by "rule". A model may downgrade an eligible case to
ESCALATE; it can never turn an ineligible case into ROLLBACK.
"""
import math
import re

from agent.untrusted import label

# Root cause reasons the Reporter may choose from, in the order shown to it.
REASONS = (
    "faulty configuration rollout",
    "container startup failure",
    "resource exhaustion",
    "network fault",
    "dependency failure",
    "insufficient evidence",
)
# Reasons a rollback to the measured healthy template can repair.
ROLLBACK_ELIGIBLE = frozenset({"faulty configuration rollout", "container startup failure"})
# Words in an incident signal's reason that show a failed rollout or startup.
# retrieve_runbooks() also accepts "unavailable"; unavailability alone is not a rollback symptom.
# "errimage" and "imagepull" match kubelet's ErrImagePull, ErrImageNeverPull and
# ImagePullBackOff: a Pod that cannot get its image, as after a rollout to a bad
# image (scenario R2). The same symptom without a template change (N1) is still
# refused by the template check.
SYMPTOM_WORDS = ("rollout", "startup", "crash", "backoff", "errimage", "imagepull")
# Why a ROLLBACK is not eligible, in the order they are checked. The first one
# found is the saved escalation code (services/planner/worker.py).
INELIGIBLE_CODES = ("INELIGIBLE_REASON", "COMPONENT_NOT_TARGET", "SYMPTOM_NOT_ROLLOUT",
                    "TEMPLATE_ALREADY_BASELINE", "CHANGE_NOT_RECENT")
DEFAULT_MAX_CHANGE_AGE_SECONDS = 3600
# Decision policies for the evaluation (roadmap P4.04): the default "gated" is P4.
POLICIES = {
    "gated": "P4: the model decides with change evidence, inside the code eligibility gate",
    "rule": "P1: the code eligibility rule decides alone; no Recovery Planner model call",
    "model": "P3: the model decides alone, without change evidence; eligibility is recorded but not enforced",
}
DEFAULT_POLICY = "gated"
# A Kubernetes object name (RFC 1123 subdomain, at most 63 characters here).
_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,61}[a-z0-9])?")
MAX_COMPONENTS = 50


def components(target_name, topology=None):
    """The component vocabulary: the reviewed topology's nodes plus the target, sorted.

    Every name must be a Kubernetes object name, so a vocabulary entry can never
    carry a line break, a heading or instruction text.
    """
    names = [target_name, *((topology or {}).get("nodes") or [])]
    if len(names) > MAX_COMPONENTS or any(not isinstance(name, str) or not _NAME.fullmatch(name)
                                          for name in names):
        raise ValueError("Component vocabulary must be at most 50 Kubernetes object names")
    return sorted(set(names))


def vocabulary(target_name, topology=None):
    return {"components": components(target_name, topology), "reasons": list(REASONS)}


def vocabulary_context(vocab):
    """The candidate sections agent/agent.py::extract_candidates reads, built only from vocab."""
    lines = ["## POSSIBLE ROOT CAUSE COMPONENTS", *("- " + name for name in vocab["components"]),
             "## POSSIBLE ROOT CAUSE REASONS", *("- " + reason for reason in vocab["reasons"])]
    return "\n".join(lines)


def policy(config):
    """The validated decision policy settings of an agent config; ValueError if unusable."""
    name = config.get("decision_policy", DEFAULT_POLICY)
    if name not in POLICIES:
        raise ValueError("decision_policy must be one of " + ", ".join(sorted(POLICIES)))
    age = config.get("max_change_age_seconds", DEFAULT_MAX_CHANGE_AGE_SECONDS)
    if type(age) not in (int, float) or not math.isfinite(age) or not 0 < age <= 7 * 86400:
        raise ValueError("max_change_age_seconds must be positive and at most seven days")
    consult = config.get("consult_model_when_ineligible", False)
    if type(consult) is not bool:
        raise ValueError("consult_model_when_ineligible must be true or false")
    return {"policy": name, "max_change_age_seconds": age, "consult_model_when_ineligible": consult}


def rollback_eligibility(incident, prediction, change, *, max_change_age_seconds):
    """Whether ROLLBACK is eligible, from code facts only; never reads model free text.

    prediction is the validated diagnosis ({"1": {field: value}}), change the
    services/planner/change.py::change_evidence result for the live template.
    """
    failures = [failure for failure in (prediction or {}).values() if isinstance(failure, dict)]
    target = incident["target"]["name"]
    reasons = sorted({str(failure.get("root cause reason")) for failure in failures})
    symptoms = sorted({label(signal["reason"]) for signal in incident["signals"]
                       if any(word in signal["reason"].lower() for word in SYMPTOM_WORDS)})
    age = change.get("change_age_bound_seconds")
    facts = {
        "reasons": reasons,
        "reasons_eligible": bool(failures) and all(
            failure.get("root cause reason") in ROLLBACK_ELIGIBLE for failure in failures),
        "component_is_target": bool(failures) and all(
            failure.get("root cause component") == target for failure in failures),
        "rollout_symptoms": symptoms[:10],
        "template_changed": change.get("changed") is True,
        "change_age_bound_seconds": age,
        "max_change_age_seconds": max_change_age_seconds,
    }
    failed = {
        "INELIGIBLE_REASON": not facts["reasons_eligible"],
        "COMPONENT_NOT_TARGET": not facts["component_is_target"],
        "SYMPTOM_NOT_ROLLOUT": not symptoms,
        "TEMPLATE_ALREADY_BASELINE": not facts["template_changed"],
        # A change whose age bound is unknown or too old: recency cannot be shown, so it is not assumed.
        "CHANGE_NOT_RECENT": facts["template_changed"] and (
            type(age) not in (int, float) or not 0 <= age <= max_change_age_seconds),
    }
    codes = [code for code in INELIGIBLE_CODES if failed[code]]
    return {"decided_by": "rule", "eligible": not codes, "codes": codes, "facts": facts}
