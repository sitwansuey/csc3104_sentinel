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
        """A copy of a real run with the private files a run folder holds next to the evidence.

        The token, key and password below are synthetic test fixtures, not secrets."""
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

    def test_a_chaos_fault_run_is_listed_as_unused_with_its_reason_not_reconstructed(self):
        catalog = json.loads(builder.CATALOG.read_text(encoding="utf-8"))
        case, reason = builder.capture(bundle("chaos-n2-agent-r1-29e41f8"), catalog)
        self.assertIsNone(case)
        self.assertIn("chaos fault (N2) acts outside the Deployment", reason)

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
        # Each plant names the check that must report it, so every check is shown to work on its own.
        plants = {
            case / "inputs/.executor-kubeconfig.json": (b"{}", "forbidden name"),
            case / "inputs/observations/state.sqlite3": (b"SQLite format 3\x00", "forbidden name"),
            out / "credentials.json": (b"{}", "forbidden name"),
            # An allowlisted path inside a case folder whose name marks a credential.
            out / "cases/copied-token-store/case.json": (b"{}", "forbidden name"),
            # An innocent name that is simply not on the allowlist.
            case / "inputs/logs.csv": (b"timestamp,message\n", "not an allowlisted corpus file"),
            # Innocent names with private content are found by the content scan.
            case / "inputs/observations/0002-observation.json": (b"SQLite format 3\x00\x00", "SQLite database bytes"),
        }
        for path, (data, reason) in plants.items():
            with self.subTest(path=path.name):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                problems = corpus.scan(out)
                self.assertTrue(any(reason in problem for problem in problems), problems)
                with self.assertRaises(corpus.CorpusError):
                    corpus.verify(out)
                path.unlink()
        shutil.rmtree(out / "cases/copied-token-store")
        self.assertEqual(corpus.scan(out), [])
        policy = case / "inputs/policy.json"
        policy.write_bytes(policy.read_bytes().replace(b'"policy_id"', b'"client-key-data": "QUJD", "policy_id"'))
        self.assertTrue(any("kubeconfig key data" in problem for problem in corpus.scan(out)))

    def test_the_content_scan_refuses_each_secret_shape_in_an_allowlisted_file(self):
        """Each plant sits in an innocent, allowlisted file and matches one content pattern only, so
        removing that pattern alone makes its subtest fail. The values are synthetic fixtures, not secrets."""
        out = self.root / "corpus"
        builder.build([bundle(GOLDEN)], out)
        case = next((out / "cases").iterdir())
        plants = (
            ("inputs/observations/0002-observation.json", b"SQLite format 3\x00\x00", "SQLite database bytes"),
            ("inputs/observations/0002-observation.json",
             b'{"note": "-----BEGIN RSA PRIVATE KEY-----\\nAAAA\\n-----END RSA PRIVATE KEY-----"}',
             "a PEM key or certificate"),
            ("inputs/policy.json", b'{"note": "-----BEGIN CERTIFICATE-----\\nAAAA\\n-----END CERTIFICATE-----"}',
             "a PEM key or certificate"),
            ("inputs/baseline.json",
             b'{"note": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJyZXBsYXkifQ.c2lnbmF0dXJlLWZpeHR1cmU"}', "a JWT"),
            ("inputs/observations/0002-observation.json",
             b'{"line": "GET /cart Authorization: Bearer 3f9a1c0d7e6b5a4c2d1e"}', "a bearer token"),
            ("inputs/live-state.json", b'{"password": "x"}', "a credential value"),
            ("inputs/dependency-finding.json", b'{"api_key" : "fixture"}', "a credential value"),
            ("inputs/incident.json", b'{"users": [{"client-key-data": "QUJD"}]}', "kubeconfig key data"),
        )
        for name, data, reason in plants:
            path = case / name
            original = path.read_bytes() if path.is_file() else None
            with self.subTest(name=name, reason=reason):
                path.write_bytes(data)
                self.assertEqual(corpus.scan(out), [f"{path.relative_to(out).as_posix()}: contains {reason}"])
                with self.assertRaisesRegex(corpus.CorpusError, "Unsafe corpus: .*contains " + reason):
                    corpus.verify(out)
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)
        self.assertEqual(corpus.scan(out), [])

    def test_a_placeholder_credential_field_is_not_a_credential_value(self):
        out = self.root / "corpus"
        builder.build([bundle(GOLDEN)], out)
        policy = next((out / "cases").iterdir()) / "inputs/policy.json"
        original = policy.read_bytes()
        # An empty value or a <placeholder> is how a document names a field without holding a secret.
        for text in (b'"password": "", "policy_id"', b'"token": "<set at deploy time>", "policy_id"',
                     b'"note": "Authorization: Bearer <token>", "policy_id"'):
            with self.subTest(text=text):
                policy.write_bytes(original.replace(b'"policy_id"', text, 1))
                self.assertEqual(corpus.scan(out), [])
        policy.write_bytes(original)

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

    def test_build_scans_its_own_output_and_deletes_an_unsafe_corpus(self):
        original = builder.write_case

        def leaky(root, case, previous=None):
            files = original(root, case, previous)
            (Path(root) / "cases" / case["case_id"] / "inputs/planner.db").write_bytes(b"SQLite format 3\x00")
            return files

        builder.write_case = leaky
        self.addCleanup(setattr, builder, "write_case", original)
        out = self.root / "corpus"
        with self.assertRaisesRegex(builder.BuildError, "failed the safety scan.*planner.db"):
            builder.build([bundle(GOLDEN)], out)
        self.assertFalse(out.exists())


