"""Blinded RCAEval evaluation with upstream-compatible AC@k and Avg@k semantics.

Reference: phamquiluan/RCAEval commit 2f703059960517b024c504c817771fa85294aa09,
RCAEval/benchmark/scoring.py, evaluation.py and agent/predictions.py.
This implementation reads the Hugging Face Parquet edition directly.
"""

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from shared import log
from .rcaeval import ROOT, checked_path, parquet, save, settings, validate_local_plan, verify

LOG = log.get_logger("rcaeval")
REFERENCE_REVISION = "2f703059960517b024c504c817771fa85294aa09"
FAULTS = {"cpu", "mem", "disk", "delay", "loss", "socket"}


def to_service(candidate):
    return candidate.split("_")[0].replace("-db", "")


def indicator(fault, suite):
    if fault not in FAULTS or suite not in {"RE1", "RE2"}:
        raise ValueError("Only non-code RE1/RE2 faults can be evaluated")
    if fault in {"delay", "loss"} or (fault == "disk" and suite == "RE1"):
        return "latency"
    return "diskio" if fault == "disk" else fault


def metric_columns(columns):
    result = []
    for column in columns:
        if column in {"time", "time.1"} or column.endswith("_latency-50"):
            continue
        if column.endswith("_latency-90"):
            column = column[:-3]
        if column not in result:
            result.append(column)
    return result


def pair(candidate):
    parts = candidate.split("_")
    return parts[0], parts[1] if len(parts) > 1 else "unknown"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def append_jsonl(path, row):
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, allow_nan=False) + "\n")


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def export_tasks(data_root, output, *, suites=None, systems=None, faults=None, limit=None, exclude_tasks=None):
    config = settings()
    plan, indexed = validate_local_plan(data_root, config)
    rows = [r for r in indexed if (not suites or r["suite"] in suites)
            and (not systems or r["system"] in systems) and (not faults or r["fault"] in faults)]
    rows.sort(key=lambda r: hashlib.sha256(r["case"].encode()).hexdigest())
    excluded = set(load_tasks(exclude_tasks)) if exclude_tasks else set()
    rows = [r for r in rows if "case-" + hashlib.sha256(r["case"].encode()).hexdigest()[:16] not in excluded]
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = rows[:limit]
    if not rows:
        raise ValueError("No cases selected")
    output.mkdir(parents=True, exist_ok=False)
    task_root = output / "tasks"
    task_root.mkdir()
    answers = output / "answers.jsonl"
    tasks = []
    by_case = defaultdict(list)
    for item in plan["files"]:
        by_case[item["path"].split("/")[0]].append(item)
    for index, row in enumerate(rows, 1):
        case = row["case"]
        case_id = "case-" + hashlib.sha256(case.encode()).hexdigest()[:16]
        case_dir = task_root / "cases" / case_id
        case_dir.mkdir(parents=True)
        files = {}
        checksums = {}
        for item in by_case[case]:
            source = checked_path(data_root / "raw", item["path"])
            if not verify(source, item):
                raise ValueError("Missing or modified source; rerun downloader: " + item["path"])
            name = source.name
            # Never put injection label files, source paths or answer-bearing names in the task view.
            if name not in {"metrics.parquet", "logs.parquet", "traces.parquet"}:
                continue
            shutil.copyfile(source, case_dir / name)
            files[name.split(".")[0]] = name
            checksums[name] = digest(case_dir / name)
        if "metrics" not in files:
            raise ValueError("Case has no metrics")
        columns = metric_columns(parquet().read_schema(case_dir / files["metrics"]).names)
        services = sorted({to_service(c) for c in columns})
        task = {"schema_version": 1, "case_id": case_id, "dataset": row["dataset"],
                "system": row["system_name"], "inject_time": row["inject_time"],
                "standard_window": {"start": row["inject_time"] - 600, "end": row["inject_time"] + 600},
                "files": files, "sha256": checksums, "candidate_services": services,
                "candidate_indicators": columns}
        save(case_dir / "task.json", task)
        target_indicator = indicator(row["fault"], row["suite"])
        answer = {"case_id": case_id, "dataset": row["dataset"], "system": row["system"],
                  "source_case": case, "service": row["root_cause_service"], "fault": row["fault"],
                  "indicator": target_indicator, "n_candidate_services": len(services),
                  "n_candidate_indicators": len(columns),
                  "answer_in_candidates": row["root_cause_service"] in services,
                  "indicator_in_candidates": (row["root_cause_service"], target_indicator) in [pair(c) for c in columns]}
        append_jsonl(answers, answer)
        tasks.append({"case_id": case_id, "task_sha256": digest(case_dir / "task.json")})
        if index % 50 == 0 or index == len(rows):
            print(f"Exported {index}/{len(rows)} blind tasks", flush=True)
    save(task_root / "manifest.json", {"schema_version": 1, "source_repo": config["repo_id"],
         "source_revision": config["revision"], "reference_code_revision": REFERENCE_REVISION,
         "n_cases": len(tasks), "cases": tasks, "selection": {"suites": suites, "systems": systems,
         "faults": faults, "limit": limit, "excluded_case_ids": sorted(excluded)}, "standard_window_seconds": 1200})
    # Answer metadata is outside the only directory supplied to diagnosis workers.
    save(output / "provenance.json", {"source_manifest_sha256": digest(data_root / "source-manifest.json"),
         "answers_sha256": digest(answers), "reference_code_revision": REFERENCE_REVISION})
    return task_root


