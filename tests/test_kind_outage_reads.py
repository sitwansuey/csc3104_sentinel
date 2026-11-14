"""Reads through kubectl while a Pod restarts or is replaced (P4.01).

On 14 Nov 2026 the S2-S9 matrix at b37017e passed S2, S3 and S4 5/5 and then
stopped in S5 r1: while the Coordinator's Pod was being replaced, a kubectl
exec into it failed (exit 1), the operator thread and the main thread both
raised, and the run ended with no summary
(docs/distributed-acceptance.md#s2-s9-matrix-b37017e-reads-while-the-coordinator-pod-was-replaced).
These tests drive the real Shell and Lab through a fake kubectl that fails a
read N times and then answers (the read is asked again with bounded back-off
and the run goes on) or never answers (a clear harness error at the step's
deadline, never a pass), for the main thread's waits, the operator thread and
S5 itself. A query that ran and failed is never asked again.
"""
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from experiments.distributed import kind_acceptance as acceptance
from experiments.distributed import kind_acceptance_live as live
from runtime.service_identity import ProtocolError
from tests.test_kind_acceptance import FakeLab

BASE = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
CONTAINER_GONE = 'error: unable to upgrade connection: container not found ("coordinator")'
WAITING = ('Error from server (BadRequest): container "coordinator" in pod "sentinel-coordinator-new" is waiting to '
           'start: ContainerCreating')
POD_GONE = 'Error from server (NotFound): pods "sentinel-coordinator-old" not found'
REFUSED = "Error from server: error dialing backend: dial tcp 172.18.0.3:10250: connect: connection refused"
NO_SERVER = "Unable to connect to the server: dial tcp 127.0.0.1:40123: connect: connection refused"
KILLED = "command terminated with exit code 137"
QUERY_ERROR = ("Traceback (most recent call last):\nsqlite3.OperationalError: no such table: incidents\n"
               "command terminated with exit code 1")
FORBIDDEN = 'Error from server (Forbidden): pods "sentinel-coordinator-old" is forbidden: User cannot create pods/exec'
TRANSIENT = {"container not found": (1, CONTAINER_GONE, "container not running"),
             "container waiting": (1, WAITING, "container not running"),
             "pod not found": (1, POD_GONE, "pod not found"),
             "kubelet refused": (1, REFUSED, "connection refused"),
             "API server refused": (1, NO_SERVER, "connection refused"),
             "killed with its container": (137, KILLED, "process killed with its container (SIGKILL)")}
ROW = {"incident_id": "INC-1", "state": "VERIFYING", "active_action_id": "ACT-1"}


def done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def pod_list(*names):
    return done(json.dumps({"items": [{"metadata": {"name": name, "uid": "uid-" + name}, "spec": {"nodeName": "n"},
                                       "status": {"phase": "Running"}} for name in names]}))


class FlakyKubectl:
    """kubectl whose exec fails as scripted, then answers from rows; records every exec and how it ended.

    failures is a list of answers (CompletedProcess, or an exception to
    raise) given to the first execs in turn; forever, when set, is given to
    every exec instead.
    """

    def __init__(self, failures=(), *, forever=None, rows=None):
        self.failures, self.forever, self.execs = list(failures), forever, []
        self.rows = [ROW] if rows is None else rows

    def __call__(self, command, **options):
        if "get" in command and "pods" in command:
            app = next(part for part in command if part.startswith("--selector=app=")).split("=", 2)[2]
            return pod_list(app + "-pod")
        if "exec" in command:
            self.execs.append(list(command))
            answer = self.forever if self.forever is not None else (self.failures.pop(0) if self.failures else None)
            if isinstance(answer, BaseException):
                raise answer
            if answer is not None:
                return answer
            if acceptance.FAULT_HELPER in command:
                return done("coordinator\n" if "reached" in command else "")
            if acceptance.BACKUP_HELPER in command:
                return done(b"SQLite format 3\x00")
            sql = command[-2]
            return done(json.dumps([{"proposal": "{}"}] if "FROM actions" in sql else self.rows))
        raise AssertionError("unexpected command: " + " ".join(command))


def virtual_lab(runner, cls=acceptance.Lab):
    """A real Lab over runner on virtual time: lab.sleeps holds every pause it took."""
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    lab = cls(acceptance.Shell("kind-sentinel-dist", runner=runner), tempfile.gettempdir(),
              clock=lambda: BASE + timedelta(seconds=now[0]), sleep=sleep, monotonic=lambda: now[0])
    lab.sleeps, lab.now = sleeps, now
    return lab


