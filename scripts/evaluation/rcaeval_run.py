"""Run one method over one split of the pre-registered RCAEval sample, resumably.

The procedure is in docs/evaluation/rcaeval/README.md and the rules are in
docs/evaluation/rcaeval/PROTOCOL.md. In short:

  python scripts/evaluation/rcaeval_run.py run --split dev --method baro
  python scripts/evaluation/rcaeval_run.py freeze --labels baro anomaly-rank sentinel
  python scripts/evaluation/rcaeval_run.py run --split eval --method baro

Output, under --out (an ignored run folder):

  <split>/export.json        which sample, split and dataset the export came from
  <split>/export/            blinded tasks and the evaluator-only answers.jsonl
  <split>/<label>/           one method configuration: run.json, predictions.jsonl,
                             cases/ (worker logs; never committed), scores/,
                             versions.json and summary.json (with provenance)

The eval split is refused until the same label has a complete dev run whose
prompt hash matches the current code and configuration, and that hash is
recorded in a committed freeze file. The prompt hash covers every file that
shapes the method (prompts, agent code, adapters, upstream baseline code) and
the effective configuration. A rerun continues only with --resume, and
completed cases are never rerun.
"""

import argparse
from datetime import UTC, datetime
import hashlib
from importlib import metadata, util
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.datasets import evaluate as ev  # noqa: E402
from agent.datasets import rcaeval as data  # noqa: E402
from shared import canonical, provenance  # noqa: E402

RUN_SCHEMA = "sentinel-rcaeval-run-v1"
VERSIONS_SCHEMA = "sentinel-rcaeval-versions-v1"
FREEZE_SCHEMA = "sentinel-rcaeval-freeze-v1"
EXPORT_SCHEMA = "sentinel-rcaeval-export-v1"
LLM_METHODS = ("sentinel", "casetoolkit")
PACKAGES = ("RCAEval", "pandas", "numpy", "pyarrow", "scikit-learn")
DEFAULT_SAMPLE = ROOT / "docs/evaluation/rcaeval/sample-ob-v1.json"
DEFAULT_FREEZE = ROOT / "docs/evaluation/rcaeval/freeze.json"
DEFAULT_OUT = ROOT / "data/experimentation/runs/rcaeval-ob-v1"
DEFAULT_DATA = ROOT / "data/experimentation/datasets/rcaeval-non-code"
RCA_CONFIG = ROOT / "data/sentinel/samples/rca-config.json"
LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
NOTE = "PASS means every case has a recorded prediction and no worker error. It is not an accuracy threshold."


class GateError(ValueError):
    """The eval split is not open for this method yet."""


def utc_now():
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def text_digest(path):
    """sha256 with CRLF read as LF, so a Windows and a Linux checkout give the same hash."""
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def read_json(path):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes((json.dumps(value, indent=2, allow_nan=False) + "\n").encode("utf-8"))
    temporary.replace(path)


def load_sample(path):
    """(sample, sha256) of a sample manifest written by rcaeval_sample.py."""
    sample = read_json(path)
    if not isinstance(sample, dict) or sample.get("schema") != data.SAMPLE_SCHEMA:
        raise ValueError(f"{path} is not an RCAEval sample manifest; draw it with rcaeval_sample.py")
    splits = sample.get("splits") or {}
    if any(not isinstance(splits.get(split), list) or not splits[split] for split in data.SPLITS):
        raise ValueError("The sample manifest needs non-empty dev and eval case lists")
    if set(splits["dev"]) & set(splits["eval"]):
        raise ValueError("The sample's dev and eval splits overlap")
    return sample, data.index_digest(path)


def upstream_root():
    spec = util.find_spec("RCAEval")
    return Path(spec.origin).parent if spec and spec.origin else None


