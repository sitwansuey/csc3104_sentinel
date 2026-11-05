"""Incident adapter for the imported, read-only Sentinel RCA engine."""

import json
from pathlib import Path

from shared.contracts import validate_message

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "data/sentinel/samples" / "rca-config.json"


def propose_from_telemetry(incident, baselines, telemetry, workspace, provider, *, config=None, llm=None):
    """Run RCA and hand its findings to an injected recovery decision provider."""
    from services.planner.core import propose
    from agent.workflows import retrieve

    diagnosis = diagnose(incident, telemetry, workspace, config=config, llm=llm)
    return propose(incident, baselines, retrieve, provider, diagnosis=diagnosis)


def diagnose(incident, telemetry, workspace, *, config=None, llm=None):
    """Analyze exported CSV telemetry and persist the full evidence trail.

    The output directory must be new. No cluster actions are performed here.
    """
    validate_message(incident)
    if incident["kind"] != "incident":
        raise ValueError("RCA requires an incident")
    from agent.agent import Agent, FIELDS, save
    from agent.llm import Ollama
    from agent.tools import Telemetry

    settings = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    settings["allow_abstention"] = True
    settings.update(config or {})
    for key in ("max_rounds", "max_steps", "max_depth", "max_rows", "num_ctx", "num_predict"):
        if type(settings[key]) is not int or settings[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    agent = Agent(llm if llm is not None else Ollama(settings),
                  Telemetry(telemetry, settings["max_rows"]), settings, workspace)
    save(Path(workspace) / "incident.json", incident)
    instruction = (
        "Diagnose the initiating root cause of this incident using the supplied telemetry. "
        "Identify the component and reason; distinguish symptoms and downstream effects. "
        "Abstain if evidence is insufficient. Incident signals: "
        + json.dumps(incident["signals"])
    )
    prediction = agent.run(instruction, context=json.dumps({"incident": incident}),
                           required_fields=FIELDS[1:])
    return {"incident_id": incident["incident_id"], "prediction": prediction,
            "report": json.loads((Path(workspace) / "report.json").read_text(encoding="utf-8")),
            "workspace": str(Path(workspace).resolve())}
