"""Validated offline and RCA-informed proposals requiring Guard authorization."""

from copy import deepcopy
import json
from pathlib import Path

from services.guard.common import json_sha256, utc_timestamp
from shared.contracts import ContractError, validate_message


def retrieve_runbooks(incident):
    """Retrieve trusted local failed-rollout guidance; no external model call."""
    reasons = " ".join(signal["reason"] for signal in incident["signals"]).lower()
    if not any(word in reasons for word in ("crash", "startup", "rollout", "backoff", "unavailable")):
        return []
    path = Path(__file__).resolve().parents[2] / "runbooks/failed-rollout.md"
    return [{"id": "failed-rollout", "content": path.read_text(encoding="utf-8")}]


def mock_provider(context):
    """Deterministic offline provider; does not claim LLM or RCA capability."""
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


def propose(incident: dict, baselines: list[dict], retriever, provider, *, diagnosis=None) -> dict:
    """Return a bound action, preserving offline and diagnosis-based providers.

    Offline providers return action, parameters, rationale and evidence_refs.
    RCA providers return action, parameters and rationale; incident evidence is
    bound by the planner and diagnosis remains advisory to the provider.
    """
    validate_message(incident)
    if incident["kind"] != "incident" or not isinstance(baselines, list):
        raise ContractError("Planner requires an incident and a baseline list")
    if diagnosis is not None and (not isinstance(diagnosis, dict)
                                 or diagnosis.get("incident_id") != incident["incident_id"]):
        raise ContractError("Diagnosis belongs to a different incident")
    incident = deepcopy(incident)
    suitable = _eligible_baselines(incident, baselines)
    evidence = [item["evidence_id"] for item in incident["signals"]]
    runbooks = _retrieve(incident, retriever, diagnosis) if suitable and (
        diagnosis is None or diagnosis.get("prediction")) else None
    if not suitable or not runbooks or (diagnosis is not None and not diagnosis.get("prediction")):
        answer = {"action": "ESCALATE", "parameters": {},
                  "rationale": "No suitable saved healthy baseline or relevant runbook is available, or RCA abstained.",
                  "evidence_refs": evidence}
    else:
        context = {"incident": incident, "baselines": suitable, "runbooks": runbooks}
        if diagnosis is not None:
            context["diagnosis"] = diagnosis
        answer = provider(deepcopy(context))
        fields = {"action", "parameters", "rationale"}
        if diagnosis is None:
            fields.add("evidence_refs")
        if not isinstance(answer, dict) or set(answer) != fields:
            raise ContractError("Provider response must contain only typed proposal fields")
        answer = deepcopy(answer)
        if diagnosis is not None:
            answer["evidence_refs"] = evidence
    message = {"schema_version": "1.0", "kind": "proposal",
               "incident_id": incident["incident_id"], "target": incident["target"],
               "based_on_resource_version": incident["observed_resource_version"], **deepcopy(answer)}
    message["action_id"] = "ACT-" + json_sha256(message)[:24]
    validate_message(message)
    if diagnosis is None and message["action"] not in {"ROLLBACK", "ESCALATE"}:
        raise ContractError("Offline Planner supports only saved-template rollback or escalation")
    if message["action"] == "ROLLBACK" and not any(
            message["parameters"] == {key: item[key] for key in ("baseline_id", "template_sha256")}
            for item in suitable):
        raise ContractError("Provider selected an unavailable or changed healthy baseline")
    if not set(message["evidence_refs"]) <= set(evidence):
        raise ContractError("Provider invented an evidence reference")
    return message
