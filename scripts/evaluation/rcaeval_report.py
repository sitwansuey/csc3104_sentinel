"""Metrics with uncertainty and paired tests from RCAEval result folders.

Each --results folder is one method configuration on one split, as written by
rcaeval_run.py (or its collected evidence bundle): summary.json,
scores/per-case.jsonl, scores/summary.json and predictions.jsonl. All folders
must be the same split with the same answers file (answers_sha256).

Reported per method, at service level unless named otherwise:
- AC@1, AC@3, AC@5, indicator-level AC@1, abstention and error rates, each with
  a 95% Wilson score interval;
- Avg@5 (mean of AC@1..AC@5) and MRR@5 (reciprocal rank, 0 beyond rank 5),
  each with a seeded percentile bootstrap interval over cases. MRR is cut at
  rank 5 for every method because the baselines record only their top five;
- latency: median, 90th percentile and maximum per-case wall time, and model
  token totals from each prediction's usage;
- unsafe-action rate: not applicable, since RCAEval scores diagnosis only.
Reported per pair: exact McNemar test on AC@1 (Holm-adjusted across the pairs
reported) and a paired bootstrap interval for the Avg@5 difference.

Example:
  python scripts/evaluation/rcaeval_report.py --results <out>/eval/baro <out>/eval/sentinel --out <dir>
"""

import argparse
from itertools import combinations
import json
import math
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = "sentinel-rcaeval-report-v1"
RUN_SCHEMA = "sentinel-rcaeval-run-v1"
Z95 = 1.959963984540054
DEFAULT_SEED = 3104
DEFAULT_RESAMPLES = 10_000
TOP1_METHODS = ("sentinel",)
# Baseline rankings are stored cut at five services, so MRR is cut there for every method.
MRR_CUTOFF = 5
# Per-prediction usage counts summed into model_tokens (they survive evidence sanitising).
TOKEN_KEYS = ("input_tokens", "output_tokens")
UNSAFE_NOTE = "Not applicable: RCAEval scores diagnosis only, and no method acts on a cluster."


def wilson(successes, n, z=Z95):
    """95% Wilson score interval [low, high] for successes out of n; None when n is 0."""
    if n == 0:
        return None
    if not 0 <= successes <= n:
        raise ValueError("successes must be between 0 and n")
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == n else min(1.0, centre + half)
    return [low, high]


def mcnemar_exact(only_a, only_b):
    """Two-sided exact McNemar p-value from the two discordant counts."""
    n = only_a + only_b
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(only_a, only_b) + 1))
    return min(1.0, 2 * tail / 2 ** n)


def quantile(ordered, q):
    """Linear-interpolation quantile of an already sorted list."""
    position = q * (len(ordered) - 1)
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def bootstrap_ci(values, *, seed, resamples):
    """95% percentile bootstrap interval of the mean; None for no values."""
    if not values:
        return None
    if resamples < 1:
        raise ValueError("resamples must be positive")
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    return [quantile(means, .025), quantile(means, .975)]


def holm(pvalues):
    """Holm-Bonferroni adjusted p-values, in the input order."""
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted, running = [0.0] * len(pvalues), 0.0
    for position, index in enumerate(order):
        running = max(running, min(1.0, (len(pvalues) - position) * pvalues[index]))
        adjusted[index] = running
    return adjusted


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def load_result(folder):
    """One method's run summary, per-case rows by case id and wall times."""
    folder = Path(folder)
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    if summary.get("schema") != RUN_SCHEMA:
        raise ValueError(f"{folder}: not an rcaeval_run.py result folder")
    scores = json.loads((folder / "scores/summary.json").read_text(encoding="utf-8"))
    if scores.get("answers_sha256") != summary.get("answers_sha256"):
        raise ValueError(f"{folder}: scores and summary disagree on the answers file")
    rows = {}
    for row in read_jsonl(folder / "scores/per-case.jsonl"):
        if row["case_id"] in rows:
            raise ValueError(f"{folder}: duplicate case {row['case_id']}")
        rows[row["case_id"]] = row
    if len(rows) != summary.get("n_cases"):
        raise ValueError(f"{folder}: per-case rows do not match n_cases")
    wall, tokens = {}, {}
    if (folder / "predictions.jsonl").is_file():
        for prediction in read_jsonl(folder / "predictions.jsonl"):
            usage = prediction.get("usage") or {}
            if finite(usage.get("wall_time_s")):
                wall[prediction["case_id"]] = float(usage["wall_time_s"])
            for key in TOKEN_KEYS:
                if finite(usage.get(key)):
                    tokens[key] = tokens.get(key, 0) + usage[key]
    return {"label": summary["label"], "method": summary["method"], "split": summary["split"],
            "answers_sha256": summary["answers_sha256"], "summary": summary, "rows": rows, "wall": wall,
            "tokens": tokens}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def hit(rank, k):
    return rank is not None and rank <= k


def rate(count, n):
    return {"value": count / n if n else None, "count": count, "ci95": wilson(count, n)}


