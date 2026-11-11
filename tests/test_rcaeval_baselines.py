"""BARO and anomaly-rank baselines through the pinned RCAEval package, on synthetic data."""

import importlib.util
import json
import math
from pathlib import Path
import random
import tempfile
import unittest

from agent.datasets import baselines
from agent.datasets import evaluate as ev

HAS_STACK = bool(importlib.util.find_spec("pyarrow") and importlib.util.find_spec("pandas")
                 and importlib.util.find_spec("RCAEval"))
SERVICES = ("adservice", "cartservice", "currencyservice", "emailservice", "frontend", "paymentservice")
INJECT = 1000


def frame(spike=None, *, drop=False, constant=False, start=400, end=1600):
    """Noisy cpu/mem series per service; `spike` = (service, metric) rises (or falls) after INJECT."""
    import pandas as pd
    times = list(range(start, end + 1, 5))
    columns = {"time": times}
    for offset, service in enumerate(SERVICES):
        for kind in ("cpu", "mem"):
            phase = offset * 3 + len(kind)
            values = []
            for index, time in enumerate(times):
                value = 10.0 if constant else 10.0 + 0.3 * math.sin(index * 1.3 + phase) + 0.1 * math.cos(index * .7)
                if spike == (service, kind) and time >= INJECT:
                    value += -6.0 if drop else 6.0
                values.append(value)
            columns[f"{service}_{kind}"] = values
    return pd.DataFrame(columns)


@unittest.skipUnless(HAS_STACK, "the pinned RCAEval package and its data readers are not installed")
class BaselineTests(unittest.TestCase):
    def test_both_baselines_rank_a_synthetic_spike_first(self):
        for method in ("baro", "anomaly-rank"):
            for spike in (("cartservice", "cpu"), ("emailservice", "mem")):
                ranks, reason = baselines.rank(method, frame(spike), INJECT, "re1-ob")
                self.assertIsNone(reason)
                self.assertEqual(ranks[0], f"{spike[0]}_{spike[1]}", (method, spike))

    def test_a_drop_is_not_an_increase(self):
        # Upstream scores the largest scaled value after the inject time, so a fall is not ranked first.
        ranks, _ = baselines.rank("baro", frame(("cartservice", "cpu"), drop=True), INJECT, "re1-ob")
        self.assertNotEqual(ranks[0], "cartservice_cpu")

    def test_a_constant_frame_abstains_instead_of_failing(self):
        for method in ("baro", "anomaly-rank"):
            self.assertEqual(baselines.rank(method, frame(constant=True), INJECT, "re1-ob"),
                             ([], "no metric changed in the window"))

    def test_no_samples_before_or_after_the_inject_time_abstains(self):
        self.assertEqual(baselines.rank("baro", frame(start=INJECT), INJECT, "re1-ob")[0], [])
        self.assertEqual(baselines.rank("baro", frame(end=INJECT - 5), INJECT, "re1-ob")[0], [])

    def test_rankings_hold_at_most_five_distinct_services(self):
        for method in ("baro", "anomaly-rank"):
            ranks, _ = baselines.rank(method, frame(("cartservice", "cpu")), INJECT, "re2-ob")
            services = list(dict.fromkeys(ev.to_service(name) for name in ranks))
            self.assertLessEqual(len(services), 5)
            self.assertGreaterEqual(len(ranks), 5)
            self.assertEqual(len(services), 5)

    def test_worker_records_an_abstention_not_an_error(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_dir = root / "task"
            task_dir.mkdir()
            for name, value in (("spike", frame(("cartservice", "cpu"))), ("flat", frame(constant=True))):
                metrics = task_dir / "metrics.parquet"
                pq.write_table(pa.Table.from_pandas(value, preserve_index=False), metrics)
                task = {"case_id": "case-" + name, "dataset": "RE2-OB", "inject_time": INJECT,
                        "standard_window": {"start": INJECT - 600, "end": INJECT + 600},
                        "files": {"metrics": "metrics.parquet"}, "sha256": {"metrics.parquet": ev.digest(metrics)}}
                (task_dir / "task.json").write_text(json.dumps(task), encoding="utf-8")
                ev.worker(task_dir, root / name / "result.json", "baro", {})
            spike = json.loads((root / "spike/result.json").read_text(encoding="utf-8"))
            flat = json.loads((root / "flat/result.json").read_text(encoding="utf-8"))
        self.assertEqual((spike["status"], spike["ranks"][0], spike["method"]), ("ok", "cartservice_cpu", "baro"))
        self.assertEqual((flat["status"], flat["ranks"]), ("abstained", []))
        self.assertIn("wall_time_s", flat["usage"])


class TruncationTests(unittest.TestCase):
    def test_truncation_keeps_every_ac_at_k_at_both_levels(self):
        rng = random.Random(7)
        names = [f"s{service}_{kind}" for service in range(9) for kind in ("cpu", "mem", "latency")]
        for trial in range(200):
            ranks = rng.sample(names, rng.randrange(1, len(names)))
            truth = rng.choice(names)
            answer = {"case_id": "a", "dataset": "RE2-OB", "system": "ob", "service": truth.split("_")[0],
                      "fault": "cpu", "indicator": truth.split("_")[1], "n_candidate_services": 9,
                      "n_candidate_indicators": 27, "answer_in_candidates": True, "indicator_in_candidates": True}
            full, _ = ev.score([answer], {"a": {"ranks": ranks}})
            cut, _ = ev.score([answer], {"a": {"ranks": baselines.truncate(ranks)}})
            for level in ("service", "indicator"):
                for metric in ("AC@1", "AC@3", "AC@5", "Avg@5"):
                    self.assertEqual(full[level][metric], cut[level][metric], (trial, level, metric))

    def test_truncation_stops_at_five_services_and_five_entries(self):
        ranks = ["a_cpu", "a_mem", "b_cpu", "c_cpu", "d_cpu", "e_cpu", "f_cpu"]
        self.assertEqual(baselines.truncate(ranks), ranks[:6])
        self.assertEqual(baselines.truncate(["a_cpu", "b_cpu"]), ["a_cpu", "b_cpu"])

    def test_unknown_baseline_is_refused(self):
        with self.assertRaises(ValueError):
            baselines.upstream("random")


if __name__ == "__main__":
    unittest.main()
