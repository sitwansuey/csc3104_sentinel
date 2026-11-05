"""Deterministic evidence checks; no ground-truth inputs."""
from datetime import datetime


def compact_evidence(evidence):
    """Keep representation balanced across tools instead of truncating a JSON prefix."""
    result = []
    per_tool = max(1, min(8, 24 // max(1, len(evidence))))
    for entry in evidence:
        item = dict(entry)
        if "result" in item:
            data = dict(item["result"])
            for key in ("findings", "events", "latency", "examples"):
                if key in data:
                    data[key + "_omitted"] = max(0, len(data[key]) - per_tool)
                    data[key] = data[key][:per_tool]
            item["result"] = data
        result.append(item)
    return result


def corroborated_onsets(evidence, tolerance_seconds=120):
    """Temporal corroboration is a screening condition, not proof of causation.

    Require a persistent metric episode plus a second KPI on the same component,
    or an error/latency event on the same component. Do not invent topology maps.
    """
    signals = []
    for entry in evidence:
        data = entry.get("result", {})
        if data.get("truncated"):
            continue
        if entry.get("tool") == "metrics":
            for finding in data.get("findings", []):
                for episode in finding.get("episodes", []):
                    if episode["sustained"] and not episode["left_censored"]:
                        signals.append({"component": finding["component"], "time": episode["onset"],
                            "source": (data["file"], finding["kpi"]), "kind": "metric",
                            "kpi": finding["kpi"],
                            "evidence_id": entry["evidence_id"], "score": episode["peak_score"]})
        else:
            for event in data.get("events", []):
                if not event.get("left_censored", False):
                    signals.append({"component": event["component"], "time": event["time"],
                        "source": (data["file"], entry.get("tool")), "kind": entry.get("tool"),
                        "evidence_id": entry["evidence_id"], "score": 0})
    candidates = []
    for signal in signals:
        if signal["kind"] != "metric":
            continue
        onset = datetime.fromisoformat(signal["time"])
        matches = [s for s in signals if s["component"] == signal["component"]
                   and s["source"] != signal["source"]
                   and (s["kind"] != "metric" or s["kpi"] != signal["kpi"])
                   and abs((datetime.fromisoformat(s["time"]) - onset).total_seconds()) <= tolerance_seconds]
        if matches:
            candidates.append({"component": signal["component"], "time": signal["time"],
                "evidence_ids": sorted({signal["evidence_id"], *(s["evidence_id"] for s in matches)}),
                "supporting_signals": len({s["source"] for s in matches}) + 1,
                "modalities": sorted({signal["kind"], *(s["kind"] for s in matches)}),
                "peak_score": signal["score"]})
    unique = {(c["component"], c["time"]): c for c in candidates}
    return sorted(unique.values(), key=lambda c: (-len(c["modalities"]), -c["supporting_signals"], -c["peak_score"]))[:30]


def timestamp_rejection(failures, candidates, analysis, confidence_threshold):
    """A reporter cannot override an incomplete diagnosis with a plausible guess."""
    timed = [f for f in failures if "root cause occurrence datetime" in f]
    if not timed:
        return None
    if analysis.get("complete") is not True or analysis.get("confidence", 0) < confidence_threshold:
        return "Analyzer did not confirm a complete diagnosis above the confidence threshold."
    for failure in timed:
        if not any(c["time"] == failure["root cause occurrence datetime"] and
                   ("root cause component" not in failure or c["component"] == failure["root cause component"])
                   for c in candidates):
            return "Proposed timestamp lacks persistent, independently corroborated onset evidence."
    return None
