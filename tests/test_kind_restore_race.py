"""The operator's restore of demo-api while its resourceVersion moves (P4.01).

On 14 Nov 2026 (deploy 13 at 0b5529f) S7 r4 ended FAILED, as S7 expects, so
the harness had to put the baseline template back itself. Its one JSON patch
(a test of the uid, a test of the resourceVersion it had just read, and the
template) failed with exit 1, the harness printed "Refused: kubectl patch
failed (exit 1)" and exited 2: no S7 r4 summary, the matrix stopped, and the
target stayed faulted. kubectl's stderr was not kept; the most likely cause
is that the crash-looping rollout's status updates moved the resourceVersion
between the read and the patch
(docs/distributed-acceptance.md#s7-r4-refused-at-its-restore-the-targets-resourceversion-moved).
These tests drive the real Shell and Lab.restore through a fake kubectl that
holds one Deployment and answers a patch as the API server does, and run_one
and run over a lab whose restore fails.
"""
from datetime import UTC, datetime, timedelta
import itertools
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from experiments.distributed import kind_acceptance as acceptance
from experiments.distributed import kind_acceptance_live as live
from services.executor.store import fingerprint
from tests.test_kind_acceptance import FakeLab

BASE = datetime(2026, 11, 14, 16, 0, tzinfo=UTC)
TEMPLATE = {"metadata": {"labels": {"app": "demo-api"}},
            "spec": {"containers": [{"name": "demo-api", "env": [{"name": "STARTUP_FAILURE", "value": "false"}]}]}}
FAULTED = {"metadata": {"labels": {"app": "demo-api"}},
           "spec": {"containers": [{"name": "demo-api", "env": [{"name": "STARTUP_FAILURE", "value": "true"}]}]}}
OTHER = {"metadata": {"labels": {"app": "demo-api"}},
         "spec": {"containers": [{"name": "demo-api", "image": "someone-else:latest"}]}}
BASELINE = {"target": {"uid": "uid-demo"}, "pod_template": TEMPLATE, "template_sha256": fingerprint(TEMPLATE)}
# What kubectl prints when the API server refuses a JSON patch whose test op failed, and for a lost update.
TEST_FAILED = "The request is invalid: the server rejected our request due to an error in our request"
TEST_FAILED_NEWER = ('Error from server (Invalid): testing value /metadata/resourceVersion failed: test failed')
CONFLICT = ('Error from server (Conflict): Operation cannot be fulfilled on deployments.apps "demo-api": the object '
            'has been modified; please apply your changes to the latest version and try again')
FORBIDDEN = ('Error from server (Forbidden): deployments.apps "demo-api" is forbidden: User "kind-operator" cannot '
             'patch resource "deployments" in API group "apps"')
DENIED = ('The deployments "demo-api" is invalid: : ValidatingAdmissionPolicy \'sentinel-executor-patch-only\' with '
          "binding 'b' denied request: failed expression")
NOT_FOUND = 'Error from server (NotFound): deployments.apps "demo-api" not found'
REFUSED = "Unable to connect to the server: dial tcp 127.0.0.1:40123: connect: connection refused"


