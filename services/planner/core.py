"""Validated offline and RCA-informed proposals requiring Guard authorization."""

from copy import deepcopy
import json
from pathlib import Path

from services.guard.common import json_sha256, utc_timestamp
from shared.actions import config_fingerprint, restart_parameters
from shared.contracts import ContractError, validate_message

# Signal sources that show Pods themselves failing (not ready, crashing, killed).
_POD_FAILURE_SOURCES = frozenset({"pod_status"})
# Signal sources that show the workload failing: Deployment replica status or Pod status.
_WORKLOAD_FAILURE_SOURCES = frozenset({"deployment_status", "pod_status"})
METRIC_ALERT_SOURCE = "otel_metric_alert"
# The fixed rationale of the metric-only rule; a code rule, never a provider, writes it.
METRIC_ONLY_RATIONALE = ("Only metric alerts show a problem: the Deployment and its Pods report no failure, so no "
                         "repair is justified. Decided by a code rule, not a provider. Operator review is required "
                         "(runbook resource-saturation). No repair is authorized.")


def metric_only(incident):
    """True when a metric alert is the incident's only failure evidence.

    Events, logs and correlated telemetry records may accompany it, but no
    Deployment-status or Pod-status signal does: every desired Pod is ready.
    """
    sources = {signal["source"] for signal in incident["signals"]}
    return METRIC_ALERT_SOURCE in sources and not sources & _WORKLOAD_FAILURE_SOURCES


def retrieve_runbooks(incident):
    """Retrieve trusted local recovery guidance by signal keywords; no external model call."""
    reasons = " ".join(signal["reason"] for signal in incident["signals"]).lower()
    folder = Path(__file__).resolve().parents[2] / "runbooks"
    selected = []
    if any(word in reasons for word in ("crash", "startup", "rollout", "backoff", "unavailable")):
        selected.append("failed-rollout")
    if any(word in reasons for word in ("notready", "unhealthy", "oomkilled")):
        selected.append("pod-failure")
    if any(signal["source"] == METRIC_ALERT_SOURCE for signal in incident["signals"]):
        selected.append("resource-saturation")
    return [{"id": name, "content": (folder / (name + ".md")).read_text(encoding="utf-8")} for name in selected]


def mock_provider(context):
    """Deterministic offline provider; does not claim LLM or RCA capability.

    It proposes RESTART only when the Planner's trusted read shows the live
    configuration unchanged from a reviewed baseline and the incident has
    Pod-level failure evidence: then a ROLLBACK would change nothing, and new
    Pods are the remaining repair. It leaves RESTART parameters empty; the
    Planner binds them. Otherwise it proposes the saved-template ROLLBACK.
    """
    if context.get("template_unchanged") and any(
            signal["source"] in _POD_FAILURE_SOURCES for signal in context["incident"]["signals"]):
        return {"action": "RESTART", "parameters": {},
                "rationale": ("Pods are failing while the live configuration equals the reviewed healthy "
                              "baseline, so a rollback would change nothing; restart the Pods from that template."),
                "evidence_refs": [item["evidence_id"] for item in context["incident"]["signals"]]}
    baseline = context["baselines"][0]
    return {"action": "ROLLBACK",
            "parameters": {key: baseline[key] for key in ("baseline_id", "template_sha256")},
            "rationale": "Restore the saved observed healthy template described by the failed-rollout runbook.",
            "evidence_refs": [item["evidence_id"] for item in context["incident"]["signals"]]}


def _eligible_baselines(incident, baselines):
    at = utc_timestamp(incident["observed_at"], "observed_at")
    suitable = []
    for item in baselines:
        if not isinstance(item, dict) or item.get("target") != incident["target"]:
            continue
        try:
            valid = (isinstance(item.get("baseline_id"), str) and bool(item["baseline_id"].strip())
                     and isinstance(item.get("pod_template"), dict) and bool(item["pod_template"])
                     and item.get("template_sha256") == json_sha256(item["pod_template"])
                     and utc_timestamp(item.get("healthy_observed_at"), "baseline time") <= at)
        except ValueError:
            valid = False
        if valid:
            suitable.append(deepcopy(item))
    suitable.sort(key=lambda item: utc_timestamp(item["healthy_observed_at"], "baseline time"), reverse=True)
    return suitable