def method_files(method):
    """{path: sha256} of every file that shapes a method's prompts or behaviour."""
    local = ["agent/datasets/evaluate.py", "agent/datasets/rcaeval.py"]
    upstream = []
    if method == "sentinel":
        local += [p.relative_to(ROOT).as_posix() for p in (ROOT / "agent").glob("*.py")]
        local += [p.relative_to(ROOT).as_posix() for p in (ROOT / "agent/knowledge").rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    elif method == "casetoolkit":
        local += ["agent/datasets/casetoolkit.py", "agent/llm.py", "agent/jsonschema_lite.py", "agent/progress.py"]
        upstream = ["agent/toolkit.py"]
    elif method in ev.BASELINES:
        local += ["agent/datasets/baselines.py"]
        upstream = ["e2e/baro.py", "e2e/__init__.py", "io/time_series.py"]
    else:
        raise ValueError("Unknown method: " + str(method))
    files = {relative: text_digest(ROOT / relative) for relative in sorted(set(local))}
    base = upstream_root()
    for relative in upstream:
        path = base / relative if base else None
        files["RCAEval/" + relative] = text_digest(path) if path and path.is_file() else "unavailable"
    return files


def effective_config(method, config_path):
    """The configuration a method runs with, the same way agent/datasets/evaluate.py builds it."""
    overrides = json.loads(Path(config_path).read_text(encoding="utf-8")) if config_path else {}
    if not isinstance(overrides, dict):
        raise ValueError("--config must hold a JSON object")
    if method not in LLM_METHODS:
        return overrides
    config = json.loads(RCA_CONFIG.read_text(encoding="utf-8"))
    config.update(overrides)
    config["allow_abstention"] = True
    return config


def fingerprint(method, config):
    """Files, configuration and the prompt hash over both."""
    files = method_files(method)
    return {"method": method, "files": files, "config": config,
            "prompt_sha256": canonical.sha256_hex({"method": method, "files": files, "config": config})}


def freeze_committed(path):
    """True when the freeze file is tracked by git with no uncommitted change."""
    try:
        relative = Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return False
    try:
        listed = subprocess.run(["git", "ls-files", "--error-unmatch", "--", relative], cwd=ROOT,
                                capture_output=True, check=False, timeout=30)
        changed = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", relative], cwd=ROOT,
                                 capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return listed.returncode == 0 and changed.returncode == 0


def eval_gate(out_root, label, current, freeze_path, sample_sha):
    """Reasons the eval split may not run yet for this label; empty when it may."""
    problems = []
    dev = read_json(Path(out_root) / "dev" / label / "summary.json")
    if not dev or not dev.get("complete"):
        problems.append(f"the dev split has not been run to completion for '{label}' under this --out")
    elif dev.get("sample_sha256") != sample_sha:
        problems.append(f"the dev run of '{label}' used another sample")
    elif dev.get("prompt_sha256") != current["prompt_sha256"]:
        problems.append(f"the code or configuration of '{label}' changed after its dev run; rerun dev")
    freeze = read_json(freeze_path)
    if not freeze:
        problems.append("no freeze file; run 'freeze' after the dev runs and commit the file")
        return problems
    if freeze.get("schema") != FREEZE_SCHEMA or freeze.get("sample_sha256") != sample_sha:
        problems.append("the freeze file is for another sample")
    entry = (freeze.get("methods") or {}).get(label)
    if not entry:
        problems.append(f"'{label}' is not in the freeze file")
    elif entry.get("prompt_sha256") != current["prompt_sha256"]:
        problems.append(f"the prompt hash of '{label}' changed after the freeze (frozen {entry.get('prompt_sha256')}, "
                        f"now {current['prompt_sha256']}); changing a frozen method is a protocol deviation")
    if not freeze_committed(freeze_path):
        problems.append("the freeze file is not committed, or has uncommitted changes")
    return problems


def freeze(out_root, labels, *, sample_path=DEFAULT_SAMPLE, freeze_path=DEFAULT_FREEZE):
    """Record the prompt hash of each label's complete dev run in the freeze file."""
    _, sample_sha = load_sample(sample_path)
    existing = read_json(freeze_path) or {}
    if existing and (existing.get("schema") != FREEZE_SCHEMA or existing.get("sample_sha256") != sample_sha):
        raise ValueError("The existing freeze file is for another sample")
    methods = dict(existing.get("methods") or {})
    for label in labels:
        dev = read_json(Path(out_root) / "dev" / label / "summary.json")
        if not dev or not dev.get("complete"):
            raise GateError(f"'{label}' has no complete dev run under {out_root}")
        if dev.get("sample_sha256") != sample_sha:
            raise GateError(f"the dev run of '{label}' used another sample")
        current = fingerprint(dev["method"], dev["config"])
        if current["prompt_sha256"] != dev.get("prompt_sha256"):
            raise GateError(f"the code of '{label}' changed after its dev run; rerun dev before freezing")
        previous = methods.get(label)
        if previous and previous.get("prompt_sha256") != current["prompt_sha256"]:
            raise GateError(f"'{label}' is already frozen with another hash; changing it is a protocol deviation: "
                            "record it and freeze the change under a new label")
        if not previous:
            methods[label] = {"method": dev["method"], "prompt_sha256": current["prompt_sha256"],
                              "files": current["files"], "dev_status": dev.get("status"),
                              "dev_finished_at": dev.get("finished_at"), "frozen_at": utc_now()}
    value = {"schema": FREEZE_SCHEMA, "sample_sha256": sample_sha, "methods": dict(sorted(methods.items()))}
    write_json(freeze_path, value)
    return value


def ensure_export(data_dir, sample, sample_sha, split, split_root):
    """The blinded export of one split, created once and reused; a mismatched export is refused."""
    export = Path(split_root) / "export"
    cases = sorted(sample["splits"][split])
    record = {"schema": EXPORT_SCHEMA, "sample_sha256": sample_sha, "split": split, "n_cases": len(cases),
              "source_manifest_sha256": data.index_digest(Path(data_dir) / "source-manifest.json")}
    if export.exists():
        if read_json(Path(split_root) / "export.json") != record:
            raise ValueError(f"{export} was made from another sample, split or dataset; use a fresh --out")
        return export
    if data.index_digest(Path(data_dir) / "cases.json") != sample["cases_sha256"]:
        raise ValueError("cases.json under --data-dir is not the one the sample was drawn from")
    done = False
    try:
        ev.export_tasks(Path(data_dir), export, cases=cases)
        if len(ev.load_tasks(export / "tasks")) != len(cases):
            raise ValueError("The export does not hold every case of the split")
        write_json(Path(split_root) / "export.json", record)
        done = True
    finally:
        if not done:
            shutil.rmtree(export, ignore_errors=True)
    return export


def package_version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unavailable"


def model_runtime(config):
    """Ollama version, model digest and placement; never raises."""
    from agent.llm import Ollama
    return Ollama(config).runtime_info()


def provenance_block(method):
    return provenance.collect(ROOT, tools=("ollama",) if method in LLM_METHODS else (),
                              captured_by="scripts/evaluation/rcaeval_run.py")


def versions(method, label, current, data_dir, sample, sample_sha):
    source = read_json(Path(data_dir) / "source-manifest.json") or {}
    config = current["config"]
    llm = method in LLM_METHODS
    return {"schema": VERSIONS_SCHEMA,
            "dataset": {"repo_id": source.get("repo_id"), "revision": source.get("revision"),
                        "license": source.get("license"), "gated": source.get("gated"),
                        "source_manifest_sha256": data.index_digest(Path(data_dir) / "source-manifest.json")},
            "sample": {"name": sample.get("name"), "sha256": sample_sha},
            "reference_code_revision": ev.REFERENCE_REVISION, "python": platform.python_version(),
            "packages": {name: package_version(name) for name in PACKAGES},
            "method": method, "label": label, "prompt_sha256": current["prompt_sha256"], "files": current["files"],
            "model": {"tag": config.get("model"), "role_models": config.get("role_models"), "seed": config.get("seed"),
                      "runtime": model_runtime(config)} if llm else None,
            "gpu": "see model.runtime placement; the GPU model is not recorded" if llm else "not used"}


def finalize(output, export, *, split, method, label, sample, sample_sha, current, data_dir, started, invoked_at,
             record, previous, interrupted):
    """Score what exists and write scores/, versions.json and summary.json for the bundle."""
    answers_path = Path(export) / "answers.jsonl"
    answers = ev.read_jsonl(answers_path)
    predictions_path = Path(output) / "predictions.jsonl"
    predictions = ev.load_predictions(predictions_path, {a["case_id"] for a in answers}) \
        if predictions_path.is_file() else {}
    scores, rows = ev.score(answers, predictions)
    scores["answers_sha256"] = ev.digest(answers_path)
    scores["predictions_sha256"] = ev.digest(predictions_path) if predictions_path.is_file() else None
    write_json(Path(output) / "scores/summary.json", scores)
    (Path(output) / "scores/per-case.jsonl").write_bytes(
        "".join(json.dumps(row, allow_nan=False) + "\n" for row in rows).encode("utf-8"))
    write_json(Path(output) / "versions.json", versions(method, label, current, data_dir, sample, sample_sha))
    complete = len(predictions) == len(answers)
    status = "INCOMPLETE" if not complete else "FAILED" if scores["n_errors"] else "PASS"
    invocations = list(previous.get("invocations") or []) + [
        {"at": invoked_at, "source_revision": record.get("source_revision"), "dirty": record.get("dirty")}]
    summary = {"schema": RUN_SCHEMA, "status": status, "complete": complete, "interrupted": interrupted,
               "started_at": started, "finished_at": utc_now(), "split": split, "method": method, "label": label,
               "sample": sample.get("name"), "sample_sha256": sample_sha, "answers_sha256": scores["answers_sha256"],
               "prompt_sha256": current["prompt_sha256"], "config": current["config"], "n_cases": len(answers),
               "n_predicted": len(predictions), "n_errors": scores["n_errors"],
               "n_abstained": sum(p.get("status") == "abstained" for p in predictions.values()),
               "service": scores["service"], "indicator": scores["indicator"], "provenance": record,
               "invocations": invocations, "note": NOTE}
    write_json(Path(output) / "summary.json", summary)
    return summary


def execute(*, split, method, data_dir=DEFAULT_DATA, sample_path=DEFAULT_SAMPLE, label=None, config_path=None,
            out_root=DEFAULT_OUT, timeout=600, resume=False, freeze_path=DEFAULT_FREEZE, run_method=None):
    """Run one labelled method configuration over one split; returns the run summary."""
    if split not in data.SPLITS:
        raise ValueError("split must be dev or eval")
    if method not in ev.METHODS:
        raise ValueError("Unknown method: " + str(method))
    label = label or method
    if not LABEL.fullmatch(label):
        raise ValueError("label must be lower-case letters, digits and dashes")
    if timeout < 1:
        raise ValueError("timeout must be positive")
    out_root = Path(out_root)
    sample, sample_sha = load_sample(sample_path)
    current = fingerprint(method, effective_config(method, config_path))
    if split == "eval":
        problems = eval_gate(out_root, label, current, freeze_path, sample_sha)
        if problems:
            raise GateError("The eval split is locked for '" + label + "':\n- " + "\n- ".join(problems))
    export = ensure_export(data_dir, sample, sample_sha, split, out_root / split)
    output = out_root / split / label
    previous = read_json(output / "summary.json") or {}
    if output.exists() and not resume:
        raise ValueError(f"{output} exists; pass --resume to continue it, or use a fresh --out")
    if previous and (previous.get("prompt_sha256") != current["prompt_sha256"] or previous.get("method") != method):
        raise ValueError("The existing run used another method, code or configuration; use a fresh --out")
    invoked_at = utc_now()
    started = previous.get("started_at") or invoked_at
    record = provenance_block(method)
    interrupted = False
    try:
        (run_method or ev.run)(export / "tasks", output, method, timeout, resume=resume,
                               config=Path(config_path) if config_path else None)
    except KeyboardInterrupt:
        interrupted = True
    summary = finalize(output, export, split=split, method=method, label=label, sample=sample, sample_sha=sample_sha,
                       current=current, data_dir=data_dir, started=started, invoked_at=invoked_at, record=record,
                       previous=previous, interrupted=interrupted)
    if interrupted:
        raise KeyboardInterrupt
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run", help="Run one method over one split")
    runner.add_argument("--split", choices=data.SPLITS, required=True)
    runner.add_argument("--method", choices=ev.METHODS, required=True)
    runner.add_argument("--label", help="Name of this configuration (default: the method)")
    runner.add_argument("--config", type=Path, help="JSON overrides, as for evaluate_rcaeval.py run")
    runner.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    runner.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    runner.add_argument("--out", type=Path, default=DEFAULT_OUT)
    runner.add_argument("--freeze-file", type=Path, default=DEFAULT_FREEZE)
    runner.add_argument("--timeout", type=int, default=600, help="Seconds per case")
    runner.add_argument("--resume", action="store_true")
    freezer = sub.add_parser("freeze", help="Record the prompt hashes of complete dev runs")
    freezer.add_argument("--labels", nargs="+", required=True)
    freezer.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    freezer.add_argument("--out", type=Path, default=DEFAULT_OUT)
    freezer.add_argument("--freeze-file", type=Path, default=DEFAULT_FREEZE)
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            value = freeze(args.out, args.labels, sample_path=args.sample, freeze_path=args.freeze_file)
            print(f"Frozen: {', '.join(value['methods'])}. Commit {args.freeze_file} before any eval run.")
            return 0
        summary = execute(split=args.split, method=args.method, label=args.label, config_path=args.config,
                          data_dir=args.data_dir, sample_path=args.sample, out_root=args.out,
                          timeout=args.timeout, resume=args.resume, freeze_path=args.freeze_file)
    except KeyboardInterrupt:
        print("Interrupted; the summary was written. Continue with the same arguments plus --resume.",
              file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print("RCAEval run failed: " + str(error), file=sys.stderr)
        return 1
    service = summary["service"] or {}
    print(f"{summary['label']} {summary['split']}: {summary['status']}, {summary['n_predicted']}/{summary['n_cases']} "
          f"cases, {summary['n_errors']} errors, service AC@1 {service.get('AC@1')}")
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
