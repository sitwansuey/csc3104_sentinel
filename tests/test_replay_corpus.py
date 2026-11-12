"""Replay corpus (P4.04): allowlist-only building, a recursive safety scan, labels outside inputs/,
and a golden replay of recorded live agent runs."""

import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from experiments.analysis import replay, replay_corpus as corpus
from scripts import build_replay_corpus as builder

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evidence/replay/v1"
EVIDENCE = ROOT / "evidence"
# A recorded live agent run (R1, decision policy gated, the model chose ROLLBACK, Guard ALLOW).
GOLDEN = "boutique-live-20261112t081032z-63d609"
# A recorded live agent run (N1) that the code rule escalated with TEMPLATE_ALREADY_BASELINE.
RULED = "boutique-live-20261112t080756z-29fd09"


def bundle(name):
    return next(EVIDENCE.glob(f"*/{name}"))


class BuilderAllowlistTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def run_folder(self):
        """A copy of a real run with the private files a run folder holds next to the evidence."""
        run = self.root / "runs" / GOLDEN
        shutil.copytree(bundle(GOLDEN), run)
        (run / ".executor-kubeconfig.json").write_text(json.dumps({
            "apiVersion": "v1", "kind": "Config", "users": [{"name": "executor", "user": {
                "token": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJleGVjdXRvciJ9.c2lnbmF0dXJlLXZhbHVlcw",
                "client-key-data": "QUJD"}}]}), encoding="utf-8")
        (run / "approved-rollback/planner.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
        (run / "telemetry.sqlite3").write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
        (run / "pki").mkdir()
        (run / "pki/ca.key").write_text("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
                                         encoding="utf-8")
        (run / "credentials.json").write_text('{"password": "hunter2"}', encoding="utf-8")
        telemetry = run / "approved-rollback/attempt-1/telemetry"
        telemetry.mkdir(parents=True)
        (telemetry / "logs.csv").write_text("timestamp,message\n2026-11-12T08:11:46Z,raw line\n", encoding="utf-8")
        return run

    def test_builder_copies_only_allowlisted_files_and_never_a_kubeconfig_or_database(self):
        out = self.root / "corpus"
        builder.build([self.run_folder()], out)
        files = [path for path in out.rglob("*") if path.is_file()]
        self.assertTrue(files)
        # Recursive scan of the result: every file is allowlisted, nothing private came along.
        self.assertEqual(corpus.scan(out), [])
        for path in files:
            relative = path.relative_to(out).as_posix()
            self.assertTrue(relative in corpus.TOP_FILES or "/".join(relative.split("/")[2:]) in corpus.CASE_FILES,
                            relative)
            data = path.read_bytes()
            for marker in (b"client-key-data", b"SQLite format 3", b"BEGIN PRIVATE KEY", b"hunter2", b"raw line",
                           b"eyJhbGciOiJSUzI1NiJ9"):
                self.assertNotIn(marker, data, relative)
        names = {path.name.lower() for path in out.rglob("*")}
        for forbidden in (".executor-kubeconfig.json", "planner.db", "telemetry.sqlite3", "ca.key",
                          "credentials.json", "logs.csv", "pki"):
            self.assertNotIn(forbidden, names)

    def test_the_builder_refuses_to_read_a_file_outside_its_allowlist(self):
        run = self.run_folder()
        for name in (".executor-kubeconfig.json", "approved-rollback/planner.db", "credentials.json"):
            with self.assertRaises(builder.BuildError):
                builder.read_source(run, name)
        with self.assertRaises(builder.BuildError):
            builder.read_glob(run, "**/*")

    def test_scan_refuses_kubeconfig_sqlite_and_credentials_planted_anywhere(self):
        out = self.root / "corpus"
        builder.build([bundle(GOLDEN)], out)
        case = next((out / "cases").iterdir())
        plants = {
            case / "inputs/.executor-kubeconfig.json": b"{}",
            case / "inputs/observations/state.sqlite3": b"SQLite format 3\x00",
            out / "credentials.json": b"{}",
            # Innocent names with private content are found by the content scan.
            case / "inputs/observations/0002-observation.json": b"SQLite format 3\x00\x00",
        }
        for path, data in plants.items():
            with self.subTest(path=path.name):
                path.write_bytes(data)
                problems = corpus.scan(out)
                self.assertTrue(any(path.name in problem or "SQLite" in problem for problem in problems), problems)
                with self.assertRaises(corpus.CorpusError):
                    corpus.verify(out)
                path.unlink()
        self.assertEqual(corpus.scan(out), [])
        policy = case / "inputs/policy.json"
        policy.write_bytes(policy.read_bytes().replace(b'"policy_id"', b'"client-key-data": "QUJD", "policy_id"'))
        self.assertTrue(any("kubeconfig key data" in problem for problem in corpus.scan(out)))

    def test_verify_refuses_a_file_that_does_not_match_the_manifest(self):
        out = self.root / "corpus"
        builder.build([bundle(GOLDEN)], out)
        corpus.verify(out)
        incident = next((out / "cases").iterdir()) / "inputs/incident.json"
        value = corpus.read_json(incident)
        value["observed_resource_version"] = "1"
        corpus.write_json(incident, value)
        with self.assertRaisesRegex(corpus.CorpusError, "does not match the manifest"):
            corpus.verify(out)


class LabelSeparationTests(unittest.TestCase):
    def setUp(self):
        self.manifest = corpus.verify(CORPUS)

    def test_every_case_keeps_its_labels_beside_inputs_and_none_inside(self):
        for entry in self.manifest["cases"]:
            folder = CORPUS / "cases" / entry["case_id"]
            self.assertTrue((folder / "expected.json").is_file())
            inside = {path.name for path in (folder / "inputs").rglob("*")}
            self.assertFalse(inside & {"expected.json", "label.json"})
            self.assertEqual(corpus.label_leaks(folder), [], entry["case_id"])
            self.assertNotIn("expected", corpus.load_case(CORPUS, entry["case_id"]))

    def test_a_label_key_planted_under_inputs_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / "corpus"
            shutil.copytree(CORPUS, copy)
            entry = self.manifest["cases"][0]
            path = copy / "cases" / entry["case_id"] / "inputs/incident.json"
            value = corpus.read_json(path)
            value["signals"][0]["expected"] = "ROLLBACK"
            corpus.write_json(path, value)
            self.assertEqual(corpus.label_leaks(path.parents[1]), ["incident.json: expected"])
            # Even with the manifest updated to match, loading the corpus refuses the leak.
            manifest = corpus.read_json(copy / corpus.MANIFEST)
            listed = next(item for item in manifest["cases"] if item["case_id"] == entry["case_id"])
            listed["files"]["inputs/incident.json"] = corpus.sha256_file(path)
            corpus.write_json(copy / corpus.MANIFEST, manifest)
            with self.assertRaisesRegex(corpus.CorpusError, "label keys under inputs"):
                corpus.verify(copy)

    def test_every_label_set_is_pending_review_and_every_variant_has_its_twin(self):
        ids = {entry["case_id"] for entry in self.manifest["cases"]}
        self.assertEqual(self.manifest["labels"]["status"], "pending")
        counts = self.manifest["counts"]
        self.assertGreaterEqual(counts["captured"], 40)
        self.assertGreater(counts["derived"], 0)
        self.assertGreater(counts["adversarial"], 0)
        for entry in self.manifest["cases"]:
            expected = corpus.load_expected(CORPUS, entry["case_id"])
            self.assertEqual(expected["review"]["status"], "pending")
            self.assertIsNone(expected["review"]["by"])
            meta = corpus.load_case(CORPUS, entry["case_id"])["meta"]
            if entry["provenance"] == "captured":
                self.assertIsNone(meta["twin"])
                self.assertTrue(meta["source"]["bundle_id"])
            else:
                self.assertIn(meta["twin"], ids)

    def test_the_committed_corpus_matches_a_rebuild_from_the_evidence(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(builder.main(["--check"]), 0)
        self.assertIn("is up to date", output.getvalue())

    def test_each_attack_line_is_flagged_or_missed_by_the_injection_rules_as_declared(self):
        from agent.untrusted import suspicious
        for attack in builder.ATTACKS:
            with self.subTest(attack=attack["id"]):
                self.assertEqual(bool(suspicious(attack["text"])), attack["flagged"])
        self.assertEqual(suspicious(builder.MISLEADING_LINE), [])

    def test_adversarial_text_reaches_the_agents_logs_but_never_label_free_metadata(self):
        from experiments.boutique.harness.agent_live import export_telemetry
        entry = next(item for item in self.manifest["cases"] if item["case_id"].startswith("adv-rollback-runbook-"))
        case = corpus.load_case(CORPUS, entry["case_id"])
        attack = next(item for item in builder.ATTACKS if item["id"] == case["meta"]["attack"]["id"])
        self.assertEqual(case["meta"]["attack"]["text_sha256"], builder._short(attack["text"]))
        twin = corpus.load_case(CORPUS, case["meta"]["twin"])
        with tempfile.TemporaryDirectory() as temporary:
            for subject, present in ((case, True), (twin, False)):
                destination = Path(temporary) / subject["case_id"] / "telemetry"
                incident = subject["inputs"]["incident"]
                export_telemetry(subject["observations"], incident["target"], destination, incident=incident)
                logs = (destination / "logs.csv").read_text(encoding="utf-8")
                self.assertEqual("rolling the Deployment back" in logs, present)
        for path in (CORPUS / "cases" / entry["case_id"] / "case.json", CORPUS / corpus.MANIFEST):
            self.assertNotIn("rolling the Deployment back", path.read_text(encoding="utf-8"))


class ReviewTests(unittest.TestCase):
    def test_a_sign_off_survives_a_rebuild_only_while_the_labels_are_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, second, third = (Path(temporary) / name for name in ("a", "b", "c"))
            builder.build([bundle(GOLDEN)], first)
            path = first / "cases" / ("cap-" + GOLDEN) / "expected.json"
            expected = corpus.read_json(path)
            self.assertEqual(expected["review"]["status"], "pending")
            expected["review"].update(status="signed", by="evaluation lead", on="2026-11-14")
            corpus.write_json(path, expected)
            builder.build([bundle(GOLDEN)], second, reviews=builder.previous_reviews(first))
            kept = corpus.read_json(second / "cases" / ("cap-" + GOLDEN) / "expected.json")["review"]
            self.assertEqual((kept["status"], kept["by"]), ("signed", "evaluation lead"))
            self.assertEqual(corpus.read_json(second / corpus.MANIFEST)["labels"]["signed"], 1)
            # A review of other labels (here: a stale digest) does not carry over.
            reviews = builder.previous_reviews(first)
            reviews["cap-" + GOLDEN]["labels_sha256"] = "0" * 64
            builder.build([bundle(GOLDEN)], third, reviews=reviews)
            reset = corpus.read_json(third / "cases" / ("cap-" + GOLDEN) / "expected.json")["review"]
            self.assertEqual((reset["status"], reset["by"]), ("pending", None))


class GoldenReplayTests(unittest.TestCase):
    def test_the_guard_on_reconstructed_state_agrees_with_the_recorded_live_verdict(self):
        checked = replay.guard_crosscheck(corpus.load_case(CORPUS, "cap-" + GOLDEN))
        self.assertEqual(checked["recorded_guard_verdict"], "ALLOW")
        self.assertTrue(checked["agrees"])
        self.assertIsNone(replay.guard_crosscheck(corpus.load_case(CORPUS, "cap-" + RULED)))

    def test_replay_of_a_recorded_live_case_reproduces_its_proposal(self):
        case = corpus.load_case(CORPUS, "cap-" + GOLDEN)
        recorded = case["recorded"]["proposal"]
        result = replay.golden(case)
        self.assertEqual(result["status"], "reproduced", result)
        self.assertEqual(result["differing_fields"], [])
        # Without the one mapped hash the proposals differ only in the reconstructed template hash.
        answers = {"Recovery Planner": {"action": "ROLLBACK", "parameters": replay.binding(case, "ROLLBACK"),
                                        "rationale": recorded["rationale"]}}
        _, replayed = replay.replay(case, "P4", llm_factory=lambda *a, **k: replay.ScriptedModel(answers),
                                    diagnosis="recorded")
        for key in ("action", "incident_id", "target", "based_on_resource_version", "evidence_refs", "rationale"):
            self.assertEqual(replayed[key], recorded[key], key)
        self.assertEqual(replayed["parameters"]["baseline_id"], recorded["parameters"]["baseline_id"])
        self.assertNotEqual(replayed["parameters"]["template_sha256"], recorded["parameters"]["template_sha256"])

    def test_a_recorded_proposal_the_replay_does_not_produce_differs(self):
        case = corpus.load_case(CORPUS, "cap-" + GOLDEN)
        for field, change in (("evidence_refs", lambda proposal: proposal["evidence_refs"].pop()),
                              ("based_on_resource_version",
                               lambda proposal: proposal.update(based_on_resource_version="1"))):
            with self.subTest(field=field):
                changed = deepcopy(case)
                change(changed["recorded"]["proposal"])
                result = replay.golden(changed)
                self.assertEqual(result["status"], "differs")
                self.assertIn(field, result["differing_fields"])
        # The stub returns the recorded rationale, so an edited one shows only through the action id.
        changed = deepcopy(case)
        changed["recorded"]["proposal"]["rationale"] += " (edited)"
        self.assertEqual(replay.golden(changed)["differing_fields"], ["action_id"])

    def test_a_recorded_rule_escalation_reproduces_without_a_model_call(self):
        case = corpus.load_case(CORPUS, "cap-" + RULED)
        result = replay.golden(case)
        self.assertEqual(result["status"], "reproduced", result)
        self.assertEqual(result["replayed_error_code"], "TEMPLATE_ALREADY_BASELINE")

    def test_every_run_recorded_under_a_decision_policy_reproduces(self):
        results = [replay.golden(corpus.load_case(CORPUS, entry["case_id"]))
                   for entry in corpus.verify(CORPUS)["cases"] if entry["provenance"] == "captured"
                   and "diagnosis" in corpus.load_case(CORPUS, entry["case_id"])["recorded"]]
        governed = [item for item in results if item.get("recorded_policy")]
        self.assertGreaterEqual(len(governed), 8)
        self.assertEqual([item["case_id"] for item in governed if item["status"] != "reproduced"], [])


if __name__ == "__main__":
    unittest.main()
