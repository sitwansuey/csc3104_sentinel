"""Incident adapter for the imported, read-only Sentinel RCA engine."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from shared.contracts import validate_message

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "data/sentinel/samples" / "rca-config.json"


def propose_from_telemetry(incident, baselines, telemetry, workspace, provider, *, config=None, llm=None, progress=None):
    """Run RCA and hand its findings to an injected recovery decision provider."""
    from services.planner.core import propose, retrieve_runbooks

    diagnosis = diagnose(incident, telemetry, workspace, config=config, llm=llm, progress=progress)
    # Recovery selection needs recovery guidance. The RCA tool catalog and
    # diagnosis workflows have already served their purpose during investigation.
    return propose(incident, baselines, lambda query: {"kp": retrieve_runbooks(incident)},
                   provider, diagnosis=diagnosis)


def diagnose(incident, telemetry, workspace, *, config=None, llm=None, progress=None):
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
    agent = Agent(llm if llm is not None else Ollama(settings, progress=progress),
                  Telemetry(telemetry, settings["max_rows"]), settings, workspace, progress=progress)
    save(Path(workspace) / "incident.json", incident)
    instruction = (
        "Diagnose the initiating root cause of this incident using the supplied telemetry. "
        "Identify the component and reason; distinguish symptoms and downstream effects. "
        "Abstain if evidence is insufficient. Use the incident signals in system_context."
    )
    initial_plan = None
    if settings.get("bootstrap_live_tools", True):
        start = min(datetime.fromisoformat(s["at"].replace("Z", "+00:00")) for s in incident["signals"])
        end = datetime.fromisoformat(incident["observed_at"].replace("Z", "+00:00"))
        leaves = []
        # The live exporter has a known schema; inspect these files directly
        # instead of spending a model call rediscovering their columns/windows.
        for name, tool, required in (("metrics.csv", "metrics", {"kpi_name", "value"}),
                                     ("logs.csv", "logs", {"message"})):
            file = next((f for f in agent.telemetry.inventory() if f["file"] == name), None)
            if file and {"timestamp", "cmdb_id", *required} <= set(file["columns"]):
                leaves.append({"kind": "tool", "goal": "Read incident-scoped live evidence", "tool": tool,
                               "args": {"file": name, "start": (start - timedelta(seconds=30)).isoformat(),
                                        "end": end.isoformat()}})
        if leaves:
            initial_plan = {"plan": {"kind": "parallel", "goal": "Inspect live incident telemetry", "children": leaves}}
    prediction = agent.run(instruction, context=json.dumps({"incident": incident}),
                           required_fields=FIELDS[1:], initial_plan=initial_plan)
    return {"incident_id": incident["incident_id"], "prediction": prediction,
            "report": json.loads((Path(workspace) / "report.json").read_text(encoding="utf-8")),
            "workspace": str(Path(workspace).resolve())}