def _retrieve(incident, retriever, diagnosis):
    if diagnosis is None:
        runbooks = retriever(deepcopy(incident))
        if not isinstance(runbooks, list) or len(runbooks) > 10 or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not isinstance(item.get("content"), str) or not 1 <= len(item["content"]) <= 20000
                for item in runbooks):
            raise ContractError("Retriever must return bounded identified runbooks")
        return runbooks
    query = " ".join(signal["reason"] + " " + signal["detail"] for signal in incident["signals"])
    runbooks = retriever(query)
    # The RCA engine exposes both workflow lists and full process/rule/tool knowledge.
    templates = runbooks.get("kp") if isinstance(runbooks, dict) else runbooks
    if not isinstance(templates, list) or len(templates) > 10 or any(
            not isinstance(template, dict) for template in templates):
        raise ContractError("RCA retriever must return bounded structured workflow knowledge")
    try:
        encoded = json.dumps(runbooks, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise ContractError("RCA workflow knowledge must be JSON serializable") from error
    if len(encoded) > 200000:
        raise ContractError("RCA workflow knowledge exceeds the context limit")
    return runbooks if templates else []


def _unchanged_baselines(suitable, live_template):
    """Suitable baselines whose configuration equals the trusted live template's."""
    if live_template is None:
        return []
    try:
        live = config_fingerprint(live_template)
        return [item for item in suitable if config_fingerprint(item["pod_template"]) == live]
    except (TypeError, ValueError, RecursionError) as error:
        raise ContractError("Trusted live template is not a valid Pod template") from error


def _bind_restart(answer, incident, unchanged):
    """A provider names RESTART with empty parameters; only the Planner binds them."""
    if answer.get("action") != "RESTART":
        return answer
    if answer.get("parameters") != {}:
        raise ContractError("RESTART parameters are bound by the Planner; the provider must leave them empty")
    if not unchanged:
        raise ContractError("RESTART requires a trusted live template whose configuration equals a reviewed baseline")
    return {**answer, "parameters": restart_parameters(incident["incident_id"], unchanged[0])}


def propose(incident: dict, baselines: list[dict], retriever, provider, *, diagnosis=None,
            live_template=None) -> dict:
    """Return a bound action, preserving offline and diagnosis-based providers.

    Offline providers return action, parameters, rationale and evidence_refs.
    RCA providers return action, parameters and rationale; incident evidence is
    bound by the planner and diagnosis remains advisory to the provider.
    live_template, when given, is a trusted read of the target's current Pod
    template. The context then says whether its configuration is unchanged from
    a suitable baseline, and only then may a provider choose RESTART.

    A metric-only incident (metric_only()) is escalated by a code rule before
    any provider call: when every Pod is ready, neither a rollback nor a restart
    is a justified repair, whatever a provider or model would answer.
    """
    validate_message(incident)
    if incident["kind"] != "incident" or not isinstance(baselines, list):
        raise ContractError("Planner requires an incident and a baseline list")
    if diagnosis is not None and (not isinstance(diagnosis, dict)
                                 or diagnosis.get("incident_id") != incident["incident_id"]):
        raise ContractError("Diagnosis belongs to a different incident")
    incident = deepcopy(incident)
    suitable = _eligible_baselines(incident, baselines)
    unchanged = _unchanged_baselines(suitable, live_template)
    evidence = [item["evidence_id"] for item in incident["signals"]]
    saturation = metric_only(incident)
    runbooks = _retrieve(incident, retriever, diagnosis) if not saturation and suitable and (
        diagnosis is None or diagnosis.get("prediction")) else None
    if saturation:
        answer = {"action": "ESCALATE", "parameters": {}, "rationale": METRIC_ONLY_RATIONALE,
                  "evidence_refs": evidence}
    elif not suitable or not runbooks or (diagnosis is not None and not diagnosis.get("prediction")):
        answer = {"action": "ESCALATE", "parameters": {},
                  "rationale": "No suitable saved healthy baseline or relevant runbook is available, or RCA abstained.",
                  "evidence_refs": evidence}
    else:
        context = {"incident": incident, "baselines": suitable, "runbooks": runbooks}
        if live_template is not None:
            context["template_unchanged"] = bool(unchanged)
        if diagnosis is not None:
            context["diagnosis"] = diagnosis
        answer = provider(deepcopy(context))
        fields = {"action", "parameters", "rationale"}
        if diagnosis is None:
            fields.add("evidence_refs")
        if not isinstance(answer, dict) or set(answer) != fields:
            raise ContractError("Provider response must contain only typed proposal fields")
        answer = _bind_restart(deepcopy(answer), incident, unchanged)
        if diagnosis is not None:
            answer["evidence_refs"] = evidence
    message = {"schema_version": "1.0", "kind": "proposal",
               "incident_id": incident["incident_id"], "target": incident["target"],
               "based_on_resource_version": incident["observed_resource_version"], **deepcopy(answer)}
    message["action_id"] = "ACT-" + json_sha256(message)[:24]
    validate_message(message)
    if diagnosis is None and message["action"] not in {"ROLLBACK", "RESTART", "ESCALATE"}:
        raise ContractError("Offline Planner supports only saved-template rollback, restart or escalation")
    if message["action"] == "ROLLBACK" and not any(
            message["parameters"] == {key: item[key] for key in ("baseline_id", "template_sha256")}
            for item in suitable):
        raise ContractError("Provider selected an unavailable or changed healthy baseline")
    if not set(message["evidence_refs"]) <= set(evidence):
        raise ContractError("Provider invented an evidence reference")
    return message
