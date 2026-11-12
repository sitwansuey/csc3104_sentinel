"""Collector: allowlist only, a code deny-list a profile cannot override, hashed MANIFEST."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from scripts.evidence import collect
from services.coordinator.core import LocalEventBus, SQLiteStore, handle
from tests.support import evidence_fixtures as fx
from tests.support.evidence_fixtures import REVISION, context, wedge_run, write

ROOT = Path(__file__).resolve().parents[1]
STARTED = "2026-11-11T10:00:00Z"


def protected_run(root):
    """A run folder with allowlisted evidence plus everything that must never be copied."""
    run = Path(root) / "boutique-live-20261111T100000Z-abc123"
    write(run / "summary.json", {"status": "PASS", "started_at": STARTED, "checks": []})
    write(run / "rbac-checks.json", [{"verb": "get", "allowed": True}])
    write(run / "approved-rollback/execution.json", {"status": "APPLIED"})
    write(run / "approved-rollback/proposal.json", {"action": "ROLLBACK"})
    write(run / "approved-rollback/deep/execution.json", {"status": "nested"})
    write(run / "extra-notes.json", {"note": "not in the profile"})
    write(run / "storefront-forward.log", "Forwarding from 127.0.0.1:8080\n")
    write(run / "coordinator.sqlite", b"SQLite format 3\x00")
    write(run / "executor.sqlite", b"SQLite format 3\x00")
    write(run / "telemetry.sqlite3", b"SQLite format 3\x00")
    write(run / "pki/client.json", {"name": "client"})
    write(run / "pki/ca.key", "not really a key")
    write(run / "pki-ca/upstream.ext", "ext")
    write(run / ".executor-kubeconfig.json", {"kind": "Config"})
    write(run / "operator-kubeconfig.yaml", "kind: Config\n")
    write(run / "locks/target.lock", "")
    write(run / "client/tls.crt", "cert")
    write(run / "outputs/summary.json", {"status": "PASS"})
    return run


class AllowlistTests(unittest.TestCase):
    def test_only_allowlisted_files_are_copied(self):
        with tempfile.TemporaryDirectory() as temp:
            run = protected_run(temp)
            evidence = Path(temp) / "evidence"
            manifest = collect.collect(run, "connected-harness", "live-a", evidence_root=evidence, context=context())
            copied = {item["path"] for item in manifest["files"]}
            self.assertEqual(copied, {"summary.json", "rbac-checks.json", "approved-rollback/execution.json",
                                      "approved-rollback/proposal.json"})
            on_disk = {p.relative_to(evidence / "2026-11-11/live-a").as_posix()
                       for p in (evidence / "2026-11-11/live-a").rglob("*") if p.is_file()}
            self.assertEqual(on_disk, copied | {"MANIFEST.json"})

    def test_deny_list_wins_over_a_catch_all_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            run = protected_run(temp)
            manifest = collect.collect(run, "everything", "live-b", evidence_root=Path(temp) / "evidence",
                                       profiles={"everything": {"include": ["**"]}}, context=context())
            copied = {item["path"] for item in manifest["files"]}
            self.assertIn("extra-notes.json", copied)
            self.assertIn("storefront-forward.log", copied)
            for path in copied:
                self.assertFalse(path.startswith(("pki", "locks", "client/", "outputs/", ".")), path)
                self.assertNotRegex(path, r"\.(sqlite3?|db|key|crt|lock)$")
                self.assertNotIn("kubeconfig", path)

    def test_denied_paths(self):
        for path in ("coordinator.sqlite", "a/b.db", "telemetry.sqlite3", "pki/x.json", "pki-ca/ca.srl",
                     ".executor-kubeconfig.json", "kubeconfig", "deep/.env", "gateway/tls.key", "x/y.pem",
                     "runs/summary.json", "locks/a.lock", "shipping.tar", "stop-ui.request"):
            self.assertTrue(collect.denied(path), path)
        for path in ("summary.json", "approved-rollback/execution.json", "data/evidence/summary.json"):
            self.assertFalse(collect.denied(path), path)

    def test_single_star_stays_in_one_folder(self):
        self.assertTrue(collect.allowed("a/execution.json", ["*/execution.json"]))
        self.assertFalse(collect.allowed("a/b/execution.json", ["*/execution.json"]))
        self.assertTrue(collect.allowed("a/b/execution.json", ["**/execution.json"]))
        self.assertTrue(collect.allowed("execution.json", ["**/execution.json"]))


class ProfileTests(unittest.TestCase):
    def test_agent_bundle_keeps_the_model_runtime_record(self):
        self.assertIn("model-runtime.json", collect.load_profiles()["agent"]["include"])
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "boutique-live-20261111T202847Z-3e370f"
            write(run / "summary.json", {"status": "PASS", "started_at": STARTED})
            write(run / "model-runtime.json", {"keep_alive": "30m", "models": {"qwen3:14b": {
                "quantization_level": "Q4_K_M", "placement": {"gpu_fraction": 1.0}}}})
            manifest = collect.collect(run, "agent", "agent-runtime", evidence_root=Path(temp) / "evidence",
                                       context=context())
            self.assertEqual({item["path"] for item in manifest["files"]}, {"summary.json", "model-runtime.json"})

    def test_executor_wedge_bundle_keeps_the_scenario_summary_and_states_its_revision(self):
        self.assertIn("executor-wedge", collect.load_profiles())
        with tempfile.TemporaryDirectory() as temp:
            run = wedge_run(temp, "executor-wedge-rv-race-20261111T202123Z")
            evidence = Path(temp) / "evidence"
            basis = "Operator record: the run used the checkout's HEAD; the scenario summary has no provenance block"
            manifest = collect.collect(run, "executor-wedge", "executor-wedge-rv-race-20261111t202123z",
                                       evidence_root=evidence, context=context(), inferred_revision=REVISION,
                                       inferred_basis=basis)
            self.assertEqual({item["path"] for item in manifest["files"]}, {"rv-race.json", "manifests.json"})
            self.assertEqual(manifest["captured_at"], "2026-11-11T20:21:23Z")
            self.assertEqual(manifest["provenance"], {"kind": "inferred", "source_revision": REVISION,
                                                      "basis": basis})
            self.assertTrue((evidence / "2026-11-11/executor-wedge-rv-race-20261111t202123z/rv-race.json").exists())

    def test_admission_deny_bundle_keeps_the_scenario_summary_and_the_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            run = wedge_run(temp, "executor-wedge-admission-deny-20261112T050853Z", scenario="admission-deny",
                            started="2026-11-12T05:08:54Z")
            manifest = collect.collect(run, "executor-wedge", "wedge-admission", evidence_root=Path(temp) / "evidence",
                                       context=context(), inferred_revision=REVISION, inferred_basis="fixture")
            self.assertEqual({item["path"] for item in manifest["files"]},
                             {"admission-deny.json", "admission-policy.json", "manifests.json"})
            self.assertEqual(manifest["captured_at"], "2026-11-12T05:08:54Z")

    def test_agent_bundle_keeps_planning_records_but_not_raw_telemetry(self):
        with tempfile.TemporaryDirectory() as temp:
            run = fx.scenario_run(temp, "boutique-live-20261112T080534Z-160519")
            manifest = collect.collect(run, "agent", "agent-scenario", evidence_root=Path(temp) / "evidence",
                                       context=context())
            self.assertEqual({item["path"] for item in manifest["files"]}, {
                "summary.json", "scenario-result.json", "approved-rollback/execution.json",
                "approved-rollback/planning-status.json", "approved-rollback/attempt-1/agent/report.json",
                "approved-rollback/attempt-1/agent/branch-003/result.json"})

    def test_wedge_scenario_summaries_give_the_capture_time(self):
        with tempfile.TemporaryDirectory() as temp:
            run = wedge_run(temp, "wedge", scenario="delete-recreate", started="2026-11-11T20:21:25Z")
            self.assertEqual(collect.captured_at(run), "2026-11-11T20:21:25Z")


class ManifestTests(unittest.TestCase):
    def test_manifest_hashes_match_file_bytes_and_index_is_appended(self):
        with tempfile.TemporaryDirectory() as temp:
            run = protected_run(temp)
            evidence = Path(temp) / "evidence"
            manifest = collect.collect(run, "connected-harness", "live-c", evidence_root=evidence, context=context(),
                                       inferred_revision=REVISION, inferred_basis="test basis")
            directory = evidence / "2026-11-11/live-c"
            for item in manifest["files"]:
                data = (directory / item["path"]).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])
                self.assertEqual(len(data), item["bytes"])
            self.assertEqual(json.loads((directory / "MANIFEST.json").read_text(encoding="utf-8")), manifest)
            self.assertEqual(manifest["source_run_dir_name"], run.name)
            self.assertEqual(manifest["provenance"], {"kind": "inferred", "source_revision": REVISION,
                                                      "basis": "test basis"})
            index = json.loads((evidence / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["bundles"], [{"bundle_id": "live-c", "class": "connected-harness",
                                                 "path": "2026-11-11/live-c", "captured_at": STARTED,
                                                 "source_revision": REVISION, "provenance": "inferred",
                                                 "status": "current", "reason": None}])

    def test_recorded_provenance_comes_from_the_run_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            write(run / "summary.json", {"status": "PASS", "started_at": STARTED,
                                         "provenance": {"source_revision": REVISION, "dirty": False}})
            evidence = Path(temp) / "evidence"
            manifest = collect.collect(run, "connected-harness", "live-d", evidence_root=evidence, context=context())
            self.assertEqual(manifest["provenance"]["kind"], "recorded")
            self.assertIs(manifest["provenance"]["dirty"], False)
            entry = json.loads((evidence / "index.json").read_text(encoding="utf-8"))["bundles"][0]
            self.assertEqual((entry["provenance"], entry["source_revision"]), ("recorded", REVISION))

    def test_existing_destination_or_bundle_id_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            run = protected_run(temp)
            evidence = Path(temp) / "evidence"
            collect.collect(run, "connected-harness", "live-e", evidence_root=evidence, context=context())
            with self.assertRaises(FileExistsError):
                collect.collect(run, "connected-harness", "live-e", evidence_root=evidence, context=context())
            (evidence / "2026-11-11/live-f").mkdir(parents=True)
            with self.assertRaises(FileExistsError):
                collect.collect(run, "connected-harness", "live-f", evidence_root=evidence, context=context())
            self.assertEqual(len(json.loads((evidence / "index.json").read_text(encoding="utf-8"))["bundles"]), 1)

    def test_invalid_bundle_ids_classes_and_dates_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            run = protected_run(temp)
            for bundle_id in ("runs", "Upper-Case", "a", "../escape"):
                with self.subTest(bundle_id=bundle_id), self.assertRaises(ValueError):
                    collect.collect(run, "connected-harness", bundle_id, evidence_root=Path(temp) / "e",
                                    context=context())
            with self.assertRaises(ValueError):
                collect.collect(run, "no-such-class", "live-g", evidence_root=Path(temp) / "e", context=context())
            undated = Path(temp) / "undated"
            write(undated / "summary.json", {"status": "PASS"})
            with self.assertRaises(ValueError):
                collect.collect(undated, "connected-harness", "live-h", evidence_root=Path(temp) / "e",
                                context=context())

    def test_capture_date_falls_back_to_a_utc_folder_timestamp(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "boutique-live-20261110T213026Z-8268a2"
            write(run / "rbac-checks.json", [])
            manifest = collect.collect(run, "agent", "live-a", evidence_root=Path(temp) / "evidence",
                                       context=context())
            self.assertEqual(manifest["captured_at"], "2026-11-10T21:30:26Z")
            self.assertTrue((Path(temp) / "evidence/2026-11-10/live-a/rbac-checks.json").exists())

    def test_local_folder_timestamp_is_never_read_as_utc(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "packaged-boutique-20261111-050418-17719f"
            write(run / "host-cleanup.json", {"exit_code": 1})
            evidence = Path(temp) / "evidence"
            with self.assertRaisesRegex(ValueError, "local time with no zone; pass --folder-utc-offset"):
                collect.collect(run, "packaged", "packaged-a", evidence_root=evidence, context=context())
            self.assertFalse(evidence.exists() and any(evidence.rglob("packaged-a")))
            self.assertIsNone(collect.captured_at(run))

    def test_local_folder_timestamp_uses_the_given_utc_offset(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "packaged-boutique-20261111-050418-17719f"
            write(run / "host-cleanup.json", {"exit_code": 1})
            evidence = Path(temp) / "evidence"
            manifest = collect.collect(run, "packaged", "packaged-a", evidence_root=evidence, context=context(),
                                       folder_utc_offset="+08:00")
            self.assertEqual(manifest["captured_at"], "2026-11-10T21:04:18Z")
            self.assertTrue((evidence / "2026-11-10/packaged-a/host-cleanup.json").exists())
            entry = json.loads((evidence / "index.json").read_text(encoding="utf-8"))["bundles"][0]
            self.assertEqual((entry["path"], entry["captured_at"]), ("2026-11-10/packaged-a", "2026-11-10T21:04:18Z"))
            west = collect.captured_at(run, collect.parse_utc_offset("-0530"))
            self.assertEqual(west, "2026-11-11T10:34:18Z")

    def test_explicit_date_without_offset_records_no_capture_time(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "packaged-boutique-20261111-050418-17719f"
            write(run / "host-cleanup.json", {"exit_code": 1})
            manifest = collect.collect(run, "packaged", "packaged-a", evidence_root=Path(temp) / "evidence",
                                       context=context(), date="2026-11-10")
            self.assertIsNone(manifest["captured_at"])

    def test_run_file_start_time_wins_over_the_folder_offset(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "packaged-boutique-20261111-050129-26a07a"
            write(run / "data/evidence/summary.json", {"started_at": "2026-11-10T21:02:08Z"})
            self.assertEqual(collect.captured_at(run, collect.parse_utc_offset("+05:00")), "2026-11-10T21:02:08Z")

    def test_bad_utc_offsets_are_refused(self):
        for value in ("8", "+8:00", "UTC+8", "+15:00", "+08:60", ""):
            with self.assertRaises(ValueError):
                collect.parse_utc_offset(value)
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "packaged-boutique-20261111-050418-17719f"
            write(run / "host-cleanup.json", {"exit_code": 1})
            with contextlib.redirect_stderr(io.StringIO()) as error:
                code = collect.main(["--run", str(run), "--class", "packaged", "--bundle-id", "packaged-a",
                                     "--evidence-root", str(Path(temp) / "evidence"), "--folder-utc-offset", "+8"])
            self.assertEqual(code, 1)
            self.assertIn("UTC offset must look like", error.getvalue())


class JournalExportTests(unittest.TestCase):
    def test_journal_is_exported_read_only_without_copying_the_database(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            write(run / "summary.json", {"status": "PASS", "started_at": STARTED})
            incident = json.loads((ROOT / "data/sentinel/samples/messages/incident.json").read_text(encoding="utf-8"))
            store = SQLiteStore(run / "coordinator.sqlite")
            try:
                handle(incident, store, LocalEventBus())
            finally:
                store.close()
            database = run / "coordinator.sqlite"
            old = time.time() - 3600
            os.utime(database, (old, old))
            before = (database.stat().st_mtime, sorted(p.name for p in run.iterdir()))
            manifest = collect.collect(run, "connected-harness", "journal-a", evidence_root=Path(temp) / "evidence",
                                       context=context(), exports=("coordinator-journal.json",))
            self.assertEqual((database.stat().st_mtime, sorted(p.name for p in run.iterdir())), before)
            self.assertIn("coordinator-journal.json", {item["path"] for item in manifest["files"]})
            journal = json.loads((Path(temp) / "evidence/2026-11-11/journal-a/coordinator-journal.json")
                                 .read_text(encoding="utf-8"))
            self.assertEqual(journal["incidents"][0]["incident_id"], incident["incident_id"])
            self.assertFalse(any(p.suffix.startswith(".sqlite") for p in (Path(temp) / "evidence").rglob("*")))


if __name__ == "__main__":
    unittest.main()
