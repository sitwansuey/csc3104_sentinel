"""Validate synthetic development messages and their cross-component links."""

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shared.contracts import validate_message


def main():
    paths = sorted((ROOT / "samples/messages").glob("*.json"))
    if not paths:
        raise ValueError("No example messages found")
    messages = [json.loads(path.read_text()) for path in paths]
    for path, message in zip(paths, messages):
        validate_message(message)
        print(f"PASS {path.relative_to(ROOT)}")
    incident = next(m for m in messages if m["kind"] == "incident")
    proposal = next(m for m in messages if m["kind"] == "proposal")
    for message in messages:
        if message["incident_id"] != incident["incident_id"] or message["target"] != incident["target"]:
            raise ValueError("Mismatched incident or target in fixtures")
        if "action_id" in message and message["action_id"] != proposal["action_id"]:
            raise ValueError("Mismatched action ID in fixtures")
    known_evidence = {s["evidence_id"] for s in incident["signals"]}
    if not set(proposal["evidence_refs"]) <= known_evidence:
        raise ValueError("Proposal references missing evidence")
    baseline = json.loads((ROOT / "samples/baseline.json").read_text())
    canonical = json.dumps(baseline["pod_template"], sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    if digest != proposal["parameters"]["template_sha256"] or digest != baseline["template_sha256"]:
        raise ValueError("Proposal does not reference the exact saved baseline template")
    if baseline["baseline_id"] != proposal["parameters"]["baseline_id"] or baseline["target"] != proposal["target"]:
        raise ValueError("Baseline identity does not match the proposal")
    print(f"Validated {len(messages)} example messages and shared references.")
    print("These are fixture checks; no recovery action was executed.")


if __name__ == "__main__":
    main()
