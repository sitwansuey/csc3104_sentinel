"""The shared heavy-lab lock (experiments/lab_lock.py) and the three live harnesses that take it: the P4.01
acceptance harness, the P4.03 Chaos Mesh install and chaos scenario runs, and the P4.05 fault pairs.

Offline: every cluster step is replaced, and the lock lives in a temporary directory, never the system one."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from experiments import lab_lock
from experiments.lab_lock import HeavyLabLock, LabLockError

DEAD_PID = 2 ** 30  # Recorded by a "killed" run; the tests' alive() says it is not running.
REAL_PID_ALIVE = lab_lock.pid_alive


def alive_except_dead(pid):
    return pid != DEAD_PID


def other_holder(path, *, pid=None, holder="another heavy run"):
    """A lock taken by another run: pid DEAD_PID makes it stale."""
    lock = HeavyLabLock(path, holder, command="other --run", pid=pid or os.getpid(), alive=alive_except_dead)
    return lock.acquire()


class LockTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.directory / lab_lock.LOCK_NAME
        # Every harness's default lock goes here, and a recorded DEAD_PID counts as not running.
        self.enterContext(patch.object(lab_lock, "default_path", return_value=self.path))
        self.enterContext(patch.object(lab_lock, "pid_alive", side_effect=alive_except_dead))

    def contents(self):
        return json.loads(self.path.read_text(encoding="utf-8"))


class SharedLockTests(LockTestCase):
    def test_the_lock_records_its_holder_and_is_removed_when_the_run_ends(self):
        with HeavyLabLock(holder="P4.05 pair x", command="harness --pair x") as lock:
            info = self.contents()
            self.assertEqual((info["holder"], info["pid"], info["command"]), ("P4.05 pair x", os.getpid(),
                                                                              "harness --pair x"))
            self.assertTrue(info["started_at"].endswith("Z"))
            self.assertIsNone(lock.check())  # Its own lock never refuses its own preflight.
        self.assertFalse(self.path.exists())

    def test_the_lock_is_released_when_the_run_raises(self):
        with self.assertRaises(RuntimeError), HeavyLabLock():
            raise RuntimeError("the run failed")
        self.assertFalse(self.path.exists())
        self.assertEqual(lab_lock.run_holding(HeavyLabLock(), lambda: 7), 7)
        with self.assertRaises(KeyboardInterrupt):
            lab_lock.run_holding(HeavyLabLock(), Mock(side_effect=KeyboardInterrupt))
        self.assertFalse(self.path.exists())

    def test_a_second_run_is_refused_while_the_lock_is_held(self):
        held = other_holder(self.path, holder="P4.01 kind_acceptance")
        before = self.path.read_text(encoding="utf-8")
        for options in ({}, {"break_stale": True}):  # A live holder is never taken over.
            with self.subTest(**options), self.assertRaises(LabLockError) as refused:
                HeavyLabLock(**options).acquire()
            self.assertIn("P4.01 kind_acceptance, pid " + str(os.getpid()), str(refused.exception))
            self.assertIn("command: other --run", str(refused.exception))
            self.assertIn("one at a time", str(refused.exception))
        with self.assertRaises(LabLockError):
            HeavyLabLock().check()
        body, stderr = Mock(), io.StringIO()
        self.assertEqual(lab_lock.run_holding(HeavyLabLock(), body, stream=stderr), 2)
        body.assert_not_called()
        self.assertIn("Refused: Another heavy lab run holds", stderr.getvalue())
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        held.release()
        self.assertFalse(self.path.exists())

    def test_a_stale_lock_is_reported_not_stolen_unless_asked(self):
        other_holder(self.path, pid=DEAD_PID, holder="P4.03 chaos scenario N2")
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(LabLockError) as refused:
            HeavyLabLock().acquire()
        message = str(refused.exception)
        self.assertIn("stale heavy-lab lock", message)
        self.assertIn(f"P4.03 chaos scenario N2, pid {DEAD_PID}", message)
        self.assertIn("no longer running", message)
        self.assertIn("--break-stale-lock", message)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)  # Left exactly as it was.
        with self.assertRaisesRegex(LabLockError, "stale"):
            HeavyLabLock().check()
        lock = HeavyLabLock(holder="mine", break_stale=True).acquire()
        self.assertEqual(lock.broken["pid"], DEAD_PID)
        self.assertEqual((self.contents()["holder"], self.contents()["pid"]), ("mine", os.getpid()))
        stderr = io.StringIO()
        lock.release()
        other_holder(self.path, pid=DEAD_PID)
        self.assertEqual(lab_lock.run_holding(HeavyLabLock(break_stale=True), lambda: 0, stream=stderr), 0)
        self.assertIn("Removed a stale heavy-lab lock", stderr.getvalue())
        self.assertEqual(list(self.directory.iterdir()), [])  # Nothing moved aside is left behind.

    def test_an_unreadable_lock_is_held_even_with_break_stale(self):
        self.path.write_text("", encoding="utf-8")  # A run killed between creating and writing the file.
        with self.assertRaisesRegex(LabLockError, "an unreadable lock file"):
            HeavyLabLock(break_stale=True).acquire()
        self.assertTrue(self.path.exists())

    def test_breaking_a_stale_lock_puts_back_a_fresh_lock_taken_meanwhile(self):
        fresh = other_holder(self.path, holder="a run that just started")
        lock = HeavyLabLock(break_stale=True)
        lock._break({"holder": "the stale one", "pid": DEAD_PID})  # What it read before the other run took it.
        self.assertEqual(self.contents()["token"], fresh.token)
        self.assertIsNone(lock.broken)
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_release_never_removes_a_lock_that_is_not_its_own(self):
        lock = HeavyLabLock().acquire()
        self.path.unlink()
        other = other_holder(self.path)
        lock.release()
        self.assertEqual(self.contents()["token"], other.token)

    def test_pid_alive_reads_the_process_table_without_signalling_it(self):
        self.assertTrue(REAL_PID_ALIVE(os.getpid()))
        running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(REAL_PID_ALIVE(running.pid))
            self.assertIsNone(running.poll())  # Asking did not stop it (os.kill would, on Windows).
        finally:
            running.kill()
            running.wait()
        self.assertFalse(REAL_PID_ALIVE(running.pid))
        for pid in (0, -1, None, True, "12"):
            self.assertFalse(REAL_PID_ALIVE(pid))


class KindAcceptanceLockTests(LockTestCase):
    """P4.01: deploy and run hold the lock; preflight and plan only read."""

    def setUp(self):
        super().setUp()
        from experiments.distributed import kind_acceptance, kind_acceptance_live
        self.acceptance, self.live = kind_acceptance, kind_acceptance_live
        self.run_dir = self.directory / "run"

    def main(self, *argv):
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            code = self.acceptance.main(list(argv))
        return code, stderr.getvalue()

    def deploying(self, result=None, error=None):
        def deploy(shell, run_dir, *, min_free_gib, lock):
            self.assertTrue(lock.held)
            self.assertEqual(self.contents()["holder"], "P4.01 kind_acceptance")
            if error is not None:
                raise error
            return result or {"deployed": True}
        return patch.object(self.live, "deploy", side_effect=deploy)

    def test_deploy_holds_the_lock_and_releases_it_on_success_and_failure(self):
        args = ("deploy", "--context", "kind-sentinel-dist", "--run-dir", str(self.run_dir))
        with self.deploying() as deploy:
            self.assertEqual(self.main(*args)[0], 0)
        deploy.assert_called_once()
        self.assertFalse(self.path.exists())
        with self.deploying(error=self.acceptance.AcceptanceError("preflight refused")):
            self.assertEqual(self.main(*args)[0], 2)
        self.assertFalse(self.path.exists())
        with self.deploying(error=RuntimeError("kubectl vanished")), self.assertRaises(RuntimeError):
            self.main(*args)
        self.assertFalse(self.path.exists())

    def test_run_holds_the_lock_and_releases_it_when_a_scenario_raises(self):
        def run(shell, run_dir, names, *, repeat, min_free_gib, lock):
            self.assertTrue(lock.held)
            raise KeyboardInterrupt
        with patch.object(self.live, "run", side_effect=run), self.assertRaises(KeyboardInterrupt):
            self.main("run", "--context", "kind-sentinel-dist", "--run-dir", str(self.run_dir), "--scenarios", "S0")
        self.assertFalse(self.path.exists())

    def test_deploy_and_run_are_refused_while_another_run_holds_the_lock(self):
        other_holder(self.path, holder="P4.05 pair redis-pod+cart-config")
        with patch.object(self.live, "deploy") as deploy, patch.object(self.live, "run") as run:
            for command in ("deploy", "run"):
                code, stderr = self.main(command, "--context", "kind-sentinel-dist", "--run-dir", str(self.run_dir))
                self.assertEqual(code, 2)
                self.assertIn("P4.05 pair redis-pod+cart-config", stderr)
        deploy.assert_not_called()
        run.assert_not_called()

    def test_a_stale_lock_is_refused_until_break_stale_lock_is_given(self):
        other_holder(self.path, pid=DEAD_PID)
        args = ("deploy", "--context", "kind-sentinel-dist", "--run-dir", str(self.run_dir))
        with self.deploying() as deploy:
            code, stderr = self.main(*args)
            self.assertEqual(code, 2)
            self.assertIn("--break-stale-lock", stderr)
            deploy.assert_not_called()
            self.assertEqual(self.main(*args, "--break-stale-lock")[0], 0)
        deploy.assert_called_once()
        self.assertFalse(self.path.exists())

    def test_plan_takes_no_lock(self):
        other_holder(self.path)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.acceptance.main(["plan"]), 0)


class ChaosInstallLockTests(LockTestCase):
    """P4.03 setup: install holds the lock; preflight only reads and takes none."""

    def setUp(self):
        super().setUp()
        from experiments.boutique.harness import chaos, chaos_recovery
        self.chaos, self.recovery = chaos, chaos_recovery
        self.digest = "sha256:" + "a" * 64
        self.installed = []

        def install(cluster, helm):
            self.installed.append(self.contents()["holder"])
        self.enterContext(patch.object(chaos, "ensure_chaos_mesh", side_effect=install))
        self.enterContext(patch.object(chaos_recovery, "ensure_metrics_server"))

    def main(self, *argv):
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            code = self.recovery.main(list(argv))
        return code, stderr.getvalue()

    def test_install_holds_the_lock_and_releases_it_on_success_and_failure(self):
        self.assertEqual(self.main("install", "--metrics-server-digest", self.digest)[0], 0)
        self.assertEqual(self.installed, ["P4.03 chaos_recovery install"])
        self.assertFalse(self.path.exists())
        with patch.object(self.recovery, "ensure_metrics_server", side_effect=RuntimeError("helm failed")), \
                self.assertRaises(RuntimeError):
            self.main("install", "--metrics-server-digest", self.digest)
        self.assertFalse(self.path.exists())

    def test_install_is_refused_while_held_and_a_stale_lock_needs_break_stale_lock(self):
        held = other_holder(self.path, holder="P4.01 kind_acceptance")
        code, stderr = self.main("install", "--metrics-server-digest", self.digest)
        self.assertEqual((code, self.installed), (2, []))
        self.assertIn("P4.01 kind_acceptance", stderr)
        held.release()
        other_holder(self.path, pid=DEAD_PID)
        code, stderr = self.main("install", "--metrics-server-digest", self.digest)
        self.assertEqual((code, self.installed), (2, []))
        self.assertIn("stale", stderr)
        self.assertEqual(self.main("install", "--metrics-server-digest", self.digest, "--break-stale-lock")[0], 0)
        self.assertEqual(len(self.installed), 1)
        self.assertFalse(self.path.exists())

    def test_preflight_reads_without_the_lock_and_refuses_break_stale_lock(self):
        other_holder(self.path)
        with patch.object(self.recovery, "preflight", return_value={"ok": True}):
            self.assertEqual(self.main("preflight")[0], 0)
        with self.assertRaises(SystemExit):
            self.main("preflight", "--break-stale-lock")


class FakeExperiment:
    """Stands in for the connected Boutique lab in a chaos scenario run: run() is the whole live part."""

    constructed, outcome, path = [], None, None

    def __init__(self, directory, **options):
        FakeExperiment.constructed.append(directory)
        self.directory, self.summary = Path(directory), {"provenance": {}}
        self.lab_control = self.approval_relay = self.dashboard = None
        self.store = Mock()
        self.credentials = self.directory / "credentials"
        self.closed = False

    def select_target(self, name):
        self.name = name

    def configure_approval(self, mode, operator):
        pass

    def approval_not_requested(self, reason):
        pass

    def run(self):
        info = json.loads(FakeExperiment.path.read_text(encoding="utf-8"))
        self.summary["lock_holder"] = info["holder"]
        if FakeExperiment.outcome is not None:
            raise FakeExperiment.outcome

    def close(self):
        # Chaos cleanup runs before the lock is released.
        self.summary["lock_held_at_close"] = FakeExperiment.path.exists()

    def close_approval_authority(self):
        pass


class ChaosScenarioLockTests(LockTestCase):
    """P4.03 scenario runs (scripts/test_boutique_live.py --connected --scenario N2|N3|T1) hold the lock."""

    def setUp(self):
        super().setUp()
        from experiments.boutique.harness import connected, live
        self.live = live
        FakeExperiment.constructed, FakeExperiment.outcome, FakeExperiment.path = [], None, self.path
        self.enterContext(patch.object(connected, "ConnectedBoutiqueExperiment", FakeExperiment))
        self.enterContext(patch.object(live, "tool_versions", return_value={}))
        self.enterContext(patch("shared.provenance.collect", return_value={"source_revision": "test"}))

    def main(self, *extra, scenario="N2"):
        run_dir = self.directory / ("run-" + str(len(FakeExperiment.constructed)))
        argv = ["harness", "--connected", "--scenario", scenario, "--approval", "auto-lab", "--run-dir",
                str(run_dir), *extra]
        stderr = io.StringIO()
        with patch("sys.argv", argv), patch("sys.stdin", io.StringIO()), redirect_stderr(stderr), \
                redirect_stdout(io.StringIO()), \
                patch("subprocess.run", side_effect=AssertionError("cluster command attempted")):
            code = self.live.main()
        summary = run_dir / "summary.json"
        return code, stderr.getvalue(), json.loads(summary.read_text(encoding="utf-8")) if summary.exists() else None

    def test_a_chaos_run_holds_the_lock_until_its_cleanup_ends_and_releases_it(self):
        code, _, summary = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(summary["lock_holder"], "P4.03 chaos scenario N2")
        self.assertTrue(summary["lock_held_at_close"])
        self.assertFalse(self.path.exists())

    def test_a_failed_or_interrupted_chaos_run_releases_the_lock(self):
        for outcome, expected in ((RuntimeError("chaos object never injected"), 1), (KeyboardInterrupt(), 130)):
            FakeExperiment.outcome = outcome
            with self.subTest(outcome=type(outcome).__name__):
                code, _, summary = self.main()
                self.assertEqual(code, expected)
                self.assertTrue(summary["lock_held_at_close"])
                self.assertFalse(self.path.exists())

    def test_a_chaos_run_is_refused_while_another_run_holds_the_lock(self):
        other_holder(self.path, holder="P4.01 kind_acceptance")
        code, stderr, summary = self.main()
        self.assertEqual((code, summary, FakeExperiment.constructed), (2, None, []))  # Nothing was started.
        self.assertIn("P4.01 kind_acceptance", stderr)
        self.assertTrue(self.path.exists())

    def test_a_stale_lock_refuses_until_break_stale_lock(self):
        other_holder(self.path, pid=DEAD_PID)
        code, stderr, _ = self.main()
        self.assertEqual((code, FakeExperiment.constructed), (2, []))
        self.assertIn("--break-stale-lock", stderr)
        code, stderr, summary = self.main("--break-stale-lock")
        self.assertEqual(code, 0)
        self.assertIn("Removed a stale heavy-lab lock", stderr)
        self.assertFalse(self.path.exists())

    def test_break_stale_lock_is_refused_outside_a_chaos_or_pair_run(self):
        with self.assertRaises(SystemExit):
            self.main("--break-stale-lock", "--agent", scenario="R1")
        self.assertEqual(FakeExperiment.constructed, [])

    def test_a_template_scenario_takes_no_lock(self):
        other_holder(self.path)
        from experiments.boutique.harness import live
        run_dir = self.directory / "rejection"
        with patch("sys.argv", ["harness", "--run-dir", str(run_dir), "--guard-rejection"]), \
                patch("sys.stdin", io.StringIO()), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()), \
                patch("subprocess.run", side_effect=AssertionError("cluster command attempted")), \
                patch.object(live.BoutiqueExperiment, "prepare", side_effect=RuntimeError("stop before the cluster")):
            self.assertEqual(live.main(), 1)  # It got past the lock to its own (patched) failure.
        self.assertTrue(self.path.exists())


class FakePair:
    constructed, outcome, path = [], None, None

    def __init__(self, directory, **options):
        FakePair.constructed.append(directory)
        self.directory, self.summary = Path(directory), {"provenance": {}}

    def configure_approval(self, mode, operator):
        pass

    def run(self):
        self.summary["lock_holder"] = json.loads(FakePair.path.read_text(encoding="utf-8"))["holder"]
        if FakePair.outcome is not None:
            raise FakePair.outcome
        self.summary["pair_result"] = {"result": {"pair": "p", "result": "RECOVERED", "writes": {}, "labels": "x",
                                                  "labels_pending": False, "ordered_recovery_shown": True}}

    def close(self):
        self.summary["lock_held_at_close"] = FakePair.path.exists()


class PairLockTests(LockTestCase):
    """P4.05: a pair run holds the lock from before its lab is built until its cleanup ends."""

    def setUp(self):
        super().setUp()
        from experiments.boutique.harness import multi_fault
        self.multi_fault = multi_fault
        FakePair.constructed, FakePair.outcome, FakePair.path = [], None, self.path
        self.enterContext(patch.object(multi_fault, "PairBoutiqueExperiment", FakePair))

    def run_pair(self, break_stale_lock=False):
        args = SimpleNamespace(pair="redis-pod+cart-config", storefront_port=8080, executor_kill=False,
                               approval="auto-lab", operator=None, break_stale_lock=break_stale_lock)
        directory = self.directory / ("pair-" + str(len(FakePair.constructed)))
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            code = self.multi_fault.run_pair(args, directory, tools={})
        summary = directory / "summary.json"
        return code, stderr.getvalue(), json.loads(summary.read_text(encoding="utf-8")) if summary.exists() else None

    def test_a_pair_holds_the_lock_and_releases_it_on_success_and_failure(self):
        code, _, summary = self.run_pair()
        self.assertEqual(code, 0)
        self.assertEqual(summary["lock_holder"], "P4.05 pair redis-pod+cart-config")
        self.assertTrue(summary["lock_held_at_close"])
        self.assertFalse(self.path.exists())
        for outcome, expected in ((RuntimeError("cart never recovered"), 1), (KeyboardInterrupt(), 130)):
            FakePair.outcome = outcome
            with self.subTest(outcome=type(outcome).__name__):
                self.assertEqual(self.run_pair()[0], expected)
                self.assertFalse(self.path.exists())

    def test_a_pair_is_refused_while_held_and_a_stale_lock_needs_break_stale_lock(self):
        held = other_holder(self.path, holder="P4.03 chaos scenario N2")
        code, stderr, summary = self.run_pair()
        self.assertEqual((code, summary, FakePair.constructed), (2, None, []))
        self.assertIn("P4.03 chaos scenario N2", stderr)
        held.release()
        other_holder(self.path, pid=DEAD_PID)
        code, stderr, _ = self.run_pair()
        self.assertEqual((code, FakePair.constructed), (2, []))
        self.assertIn("--break-stale-lock", stderr)
        code, stderr, summary = self.run_pair(break_stale_lock=True)
        self.assertEqual(code, 0)
        self.assertIn("Removed a stale heavy-lab lock", stderr)
        self.assertEqual(summary["lock_holder"], "P4.05 pair redis-pod+cart-config")
        self.assertFalse(self.path.exists())

    def test_the_live_entry_point_passes_break_stale_lock_to_the_pair(self):
        from experiments.boutique.harness import live
        other_holder(self.path, pid=DEAD_PID)
        argv = ["harness", "--connected", "--pair", "redis-pod+cart-config", "--approval", "auto-lab",
                "--run-dir", str(self.directory / "via-live"), "--break-stale-lock"]
        with patch("sys.argv", argv), patch.object(live, "tool_versions", return_value={}), \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(live.main(), 0)
        self.assertEqual(len(FakePair.constructed), 1)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
