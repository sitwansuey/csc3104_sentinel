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
    def test_phase5_profiles_keep_acceptance_proofs_and_exclude_private_inputs(self):
        proofs = {
            "scale-recovery": ["scenario-result.json", "scale-recovery/transport-stats.json", "forced-scale/proof.json"],
            "cve-gate": ["inventory.json", "admission-deny.json", "guard-regression.json", "zero-write.json",
                         "scans/digest/summary.json"],
            "security-lab": ["inspector-burst.json", "inspection-comparison.json", "inspector-outage.json"],
            "protected-recovery": ["protected-rbac-checks.json", "bypass-during.json",
                                   "approved-rollback/transport-stats.json"],
            "cert-lifecycle": ["rotation.json", "identity-expiring.json", "rollover-finish.json"],
        }
        with tempfile.TemporaryDirectory() as temp:
            profiles = collect.load_profiles()
            for name, paths in proofs.items():
                with self.subTest(profile=name):
                    run = Path(temp) / name
                    for path in paths:
                        write(run / path, {"passed": True})
                    for path in ("pki/ca.key", "executor.kubeconfig", "reader-credentials.json", "baseline.json",
                                 "certificates.json", "worker.json", "security.db", "scans/digest/raw.json"):
                        write(run / path, {"private": True})
                    self.assertEqual(collect.select_files(run, profiles[name]["include"]), sorted(paths))

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

    def test_scenario_pair_bundle_keeps_the_pair_and_lane_records_and_nothing_private(self):
        with tempfile.TemporaryDirectory() as temp:
            run = fx.pair_run(temp, "boutique-pair-20261113T095052Z-613964")
            manifest = collect.collect(run, "scenario-pair", "boutique-pair-20261113t095052z-613964",
                                       evidence_root=Path(temp) / "evidence", context=context())
            kept = {item["path"] for item in manifest["files"]}
            self.assertEqual(kept, {
                "summary.json", "pair-result.json", "pair-storefront-impact.json", "rbac-checks.json", "images.json",
                "policies/cartservice.json", "policies/redis-cart.json", "attribution.json", "chaos-experiment.json",
                "dependency-readiness-cartservice.json", "dependency-readiness-redis-cart.json",
                "cartservice-pair-2-incident.json", "redis-cart-pair-2-incident.json",
                "pair-redis-cart/proposal-ACT-R.json", "pair-redis-cart/dependencies-ACT-R.json",
                "pair-redis-cart/guard-ACT-R-0.json", "pair-redis-cart/dependencies.json",
                "pair-redis-cart/execution-ACT-R.json", "pair-redis-cart/outcome-ACT-R.json",
                "pair-redis-cart/outcome.json", "pair-redis-cart/scenario-result.json",
                "pair-cartservice/proposal-ACT-C.json", "pair-cartservice/guard-ACT-C-0.json",
                "pair-cartservice/replan-1.json", "pair-cartservice/proposal.json",
                "pair-cartservice/reconciliation.json", "pair-cartservice/scenario-result.json"})
            for path in kept:
                with self.subTest(path=path):
                    self.assertNotRegex(path.lower(), r"kubeconfig|credential|token|secret|sqlite|\.db$|lock|worker")
            # The worker configuration names the private credential file; baselines are not kept by any class.
            self.assertFalse(any(path.startswith("baselines/") for path in kept))
            self.assertEqual(manifest["captured_at"], "2026-11-13T09:50:52Z")
            self.assertEqual(manifest["provenance"]["kind"], "recorded")

    def test_scenario_pair_bundle_of_a_run_that_stopped_before_judging(self):
        with tempfile.TemporaryDirectory() as temp:
            run = fx.pair_run(temp, "boutique-pair-20261113T093435Z-2f30b6", status="FAILED", judged=False)
            manifest = collect.collect(run, "scenario-pair", "pair-stopped", evidence_root=Path(temp) / "evidence",
                                       context=context())
            self.assertEqual({item["path"] for item in manifest["files"]}, {
                "summary.json", "rbac-checks.json", "images.json", "policies/cartservice.json",
                "policies/redis-cart.json", "chaos-experiment.json", "cartservice-pair-2-incident.json",
                "redis-cart-pair-2-incident.json"})

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