class ClassificationTests(unittest.TestCase):
    def test_kubectls_own_failures_to_reach_a_pod_are_transient(self):
        for label, (code, stderr, reason) in TRANSIENT.items():
            with self.subTest(label=label):
                self.assertEqual(acceptance.transient_failure(done(code=code, stderr=stderr)), reason)

    def test_a_command_that_ran_and_failed_is_never_transient(self):
        cases = {"query error": (1, QUERY_ERROR), "forbidden": (1, FORBIDDEN), "success": (0, ""),
                 "the probe's own connection refused": (1, NO_SERVER + "\ncommand terminated with exit code 1"),
                 "another exit status": (2, CONTAINER_GONE), "nothing on stderr": (1, ""),
                 "terminated by the command, not its container": (137, "command terminated with exit code 1")}
        for label, (code, stderr) in cases.items():
            with self.subTest(label=label):
                self.assertIsNone(acceptance.transient_failure(done(code=code, stderr=stderr)))

    def test_the_shell_raises_transient_read_by_class_and_names_no_argument(self):
        for stderr, kind in ((CONTAINER_GONE, acceptance.TransientRead), (QUERY_ERROR, acceptance.AcceptanceError),
                             (FORBIDDEN, acceptance.AcceptanceError)):
            with self.subTest(stderr=stderr[:30]):
                shell = acceptance.Shell("kind-sentinel-dist", runner=lambda command, stderr=stderr, **options: done(
                    code=1, stderr=stderr))
                with self.assertRaises(kind) as caught:
                    shell.kubectl("get", "secret", "sentinel-dist-operator-1", namespace=acceptance.NAMESPACE)
                self.assertIs(type(caught.exception), kind)
                self.assertTrue(str(caught.exception).startswith("kubectl get failed (exit 1)"), caught.exception)
                self.assertNotIn("sentinel-dist-operator-1", str(caught.exception))
                self.assertNotIn("--context", str(caught.exception))  # 14 Nov 2026: "kubectl --context=... failed".