class OutputFolderTests(unittest.TestCase):
    """The command line replaces an existing --out only when it already is a replay corpus."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def main(self, out):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            code = builder.main(["--runs", str(bundle(GOLDEN)), "--out", str(out)])
        return code, errors.getvalue()

    def snapshot(self, folder):
        return {path.relative_to(folder).as_posix(): path.read_bytes()
                for path in sorted(folder.rglob("*")) if path.is_file()}

    def assertNoStaging(self):
        self.assertEqual([path.name for path in self.root.iterdir() if "-build-" in path.name], [])

    def test_a_folder_that_is_not_a_corpus_is_refused_and_left_untouched(self):
        victim = self.root / "victim"
        (victim / "important").mkdir(parents=True)
        (victim / "important/notes.txt").write_text("uncommitted work\n", encoding="utf-8")
        # A corpus manifest is not enough: any file a corpus never holds marks someone's folder.
        mixed = self.root / "mixed"
        builder.build([bundle(GOLDEN)], mixed)
        (mixed / "notes.txt").write_text("uncommitted work\n", encoding="utf-8")
        # A folder of corpus-shaped files whose manifest is not a replay corpus manifest.
        other = self.root / "other"
        other.mkdir()
        (other / "corpus-manifest.json").write_text('{"schema": "something-else"}\n', encoding="utf-8")
        (other / "README.md").write_text("notes\n", encoding="utf-8")
        a_file = self.root / "a-file.txt"
        a_file.write_text("keep\n", encoding="utf-8")
        for out in (victim, mixed, other):
            before = self.snapshot(out)
            with self.subTest(out=out.name):
                code, errors = self.main(out)
                self.assertEqual(code, 2)
                self.assertIn("is not a replay corpus", errors)
                self.assertEqual(self.snapshot(out), before)
        self.assertEqual(self.main(a_file)[0], 2)
        self.assertEqual(a_file.read_text(encoding="utf-8"), "keep\n")
        self.assertNoStaging()

    def test_an_existing_corpus_or_empty_folder_is_replaced_and_a_new_folder_is_built(self):
        existing = self.root / "existing"
        builder.build([bundle(GOLDEN)], existing)
        case = next((existing / "cases").iterdir())
        (case / "inputs/live-state.json").write_text('{"stale": true}\n', encoding="utf-8")
        empty = self.root / "empty"
        empty.mkdir()
        for out in (existing, empty, self.root / "new" / "corpus"):
            with self.subTest(out=out.name):
                self.assertEqual(self.main(out), (0, ""))
                corpus.verify(out)
        self.assertNotIn("stale", (case / "inputs/live-state.json").read_text(encoding="utf-8"))
        self.assertNoStaging()

    def test_a_failed_build_keeps_the_previous_corpus(self):
        out = self.root / "corpus"
        builder.build([bundle(GOLDEN)], out)
        before = self.snapshot(out)
        original = builder.build

        def failing(sources, target, **_):
            original(sources, target)
            raise builder.BuildError("simulated failure after writing")

        builder.build = failing
        self.addCleanup(setattr, builder, "build", original)
        code, errors = self.main(out)
        self.assertEqual(code, 2)
        self.assertIn("simulated failure", errors)
        self.assertEqual(self.snapshot(out), before)
        self.assertNoStaging()


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
