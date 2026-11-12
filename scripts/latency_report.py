"""Build the per-stage latency ledger from evidence bundles and, read only, their run folders.

Each current agent and connected-harness bundle becomes one row. When
--runs-root holds the bundle's original run folder, files the bundle does not
keep (planning-progress.jsonl) are read from there and the affected stages are
marked 'run_folder'; summarize.py --check recomputes every 'bundle' stage from
the committed bundle. The ledger holds timing numbers, roles, statuses, model
tags and token counts only. Nothing is written unless --out is given.

Example:
  python scripts/latency_report.py --runs-root data/experimentation/runs \
      --out evidence/evaluation/latency/<YYYY-MM-DD>
"""

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.analysis import timeline  # noqa: E402

LEDGER_SCHEMA = "sentinel-latency-ledger-v1"
CLASSES = {"agent": "agent", "connected-harness": "harness"}
METHOD = {
    "unit": "one run; stages are wall-clock differences between recorded timestamps, in seconds",
    "start": "fault_injected_at when recorded, else the timeline fault_injected phase, else the first failing "
             "Observer snapshot (from_first_failing_snapshot: a lower bound on detection, never imputed)",
    "percentiles": "median; p90 and p95 by nearest rank; null when n is 0",
    "cold_call": f"a model call whose load_duration_seconds is over {timeline.COLD_LOAD_SECONDS:g}; calls without a "
                 "recorded load duration are unclassified, never counted as warm",
    "stages": {
        "detection": "start -> incident observed",
        "planning": "incident observed -> last planning-progress record (run folder only)",
        "model": "sum of model call wall times; null when planning did not finish",
        "guard": "planning end -> Guard ALLOW; includes the dependency check and the lab approval (pre-authorised "
                 "before 4139385, automated auto-lab through the approval API from 4139385)",
        "decision": "incident observed -> Guard ALLOW; in harness mode includes the scripted negative cases",
        "execution": "Guard ALLOW -> Executor result recorded",
        "verification": "Executor result -> end of the Verifier outcome window",
        "repair": "start -> Executor result recorded",
        "total": "start -> end of the Verifier outcome window",
    },
}
LIMITATIONS = [
    "One Windows/amd64 host with Docker Desktop and kind; runs share the host, cluster and model.",
    "Mostly one fault type (invalid shipping PORT); from 870aa7d also the unscored catalog scenarios R2 and N1. "
    "Lab approval is pre-authorised or automated (auto-lab), so no human wait is included.",
    "Runs before fault_injected_at was recorded start from the first failing snapshot, which understates detection.",
    "Stages marked run_folder come from local run folders that are not committed and cannot be rechecked in CI.",
    "Model time depends on the GPU, model and context size of the run; it is not portable to other hosts.",
]


def load_index(root):
    path = Path(root) / "evidence/index.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"bundles": []}


def runs_roots(runs_root):
    """None, one folder or several folders of run folders, as a list searched in order."""
    if runs_root is None:
        return []
    return [Path(runs_root)] if isinstance(runs_root, (str, os.PathLike)) else [Path(path) for path in runs_root]


def bundle_row(root, entry, runs_root=None):
    """Ledger row for one indexed bundle, with its run folder as a read-only supplement.

    runs_root may name several folders; the first one holding the bundle's run folder is used.
    """
    directory = Path(root) / "evidence" / entry["path"]
    manifest = json.loads((directory / "MANIFEST.json").read_text(encoding="utf-8"))
    name = manifest.get("source_run_dir_name") or entry["bundle_id"]
    directories = [(directory, timeline.BUNDLE)]
    folder = next((base / name for base in runs_roots(runs_root) if (base / name).is_dir()), None)
    if folder is not None:
        directories.append((folder, timeline.RUN_FOLDER))
    row = timeline.run_timeline(directories, run=name, bundle_id=entry["bundle_id"],
                                planning_mode=CLASSES[entry["class"]])
    row["run_folder_read"] = len(directories) > 1
    return row


def build_ledger(root=ROOT, runs_root=None):
    entries = [entry for entry in load_index(root)["bundles"]
               if entry["class"] in CLASSES and entry.get("status") != "withdrawn"]
    rows = sorted((bundle_row(root, entry, runs_root) for entry in entries), key=lambda row: row["run"])
    return {"schema": LEDGER_SCHEMA, "generator": "scripts/latency_report.py", "method": METHOD,
            "limitations": LIMITATIONS, "rows": rows}


def text_summary(ledger):
    lines = []
    for mode, summary in timeline.aggregate(ledger["rows"]).items():
        lines.append(f"{mode}: {summary['runs']} runs, {summary['passed']} PASS, stopped at "
                     f"{summary['failure_stages'] or 'none'}; detection start {summary['detection_start_basis']}")
        for name, values in summary["stages"].items():
            if values["n"]:
                lines.append(f"  {name:<12} n={values['n']:<3} median={values['median']:.1f}s "
                             f"p90={values['p90']:.1f}s max={values['max']:.1f}s")
            else:
                lines.append(f"  {name:<12} n=0")
        cold = summary["cold_load"]
        if cold["n"]:
            lines.append(f"  cold loads   n={cold['n']} median={cold['median']:.1f}s max={cold['max']:.1f}s")
    return "\n".join(lines)


def write_ledger(ledger, out):
    out = Path(out)
    target = out / "timings.json"
    if target.exists():
        raise FileExistsError("Refusing to overwrite " + target.as_posix() + "; choose a new dated directory")
    out.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes((json.dumps(ledger, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
    os.replace(temporary, target)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs-root", type=Path, action="append",
                        help="Folder of original run folders, read only; repeat to search several in order")
    parser.add_argument("--out", type=Path, help="Write <out>/timings.json; an existing ledger is refused")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    ledger = build_ledger(args.root, args.runs_root)
    print(text_summary(ledger))
    if args.out is not None:
        try:
            print("Wrote " + write_ledger(ledger, args.out).as_posix())
        except FileExistsError as error:
            print("FAIL: " + str(error), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