class MainThreadReadTests(unittest.TestCase):
    def test_a_wait_rides_out_reads_that_fail_n_times_then_answer(self):
        for label, (code, stderr, reason) in TRANSIENT.items():
            with self.subTest(label=label):
                kubectl = FlakyKubectl([done(code=code, stderr=stderr)] * 4)
                lab = virtual_lab(kubectl)
                self.assertEqual(lab.wait_state({"VERIFYING"}, 150), ROW)
                self.assertEqual(len(kubectl.execs), 5)
                self.assertEqual(lab.sleeps, [1.0, 2.0, 4.0, 8.0])  # Bounded back-off, not a tight loop.
                [outage] = lab.read_outages
                self.assertEqual((outage["attempts"], outage["reason"], outage["recovered"]), (4, reason, True))
                self.assertEqual(outage["what"], "an incident in VERIFYING")

    def test_a_kubectl_that_does_not_answer_in_time_is_asked_again(self):
        kubectl = FlakyKubectl([subprocess.TimeoutExpired(["kubectl"], 60)] * 2)
        lab = virtual_lab(kubectl)
        self.assertEqual(lab.wait_state({"VERIFYING"}, 150), ROW)
        self.assertEqual(lab.read_outages[0]["reason"], "kubectl did not answer in time")

    def test_a_read_that_never_answers_is_a_harness_error_at_the_steps_deadline(self):
        kubectl = FlakyKubectl(forever=done(code=1, stderr=CONTAINER_GONE))
        lab = virtual_lab(kubectl)
        with self.assertRaises(acceptance.ReadUnavailable) as caught:
            lab.wait_state({"VERIFYING"}, 150)
        self.assertIn("an incident in VERIFYING: the reads kept failing", str(caught.exception))
        self.assertIn("150 s limit", str(caught.exception))
        self.assertLessEqual(lab.now[0], 150)  # Bounded by the step's own deadline.
        self.assertLessEqual(max(lab.sleeps), acceptance.READ_BACKOFF_SECONDS)
        self.assertFalse(lab.read_outages[-1]["recovered"])
        with self.assertRaises(acceptance.ReadUnavailable):  # Never a terminal "not in time": not a pass either.
            virtual_lab(FlakyKubectl(forever=done(code=1, stderr=POD_GONE))).wait_terminal(300)

    def test_a_query_that_ran_and_failed_is_raised_at_once(self):
        for stderr in (QUERY_ERROR, FORBIDDEN):
            with self.subTest(stderr=stderr[:30]):
                kubectl = FlakyKubectl([done(code=1, stderr=stderr)])
                lab = virtual_lab(kubectl)
                with self.assertRaisesRegex(acceptance.AcceptanceError, "Coordinator's database could not be read") \
                        as caught:
                    lab.wait_state({"VERIFYING"}, 150)
                self.assertNotIsInstance(caught.exception, acceptance.TransientRead)
                self.assertEqual(len(kubectl.execs), 1)  # Never asked again, never read as "no incident yet".
                self.assertEqual(lab.read_outages, [])

    def test_a_strict_read_is_asked_again_within_its_bound(self):
        kubectl = FlakyKubectl([done(code=1, stderr=REFUSED)] * 3)
        lab = virtual_lab(kubectl)
        self.assertEqual(lab.all_incidents(strict=True), [ROW])
        lab = virtual_lab(FlakyKubectl(forever=done(code=1, stderr=REFUSED)))
        with self.assertRaisesRegex(acceptance.ReadUnavailable, "Coordinator's database could not be read"):
            lab.all_incidents(strict=True)
        self.assertGreaterEqual(lab.now[0], acceptance.READ_RETRY_SECONDS - acceptance.READ_BACKOFF_SECONDS)
        self.assertLessEqual(lab.now[0], acceptance.READ_RETRY_SECONDS)

    def test_a_copy_and_a_fault_point_are_read_again_while_the_pod_restarts(self):
        kubectl = FlakyKubectl([done(code=1, stderr=CONTAINER_GONE)] * 2)
        lab = virtual_lab(kubectl)
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(lab.copy_database("coordinator", "/data/coordinator.sqlite3",
                                              Path(directory) / "coordinator.db"))
        kubectl = FlakyKubectl([done(code=137, stderr=KILLED), done(code=1, stderr=CONTAINER_GONE)])
        lab = virtual_lab(kubectl)
        self.assertEqual(lab.first_reached(["coordinator"], "coordinator.after_commit_before_reply"), "coordinator")
        kubectl = FlakyKubectl([done(code=1, stderr=FORBIDDEN)])
        with self.assertRaises(acceptance.AcceptanceError):  # Not "not reached yet" until the deadline.
            virtual_lab(kubectl).first_reached(["coordinator"], "coordinator.after_commit_before_reply")
        self.assertEqual(len(kubectl.execs), 1)


class Ticks:
    """threading.Event's wait for _operator: False for ticks calls, each a second of virtual time, then True."""

    def __init__(self, lab, ticks):
        self.lab, self.left, self.calls = lab, ticks, 0

    def wait(self, timeout):
        self.calls += 1
        self.lab.now[0] += timeout
        self.left -= 1
        return self.left < 0


class OperatorLab(acceptance.Lab):
    """The real Lab with the approval API replaced by a recorder (no port-forward, no HTTP)."""

    def port_forward(self, port=18767):
        return port

    def approval_client(self, port):
        lab = self

        class Client:
            def post(self, operation, body):
                if getattr(lab, "refuse", None):
                    raise ProtocolError(409, "approval refused")
                lab.posts.append((operation, body["action_id"]))
        return Client()


def operator_lab(kubectl):
    lab = virtual_lab(kubectl, OperatorLab)
    lab.posts, lab.refuse = [], False
    return lab


AWAITING = [{"incident_id": "INC-1", "state": "AWAITING_APPROVAL", "active_action_id": "ACT-1"}]