def method_metrics(result, *, seed, resamples):
    rows = [result["rows"][key] for key in sorted(result["rows"])]
    n = len(rows)
    ranks = [row["service_rank"] for row in rows]
    average = [sum(hit(rank, k) for k in range(1, 6)) / 5 for rank in ranks]
    reciprocal = [1 / rank if hit(rank, MRR_CUTOFF) else 0.0 for rank in ranks]
    walls = sorted(result["wall"].values())
    metrics = {"method": result["method"], "n": n}
    for k in (1, 3, 5):
        metrics[f"AC@{k}"] = rate(sum(hit(rank, k) for rank in ranks), n)
    metrics["Avg@5"] = {"value": sum(average) / n if n else None,
                        "ci95": bootstrap_ci(average, seed=seed, resamples=resamples)}
    metrics["MRR@5"] = {"value": sum(reciprocal) / n if n else None,
                      "ci95": bootstrap_ci(reciprocal, seed=seed, resamples=resamples)}
    metrics["indicator_AC@1"] = rate(sum(hit(row["indicator_rank"], 1) for row in rows), n)
    metrics["abstention_rate"] = rate(sum(row["status"] == "abstained" for row in rows), n)
    metrics["error_rate"] = rate(sum(row["status"] == "error" for row in rows), n)
    metrics["missing"] = sum(row["status"] == "missing" for row in rows)
    metrics["unsafe_action_rate"] = None
    metrics["latency_s"] = {"n": len(walls), "median": quantile(walls, .5), "p90": quantile(walls, .9),
                            "max": walls[-1]} if walls else None
    metrics["model_tokens"] = dict(sorted(result["tokens"].items())) or None
    metrics["top1_only"] = result["method"] in TOP1_METHODS
    return metrics


def paired(a, b, *, seed, resamples):
    """Exact McNemar on AC@1 and a paired bootstrap of the Avg@5 difference (a minus b)."""
    if set(a["rows"]) != set(b["rows"]):
        raise ValueError(f"{a['label']} and {b['label']} do not cover the same cases")
    ids = sorted(a["rows"])
    first = [a["rows"][i]["service_rank"] for i in ids]
    second = [b["rows"][i]["service_rank"] for i in ids]
    only_a = sum(hit(x, 1) and not hit(y, 1) for x, y in zip(first, second, strict=True))
    only_b = sum(hit(y, 1) and not hit(x, 1) for x, y in zip(first, second, strict=True))
    differences = [sum(hit(x, k) for k in range(1, 6)) / 5 - sum(hit(y, k) for k in range(1, 6)) / 5
                   for x, y in zip(first, second, strict=True)]
    return {"a": a["label"], "b": b["label"], "n": len(ids), "only_a_correct": only_a, "only_b_correct": only_b,
            "mcnemar_p": mcnemar_exact(only_a, only_b), "delta_AC@1": (only_a - only_b) / len(ids) if ids else None,
            "delta_Avg@5": {"value": sum(differences) / len(ids) if ids else None,
                            "ci95": bootstrap_ci(differences, seed=seed, resamples=resamples)}}


def deviations(results, freeze):
    """Protocol deviations that the result files themselves show."""
    found = []
    revisions = set()
    for result in results:
        summary, label = result["summary"], result["label"]
        if summary.get("status") != "PASS":
            found.append(f"{label}: run status {summary.get('status')} ({summary.get('n_predicted')}/"
                         f"{summary.get('n_cases')} predicted, {summary.get('n_errors')} errors)")
        invocations = summary.get("invocations") or [{"source_revision": (summary.get("provenance") or {})
                                                       .get("source_revision"),
                                                       "dirty": (summary.get("provenance") or {}).get("dirty")}]
        if any(item.get("dirty") is not False for item in invocations):
            found.append(f"{label}: run from a dirty or unknown working tree")
        label_revisions = {item.get("source_revision") for item in invocations}
        if len(label_revisions) > 1:
            found.append(f"{label}: resumed at a different source revision")
        revisions |= label_revisions
        if freeze is not None and result["split"] == "eval":
            entry = (freeze.get("methods") or {}).get(label)
            if not entry:
                found.append(f"{label}: not in the freeze file")
            elif entry.get("prompt_sha256") != summary.get("prompt_sha256"):
                found.append(f"{label}: prompt hash differs from the freeze file")
    if len(revisions) > 1:
        found.append("methods were run at different source revisions: " + ", ".join(sorted(map(str, revisions))))
    return found