def done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class Deployment:
    """A fake kubectl over one Deployment: get, a JSON patch checked as the API server checks it, rollout status.

    moves is how many times the controller updates the object's status (a new
    resourceVersion) between the harness's read and its patch; on_read, when
    set, changes the object after each read (another writer). refuse, when
    set, is the stderr every patch is refused with (exit 1); flaky is a list
    of answers given to the first patches instead of applying them.
    """

    def __init__(self, *, template=FAULTED, uid="uid-demo", moves=0, on_read=None, refuse=None, flaky=()):
        self.uid, self.rv, self.template = uid, 100, template
        self.moves, self.on_read, self.refuse, self.flaky = moves, on_read, refuse, list(flaky)
        self.reads, self.patches, self.rollouts, self.calls = 0, [], 0, []

    def document(self):
        return {"metadata": {"name": "demo-api", "uid": self.uid, "resourceVersion": str(self.rv), "generation": 29},
                "spec": {"template": self.template}}

    def __call__(self, command, **options):
        self.calls.append(list(command))
        if "get" in command and "deployment" in command:
            self.reads += 1
            answer = done(json.dumps(self.document()))
            if self.on_read:
                self.on_read(self, self.reads)
            return answer
        if "patch" in command:
            [argument] = [part for part in command if part.startswith("--patch-file=")]
            document = json.loads(Path(argument.split("=", 1)[1]).read_text(encoding="utf-8"))
            self.patches.append(document)
            if self.flaky:
                return self.flaky.pop(0)
            if self.refuse:
                return done(code=1, stderr=self.refuse)
            if self.moves:  # The controller wrote the status after the harness read it.
                self.moves -= 1
                self.rv += 1
            for op in document:
                if op["op"] == "test":
                    actual = self.uid if op["path"] == "/metadata/uid" else str(self.rv)
                    if actual != op["value"]:
                        return done(code=1, stderr=TEST_FAILED)
            self.template = next(op["value"] for op in document if op["op"] == "replace")
            self.rv += 1
            return done("deployment.apps/demo-api patched\n")
        if "rollout" in command:
            self.rollouts += 1
            return done()
        raise AssertionError("unexpected command: " + " ".join(command))