class OperatorThreadTests(unittest.TestCase):
    def test_the_operator_outlives_n_failed_reads_then_approves(self):
        for label, (code, stderr, reason) in TRANSIENT.items():
            with self.subTest(label=label):
                lab = operator_lab(FlakyKubectl([done(code=code, stderr=stderr)] * 5, rows=AWAITING))
                record, ticks = live.operator_record(), Ticks(lab, 10)
                live._operator(lab, "v1", ticks, record)
                self.assertEqual(ticks.calls, 11)  # Every tick ran: the failures did not end it.
                self.assertEqual(lab.posts, [("create", "ACT-1")])
                self.assertEqual(record["attempts_failed"], 5)
                self.assertEqual({item["kind"] for item in record["failures"]}, {"transient"})
                [outage] = record["outages"]
                self.assertEqual((outage["attempts"], outage["reason"], outage["ended"]), (5, reason, "answered"))
                self.assertEqual(record["fatal"], [])

    def test_an_operator_that_cannot_read_past_the_bound_is_fatal_once_and_keeps_going(self):
        lab = operator_lab(FlakyKubectl(forever=done(code=1, stderr=CONTAINER_GONE), rows=AWAITING))
        record, ticks = live.operator_record(), Ticks(lab, acceptance.READ_RETRY_SECONDS + 60)
        live._operator(lab, "v1", ticks, record)
        self.assertEqual(ticks.calls, acceptance.READ_RETRY_SECONDS + 61)
        self.assertEqual(lab.posts, [])
        [fatal] = record["fatal"]
        self.assertIn("could not read or reach the Coordinator", fatal)
        self.assertIn("container not running", fatal)
        self.assertEqual(len(record["failures"]), live.OPERATOR_FAILURES_KEPT)
        self.assertEqual(record["attempts_failed"], acceptance.READ_RETRY_SECONDS + 60)
        self.assertEqual(record["outages"][-1]["ended"], "the operator stopped first")

    def test_a_query_error_or_an_unexpected_error_is_fatal_and_a_refusal_is_recorded(self):
        lab = operator_lab(FlakyKubectl(forever=done(code=1, stderr=QUERY_ERROR), rows=AWAITING))
        record = live.operator_record()
        live._operator(lab, "v1", Ticks(lab, 5), record)
        self.assertEqual(record["attempts_failed"], 5)
        [fatal] = record["fatal"]  # Once, however many ticks repeat it.
        self.assertIn("Coordinator's database could not be read", fatal)
        lab = operator_lab(FlakyKubectl(rows=[{"incident_id": "INC-1", "state": "AWAITING_APPROVAL"}]))
        record = live.operator_record()
        live._operator(lab, "v1", Ticks(lab, 3), record)  # A row without active_action_id: KeyError.
        self.assertEqual((record["attempts_failed"], [item["kind"] for item in record["failures"]]),
                         (3, ["fatal"] * 3))
        self.assertIn("KeyError", record["fatal"][0])
        lab = operator_lab(FlakyKubectl(rows=AWAITING))
        lab.refuse = True
        record = live.operator_record()
        live._operator(lab, "v1", Ticks(lab, 3), record)
        self.assertEqual([item["kind"] for item in record["failures"]], ["refused"] * 3)
        self.assertEqual(record["fatal"], [])

    def test_the_operator_thread_stays_alive_through_failures(self):
        lab = operator_lab(FlakyKubectl([done(code=1, stderr=POD_GONE)] * 3, rows=AWAITING))
        record, stopping = live.operator_record(), threading.Event()
        with patch.object(live, "OPERATOR_TICK_SECONDS", 0.001):
            thread = threading.Thread(target=live._operator, args=(lab, "v1", stopping, record), daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            while not lab.posts and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(thread.is_alive())
            stopping.set()
            thread.join(5)
        self.assertEqual(lab.posts, [("create", "ACT-1")])
        self.assertEqual(record["attempts_failed"], 3)
        self.assertFalse(thread.is_alive())


class FailingOperatorLab(FakeLab):
    """FakeLab whose operator reads fail failing times (forever when None) before answering."""

    def __init__(self, rows, failing=None, **options):
        super().__init__(rows, **options)
        self.failing, self.calls = failing, 0

    def approve_pending(self, policy_version):
        self.calls += 1
        if self.failing is None or self.calls <= self.failing:
            raise acceptance.TransientRead("kubectl exec failed (exit 1): pod not found", "pod not found")
        return None


class RunOneTests(unittest.TestCase):
    def setUp(self):
        self.deployed = {"policy_version": "v1", "versions": {}, "api_server": {"endpoint": "172.18.0.4"}}
        self.tokens = {"executor-a": {"delivery-token": "q" * 43}}
        patcher = patch.object(live, "git_revision", lambda: "abc123")
        patcher.start()
        self.addCleanup(patcher.stop)
        tick = patch.object(live, "OPERATOR_TICK_SECONDS", 0.001)
        tick.start()
        self.addCleanup(tick.stop)

    def run_s0(self, lab, calls):
        def runner(run):  # The scenario lasts until the operator has tried calls times (or 10 s).
            deadline = time.monotonic() + 10
            while lab.calls < calls and time.monotonic() < deadline:
                time.sleep(0.005)
        with tempfile.TemporaryDirectory() as directory, patch.dict(live.RUNNERS, {"S0": runner}):
            summary = live.run_one(lab, acceptance.SCENARIOS["S0"], 1, self.deployed, {}, self.tokens,
                                   Path(directory))
            [folder] = list((Path(directory) / "2026-11-13").iterdir())
            written = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(written["operator"], summary["operator"])
        return summary

    def test_an_operator_outage_that_ends_is_in_the_summary_and_is_no_harness_error(self):
        summary = self.run_s0(FailingOperatorLab([], failing=3), calls=6)
        operator = summary["operator"]
        self.assertEqual(operator["attempts_failed"], 3)
        self.assertEqual(operator["outages"][0]["ended"], "answered")
        self.assertEqual(operator["fatal"], [])
        self.assertFalse(operator["still_running"])
        self.assertIsNone(summary["harness_error"])

    def test_an_operator_that_never_reads_fails_the_run_with_a_harness_error(self):
        summary = self.run_s0(FailingOperatorLab([]), calls=80)  # FakeLab's clock moves 5 s per read.
        self.assertIn("operator: the operator could not read or reach the Coordinator", summary["harness_error"])
        self.assertEqual(len(summary["operator"]["fatal"]), 1)
        self.assertFalse(summary["passed"])

    def test_a_terminal_wait_whose_reads_never_came_back_is_recorded_and_fails_the_run(self):
        class Unreadable(FailingOperatorLab):
            def wait_terminal(self, timeout):
                self.read_outages.append({"what": "every incident terminal", "attempts": 40, "recovered": False})
                raise acceptance.ReadUnavailable("every incident terminal: the reads kept failing")
        summary = self.run_s0(Unreadable([], failing=0), calls=1)
        self.assertIn("ReadUnavailable: every incident terminal", summary["harness_error"])
        self.assertFalse(summary["terminal_in_time"])
        self.assertEqual(summary["read_outages"][0]["recovered"], False)
        self.assertFalse(summary["passed"])


class CoordinatorWorld:
    """kubectl against a scripted Coordinator for S5 on virtual time (lab.now).

    Its process exits 5 s after the fault point is armed and its container is
    back and Ready 20 s later. After the delete the old Pod terminates for
    10 s, there is no Pod for 5 s, the replacement is Pending for 15 s and
    Running but not Ready for 10 s more; the API server refuses one Pod list
    in between. readable False keeps the replacement's container from ever
    answering an exec.
    """

    def __init__(self, *, readable=True):
        self.lab, self.armed_at, self.deleted_at, self.readable = None, None, None, readable
        self.refused_once, self.deletes = False, []

    def t(self):
        return self.lab.now[0]

    def at(self, seconds):
        return acceptance.stamp(BASE + timedelta(seconds=seconds))

    def pods(self):
        exit_at = None if self.armed_at is None else self.armed_at + 5
        if self.deleted_at is None or self.t() < self.deleted_at + 10:
            restarting = exit_at is not None and exit_at <= self.t() < exit_at + 20
            ready_at = (exit_at + 20) if exit_at is not None and self.t() >= exit_at + 20 else -100
            old = self.pod("sentinel-coordinator-old", ready_at=None if restarting else ready_at,
                           restarts=1 if exit_at is not None and self.t() >= exit_at else 0,
                           last_exit=(1, self.at(exit_at)) if exit_at is not None and self.t() >= exit_at else None,
                           deleting=self.deleted_at is not None)
            return [old]
        since = self.t() - self.deleted_at
        if since < 15:
            return []
        if since < 30:
            return [self.pod("sentinel-coordinator-new", phase="Pending", ready_at=None)]
        return [self.pod("sentinel-coordinator-new", ready_at=self.deleted_at + 40 if since >= 40 else None)]

    def pod(self, name, *, phase="Running", ready_at=None, restarts=0, last_exit=None, deleting=False):
        container = {"name": "coordinator", "containerID": f"containerd://{name}-{restarts}", "restartCount": restarts,
                     "state": {"running": {}} if ready_at is not None else {"waiting": {}}}
        if last_exit:
            container["lastState"] = {"terminated": {"exitCode": last_exit[0], "finishedAt": last_exit[1]}}
        metadata = {"name": name, "uid": "uid-" + name}
        if deleting:
            metadata["deletionTimestamp"] = self.at(self.deleted_at)
        conditions = [{"type": "Ready", "status": "True" if ready_at is not None else "False",
                       "lastTransitionTime": self.at(ready_at) if ready_at is not None else None}]
        return {"metadata": metadata, "spec": {"nodeName": "sentinel-dist-worker3"},
                "status": {"phase": phase, "conditions": conditions, "containerStatuses": [container]}}

    def __call__(self, command, **options):
        if "get" in command and "pods" in command:
            if self.deleted_at is not None and self.t() >= self.deleted_at + 12 and not self.refused_once:
                self.refused_once = True
                return done(code=1, stderr=NO_SERVER)
            return done(json.dumps({"items": self.pods()}))
        if "delete" in command and "pod" in command:
            self.deletes.append(command[command.index("pod") + 1])
            self.deleted_at = self.t()
            return done()
        if "logs" in command:
            return done("coordinator log\n")
        if "exec" in command:
            pod = command[command.index("exec") + 1]
            live_pods = {item["metadata"]["name"]: item for item in self.pods()}
            item = live_pods.get(pod)
            if item is None:
                return done(code=1, stderr=POD_GONE)
            running = item["status"]["containerStatuses"][0]["state"].get("running") is not None
            if not running or (pod.endswith("-new") and not self.readable):
                return done(code=1, stderr=CONTAINER_GONE)
            if acceptance.FAULT_HELPER in command:
                if "arm" in command:
                    self.armed_at = self.t()
                    return done()
                # The process reaches the point, and exits on it, 5 s after it was armed.
                reached = "reached" in command and self.armed_at is not None and self.t() >= self.armed_at + 5
                return done("coordinator\n" if reached else "")
            return done(json.dumps([ROW]))
        raise AssertionError("unexpected command: " + " ".join(command))


class CoordinatorRestartTests(unittest.TestCase):
    """S5 itself: the Coordinator unreadable while it restarts and while its Pod is replaced."""

    def s5(self, world):
        lab = virtual_lab(world)
        world.lab = lab
        run = live.Run(lab, acceptance.SCENARIOS["S5"], 1, "v1")
        run.since = acceptance.stamp(BASE)
        return lab, run

    def test_s5_waits_out_both_outages_and_measures_ready_from_the_replacement(self):
        world = CoordinatorWorld()
        lab, run = self.s5(world)
        live.s5(run)
        self.assertEqual(world.deletes, ["sentinel-coordinator-old"])
        exited = run.notes["after_commit_exit"]
        self.assertEqual((exited["exit_code"], exited["restarts"]), (1, 1))
        self.assertEqual(exited["ready_at"], world.at(world.armed_at + 25))
        deleted = run.notes["pod_delete"]
        self.assertEqual(deleted["replacement"], "sentinel-coordinator-new")
        self.assertEqual(deleted["ready_at"], world.at(world.deleted_at + 40))  # The cluster's Ready, not a guess.
        self.assertGreaterEqual(acceptance._seconds(deleted["readable_at"]), acceptance._seconds(deleted["ready_at"]))
        self.assertTrue(world.refused_once)
        self.assertEqual(run.armed, [])
        self.assertTrue(lab.read_outages)  # The outages it waited out are kept for the summary.
        self.assertTrue(all(item["recovered"] for item in lab.read_outages), lab.read_outages)
        recovery = acceptance.coordinator_recovery(run.fault_at, deleted["ready_at"], [], [])
        self.assertEqual(recovery["to_ready"], 40.0)

    def test_s5_stops_with_a_harness_error_when_the_replacement_never_answers(self):
        world = CoordinatorWorld(readable=False)
        lab, run = self.s5(world)
        with self.assertRaisesRegex(acceptance.ReadUnavailable, "Coordinator's database") as caught:
            live.s5(run)
        self.assertIn("kept failing", str(caught.exception))
        self.assertNotIn("readable_at", run.notes["pod_delete"])
        self.assertFalse(lab.read_outages[-1]["recovered"])


if __name__ == "__main__":
    unittest.main()