def rounded(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [rounded(item) for item in value]
    return value


def build(folders, *, seed=DEFAULT_SEED, resamples=DEFAULT_RESAMPLES, pairs=None, freeze=None):
    """The report dictionary for result folders of one split."""
    results = [load_result(folder) for folder in folders]
    if not results:
        raise ValueError("No result folders")
    labels = [result["label"] for result in results]
    if len(set(labels)) != len(labels):
        raise ValueError("Duplicate method labels: " + ", ".join(labels))
    if len({result["split"] for result in results}) != 1:
        raise ValueError("Result folders mix splits")
    if len({result["answers_sha256"] for result in results}) != 1:
        raise ValueError("Result folders were scored against different answers (answers_sha256 differs)")
    by_label = {result["label"]: result for result in results}
    chosen = pairs if pairs is not None else list(combinations(sorted(by_label), 2))
    unknown = sorted({label for pair in chosen for label in pair} - set(by_label))
    if unknown:
        raise ValueError("Pairs name unknown labels: " + ", ".join(unknown))
    comparisons = [paired(by_label[a], by_label[b], seed=seed, resamples=resamples) for a, b in chosen]
    for comparison, adjusted in zip(comparisons, holm([c["mcnemar_p"] for c in comparisons]), strict=True):
        comparison["mcnemar_p_holm"] = adjusted
    first = results[0]["summary"]
    return rounded({
        "schema": SCHEMA, "split": results[0]["split"], "sample": first.get("sample"),
        "sample_sha256": first.get("sample_sha256"), "answers_sha256": results[0]["answers_sha256"],
        "level": "service", "seed": seed, "resamples": resamples,
        "methods": {label: method_metrics(by_label[label], seed=seed, resamples=resamples)
                    for label in sorted(by_label)},
        "paired": comparisons, "unsafe_action_rate_note": UNSAFE_NOTE,
        "deviations": deviations(results, freeze)})


def number(value):
    return "n/a" if value is None else f"{value:.3f}"


def with_ci(entry):
    interval = entry.get("ci95")
    text = number(entry.get("value"))
    return text if not interval else f"{text} [{number(interval[0])}, {number(interval[1])}]"


def markdown(report):
    lines = [f"# RCAEval {report['split']} results", "",
             f"Sample `{report['sample']}`, answers sha256 `{str(report['answers_sha256'])[:12]}`. Service-level "
             "scores. Intervals are 95%: Wilson score for AC@k and rates; a percentile bootstrap over cases "
             f"({report['resamples']} resamples, seed {report['seed']}) for Avg@5, MRR@5 and paired differences.", "",
             "| Method | n | AC@1 | AC@3 | Avg@5 | MRR@5 | Indicator AC@1 | Abstained | Errors | Median s | p90 s |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for label, metrics in report["methods"].items():
        latency = metrics["latency_s"] or {}
        lines.append(f"| {label} | {metrics['n']} | {with_ci(metrics['AC@1'])} | {with_ci(metrics['AC@3'])} | "
                     f"{with_ci(metrics['Avg@5'])} | {with_ci(metrics['MRR@5'])} | {with_ci(metrics['indicator_AC@1'])} | "
                     f"{metrics['abstention_rate']['count']} | {metrics['error_rate']['count']} | "
                     f"{number(latency.get('median'))} | {number(latency.get('p90'))} |")
    lines += ["", "Paired comparisons: exact McNemar test on AC@1, Holm-adjusted across the rows below, and a "
              "paired bootstrap interval for the Avg@5 difference (A minus B).", "",
              "| A | B | n | Only A correct | Only B correct | McNemar p | Holm p | AC@1 difference | "
              "Avg@5 difference |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for item in report["paired"]:
        lines.append(f"| {item['a']} | {item['b']} | {item['n']} | {item['only_a_correct']} | "
                     f"{item['only_b_correct']} | {number(item['mcnemar_p'])} | {number(item['mcnemar_p_holm'])} | "
                     f"{number(item['delta_AC@1'])} | {with_ci(item['delta_Avg@5'])} |")
    if not report["paired"]:
        lines.append("| _none_ | | | | | | | | |")
    lines += ["", "Notes:", "", "- Unsafe-action rate: " + report["unsafe_action_rate_note"]]
    top1 = [label for label, metrics in report["methods"].items() if metrics["top1_only"]]
    if top1:
        lines.append(f"- {', '.join(top1)} returns one service, so AC@3, AC@5 and Avg@5 equal AC@1 by construction.")
    lines += ["- Repetitions of the same fault cell are correlated, and n is small: read intervals, not point "
              "values, and do not claim one method is better without a paired test.", "", "Deviations:", ""]
    lines += [f"- {item}" for item in report["deviations"]] or ["- none found in the result files"]
    return "\n".join(lines) + "\n"


def parse_pair(text):
    first, separator, second = text.partition(":")
    if not separator or not first or not second:
        raise argparse.ArgumentTypeError("a pair is LABEL_A:LABEL_B")
    return first, second


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--pairs", type=parse_pair, nargs="+", help="Pre-registered pairs (default: all pairs)")
    parser.add_argument("--freeze-file", type=Path, help="Check eval results against this freeze file")
    args = parser.parse_args(argv)
    try:
        freeze = json.loads(args.freeze_file.read_text(encoding="utf-8")) if args.freeze_file else None
        report = build(args.results, seed=args.seed, resamples=args.resamples, pairs=args.pairs, freeze=freeze)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "report.json").write_bytes((json.dumps(report, indent=2) + "\n").encode("utf-8"))
        (args.out / "report.md").write_bytes(markdown(report).encode("utf-8"))
    except (OSError, ValueError, KeyError) as error:
        print("RCAEval report failed: " + str(error), file=sys.stderr)
        return 1
    print(markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