def virtual_lab(runner, directory):
    """A real Lab over runner on virtual time: lab.sleeps holds every pause it took."""
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    lab = acceptance.Lab(acceptance.Shell("kind-sentinel-dist", runner=runner), directory,
                         clock=lambda: BASE + timedelta(seconds=now[0]), sleep=sleep, monotonic=lambda: now[0])
    lab.sleeps = sleeps
    return lab


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def assert_guarded(self, patches, read_versions):
        """Every patch tests the uid and the resourceVersion read just before it, then replaces the template."""
        self.assertEqual(len(patches), len(read_versions))
        for document, version in zip(patches, read_versions, strict=True):
            self.assertEqual(document, [
                {"op": "test", "path": "/metadata/uid", "value": "uid-demo"},
                {"op": "test", "path": "/metadata/resourceVersion", "value": version},
                {"op": "replace", "path": "/spec/template", "value": TEMPLATE}])

    def test_a_resource_version_that_moves_once_is_read_again_and_patched_under_both_tests(self):
        for stderr in (TEST_FAILED, TEST_FAILED_NEWER, CONFLICT):
            with self.subTest(stderr=stderr[:40]):
                deployment = Deployment(flaky=[done(code=1, stderr=stderr)])
                lab = virtual_lab(deployment, self.directory)
                deployment.on_read = lambda world, reads: setattr(world, "rv", world.rv + 1) if reads == 1 else None
                result = lab.restore(BASELINE)
                self.assertTrue(result["operator_restore"])
                self.assertEqual(deployment.template, TEMPLATE)
                self.assert_guarded(deployment.patches, ["100", "101"])
                self.assertEqual([item["outcome"] for item in result["attempts"]],
                                 ["test failed" if stderr != CONFLICT else "conflict", "applied"])
                self.assertEqual(deployment.rollouts, 1)
                self.assertEqual(list(self.directory.iterdir()), [])  # Every patch file removed.

    def test_the_status_moving_between_read_and_patch_is_ridden_out_by_the_api_servers_own_test(self):
        deployment = Deployment(moves=2)
        lab = virtual_lab(deployment, self.directory)
        result = lab.restore(BASELINE)
        self.assertTrue(result["operator_restore"])
        self.assert_guarded(deployment.patches, ["100", "101", "102"])
        self.assertEqual([item["outcome"] for item in result["attempts"]], ["test failed", "test failed", "applied"])
        self.assertTrue(all(0 < pause <= acceptance.RESTORE_PAUSE_SECONDS for pause in lab.sleeps))

    def test_a_resource_version_that_keeps_moving_stops_at_the_bound_with_a_clear_error(self):
        deployment = Deployment(moves=1000)
        lab = virtual_lab(deployment, self.directory)
        with self.assertRaises(acceptance.AcceptanceError) as caught:
            lab.restore(BASELINE)
        message = str(caught.exception)
        self.assertIn("not restored", message)
        self.assertIn("test failed", message)
        self.assertIn("restore it by hand", message)
        self.assertEqual(len(deployment.patches), acceptance.RESTORE_ATTEMPTS)
        self.assert_guarded(deployment.patches, [str(100 + index) for index in range(acceptance.RESTORE_ATTEMPTS)])
        self.assertLessEqual(sum(lab.sleeps), acceptance.RESTORE_RETRY_SECONDS)
        self.assertEqual(deployment.template, FAULTED)
        self.assertEqual(deployment.rollouts, 0)

    def test_a_target_with_another_uid_is_refused_and_never_overwritten(self):
        # Recreated between the harness's read and its patch: the API server's uid test refuses the patch.
        def recreate(world, reads):
            if reads == 1:
                world.uid = "uid-recreated"
        deployment = Deployment(on_read=recreate)
        lab = virtual_lab(deployment, self.directory)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "uid"):
            lab.restore(BASELINE)
        self.assertEqual(len(deployment.patches), 1)  # The refused one; no patch with the new uid.
        self.assertEqual(deployment.template, FAULTED)
        # Already another Deployment when the restore first reads it: nothing is patched at all.
        deployment = Deployment(uid="uid-recreated")
        with self.assertRaisesRegex(acceptance.AcceptanceError, "uid"):
            virtual_lab(deployment, self.directory).restore(BASELINE)
        self.assertEqual(deployment.patches, [])

    def test_a_template_someone_else_wrote_is_refused_and_never_overwritten(self):
        def write(world, reads):
            if reads == 1:
                world.template, world.rv = OTHER, world.rv + 1
        deployment = Deployment(on_read=write)
        lab = virtual_lab(deployment, self.directory)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "neither the baseline nor"):
            lab.restore(BASELINE)
        self.assertEqual(len(deployment.patches), 1)
        self.assertEqual(deployment.template, OTHER)

    def test_a_target_another_writer_already_restored_is_left_as_it_is(self):
        def restored(world, reads):
            if reads == 1:
                world.template, world.rv = TEMPLATE, world.rv + 1
        deployment = Deployment(on_read=restored)
        result = virtual_lab(deployment, self.directory).restore(BASELINE)
        self.assertIs(result["operator_restore"], False)
        self.assertIn("already at its baseline", result["note"])
        self.assertEqual(len(deployment.patches), 1)
        self.assertEqual(deployment.rollouts, 0)
        # At its baseline from the start: no patch, the result as before.
        deployment = Deployment(template=TEMPLATE)
        self.assertEqual(virtual_lab(deployment, self.directory).restore(BASELINE), {"operator_restore": False})
        self.assertEqual(deployment.patches, [])

    def test_any_other_refusal_is_raised_after_one_patch_with_its_class(self):
        for stderr, reason in ((FORBIDDEN, "forbidden"), (DENIED, "admission denied"), (NOT_FOUND, "not found")):
            with self.subTest(reason=reason):
                deployment = Deployment(refuse=stderr)
                lab = virtual_lab(deployment, self.directory)
                with self.assertRaises(acceptance.AcceptanceError) as caught:
                    lab.restore(BASELINE)
                self.assertEqual(str(caught.exception), f"kubectl patch failed (exit 1): {reason}")
                self.assertEqual(len(deployment.patches), 1)
                self.assertEqual(lab.sleeps, [])

    def test_a_transient_failure_is_asked_again_within_the_same_bound(self):
        deployment = Deployment(flaky=[done(code=1, stderr=REFUSED)])
        lab = virtual_lab(deployment, self.directory)
        result = lab.restore(BASELINE)
        self.assertTrue(result["operator_restore"])
        self.assertEqual([item["outcome"] for item in result["attempts"]],
                         ["transient: connection refused", "applied"])
        self.assert_guarded(deployment.patches, ["100", "100"])  # Not applied: the version had not moved.
        # A patch whose answer was lost and that did apply: the target is at its baseline, the restore in doubt.
        deployment = Deployment()
        lost = deployment.__call__

        def apply_then_lose(command, **options):
            answer = lost(command, **options)
            return done(code=1, stderr=REFUSED) if "patch" in command else answer
        lab = virtual_lab(apply_then_lose, self.directory)
        result = lab.restore(BASELINE)
        self.assertIsNone(result["operator_restore"])
        self.assertIn("may have applied", result["note"])
        self.assertEqual(len(deployment.patches), 1)
        # A read that stays down is a harness error at the bound, never a restore.
        deployment = Deployment()

        def unreachable(command, **options):
            return done(code=1, stderr=REFUSED) if "get" in command else deployment(command, **options)
        lab = virtual_lab(unreachable, self.directory)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "not restored"):
            lab.restore(BASELINE)
        self.assertEqual(deployment.patches, [])

    def test_patch_and_apply_failures_name_a_stderr_class_and_never_an_argument_or_the_patch(self):
        cases = ((TEST_FAILED, "test failed"), (TEST_FAILED_NEWER, "test failed"), (CONFLICT, "conflict"),
                 (NOT_FOUND, "not found"), (FORBIDDEN, "forbidden"), (DENIED, "admission denied"),
                 ("error: something new", "stderr not classified"))
        for stderr, reason in cases:
            for verb in ("patch", "apply"):
                with self.subTest(verb=verb, reason=reason):
                    shell = acceptance.Shell("kind-sentinel-dist",
                                             runner=lambda command, stderr=stderr, **options: done(code=1,
                                                                                                    stderr=stderr))
                    with self.assertRaises(acceptance.AcceptanceError) as caught:
                        shell.kubectl(verb, "deployment", "demo-api", "--type=json",
                                      "--patch-file=/private/restore-patch-x.json", namespace=acceptance.WORKLOAD)
                    text = str(caught.exception)
                    self.assertEqual(text, f"kubectl {verb} failed (exit 1): {reason}")
                    self.assertEqual(getattr(caught.exception, "reason", None), reason)
                    for leaked in ("demo-api", "restore-patch", "--context", "resourceVersion", "kind-operator"):
                        self.assertNotIn(leaked, text)
        # A read keeps its message as before: no class, no argument.
        shell = acceptance.Shell("kind-sentinel-dist", runner=lambda command, **options: done(code=1,
                                                                                               stderr=NOT_FOUND))
        with self.assertRaises(acceptance.AcceptanceError) as caught:
            shell.kubectl("get", "deployment", "demo-api", namespace=acceptance.WORKLOAD)
        self.assertEqual(str(caught.exception), "kubectl get failed (exit 1)")


