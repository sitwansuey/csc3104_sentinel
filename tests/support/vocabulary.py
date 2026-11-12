"""What a scripted Reporter adds when the live path asks it to cite evidence.

In agent mode the Reporter's schema requires evidence_ids for each failure:
ids of collected tool results that name the diagnosed component
(agent/agent.py::cite). A scripted model that follows that rule cites, from
the evidence quoted in its own prompt, every offered id whose result names
the component. Answers that already carry evidence_ids, and schemas that do
not ask for them, are left unchanged.
"""
from copy import deepcopy
import json

from agent.agent import FIELDS, mentions
from agent.untrusted import UNTRUSTED_KEY

# A reason from the code-owned taxonomy (services/planner/taxonomy.py::REASONS) that a rollback can repair.
STARTUP = "container startup failure"


def offered_ids(schema):
    items = ((schema.get("properties") or {}).get("failures") or {}).get("items") or {}
    return ((items.get("properties") or {}).get("evidence_ids") or {}).get("items", {}).get("enum") or []


def cite_supporting(answer, prompt, schema):
    ids = offered_ids(schema)
    if not ids or not isinstance(answer, dict) or not isinstance(answer.get("failures"), list):
        return answer
    data, _ = json.JSONDecoder().raw_decode(prompt)
    quoted = data.get(UNTRUSTED_KEY)
    evidence = {item["evidence_id"]: item["result"] for item in quoted if "result" in item} \
        if isinstance(quoted, list) else {}
    answer = deepcopy(answer)
    for failure in answer["failures"]:
        component = failure.get(FIELDS[1]) if isinstance(failure, dict) else None
        if isinstance(component, str) and "evidence_ids" not in failure:
            failure["evidence_ids"] = [i for i in ids if i in evidence and mentions(evidence[i], component)] \
                or ids[:1]
    return answer
