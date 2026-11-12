"""Draw the pre-registered, stratified Online Boutique sample from RCAEval's filtered index.

The rule is fixed in docs/evaluation/rcaeval/PROTOCOL.md:

- A cell is one (suite, root-cause service, fault) of the Online Boutique cases
  in the allowed suites (RE1, RE2). Cells come from cases.json, never from
  hard-coded counts.
- eval: the case with repetition --eval-repetition of every cell, so each cell
  appears exactly once.
- dev: for each (suite, fault), the --dev-per-fault services that sort first by
  sha256('<seed>|<suite>|<fault>|<service>'), each with repetition
  --dev-repetition. Dev and eval use different repetitions, so they never share
  a case.

The manifest lists case ids only, plus the rule, the seed and the sha256 of the
cases.json it was drawn from. The same cases.json and arguments always give a
byte-identical manifest. A different existing manifest is never overwritten: a
new sample needs a new name.

Example (after 'python scripts/download_rcaeval.py --index-only'):
  python scripts/evaluation/rcaeval_sample.py
"""

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.datasets import rcaeval as data  # noqa: E402

NAME = "sample-ob-v1"
SYSTEM = "ob"
SUITES = ("RE1", "RE2")
DEFAULT_SEED = 3104
DEFAULT_DATA = ROOT / "data/experimentation/datasets/rcaeval-non-code"
DEFAULT_OUT = ROOT / "docs/evaluation/rcaeval" / (NAME + ".json")


def dev_order(seed, suite, fault, service):
    """Seeded, platform-independent order of services within one (suite, fault) stratum."""
    return hashlib.sha256(f"{seed}|{suite}|{fault}|{service}".encode()).hexdigest()


def draw(rows, *, suites=SUITES, eval_repetition=1, dev_repetition=2, dev_per_fault=1, seed=DEFAULT_SEED):
    """{'dev': [...], 'eval': [...]} sorted case names from validated index rows."""
    if any(suite not in SUITES for suite in suites) or not suites:
        raise ValueError("Only the RE1 and RE2 suites can be sampled")
    if eval_repetition == dev_repetition:
        raise ValueError("Dev and eval must use different repetitions")
    if min(eval_repetition, dev_repetition, dev_per_fault) < 1:
        raise ValueError("Repetitions and dev-per-fault must be positive")
    cells = defaultdict(dict)
    for row in rows:
        if row["system"] != SYSTEM or row["suite"] not in suites:
            continue
        cells[(row["suite"], row["root_cause_service"], row["fault"])][row["repetition"]] = row["case"]
    if not cells:
        raise ValueError("The index has no Online Boutique cases in the selected suites")
    evaluation, development = [], []
    for cell in sorted(cells):
        if eval_repetition not in cells[cell]:
            raise ValueError(f"Cell {'/'.join(cell)} has no repetition {eval_repetition}")
        evaluation.append(cells[cell][eval_repetition])
    strata = defaultdict(list)
    for suite, service, fault in cells:
        strata[(suite, fault)].append(service)
    for (suite, fault), services in sorted(strata.items()):
        for service in sorted(services, key=lambda s: dev_order(seed, suite, fault, s))[:dev_per_fault]:
            repetitions = cells[(suite, service, fault)]
            if dev_repetition not in repetitions:
                raise ValueError(f"Cell {suite}/{service}/{fault} has no repetition {dev_repetition}")
            development.append(repetitions[dev_repetition])
    if set(evaluation) & set(development):
        raise ValueError("Dev and eval splits overlap")
    return {"dev": sorted(development), "eval": sorted(evaluation)}


def split_bytes(data_dir, revision, splits):
    """Planned telemetry bytes per split from an --index-only source manifest, or None."""
    path = Path(data_dir) / "source-manifest.json"
    if not path.is_file():
        return None
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("revision") != revision:
        return None
    sizes = defaultdict(int)
    for item in plan.get("files", []):
        sizes[item["path"].split("/")[0]] += item["bytes"]
    return {name: sum(sizes[case] for case in cases) for name, cases in splits.items()}


def build(data_dir, *, suites=SUITES, eval_repetition=1, dev_repetition=2, dev_per_fault=1, seed=DEFAULT_SEED,
          name=NAME):
    """The sample manifest for the cases.json under data_dir."""
    config = data.settings()
    index = Path(data_dir) / "cases.json"
    rows = data.select_cases(json.loads(index.read_text(encoding="utf-8")), config)
    splits = draw(rows, suites=suites, eval_repetition=eval_repetition, dev_repetition=dev_repetition,
                  dev_per_fault=dev_per_fault, seed=seed)
    return {
        "schema": data.SAMPLE_SCHEMA, "name": name,
        "dataset": {"repo_id": config["repo_id"], "revision": config["revision"]},
        "cases_sha256": data.index_digest(index),
        "rule": {"system": SYSTEM, "suites": list(suites), "cell": "suite x root-cause service x fault",
                 "eval": f"repetition {eval_repetition} of every cell", "eval_repetition": eval_repetition,
                 "dev": f"per (suite, fault), the first {dev_per_fault} service(s) by "
                        "sha256('<seed>|<suite>|<fault>|<service>'), repetition " + str(dev_repetition),
                 "dev_repetition": dev_repetition, "dev_per_fault": dev_per_fault, "seed": seed},
        "counts": {split: len(cases) for split, cases in splits.items()},
        "bytes": split_bytes(data_dir, config["revision"], splits),
        "splits": splits,
    }


def write(manifest, out):
    """Write the manifest; refuse to replace a different one. Returns 'written' or 'unchanged'."""
    out = Path(out)
    text = json.dumps(manifest, indent=2) + "\n"
    if out.exists():
        if out.read_text(encoding="utf-8").replace("\r\n", "\n") == text:
            return "unchanged"
        raise ValueError(f"{out} exists with a different sample; a new sample needs a new name and file")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(text.encode("utf-8"))
    return "written"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA, help="Dataset root holding cases.json")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--name", default=NAME)
    parser.add_argument("--suites", nargs="+", choices=SUITES, default=list(SUITES))
    parser.add_argument("--eval-repetition", type=int, default=1)
    parser.add_argument("--dev-repetition", type=int, default=2)
    parser.add_argument("--dev-per-fault", type=int, default=1)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    try:
        manifest = build(args.data_dir, suites=tuple(args.suites), eval_repetition=args.eval_repetition,
                         dev_repetition=args.dev_repetition, dev_per_fault=args.dev_per_fault, seed=args.seed,
                         name=args.name)
        result = write(manifest, args.out)
    except (OSError, ValueError, KeyError) as error:
        print("RCAEval sample failed: " + str(error), file=sys.stderr)
        return 1
    sizes = manifest["bytes"]
    size_text = (", ".join(f"{split} {value / 1e9:.2f} GB" for split, value in sizes.items())
                 if sizes else "sizes unknown (run the downloader with --index-only first)")
    print(f"{result} {args.out}: {manifest['counts']['eval']} eval and {manifest['counts']['dev']} dev cases; "
          + size_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