class Phase3CollectTests(unittest.TestCase):
    """Distributed scenario folders, operator attachments and orchestrator event files."""

    def test_distributed_bundle_keeps_the_run_records_but_not_the_store(self):
        with tempfile.TemporaryDirectory() as temp:
            run = fx.distributed_run(temp, "2026-11-12-S1b")
            manifest = collect.collect(run, "distributed-scenario", "dist-a", evidence_root=Path(temp) / "evidence",
                                       context=context(), inferred_revision=REVISION, inferred_basis="fixture")
            self.assertEqual({item["path"] for item in manifest["files"]},
                             {"summary.json", "timeline.jsonl", "coordinator-records.json"})
            self.assertEqual(manifest["captured_at"], "2026-11-12T17:55:29.797541Z")
            self.assertNotIn("attached", manifest)

    def test_attachment_is_sanitised_allowlisted_and_named_in_the_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            run = fx.distributed_run(temp, "2026-11-12-S8")
            wrapper = write(Path(temp) / "S8-wrapper.json", {"stdout": "C:\\Users\\tester\\run", "token": "abc"})
            manifest = collect.collect(run, "distributed-scenario", "dist-b", evidence_root=Path(temp) / "evidence",
                                       context=context(), inferred_revision=REVISION, inferred_basis="fixture",
                                       attachments=((wrapper, "kind-wrapper.json"),))
            self.assertEqual(manifest["attached"], [{"path": "kind-wrapper.json", "source_name": "S8-wrapper.json"}])
            kept = (Path(temp) / "evidence/2026-11-12/dist-b/kind-wrapper.json").read_text(encoding="utf-8")
            self.assertNotIn("tester", kept)
            self.assertNotIn('"abc"', kept)
            self.assertIn("<redacted:", kept)

    def test_attachments_outside_the_allowlist_or_colliding_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            run = fx.distributed_run(temp, "2026-11-12-S2")
            extra = write(Path(temp) / "extra.json", {"a": 1})
            kubeconfig = write(Path(temp) / "executor-kubeconfig.json", {"kind": "Config"})
            patterns = collect.load_profiles()["distributed-scenario"]["include"]
            for source, published in ((extra, "notes.json"), (extra, "../kind-wrapper.json"),
                                      (extra, "sub\\kind-wrapper.json"), (extra, "summary.json"),
                                      (Path(temp) / "missing.json", "kind-wrapper.json"),
                                      (kubeconfig, "kind-wrapper.json")):
                with self.subTest(published=published, source=source.name), self.assertRaises(ValueError):
                    collect.attachment_sources(((source, published),), patterns, run)
            with self.assertRaises(ValueError):
                collect.attachment_sources(((extra, "kind-wrapper.json"), (extra, "kind-wrapper.json")), patterns, run)
            self.assertFalse((Path(temp) / "evidence").exists())

    def test_attach_argument_splits_at_the_last_equals_sign(self):
        self.assertEqual(collect.parse_attachment("C:/a=b/S8-wrapper.json=kind-wrapper.json"),
                         (Path("C:/a=b/S8-wrapper.json"), "kind-wrapper.json"))
        for value in ("kind-wrapper.json", "=kind-wrapper.json", "source.json="):
            with self.subTest(value=value), self.assertRaises(ValueError):
                collect.parse_attachment(value)

    def test_orchestrator_events_give_the_capture_time(self):
        with tempfile.TemporaryDirectory() as temp:
            run = fx.container_run(temp, "task2")
            self.assertEqual(collect.captured_at(run), "2026-11-12T18:10:36Z")
            manifest = collect.collect(run, "worker-container", "container-a", evidence_root=Path(temp) / "evidence",
                                       context=context(), inferred_revision=REVISION, inferred_basis="fixture")
            self.assertEqual({item["path"] for item in manifest["files"]},
                             {"events.jsonl", "health.jsonl", "planner-container.state.json",
                              "planner-container.log"})

    def test_admission_summary_gives_the_capture_time(self):
        with tempfile.TemporaryDirectory() as temp:
            run = fx.admission_run(temp, "executor-admission-20261112T135924Z")
            write(run / "executor.kubeconfig", "kind: Config\n")
            manifest = collect.collect(run, "executor-admission", "admission-a", evidence_root=Path(temp) / "evidence",
                                       context=context(), inferred_revision=REVISION, inferred_basis="fixture")
            self.assertEqual(manifest["captured_at"], "2026-11-12T13:59:24Z")
            self.assertEqual([item["path"] for item in manifest["files"]], ["admission.json"])


if __name__ == "__main__":
    unittest.main()