class FailingRestoreLab(FakeLab):
    """FakeLab whose restore is the real Lab.restore over a fake Deployment, or raises error; and run()'s reads."""

    def __init__(self, rows, *, deployment=None, error=None, settle_error=None):
        super().__init__(rows, generations=itertools.repeat(4))
        self.deployment, self.error, self.settle_error = deployment, error, settle_error
        self.closed = False

    def restore(self, baseline):
        self.steps.append("restore")
        if self.error is not None:
            raise self.error
        directory = Path(tempfile.mkdtemp())
        try:
            return virtual_lab(self.deployment, directory).restore(baseline)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def settle(self, baseline, *, leftovers=None):
        if self.settle_error is not None:
            self.steps.append("settle")
            raise self.settle_error
        return super().settle(baseline, leftovers=leftovers)

    def labelled_chaos(self):
        return []

    def close(self):
        self.closed = True


class RunOneRestoreTests(unittest.TestCase):
    def setUp(self):
        from experiments.distributed import audit
        events, _ = audit.parse((Path(__file__).resolve().parent / "fixtures" / "distributed_audit" /
                                 "one-write.jsonl").read_text(encoding="utf-8"))
        self.rows = audit.extract(events)
        self.deployed = {"policy_version": "v1", "versions": {"node_image": acceptance.NODE_IMAGE},
                         "api_server": {"endpoint": "172.18.0.4"}, "deployed_at": "2026-11-14T15:00:00Z",
                         "placement": {}}
        self.tokens = {"executor-a": {"delivery-token": "q" * 43}, acceptance.OPERATOR: {"approval-token": "o" * 43}}
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        for patcher in (patch.object(live, "git_revision", lambda: "abc123"),
                        patch.object(live, "_operator", lambda lab, version, stopping, record: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_one(self, lab):
        summary = live.run_one(lab, acceptance.SCENARIOS["S0"], 1, self.deployed, BASELINE, self.tokens,
                               self.directory)
        [folder] = list((self.directory / "2026-11-13").iterdir())
        written = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(written, json.loads(json.dumps(summary)))
        return written

    def test_a_restore_that_keeps_losing_the_race_is_a_harness_error_in_the_written_summary(self):
        lab = FailingRestoreLab(self.rows, deployment=Deployment(moves=1000))
        summary = self.run_one(lab)
        self.assertFalse(summary["passed"])
        self.assertIn("restore: AcceptanceError: ", summary["harness_error"])
        self.assertIn("not restored", summary["harness_error"])
        self.assertIn("not restored", summary["restore_error"])
        self.assertIsNone(summary["settle"])  # Not settled on a faulted target: the restore failed first.
        self.assertNotIn("settle", lab.steps)
        self.assertIn("copy", lab.steps)  # The evidence is still collected and written.

    def test_a_restore_that_raises_writes_a_failed_summary_and_keeps_the_cleanup(self):
        cleaned = []
        lab = FailingRestoreLab(self.rows, error=acceptance.AcceptanceError("kubectl patch failed (exit 1): forbidden"))
        original = live.Run.cleanup

        def cleanup(run):
            cleaned.append(True)
            return original(run)
        with patch.object(live.Run, "cleanup", cleanup):
            summary = self.run_one(lab)
        self.assertEqual(cleaned, [True])
        self.assertFalse(summary["passed"])
        self.assertEqual(summary["restore_error"], "restore: AcceptanceError: kubectl patch failed (exit 1): forbidden")
        self.assertIn("restore: AcceptanceError: kubectl patch failed (exit 1): forbidden", summary["harness_error"])

    def test_a_settle_that_raises_is_a_harness_error_too(self):
        lab = FailingRestoreLab(self.rows, deployment=Deployment(),
                                settle_error=acceptance.AcceptanceError("leftovers could not be read"))
        summary = self.run_one(lab)
        self.assertFalse(summary["passed"])
        self.assertTrue(summary["operator_actions"][-1]["operator_restore"])
        self.assertEqual(summary["restore_error"], "settle: AcceptanceError: leftovers could not be read")
        self.assertIn("settle: AcceptanceError: leftovers could not be read", summary["harness_error"])

    def test_a_restore_that_succeeds_after_a_retry_records_its_attempts_and_no_error(self):
        lab = FailingRestoreLab(self.rows, deployment=Deployment(moves=1))
        summary = self.run_one(lab)
        self.assertIsNone(summary["restore_error"])
        self.assertIsNone(summary["harness_error"])
        restored = summary["operator_actions"][-1]
        self.assertEqual([item["outcome"] for item in restored["attempts"]], ["test failed", "applied"])

    def test_the_matrix_stops_after_a_failed_restore_and_says_the_lab_needs_a_manual_restore(self):
        run_dir = self.directory
        (run_dir / "lab.json").write_text(json.dumps(self.deployed), encoding="utf-8")
        (run_dir / "inputs").mkdir()
        (run_dir / "inputs" / "baseline.json").write_text(json.dumps(BASELINE), encoding="utf-8")
        lab = FailingRestoreLab(self.rows, deployment=Deployment(moves=1000))
        lab.target = lambda: {"metadata": {"uid": "uid-demo", "generation": 4}}
        with patch.object(live, "preflight", lambda *args, **options: {}), \
                patch.object(live, "read_tokens", lambda shell: self.tokens), \
                self.assertRaises(acceptance.AcceptanceError) as caught:
            live.run(acceptance.Shell("kind-sentinel-dist", runner=lambda *args, **options: done()), run_dir,
                     ["S0", "S1a"], repeat=2, min_free_gib=1, lock=None, lab=lab)
        message = str(caught.exception)
        self.assertIn("S0 r1", message)
        self.assertIn("restore", message)
        self.assertIn("by hand", message)
        self.assertIn("teardown", message)
        self.assertEqual(lab.steps.count("restore"), 1)  # No second run started.
        self.assertTrue(lab.closed)
        [folder] = list((run_dir / "2026-11-13").iterdir())
        written = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
        self.assertFalse(written["passed"])
        overview = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual((overview["runs"], overview["passed"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
