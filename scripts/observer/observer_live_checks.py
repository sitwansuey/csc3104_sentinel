"""Acceptance checks for recorded live Observer polls (issue #3)."""

from datetime import datetime


def deployment_is_healthy(sample):
    desired = sample["desiredReplicas"]
    return (
        desired > 0
        and sample["observedGeneration"] >= sample["generation"]
        and sample["readyReplicas"] >= desired
        and sample["availableReplicas"] >= desired
    )


def assess_report(report, expectation, *, window_seconds=60):
    """Fail closed on incomplete collection, wrong timing or insufficient windows.

    This independently checks readiness and emission timestamps; it does not
    replay the Observer to generate the expected answer.
    """
    problems = []
    result = {"expectation": expectation, "window_seconds": window_seconds}
    polls = report["polls"]
    emissions = report["emissions"]
    if report["errors"] or report["observations_read"] != report["poll_count"]:
        problems.append("All requested polls must succeed without collection errors.")
    if len(polls) < 2:
        problems.append("At least two successful polls are required.")
    if len(polls) != report["observations_read"]:
        problems.append("Poll evidence is incomplete.")
    if len(emissions) != report["incident_count"]:
        problems.append("Emission evidence is incomplete.")
    if problems:
        return {**result, "passed": False, "problems": problems}

    times = [datetime.fromisoformat(p["captured_at"].replace("Z", "+00:00")) for p in polls]
    if any(b <= a for a, b in zip(times, times[1:])):
        problems.append("Poll timestamps must increase strictly.")
    result["observed_seconds"] = (times[-1] - times[0]).total_seconds()
    identities = {(p["uid"], p["generation"]) for p in polls}
    if len(identities) != 1:
        problems.append("Deployment identity or generation changed during observation.")
    healthy = [deployment_is_healthy(p) for p in polls]

    if expectation == "healthy":
        if not all(healthy):
            problems.append("Every healthy-window poll must have all desired replicas ready and available.")
        if report["incident_count"] != 0:
            problems.append("Healthy deployment emitted an incident.")
        if result["observed_seconds"] < window_seconds:
            problems.append("Healthy observation window is too short.")
    elif expectation == "persistent-failure":
        failing = [i for i, is_healthy in enumerate(healthy) if not is_healthy]
        if not failing:
            problems.append("No deployment failure was observed.")
        else:
            first = failing[0]
            if any(healthy[first:]):
                problems.append("Failure did not remain continuous through duplicate suppression.")
            first_at = times[first]
            threshold = report["failure_duration_seconds"]
            eligible = next((i for i in range(first, len(polls))
                             if (times[i] - first_at).total_seconds() >= threshold), None)
            result["initial_detection_at"] = polls[first]["captured_at"]
            result["failure_duration_seconds"] = threshold
            if eligible is None:
                problems.append("No poll reached the configured failure-duration threshold.")
            if report["incident_count"] != 1 or len(emissions) != 1:
                problems.append("Persistent failure must emit exactly one incident.")
            elif eligible is not None:
                emission = emissions[0]
                if emission["poll_index"] != eligible:
                    problems.append("Incident was not emitted on the first poll at or after the threshold.")
                index = emission["poll_index"]
                if not 0 <= index < len(polls):
                    problems.append("Emission references an invalid poll.")
                else:
                    incident = report["incidents"][0]
                    if incident["observed_at"] != polls[index]["captured_at"]:
                        problems.append("Incident timestamp does not match its emission poll.")
                    if incident["incident_id"] != emission["incident_id"]:
                        problems.append("Incident identity does not match the emission evidence.")
                    result["emitted_at"] = polls[index]["captured_at"]
                    result["detection_delay_seconds"] = (times[index] - first_at).total_seconds()
                    result["duplicate_observation_seconds"] = (times[-1] - times[index]).total_seconds()
                    if result["duplicate_observation_seconds"] < window_seconds:
                        problems.append("Duplicate-suppression observation window is too short.")
    else:
        raise ValueError("Unknown live-test expectation")
    return {**result, "passed": not problems, "problems": problems}
