"""Pre-registered RCAEval sample, opt-in subset download and export by case list."""

from contextlib import ExitStack, redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.datasets import evaluate as ev
from agent.datasets import rcaeval as data
from scripts.evaluation import rcaeval_sample as sampler
from tests.support import rcaeval_fixture as fixture

HAS_PARQUET = bool(importlib.util.find_spec("pyarrow") and importlib.util.find_spec("pandas"))


class Fixture(unittest.TestCase):
    """A temporary synthetic dataset root with the adapter's pinned config pointed at it."""

    telemetry = False
    index_rows = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_dir, self.rows, config = fixture.build(self.root / "data", self.index_rows,
                                                         telemetry=self.telemetry)
        self.config = fixture.write_config(self.root / "rcaeval.json", config)
        patcher = patch.object(data, "CONFIG", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temp.cleanup()


class SampleTests(Fixture):
    def test_same_index_gives_a_byte_identical_manifest(self):
        first, second = self.root / "a.json", self.root / "b.json"
        self.assertEqual(sampler.main(["--data-dir", str(self.data_dir), "--out", str(first)]), 0)
        self.assertEqual(sampler.main(["--data-dir", str(self.data_dir), "--out", str(second)]), 0)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_every_cell_once_in_eval_and_one_dev_case_per_fault_disjoint(self):
        manifest = sampler.build(self.data_dir)
        evaluation, development = manifest["splits"]["eval"], manifest["splits"]["dev"]
        cells = {(r["suite"], r["root_cause_service"], r["fault"]) for r in self.rows if r["system"] == "ob"}
        by_case = {r["case"]: r for r in self.rows}
        self.assertEqual(sorted((by_case[c]["suite"], by_case[c]["root_cause_service"], by_case[c]["fault"])
                                for c in evaluation), sorted(cells))
        self.assertTrue(all(by_case[c]["repetition"] == 1 for c in evaluation))
        self.assertEqual(sorted((by_case[c]["suite"], by_case[c]["fault"]) for c in development),
                         sorted({(suite, fault) for suite, faults in fixture.FAULTS.items() for fault in faults}))
        self.assertTrue(all(by_case[c]["repetition"] == 2 for c in development))
        self.assertFalse(set(evaluation) & set(development))
        self.assertEqual(manifest["counts"], {"dev": 4, "eval": 12})

    def test_sock_shop_train_ticket_and_re3_are_never_sampled(self):
        manifest = sampler.build(self.data_dir)
        names = manifest["splits"]["eval"] + manifest["splits"]["dev"]
        self.assertTrue(all(name.startswith(("re1ob_", "re2ob_")) for name in names))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            sampler.main(["--data-dir", str(self.data_dir), "--suites", "RE3"])
        with self.assertRaises(ValueError):
            sampler.draw(self.rows, suites=("RE3",))

    def test_the_seed_changes_only_the_dev_choice(self):
        manifests = [sampler.build(self.data_dir, seed=seed) for seed in range(8)]
        self.assertEqual(len({tuple(m["splits"]["eval"]) for m in manifests}), 1)
        self.assertGreater(len({tuple(m["splits"]["dev"]) for m in manifests}), 1)
        self.assertEqual(sampler.build(self.data_dir, seed=5), sampler.build(self.data_dir, seed=5))

    def test_manifest_holds_case_ids_rule_and_index_hash_only(self):
        manifest = sampler.build(self.data_dir)
        self.assertEqual(set(manifest), {"schema", "name", "dataset", "cases_sha256", "rule", "counts", "bytes",
                                         "splits"})
        self.assertEqual(manifest["cases_sha256"], data.index_digest(self.data_dir / "cases.json"))
        self.assertEqual(manifest["bytes"], {"dev": 4 * 4_000_010, "eval": 12 * 4_000_010})
        text = json.dumps(manifest)
        for label_field in ("root_cause_service", "inject_time", '"fault"'):
            self.assertNotIn(label_field, text)

    def test_index_hash_ignores_line_endings(self):
        path = self.data_dir / "cases.json"
        before = data.index_digest(path)
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(data.index_digest(path), before)

    def test_bad_rules_and_missing_repetitions_are_refused(self):
        for arguments in ({"eval_repetition": 2, "dev_repetition": 2}, {"dev_per_fault": 0},
                          {"eval_repetition": 0}):
            with self.assertRaises(ValueError):
                sampler.draw(self.rows, **arguments)
        with self.assertRaisesRegex(ValueError, "no repetition 3"):
            sampler.draw(self.rows, dev_repetition=3)
        with self.assertRaisesRegex(ValueError, "no Online Boutique"):
            sampler.draw([r for r in self.rows if r["system"] != "ob"])

    def test_pinned_counts_are_enforced_for_the_index(self):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["expected_counts"]["RE1-OB"] += 1
        fixture.write_config(self.config, config)
        with self.assertRaisesRegex(ValueError, "counts changed"):
            sampler.build(self.data_dir)

    def test_an_existing_different_manifest_is_never_replaced(self):
        out = self.root / "sample.json"
        self.assertEqual(sampler.write(sampler.build(self.data_dir), out), "written")
        out.write_bytes(out.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(sampler.write(sampler.build(self.data_dir), out), "unchanged")
        with self.assertRaisesRegex(ValueError, "new name"):
            sampler.write(sampler.build(self.data_dir, seed=1), out)
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(sampler.main(["--data-dir", str(self.data_dir), "--out", str(out), "--seed", "1"]), 1)
        self.assertIn("new name", errors.getvalue())


class Re3IndexTests(Fixture):
    def test_re3_rows_in_the_index_are_skipped(self):
        rows = self.rows + [fixture.row("RE3", "ob", "cartservice", "f1", 1)]
        (self.data_dir / "cases.json").write_text(json.dumps(rows), encoding="utf-8")
        manifest = sampler.build(self.data_dir)
        self.assertFalse(any(name.startswith("re3") for split in manifest["splits"].values() for name in split))


class LicenceTests(unittest.TestCase):
    def test_licence_and_gating_are_read_from_the_dataset_card(self):
        self.assertEqual(data.licence_of({"cardData": {"license": "MIT"}, "gated": False}), ("mit", False))
        self.assertEqual(data.licence_of({"cardData": {"license": ["mit"]}, "gated": "auto"}), ("mit", "auto"))
        self.assertEqual(data.licence_of({}), (None, None))
        self.assertIsNone(data.licence_problem("mit", False))
        for licence, gated in (("other", False), (None, False), ("mit", True), ("mit", "manual"), ("mit", None)):
            self.assertIsNotNone(data.licence_problem(licence, gated))


@unittest.skipUnless(HAS_PARQUET, "optional evaluation dependencies are not installed")
class DownloaderTests(Fixture):
    def remote(self, *, licence="mit", gated=False, metric_bytes=1000):
        import pyarrow as pa
        import pyarrow.parquet as pq
        stream = io.BytesIO()
        pq.write_table(pa.Table.from_pylist(self.rows), stream)
        siblings = []
        for item in self.rows:
            siblings.append({"rfilename": item["case"] + "/metrics.parquet", "size": metric_bytes,
                             "lfs": {"sha256": "a" * 64}})
            siblings.append({"rfilename": item["case"] + "/inject_time.txt", "size": 10, "blobId": "b" * 40})
        info = {"sha": fixture.REVISION, "siblings": siblings, "cardData": {"license": licence}, "gated": gated}

        def fetch(url, limit=None):
            return stream.getvalue() if url.endswith("cases.parquet") else json.dumps(info).encode()
        return fetch

    def download(self, arguments, **remote):
        output = self.root / "download"
        with ExitStack() as stack:
            stack.enter_context(patch.object(data, "fetch", side_effect=self.remote(**remote)))
            downloads = stack.enter_context(patch.object(data, "download_file", side_effect=lambda r, i, b: i["path"]))
            errors = stack.enter_context(redirect_stderr(io.StringIO()))
            stack.enter_context(patch("sys.stdout", new_callable=io.StringIO))
            code = data.main(["--output", str(output), *arguments])
        return code, downloads, errors.getvalue(), output

    def test_other_licence_or_gated_dataset_refuses_before_any_telemetry(self):
        for remote in ({"licence": "other"}, {"gated": True}, {"licence": None}):
            code, downloads, errors, _ = self.download([], **remote)
            self.assertEqual(code, 1, remote)
            downloads.assert_not_called()
            self.assertIn("RCAEval import failed", errors)

    def test_index_only_records_the_licence_without_downloading(self):
        code, downloads, _, output = self.download(["--index-only"], licence="other")
        self.assertEqual(code, 0)
        downloads.assert_not_called()
        manifest = json.loads((output / "source-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["license"], manifest["gated"]), ("other", False))

    def test_a_plan_above_200_mb_needs_yes(self):
        code, downloads, errors, _ = self.download([], metric_bytes=20_000_000)
        self.assertEqual(code, 1)
        downloads.assert_not_called()
        self.assertIn(str(len(self.rows) * 20_000_010) + " bytes", errors)
        code, downloads, _, _ = self.download(["--yes"], metric_bytes=20_000_000)
        self.assertEqual(code, 0)
        self.assertEqual(downloads.call_count, 2 * len(self.rows))

    def test_cases_from_downloads_only_that_split(self):
        _, _, _, output = self.download(["--index-only"])
        sample = self.root / "sample.json"
        sampler.write(sampler.build(output), sample)
        dev = set(json.loads(sample.read_text(encoding="utf-8"))["splits"]["dev"])
        code, downloads, _, output = self.download(["--cases-from", str(sample), "--split", "dev"])
        self.assertEqual(code, 0)
        self.assertEqual({call.args[1]["path"].split("/")[0] for call in downloads.call_args_list}, dev)
        status = json.loads((output / "download-status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["requested_files"], 2 * len(dev))
        self.assertIn("split dev", status["scope"])

    def sample_from_index(self):
        _, _, _, output = self.download(["--index-only"])
        sample = self.root / "sample.json"
        sampler.write(sampler.build(output), sample)
        return sample

    def test_cases_from_above_200_mb_needs_yes(self):
        sample = self.sample_from_index()
        cases = json.loads(sample.read_text(encoding="utf-8"))["splits"]
        code, downloads, errors, _ = self.download(["--cases-from", str(sample), "--split", "all"],
                                                   metric_bytes=20_000_000)
        self.assertEqual(code, 1)
        downloads.assert_not_called()
        self.assertIn(str((len(cases["dev"]) + len(cases["eval"])) * 20_000_010) + " bytes", errors)
        code, downloads, _, _ = self.download(["--cases-from", str(sample), "--split", "all", "--yes"],
                                              metric_bytes=20_000_000)
        self.assertEqual(code, 0)
        self.assertEqual(downloads.call_count, 2 * (len(cases["dev"]) + len(cases["eval"])))

    def test_one_case_or_a_sample_still_refuses_another_licence(self):
        sample = self.sample_from_index()
        for arguments in (["--case", self.rows[0]["case"]], ["--cases-from", str(sample), "--split", "dev"]):
            for remote in ({"licence": "other"}, {"gated": True}):
                code, downloads, errors, _ = self.download(arguments, **remote)
                self.assertEqual(code, 1, (arguments, remote))
                downloads.assert_not_called()
                self.assertIn("RCAEval import failed", errors)

    def test_cases_from_with_unknown_cases_or_another_index_refuses(self):
        _, _, _, output = self.download(["--index-only"])
        sample = self.root / "sample.json"
        manifest = sampler.build(output)
        manifest["splits"]["dev"].append("re3ob_cartservice_f1_1")
        sample.write_text(json.dumps(manifest), encoding="utf-8")
        code, downloads, errors, _ = self.download(["--cases-from", str(sample), "--split", "dev"])
        self.assertEqual(code, 1)
        downloads.assert_not_called()
        self.assertIn("re3ob_cartservice_f1_1", errors)
        manifest = sampler.build(output)
        manifest["cases_sha256"] = "0" * 64
        sample.write_text(json.dumps(manifest), encoding="utf-8")
        code, downloads, errors, _ = self.download(["--cases-from", str(sample)])
        self.assertEqual(code, 1)
        self.assertIn("different cases.json", errors)
        downloads.assert_not_called()

    def test_split_needs_cases_from_and_excludes_case(self):
        for arguments in (["--split", "dev"], ["--case", "x", "--cases-from", "y.json"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                data.main(arguments)


@unittest.skipUnless(HAS_PARQUET, "optional evaluation dependencies are not installed")
class ExportByCaseListTests(Fixture):
    telemetry = True
    index_rows = fixture.rows(services=("adservice", "cartservice"), faults={"RE1": ("cpu",), "RE2": ("cpu",)},
                              repetitions={"RE1": 2, "RE2": 2})

    def test_only_the_listed_cases_are_exported_and_unknown_names_refused(self):
        wanted = ["re1ob_cartservice_cpu_1", "re2ob_adservice_cpu_2"]
        tasks = ev.export_tasks(self.data_dir, self.root / "export", cases=wanted)
        answers = ev.read_jsonl(self.root / "export/answers.jsonl")
        self.assertEqual(sorted(a["source_case"] for a in answers), wanted)
        manifest = json.loads((tasks / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["selection"]["explicit_cases"], 2)
        self.assertNotIn("cartservice_cpu_1", (tasks / "manifest.json").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "re3ob_cartservice_f1_1"):
            ev.export_tasks(self.data_dir, self.root / "other", cases=["re3ob_cartservice_f1_1"])

    def test_a_missing_raw_file_names_the_downloader(self):
        (self.data_dir / "raw/re1ob_cartservice_cpu_1/metrics.parquet").unlink()
        with self.assertRaisesRegex(ValueError, "rerun downloader: re1ob_cartservice_cpu_1/metrics.parquet"):
            ev.export_tasks(self.data_dir, self.root / "export", cases=["re1ob_cartservice_cpu_1"])


if __name__ == "__main__":
    unittest.main()
