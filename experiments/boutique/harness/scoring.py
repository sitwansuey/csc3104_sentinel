"""Scenario-result facts shared by agent mode and the Chaos Mesh recovery harness.

What happened after planning is read from the Coordinator's own records and the
Executor's own transport statistics, never inferred from a passing run.
"""
import json

from experiments.recovery.live import save


class ScenarioScoringMixin:
    """scenario_facts() and save_scenario_result() for a live Boutique experiment."""

    def scenario_facts(self, incident, state, action_id, folder):
        """What happened after planning, read from the Coordinator and the Executor's own stats."""
        action = self.store.action(action_id) if action_id else None
        guard = (action or {}).get("guard_result")
        routes = 0
        if action_id:
            routes = self.store.db.execute("SELECT COUNT(*) FROM outbox WHERE action_id=? AND topic='executor.request'",
                                           (action_id,)).fetchone()[0]
        stats = folder / "transport-stats.json"
        # With no Executor route nothing could write; with a route, only the Executor's own stats count.
        if stats.is_file():
            writes = json.loads(stats.read_text(encoding="utf-8")).get("write_attempts")
        else:
            writes = 0 if routes == 0 else None
        return {"incident_id": incident["incident_id"] if incident else None, "coordinator_state": state,
                "guard_verdict": guard["verdict"] if guard else None,
                "guard_reason_codes": list(guard["reason_codes"]) if guard else [],
                "executor_routes": routes, "executor_writes": writes,
                "fault_injected_at": self.summary.get("fault_injected_at"),
                "incident_observed_at": incident["observed_at"] if incident else None}

    def save_scenario_result(self, graded):
        save(self.directory / "scenario-result.json", graded)
        self.summary["scenario_result"] = graded
        self.event(3, "status", f"Scenario {graded['scenario']}: decision {graded['actual']} "
                   f"from {graded['decision_source']}, scored {graded['score']}",
                   target=self.target)
        return graded