def load_tasks(root):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    tasks = {}
    for entry in manifest["cases"]:
        case_id = entry["case_id"]
        if not isinstance(case_id, str) or not re.fullmatch(r"case-[0-9a-f]{16}", case_id):
            raise ValueError("Invalid blind case ID")
        path = checked_path(root, "cases/" + case_id + "/task.json")
        if case_id in tasks or digest(path) != entry["task_sha256"]:
            raise ValueError("Duplicate or modified task manifest")
        task = json.loads(path.read_text(encoding="utf-8"))
        if task["case_id"] != case_id or task["dataset"].split("-")[0] not in {"RE1", "RE2"}:
            raise ValueError("Excluded or inconsistent task")
        tasks[case_id] = task
    if len(tasks) != manifest["n_cases"]:
        raise ValueError("Task manifest count mismatch")
    return tasks


def load_predictions(path, known):
    predictions = {}
    for row in read_jsonl(path):
        if not isinstance(row, dict) or not isinstance(row.get("case_id"), str):
            raise ValueError("Prediction must have a string case_id")
        case_id = row["case_id"]
        if case_id not in known or case_id in predictions:
            raise ValueError("Unknown or duplicate prediction case_id: " + case_id)
        if not isinstance(row.get("ranks"), list) or any(not isinstance(x, str) for x in row["ranks"]):
            raise ValueError("Prediction ranks must be a list of strings")
        if "usage" in row and not isinstance(row["usage"], dict):
            raise ValueError("Prediction usage must be an object")
        row["ranks"] = [r.strip().lower() for r in row["ranks"] if r.strip()]
        if row.get("status") in {"error", "abstained"} and row["ranks"]:
            raise ValueError("Failed/abstained predictions must have empty rankings")
        predictions[case_id] = row
    return predictions


def aggregate(rows, level):
    if not rows:
        return None
    positions = [r[level + "_rank"] for r in rows]
    accuracy = {k: sum(p is not None and p <= k for p in positions) / len(rows) for k in range(1, 6)}
    chance = sum(sum(min(k, r["n_candidate_" + level]) / r["n_candidate_" + level]
                    if r["n_candidate_" + level] and r[level + "_in_candidates"] else 0
                    for k in range(1, 6)) / 5 for r in rows) / len(rows)
    average = sum(accuracy.values()) / 5
    return {"n": len(rows), **{f"AC@{k}": accuracy[k] for k in (1, 3, 5)},
            "Avg@5": average, "Chance@5": chance, "Lift@5": average - chance,
            "MRR": sum(1 / p if p else 0 for p in positions) / len(rows)}


