"""RCA-informed proposals; execution still requires Guard authorization."""

from copy import deepcopy
from uuid import uuid4
from shared.contracts import validate_message


def propose(incident: dict, baselines: list[dict], retriever, provider, *, diagnosis=None) -> dict:
    """Return a typed action with incident evidence; escalate if no baseline exists."""
    validate_message(incident)
    if incident["kind"] != "incident":
        raise ValueError("Planner requires an incident")
    if diagnosis is not None and diagnosis.get("incident_id") != incident["incident_id"]:
        raise ValueError("Diagnosis belongs to a different incident")
    matching = [b for b in baselines if b.get("target") == incident["target"]]
    proposal = {
        "schema_version": "1.0", "kind": "proposal",
        "incident_id": incident["incident_id"], "target": deepcopy(incident["target"]),
        "action_id": "ACT-" + uuid4().hex,
        "based_on_resource_version": incident["observed_resource_version"],
        "evidence_refs": [s["evidence_id"] for s in incident["signals"]],
    }
    if not matching or (diagnosis is not None and not diagnosis.get("prediction")):
        decision = {"action": "ESCALATE", "parameters": {},
                    "rationale": "No matching baseline or RCA abstained; human review required."}
    else:
        query = " ".join(s["reason"] + " " + s["detail"] for s in incident["signals"])
        decision = provider(deepcopy({"incident": incident, "baselines": matching,
                                     "runbooks": retriever(query), "diagnosis": diagnosis}))
        if not isinstance(decision, dict) or set(decision) != {"action", "parameters", "rationale"}:
            raise ValueError("Provider must return action, parameters and rationale only")
    proposal.update(decision)
    validate_message(proposal)
    if proposal["action"] == "ROLLBACK" and not any(
        b.get("baseline_id") == proposal["parameters"]["baseline_id"]
        and b.get("template_sha256") == proposal["parameters"]["template_sha256"] for b in matching
    ):
        raise ValueError("Rollback must reference a supplied baseline for this target")
    return proposal
