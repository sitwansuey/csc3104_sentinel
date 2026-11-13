"""Replay decision policies P0-P4 at validation levels V0-V2 over the replay corpus (roadmap P4.04).

    # Deterministic policies, offline, seconds:
    python scripts/evaluate_recovery.py run --policy P0 P1 --out <new folder>
    # Golden replay of the recorded agent cases (recorded diagnosis and Recovery Planner answer):
    python scripts/evaluate_recovery.py golden
    # Model policies on the local Ollama model (hours; resumable):
    python scripts/evaluate_recovery.py run --policy P2 P3 P4 --seeds 42 43 44 --model qwen3:14b \
        --model-digest <full sha256 of the model> --num-ctx 16384 --out <new folder>
    python scripts/evaluate_recovery.py run ... --out <same folder> --resume
    # Recompute results.json and table.md from saved rows:
    python scripts/evaluate_recovery.py report --out <folder>

Every validation level is judged on each unit's one proposal, so V0, V1 and V2
always see identical inputs. A run folder holds run.json (the run identity:
corpus manifest hash, policies, seeds, model settings, model digest and source
hashes), per-case.jsonl (one row per finished unit, appended as it finishes),
results.json and table.md. --resume skips finished units, runs error rows again
(a model server that was down, a budget that ran out) and refuses a run
whose identity changed, so a changed corpus, policy set, model or model digest
needs a new folder. Nothing here writes to a cluster.
"""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.analysis import replay, replay_corpus as corpus  # noqa: E402

DEFAULT_CORPUS = ROOT / "evidence/replay/v1"


def model_config(args):
    """The agent configuration for model policies: the RCA defaults plus the ablation settings."""
    from services.rca.core import DEFAULT_CONFIG
    config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    config.update(model=args.model, num_ctx=args.num_ctx, keep_alive="30m")
    if args.planning_budget is not None:
        config["planning_budget_seconds"] = args.planning_budget
    return config


def ollama_factory(config, *, progress=None, deadline=None):
    from agent.llm import Ollama
    return Ollama(config, progress=progress, deadline=deadline)


def pinned_digest(text):
    """A --model-digest value as 64 lower-case hex characters; a model run must pin its model."""
    if not text:
        raise SystemExit("Model policies need --model-digest, the full SHA-256 of the pinned model "
                         "(64 hexadecimal characters, as in the run guide)")
    digest = text.removeprefix("sha256:").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit("--model-digest must be the full SHA-256: 64 hexadecimal characters")
    return digest


def check_digest(reported, expected):
    """The reported model digest, if it is the pinned one."""
    expected = pinned_digest(expected)
    if not reported or reported == "unavailable":
        raise SystemExit("Ollama did not report a model digest; a run must name its model digest")
    reported = reported.removeprefix("sha256:").lower()
    if reported != expected:
        raise SystemExit(f"The local model digest {reported[:12]} is not the pinned {expected[:12]}; "
                         "pull the pinned model or pin the new digest in a new run folder")
    return "sha256:" + reported


def model_digest(config, expected):
    """The digest of the configured model, read from the local Ollama server and checked against the pin."""
    from agent.llm import Ollama
    client = Ollama(config)
    client.preflight()
    info = client.runtime_info()
    return check_digest((info.get("models", {}).get(config["model"]) or {}).get("digest"), expected)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="replay policies over the corpus into a run folder")
    run.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    run.add_argument("--policy", nargs="+", required=True, choices=sorted(replay.POLICIES))
    run.add_argument("--seeds", nargs="+", type=int, default=[42], help="model seeds (model policies only)")
    run.add_argument("--diagnosis", choices=replay.DIAGNOSES, default="live",
                     help="P3/P4: run the RCA agent (live) or use each captured case's recorded diagnosis")
    run.add_argument("--model", default="qwen3:14b", help="Ollama model tag for P2-P4")
    run.add_argument("--model-digest", help="pinned model digest (the full SHA-256, 64 hex characters); "
                     "required for P2-P4, and the run refuses a local model with another digest")
    run.add_argument("--num-ctx", type=int, default=16384, help="context window for P2-P4 (#86 ablation)")
    run.add_argument("--planning-budget", type=float, help="planning_budget_seconds per unit (default 600)")
    run.add_argument("--runbooks", choices=("all", "none"), default="all",
                     help="none empties the Recovery Planner's runbooks and the RCA agent's workflows (the "
                     "P3-noRAG ablation; the RCA policies then need --diagnosis live)")
    run.add_argument("--case", nargs="+", help="only these case ids")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--resume", action="store_true")
    golden = commands.add_parser("golden", help="replay recorded agent cases and compare with the recorded proposal")
    golden.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    golden.add_argument("--out", type=Path, help="also write the comparison as JSON")
    golden.add_argument("--strict", action="store_true", help="exit 1 when a run recorded under a decision "
                        "policy does not reproduce")
    report = commands.add_parser("report", help="recompute results.json and table.md from per-case.jsonl")
    report.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    report.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "report":
        try:
            replay.report(args.corpus, args.out)
        except replay.ReplayError as error:
            raise SystemExit(str(error)) from None
        print((args.out / "table.md").read_text(encoding="utf-8"))
        return 0
    if args.command == "golden":
        manifest = corpus.verify(args.corpus)
        results, guards = [], []
        for entry in manifest["cases"]:
            if entry["provenance"] != "captured":
                continue
            case = corpus.load_case(args.corpus, entry["case_id"])
            if "diagnosis" in case["recorded"]:
                results.append(replay.golden(case))
            checked = replay.guard_crosscheck(case)
            if checked is not None:
                guards.append(checked)
        for item in results:
            print(f"{item['status']:15} {item['case_id']}  recorded {item.get('recorded_action', '-')}, "
                  f"replayed {item.get('replayed_action', '-')} {item.get('differing_fields') or ''}")
        summary = {status: sum(item["status"] == status for item in results)
                   for status in ("reproduced", "differs", "error", "not_replayable")}
        summary["guard_crosscheck"] = {"agrees": sum(item["agrees"] for item in guards), "cases": len(guards)}
        print(json.dumps(summary))
        if args.out:
            corpus.write_json(args.out, {"schema": "sentinel-replay-golden-v1",
                                         "corpus_manifest_sha256": corpus.manifest_sha256(args.corpus),
                                         "summary": summary, "cases": results, "guard_crosscheck": guards})
        failed = [item for item in results if item["status"] in ("differs", "error") and item.get("recorded_policy")]
        return 1 if args.strict and failed else 0

    needs_model = any(replay.needs_model(policy, args.diagnosis) for policy in args.policy)
    if needs_model:
        pinned_digest(args.model_digest)
    config = model_config(args) if needs_model else {}
    if args.runbooks == "none":
        config["runbooks"] = "none"
    digest = model_digest(config, args.model_digest) if needs_model else None
    results = replay.run(args.corpus, args.out, policies=args.policy, seeds=args.seeds, diagnosis=args.diagnosis,
                         config=config, llm_factory=ollama_factory if needs_model else None, model_digest=digest,
                         resume=args.resume, cases=set(args.case or []),
                         progress=lambda row: print(f"{row['case_id']} {row['policy']} seed={row['seed']} "
                                                    f"{row['status']} {row['error'] or ''} {row['seconds']}s", flush=True))
    print((args.out / "table.md").read_text(encoding="utf-8"))
    print(f"{results['rows']} rows; {results['labels_pending_review']} label sets pending review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
