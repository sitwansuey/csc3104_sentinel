"""Demonstrate offline Verifier outcomes; no cluster writes or live health checks."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from services.verifier.core import inspect_dependencies, verify


def main():
    def load(name):
        return json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8"))

    proposal = load("messages/proposal.json")
    execution = load("messages/execution-result.json")
    graph = load("dependencies.json")
    health = load("health.json")
    policy = load("policy.json")
    now = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)
    blocked = deepcopy(graph)
    blocked["known_health"]["mock-db"] = False
    unknown = deepcopy(graph)
    unknown["known_health"].pop("mock-db")
    results = {
        "dependency_clear": inspect_dependencies(proposal, graph, {}, now=now),
        "dependency_blocked": inspect_dependencies(proposal, blocked, {}, now=now),
        "dependency_unknown": inspect_dependencies(proposal, unknown, {}, now=now),
        "stable_health": verify(execution, health["stable"], policy),
        "unstable_health": verify(execution, health["unstable"], policy),
        "insufficient_health": verify(execution, health["stable"][:1], policy),
    }
    print(json.dumps({"mode": "synthetic_offline", "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