def score(answers, predictions):
    rows, ids = [], set()
    for answer in answers:
        case_id = answer["case_id"]
        if case_id in ids or answer["fault"] not in FAULTS or answer["dataset"].split("-")[0] not in {"RE1", "RE2"}:
            raise ValueError("Duplicate or excluded answer")
        ids.add(case_id)
        prediction = predictions.get(case_id)
        ranks = prediction["ranks"] if prediction else []
        services = list(dict.fromkeys(to_service(x) for x in ranks))
        indicators = [pair(x) for x in ranks]
        target = (answer["service"], answer["indicator"])
        rows.append({"case_id": case_id, "dataset": answer["dataset"], "system": answer["system"],
                     "fault": answer["fault"], "service": answer["service"], "indicator": answer["indicator"],
                     "predicted": prediction is not None, "status": (prediction or {}).get("status", "missing"),
                     "service_rank": services.index(answer["service"]) + 1 if answer["service"] in services else None,
                     "indicator_rank": indicators.index(target) + 1 if target in indicators else None,
                     "n_candidate_service": answer["n_candidate_services"],
                     "n_candidate_indicator": answer["n_candidate_indicators"],
                     "service_in_candidates": answer["answer_in_candidates"],
                     "indicator_in_candidates": answer["indicator_in_candidates"], "top5": ranks[:5]})
    if set(predictions) - ids:
        raise ValueError("Predictions contain unknown cases")
    summary = {"n_cases": len(rows), "n_predicted": len(predictions),
               "n_nonempty": sum(bool(p["ranks"]) for p in predictions.values()),
               "n_errors": sum(p.get("status") == "error" for p in predictions.values()),
               "service": aggregate(rows, "service"), "indicator": aggregate(rows, "indicator"),
               "reference_code_revision": REFERENCE_REVISION}
    usage = defaultdict(list)
    for prediction in predictions.values():
        for key, value in prediction.get("usage", {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                usage[key].append(value)
    summary["usage"] = {key: {"total": sum(values), "mean": sum(values) / len(values), "n": len(values)}
                        for key, values in usage.items()}
    for group in ("dataset", "system", "fault"):
        summary["by_" + group] = {key: {"service": aggregate([r for r in rows if r[group] == key], "service"),
                                             "indicator": aggregate([r for r in rows if r[group] == key], "indicator")}
                                    for key in sorted({r[group] for r in rows})}
    return summary, rows


def metric_frame(path, task):
    frame = parquet().read_table(path).to_pandas()
    if "time" not in frame:
        raise ValueError("Metrics lack a time column")
    keep = [c for c in frame.columns if c != "time.1" and not c.endswith("_latency-50")]
    frame = frame[keep].rename(columns={c: c[:-3] for c in keep if c.endswith("_latency-90")})
    frame = frame.loc[:, ~frame.columns.duplicated()]
    start, end = task["standard_window"]["start"], task["standard_window"]["end"]
    return frame[(frame["time"] >= start) & (frame["time"] <= end)].sort_values("time")


def sentinel_prediction(task_dir, task, output, overrides):
    from agent.agent import Agent, FIELDS
    from agent.llm import Ollama
    from agent.tools import Telemetry
    # CSV conversion is local to the blinded worker workspace and contains no labels.
    import csv
    telemetry = output / "telemetry"
    telemetry.mkdir(parents=True)
    frame = metric_frame(task_dir / task["files"]["metrics"], task)
    metric_dir = telemetry / "metric"
    metric_dir.mkdir()
    long = frame.melt(id_vars=["time"], var_name="metric", value_name="value")
    long["cmdb_id"] = long["metric"].map(lambda x: x.split("_")[0])
    long["kpi_name"] = long["metric"].map(lambda x: x.partition("_")[2])
    long.rename(columns={"time": "timestamp"})[["timestamp", "cmdb_id", "kpi_name", "value"]].to_csv(metric_dir / "data.csv", index=False)
    for modality, directory in [("logs", "log"), ("traces", "trace")]:
        if modality not in task["files"]:
            continue
        target = telemetry / directory
        target.mkdir()
        reader = parquet().ParquetFile(task_dir / task["files"][modality])
        names = reader.schema_arrow.names
        mapping = {"container_name": "cmdb_id"} if modality == "logs" else {"time": "timestamp", "serviceName": "cmdb_id"}
        with (target / "data.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow([mapping.get(n, n) for n in names])
            for batch in reader.iter_batches(batch_size=8192):
                for row in batch.to_pylist():
                    writer.writerow([row[n] for n in names])
    config = json.loads((ROOT / "data/sentinel/samples/rca-config.json").read_text(encoding="utf-8"))
    config.update(overrides)
    config["allow_abstention"] = True
    llm = Ollama(config)
    agent = Agent(llm, Telemetry(telemetry, config["max_rows"]), config, output / "agent")
    context = ("System: " + task["system"] + "\nKnown anomaly timestamp (Unix seconds): " + str(task["inject_time"])
               + "\n## POSSIBLE ROOT CAUSE COMPONENTS\n" + "\n".join("- " + s for s in task["candidate_services"]))
    prediction = agent.run("Identify the single most likely initiating root-cause service from telemetry. Abstain if unsupported.",
                           context=context, required_fields=[FIELDS[1]], failure_count=1)
    ranks = [prediction["1"][FIELDS[1]]] if "1" in prediction else []
    usage = {"input_tokens": sum(c.get("prompt_tokens", 0) for c in llm.calls),
             "output_tokens": sum(c.get("output_tokens", 0) for c in llm.calls), "tool_calls": agent.steps}
    return ranks, usage


def worker(task_dir, output, method, config):
    task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    for modality, name in task["files"].items():
        if modality not in {"metrics", "logs", "traces"} or name != modality + ".parquet":
            raise ValueError("Only blinded telemetry files may be read by a worker")
        path = checked_path(task_dir, name)
        if digest(path) != task["sha256"][name]:
            raise ValueError("Task telemetry was modified")
    started = time.monotonic()
    usage = {}
    if method == "casetoolkit":
        from .casetoolkit import predict
        ranks, usage = predict(task_dir, task, output.parent, config)
    elif method == "sentinel":
        ranks, usage = sentinel_prediction(task_dir, task, output.parent, config)
    else:
        raise ValueError("Unknown diagnosis method: " + method)
    usage["wall_time_s"] = time.monotonic() - started
    save(output, {"case_id": task["case_id"], "ranks": list(ranks), "method": method,
                  "status": "ok" if ranks else "abstained", "usage": usage})


def run(tasks_root, output, method, timeout, *, resume=False, config=None):
    tasks = load_tasks(tasks_root)
    configuration = json.loads(config.read_text(encoding="utf-8")) if config else {}
    effective_configuration = configuration
    if method in {"sentinel", "casetoolkit"}:
        effective_configuration = json.loads((ROOT / "data/sentinel/samples/rca-config.json").read_text(encoding="utf-8"))
        effective_configuration.update(configuration)
        effective_configuration["allow_abstention"] = True
    identity = {"tasks_sha256": digest(tasks_root / "manifest.json"), "method": method, "timeout": timeout,
                "config": effective_configuration, "harness_sha256": digest(Path(__file__)),
                "agent_sha256": {name: digest(ROOT / "agent" / name) for name in ("agent.py", "tools.py", "llm.py")}
                    if method in {"sentinel", "casetoolkit"} else None}
    if method == "casetoolkit":
        from importlib.metadata import version
        import RCAEval.agent.toolkit as toolkit_module
        identity["casetoolkit_sha256"] = digest(Path(__file__).with_name("casetoolkit.py"))
        identity["upstream_toolkit_sha256"] = digest(Path(toolkit_module.__file__))
        identity["rcaeval_version"] = version("RCAEval")
    predictions_path = output / "predictions.jsonl"
    if output.exists():
        if not resume or json.loads((output / "run.json").read_text(encoding="utf-8")) != identity:
            raise ValueError("Use a fresh output directory, or --resume with identical tasks/method/config")
    else:
        output.mkdir(parents=True)
        save(output / "run.json", identity)
    predictions = load_predictions(predictions_path, tasks) if predictions_path.exists() else {}
    for index, (case_id, task) in enumerate(tasks.items(), 1):
        if case_id in predictions:
            continue
        case_output = output / "cases" / case_id
        case_output.mkdir(parents=True, exist_ok=True)
        result_path = case_output / "result.json"
        command = [sys.executable, str(ROOT / "scripts/evaluate_rcaeval.py"), "_worker", "--task-dir",
                   str(tasks_root / "cases" / case_id), "--out", str(result_path), "--method", method]
        if config:
            command.extend(["--config", str(config.resolve())])
        started = time.monotonic()
        try:
            with (case_output / "stdout.log").open("w", encoding="utf-8") as stdout, (case_output / "stderr.log").open("w", encoding="utf-8") as stderr:
                process = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=timeout, check=False)
            if process.returncode:
                raise RuntimeError(f"Diagnosis worker exited {process.returncode}; see stderr.log")
            prediction = json.loads(result_path.read_text(encoding="utf-8"))
            if prediction.get("case_id") != case_id or not isinstance(prediction.get("ranks"), list):
                raise ValueError("Invalid worker output")
        except Exception as error:  # noqa: BLE001 - a failed case is scored as an error and the run continues
            log.failure(LOG, "rcaeval.case.failed", error)
            prediction = {"case_id": case_id, "ranks": [], "status": "error", "method": method,
                          "error": str(error), "usage": {"wall_time_s": time.monotonic() - started}}
        append_jsonl(predictions_path, prediction)
        print(f"[{index}/{len(tasks)}] {case_id}: {prediction['status']}", flush=True)
    return predictions_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="Create blinded tasks and a separate evaluator answer file")
    export.add_argument("--data", type=Path, default=ROOT / "data/experimentation/datasets/rcaeval-non-code")
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--suites", nargs="+", choices=("RE1", "RE2"))
    export.add_argument("--systems", nargs="+", choices=("ob", "ss", "tt"))
    export.add_argument("--faults", nargs="+", choices=sorted(FAULTS))
    export.add_argument("--limit", type=int)
    export.add_argument("--exclude-tasks", type=Path, help="Exclude cases from an earlier task export before applying --limit")
    runner = sub.add_parser("run", help="Run diagnosis with per-case timeout and resumable predictions")
    runner.add_argument("--tasks", type=Path, required=True)
    runner.add_argument("--out", type=Path, required=True)
    runner.add_argument("--method", choices=("sentinel", "casetoolkit"), default="casetoolkit")
    runner.add_argument("--timeout", type=int, default=600)
    runner.add_argument("--resume", action="store_true")
    runner.add_argument("--config", type=Path)
    scorer = sub.add_parser("score", help="Score predictions; missing/error/abstained cases count as misses")
    scorer.add_argument("--answers", type=Path, required=True)
    scorer.add_argument("--predictions", type=Path, required=True)
    scorer.add_argument("--out", type=Path, required=True)
    internal = sub.add_parser("_worker", help=argparse.SUPPRESS)
    internal.add_argument("--task-dir", type=Path, required=True)
    internal.add_argument("--out", type=Path, required=True)
    internal.add_argument("--method", choices=("sentinel", "casetoolkit"), required=True)
    internal.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            print(export_tasks(args.data.resolve(), args.out.resolve(), suites=args.suites, systems=args.systems, faults=args.faults, limit=args.limit, exclude_tasks=args.exclude_tasks))
        elif args.command == "run":
            if args.timeout < 1:
                raise ValueError("timeout must be positive")
            predictions_path = run(args.tasks.resolve(), args.out.resolve(), args.method, args.timeout, resume=args.resume, config=args.config)
            print(predictions_path)
            if any(row.get("status") == "error" for row in read_jsonl(predictions_path)):
                return 1
        elif args.command == "score":
            answers = read_jsonl(args.answers)
            predictions = load_predictions(args.predictions, {a["case_id"] for a in answers})
            summary, rows = score(answers, predictions)
            args.out.mkdir(parents=True, exist_ok=False)
            summary["answers_sha256"] = digest(args.answers)
            summary["predictions_sha256"] = digest(args.predictions)
            save(args.out / "summary.json", summary)
            for row in rows:
                append_jsonl(args.out / "per-case.jsonl", row)
            print(json.dumps({k: summary[k] for k in ("n_cases", "n_predicted", "n_errors", "service", "indicator")}, indent=2))
        else:
            worker(args.task_dir.resolve(), args.out.resolve(), args.method,
                   json.loads(args.config.read_text(encoding="utf-8")) if args.config else {})
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print("RCAEval evaluation failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
