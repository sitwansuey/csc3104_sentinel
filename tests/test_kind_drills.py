"""P4.02 measurements and destructive-step ordering, entirely offline."""
from copy import deepcopy
from contextlib import closing
from datetime import timedelta
import io
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from experiments.distributed import kind_acceptance as ka, kind_acceptance_live as live, kind_drills as drills
from tests.test_kind_acceptance import audit_row
from tests.test_kind_chaos_confirmation import BASE, FakeCluster, ScenarioLab


def at(value):
    return ka.stamp(BASE + timedelta(seconds=value))


def answer(value="", code=0):
    return subprocess.CompletedProcess([], code, value, "")


def request(value, *, identity="executor-a", operation="claim", outcome="idle", status=200):
    return {"ts": at(value), "identity": identity, "operation": operation, "outcome": outcome,
            "http_status": status, "event": "http.access"}


class DirectoryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)


class RtoTests(DirectoryTest):
    def save(self, name, *, pending=False, offset=False):
        folder = self.directory / name
        folder.mkdir()
        summary = {"scenario": "S5", "passed": True, "fault_at": at(8 if offset else 10),
                   "clock": {"control_plane_minus_harness": [{"offset_seconds": 2}]} if offset else
                   {"fault_at_cluster_clock": at(10)}, "notes": {"pod_delete": {"ready_at": at(12)}}}
        access = [request(9), request(10.1, identity="operator-1"), request(10.2, identity="observer-1"),
                  request(11, status=503), request(13, operation="clock", outcome="ok"), request(14)]
        if pending:
            access.append(request(15, outcome="claimed"))
        records = {"access": access, "transitions": [{"recorded_at": at(16)}] if pending else []}
        (folder / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        (folder / "records.json").write_text(json.dumps(records), encoding="utf-8")
        return folder

    def test_nothing_pending_still_has_served_worker_rto(self):
        report = drills.rto_from_runs([self.save("s5-idle")])
        self.assertTrue(report["passed"])
        [row] = report["runs"]
        self.assertIsNone(row["to_first_claim"])
        self.assertIsNone(row["to_next_transition"])
        self.assertEqual(row["to_first_served_request"], 3)
        self.assertEqual(row["to_ready"], 2)

    def test_pending_claim_and_offset_and_aggregates(self):
        self.save("one", pending=True, offset=True)
        self.save("two")
        report = drills.rto_from_runs([self.directory, self.directory / "one"])
        self.assertEqual(len(report["runs"]), 2)  # No duplicate when inputs overlap.
        self.assertEqual(report["runs"][0]["to_first_claim"], 5)
        self.assertEqual(report["aggregate_seconds"]["to_first_served_request"],
                         {"samples": 2, "missing": 0, "min": 3, "median": 3, "max": 3})
        self.assertEqual(report["aggregate_seconds"]["to_first_claim"]["missing"], 1)

    def test_min_median_max_use_distinct_runs(self):
        for name, served in (("one", 11), ("two", 13), ("three", 17)):
            folder = self.save(name)
            (folder / "records.json").write_text(json.dumps({"access": [request(served, operation="clock")],
                                                            "transitions": []}), encoding="utf-8")
        report = drills.rto_from_runs([self.directory])
        self.assertEqual(report["aggregate_seconds"]["to_first_served_request"],
                         {"samples": 3, "missing": 0, "min": 1, "median": 3, "max": 7})

    def test_missing_records_or_timing_never_pass(self):
        folder = self.save("broken")
        (folder / "records.json").unlink()
        report = drills.rto_from_runs([folder])
        self.assertFalse(report["passed"])
        self.assertEqual(len(report["errors"]), 1)
        self.assertFalse(drills.rto_from_runs([])["passed"])

    def test_offline_cli_writes_summary_and_markdown_without_shell(self):
        source = self.save("saved")
        with patch.object(ka, "Shell", side_effect=AssertionError("offline must never construct Shell")), \
                patch("sys.stdout", new_callable=io.StringIO):
            code = drills.main(["rto-from-runs", str(source), "--run-dir", str(self.directory / "out")])
        self.assertEqual(code, 0)
        [summary] = (self.directory / "out").rglob("summary.json")
        self.assertTrue(json.loads(summary.read_text())["passed"])
        self.assertIn("rto-from-runs | PASS", summary.with_name("results.md").read_text())


class PartitionMeasurements(unittest.TestCase):
    def measure(self, *, rows=(), extra=(), served=133, transition=134, error=0):
        return drills.partition_measurements(at(10), at(130), at(131),
            [request(served), *extra], [{"recorded_at": at(transition)}], rows, 0.5, error)

    def test_quiet_partition_and_bounded_progress_pass(self):
        result = self.measure(extra=[request(20, operation="renew", status=503),
                                     request(30, identity="operator-1", operation="renew")])
        self.assertTrue(result["passed"])
        self.assertEqual(result["renewal_operation"], "renew")
        self.assertEqual(result["recovery_seconds"]["to_first_served_request"], 2)

    def test_write_during_partition_fails_even_if_recovery_fast(self):
        for effect in ("template_write", "unclassified"):
            with self.subTest(effect=effect):
                result = self.measure(rows=[audit_row(effect, at=at(50))])
                self.assertFalse(result["passed"])
                self.assertEqual(result["template_writes"], 1)

    def test_successful_renewal_during_partition_fails(self):
        result = self.measure(extra=[request(20, operation="renew", outcome="renewed")])
        self.assertFalse(result["passed"])
        self.assertEqual(result["renewals_answered"], 1)

    def test_slow_request_or_transition_or_uncertain_boundary_fails(self):
        self.assertFalse(self.measure(served=147)["passed"])
        self.assertFalse(self.measure(transition=147)["passed"])
        self.assertFalse(self.measure(transition=146.4, error=0.2)["passed"])
        self.assertTrue(self.measure(transition=146.5)["passed"])

    def test_absent_post_heal_evidence_fails(self):
        report = drills.partition_measurements(at(10), at(130), at(131), [], [], [], 0.2)
        self.assertFalse(report["passed"])
        self.assertEqual(len(report["failures"]), 2)


class PartitionLab(ScenarioLab):
    """Real Run chaos lifecycle over FakeCluster; measured logs use virtual UTC."""

    def __init__(self, directory, *, injects=True, approval_error=False, write=False, renewal=False, slow=False):
        super().__init__(FakeCluster(injects=injects), directory)
        self.run_dir = directory
        self.approval_error, self.write, self.renewal, self.slow = approval_error, write, renewal, slow
        self.approvals, self.approval_attempts, self.read_outages = [], [], []

    def clock_offset(self):
        return {"offset_seconds": 0, "uncertainty_seconds": 0.01, "at": at(self.cluster.now)}

    def begin_run(self, baseline, policy=None):
        self.seen.append(("begin", False))
        return frozenset()

    def require_debug(self):
        return {"level": "DEBUG", "pod_uid": "uid"}

    def rtt(self):
        self.sleep(0.2)
        return 0.2

    def log_text(self, identity, since):
        lines = [request(0), request(self.cluster.now)]
        if self.renewal:
            lines.append(request(60, operation="renew", outcome="renewed"))
        return "\n".join(json.dumps(line) for line in lines)

    def query(self, sql, parameters=(), **kwargs):
        return [{"recorded_at": at(self.cluster.now + (20 if self.slow else 0)), "incident_id": "INC-1"}]

    def approve(self, action_id, policy_version):
        self.note("approve")
        if self.approval_error:
            raise ka.AcceptanceError("approval failed in transit")

    def approve_pending(self, policy_version):
        return None

    def incidents(self):
        return [{"incident_id": "INC-1", "state": "RECOVERED"}]

    def inject(self):
        self.note("inject")
        return {"operator_action": "inject"}

    def restore(self, baseline):
        self.note("restore")
        return {"operator_restore": True}

    def settle(self, baseline, **kwargs):
        self.note("settle")
        return {"settled": not kwargs["leftovers"](), "leftovers": kwargs["leftovers"]()}

    def audit_log(self, since, until):
        rows = [audit_row("template_write", sentinel=False, at=at(0.1))]
        if self.write:
            rows.append(audit_row("template_write", at=at(60)))
        return {"rows": rows, "skipped": 0, "first_at": at(-10), "files": 1}


class PartitionLifecycleTests(DirectoryTest):
    def run_drill(self, **kwargs):
        lab = PartitionLab(self.directory, **kwargs)
        report = drills.live_drill(lab, "partition", {"policy_version": "v1"}, {}, {}, duration=120)
        self.assertEqual(lab.cluster.left_behind(), {"chaos": [], "blocked": [], "armed": [], "paused": []})
        self.assertIn("restore", [step for step, _ in lab.seen])
        self.assertIn("settle", [step for step, _ in lab.seen])
        return lab, report

    def test_confirms_all_workers_before_approval_and_holds_full_duration(self):
        lab, report = self.run_drill()
        self.assertTrue(report["passed"], report["failures"])
        self.assertTrue(any(step == "approve" and effective for step, effective in lab.seen))
        [fault] = lab.cluster.history
        self.assertGreaterEqual(fault["deleted_at"] - fault["injected_at"], 120)
        self.assertEqual(set(fault["doc"]["spec"]["target"]["selector"]["expressionSelectors"][0]["values"]),
                         {value[0] for value in ka.IDENTITIES.values()})
        self.assertIsNotNone(report["chaos"][0]["confirmation"])

    def test_failed_approval_always_removes_chaos_and_restores(self):
        _, report = self.run_drill(approval_error=True)
        self.assertFalse(report["passed"])
        self.assertIn("approval failed", " ".join(report["failures"]))
        self.assertIn("remove chaos", [row["operator_action"] for row in report["cleanup"]["actions"]])

    def test_unconfirmed_injection_never_passes_and_is_removed(self):
        lab, report = self.run_drill(injects=False)
        self.assertFalse(report["passed"])
        self.assertFalse(any(step == "approve" for step, _ in lab.seen))
        self.assertIn("not confirmed", " ".join(report["failures"]))

    def test_live_write_renewal_and_slow_transition_each_fail(self):
        for kwargs in ({"write": True}, {"renewal": True}, {"slow": True}):
            with self.subTest(kwargs=kwargs):
                _, report = self.run_drill(**kwargs)
                self.assertFalse(report["passed"])

    def test_lingering_chaos_fails_with_manual_recovery_instruction(self):
        lab = PartitionLab(self.directory)
        lab.cluster.lingers = True
        report = drills.live_drill(lab, "partition", {"policy_version": "v1"}, {}, {}, duration=120)
        self.assertFalse(report["passed"])
        self.assertTrue(report["cleanup"]["problems"])
        self.assertIn("by hand", report["manual_recovery"])
        self.assertFalse(report["settle"]["settled"])


def check_report(*, executor_missing=True, verifier_missing=True, verifier_replay=False, rolled_back=True):
    def missing(kind, redrive):
        return {"kind": kind, "incident_id": "INC-1", "action_id": "ACT-1",
                "topic": "executor.request" if kind == "execution_result" else "verifier.request",
                "redrive": redrive}
    journals = []
    for identity in drills.WORKERS:
        items = []
        if identity == "executor-a" and executor_missing:
            items.append(missing("execution_result", "replay"))
        if identity == "verifier-1" and verifier_missing:
            items.append(missing("outcome", "replay" if verifier_replay else "review"))
        journals.append({"journal": identity + "/transport.db", "missing": items,
                         "acknowledged": 2, "unsent": 0})
    count = sum(len(row["missing"]) for row in journals)
    return {"database": {"schema_version": 5, "generation": "gen", "commit_counter": 10 if rolled_back else 100},
            "previous": {"generation": "gen", "commit_counter": 100},
            "different_database": False, "rolled_back": rolled_back, "lost_commits": 90 if rolled_back else 0,
            "journals": journals, "missing": count, "may_lose_acknowledged_work": bool(rolled_back or count)}


class CheckShell:
    context = ka.CONTEXT

    def __init__(self, lab, *, bad_exit=None):
        self.lab, self.bad_exit = lab, bad_exit

    def run(self, command, **options):
        self.lab.seen.append("check")
        self.lab.commands.append(command)
        if self.bad_exit is not None:
            return answer("{}", self.bad_exit)
        executor = "executor-a" not in self.lab.cleared
        verifier = "verifier-1" not in self.lab.cleared
        report = check_report(executor_missing=executor, verifier_missing=verifier,
                              verifier_replay=not executor, rolled_back=executor)
        return answer(json.dumps(report), int(report["may_lose_acknowledged_work"]))


class RestoreLab:
    """No subprocesses: the worker/checker state changes only when a replay holder is cleared."""

    def __init__(self, directory, *, bad_exit=None, detect=True, probe_status=503, claim_after=False):
        self.run_dir, self.now, self.seen, self.cleared, self.commands = directory, 0.0, [], [], []
        self.shell = CheckShell(self, bad_exit=bad_exit)
        self.detect, self.probe_status, self.claim_after = detect, probe_status, claim_after
        self.restored_at, self.fresh = None, False
        self.approval_attempts, self.read_outages, self.approvals = [], [], []

    def clock(self):
        return BASE + timedelta(seconds=self.now)

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration

    def wait(self, check, timeout, what, interval=1):
        deadline = self.now + timeout
        while self.now < deadline:
            value = check()
            if value:
                return value
            self.sleep(interval)
        raise TimeoutError(what)

    def arm(self, identities, stem, action):
        self.seen.append("arm")

    def inject(self):
        self.seen.append("fresh-inject" if self.fresh else "inject")

    def wait_state(self, states, timeout):
        self.seen.append("ready" if states == {"READY"} else "awaiting")
        return {"incident_id": "INC-1", "state": "READY" if states == {"READY"} else "AWAITING_APPROVAL",
                "active_action_id": "ACT-1"}

    def approve(self, action_id, policy_version):
        self.seen.append("approve")

    def first_reached(self, identities, stem, timeout):
        self.seen.append("held")
        return "executor-a"

    def holder_facts(self, identity):
        return {"pod": "executor-pod", "uid": "executor-uid", "container_id": "container"}

    def fault(self, *args):
        self.seen.append("release")
        return answer("")

    def backup(self):
        self.seen.append("backup")
        return {"generation": "gen", "commit_counter": 10}

    def approve_pending(self, policy_version):
        return None

    def incidents(self):
        self.seen.append("fresh-recovered" if self.fresh else "recovered")
        return [{"incident_id": "INC-FRESH" if self.fresh else "INC-1", "state": "RECOVERED"}]

    def position(self):
        self.seen.append("counter")
        return {"generation": "gen", "commit_counter": 98}

    def copy_database(self, identity, path, destination):
        self.seen.append("copy-" + identity)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fixture")
        return True

    def stop_coordinator(self):
        self.seen.append("stop")

    def maintenance(self, name):
        self.seen.append("maintenance")

    def maintenance_exec(self, name, script, *args, binary=False):
        if script == ka.QUERY_HELPER:
            self.seen.append("final-counter")
            return answer(json.dumps([{"generation": "gen", "commit_counter": 100}]))
        if binary:
            self.seen.append("restored-copy")
            return answer(b"fixture")
        self.seen.append("restore-copy")
        self.restored_at = self.now
        return answer(json.dumps({"generation": "gen", "commit_counter": 10}))

    def remove_maintenance(self, name):
        self.seen.append("remove-maintenance")

    def start_coordinator(self):
        self.seen.append("start")
        self.sleep(4)
        return at(self.now)

    def log_text(self, identity, since):
        if identity == "coordinator":
            return json.dumps(request(self.now)) if self.claim_after else ""
        if not self.detect:
            return ""
        return json.dumps({"ts": at(self.restored_at + 2), "event": "worker.store_rollback_detected",
                           "previous_generation": "gen", "previous_commit": 100, "commit_counter": 11})

    def probe_worker(self, identity):
        return {"http_status": self.probe_status, "body": {"store_changed": True}}

    def accept_worker(self, identity):
        self.seen.append("accept-" + identity)
        self.cleared.append(identity)

    def restore(self, baseline):
        self.seen.append("target-restore")

    def settle(self, baseline, **kwargs):
        self.seen.append("settle")
        return {"settled": True}

    def begin_run(self, baseline, policy):
        self.seen.append("fresh-begin")
        self.fresh = True


class RestoreLiveLab(RestoreLab):
    """RestoreLab with the outer restore/settle/evidence lifecycle enabled."""

    def target(self):
        return {"metadata": {"generation": 3}}

    def clock_offset(self):
        return {"offset_seconds": 0, "uncertainty_seconds": 0.01, "at": at(self.now)}

    def begin_run(self, baseline, policy=None):
        if self.restored_at is not None:
            super().begin_run(baseline, policy)
        return frozenset()

    def require_debug(self):
        return {"level": "DEBUG", "pod_uid": "uid"}

    def log_text(self, identity, since):
        if identity == "coordinator" and self.restored_at is None:
            return json.dumps(request(self.now))
        return super().log_text(identity, since)

    def audit_log(self, since, until):
        return {"rows": [audit_row("template_write", sentinel=False, at=at(0))],
                "skipped": 0, "first_at": at(-10), "files": 1}


class RestoreTests(DirectoryTest):
    def execute(self, **options):
        lab = RestoreLab(self.directory, **options)
        run = live.Run(lab, ka.SCENARIOS["S0"], 402, "v1")
        run.since, run.offset = at(-1), {"offset_seconds": 0, "uncertainty_seconds": 0}
        report = {"baseline": {}, "policy": {}}
        steps = drills.Steps(lab, report)
        drills.restore_drill(lab, run, steps, report, self.directory)
        return lab, report

    def test_step_order_and_replay_before_review_then_fresh_incident(self):
        lab, report = self.execute()
        expected = ["arm", "inject", "approve", "held", "backup", "release", "recovered", "counter",
                    "stop", "maintenance", "final-counter", "restore-copy", "restored-copy",
                    "remove-maintenance", "start", "check", "accept-executor-a", "check", "accept-verifier-1",
                    "check", "accept-planner-1", "target-restore", "settle", "fresh-begin", "fresh-inject",
                    "fresh-recovered"]
        index = 0
        for item in lab.seen:
            if index < len(expected) and item == expected[index]:
                index += 1
        self.assertEqual(index, len(expected), lab.seen)
        self.assertEqual(set(lab.cleared), set(drills.WORKERS))
        self.assertEqual(report["rpo_lost_commits"], 90)
        self.assertEqual(report["detection_log_seconds"], 2)  # The log can precede the Pod Ready condition.
        self.assertEqual(report["detection_seconds"], 4)  # Readiness 503 is proven by the later probe.
        self.assertEqual(report["restore_check"]["exit_code"], 1)
        self.assertIn("replay:", report["restore_check"]["listing"])
        self.assertIn("review:", report["restore_check"]["listing"])
        self.assertTrue(report["measured"])
        self.assertEqual(report["fresh_incident"][0]["incident_id"], "INC-FRESH")
        for command in lab.commands:
            self.assertIn("--previous-commit", command)
            self.assertEqual(command[command.index("--previous-commit") + 1], "100")
            self.assertEqual(command.count("--journal"), len(drills.WORKERS))

    def test_refuses_every_other_context_before_mutating(self):
        for context in ("kind-other", "kind-sentinel-lab", "sentinel-dist", ""):
            lab = RestoreLab(self.directory)
            lab.shell.context = context
            with self.subTest(context=context), self.assertRaises(ka.AcceptanceError):
                drills.restore_drill(lab, None, None, {}, self.directory)
            self.assertEqual(lab.seen, [])

    def test_check_exit_two_and_unexpected_zero_never_clear_workers(self):
        for code in (0, 2, 7):
            lab = RestoreLab(self.directory, bad_exit=code)
            run = live.Run(lab, ka.SCENARIOS["S0"], 402, "v1")
            run.since, run.offset = at(-1), {"offset_seconds": 0}
            report = {"baseline": {}, "policy": {}}
            with self.subTest(code=code), self.assertRaises((ka.AcceptanceError, KeyError)):
                drills.restore_drill(lab, run, drills.Steps(lab, report), report, self.directory)
            self.assertEqual(lab.cleared, [])
            self.assertLess(lab.seen.index("remove-maintenance"), lab.seen.index("start"))
            self.assertIn("check", lab.seen)

    def test_successful_check_with_no_rollback_is_not_a_restore_drill_pass(self):
        lab = RestoreLab(self.directory)
        checked = check_report(executor_missing=False, verifier_missing=False, rolled_back=False)
        lab.shell.run = unittest.mock.Mock(return_value=answer(json.dumps(checked), 0))
        run = live.Run(lab, ka.SCENARIOS["S0"], 402, "v1")
        run.since, run.offset = at(-1), {"offset_seconds": 0}
        report = {"baseline": {}, "policy": {}}
        with self.assertRaisesRegex(ka.AcceptanceError, "did not confirm acknowledged work lost"):
            drills.restore_drill(lab, run, drills.Steps(lab, report), report, self.directory)
        self.assertEqual(report["restore_check"]["exit_code"], 0)
        self.assertEqual(lab.cleared, [])

    def test_outer_restore_pass_requires_all_measurements_and_settle(self):
        lab = RestoreLiveLab(self.directory)
        report = drills.live_drill(lab, "restore", {"policy_version": "v1"}, {}, {})
        self.assertTrue(report["passed"], report["failures"])
        self.assertTrue(report["settle"]["settled"])
        self.assertTrue(report["maintenance_removed"])
        self.assertTrue(report["coordinator_restarted"])
        self.assertNotIn("manual_recovery", report)

    def test_outer_restore_failure_records_checker_exit_and_manual_recovery(self):
        lab = RestoreLiveLab(self.directory, bad_exit=2)
        report = drills.live_drill(lab, "restore", {"policy_version": "v1"}, {}, {})
        self.assertFalse(report["passed"])
        self.assertEqual(report["restore_checks"][0]["exit_code"], 2)
        self.assertEqual(lab.cleared, [])
        self.assertIn("manual_recovery", report)
        self.assertIn("target-restore", lab.seen)
        self.assertIn("settle", lab.seen)

    def test_detection_log_and_503_both_required(self):
        for options in ({"detect": False}, {"probe_status": 200}, {"claim_after": True}):
            lab = RestoreLab(self.directory, **options)
            lab.restored_at = 0
            lab.sleep(4)
            report = {}
            with self.subTest(options=options), self.assertRaises((TimeoutError, ka.AcceptanceError)):
                drills.detection(lab, at(-1), at(0), {"generation": "gen", "commit_counter": 10},
                                 drills.Steps(lab, report), report, timeout=3)
            self.assertEqual(lab.cleared, [])

    def test_copy_error_terminates_maintenance_before_starting_writer(self):
        lab = RestoreLab(self.directory)
        original = lab.maintenance_exec

        def fail(name, script, *args, **kwargs):
            if script == drills.RESTORE_SCRIPT:
                raise subprocess.TimeoutExpired("fixture", 120)
            return original(name, script, *args, **kwargs)
        lab.maintenance_exec = fail
        run = live.Run(lab, ka.SCENARIOS["S0"], 402, "v1")
        report = {"baseline": {}, "policy": {}}
        with self.assertRaises(subprocess.TimeoutExpired):
            drills.restore_drill(lab, run, drills.Steps(lab, report), report, self.directory)
        self.assertLess(lab.seen.index("remove-maintenance"), lab.seen.index("start"))
        self.assertEqual(lab.cleared, [])

    def test_helper_delete_failure_keeps_coordinator_stopped(self):
        lab = RestoreLab(self.directory)
        lab.remove_maintenance = unittest.mock.Mock(side_effect=ka.AcceptanceError("still terminating"))
        run = live.Run(lab, ka.SCENARIOS["S0"], 402, "v1")
        report = {"baseline": {}, "policy": {}}
        with self.assertRaisesRegex(ka.AcceptanceError, "still terminating"):
            drills.restore_drill(lab, run, drills.Steps(lab, report), report, self.directory)
        self.assertNotIn("start", lab.seen)
        self.assertEqual(lab.cleared, [])

    def test_review_only_stops_without_clearing_any_worker(self):
        lab = RestoreLab(self.directory)
        checked = {"report": check_report(executor_missing=False), "exit_code": 1}
        report = {}
        with self.assertRaisesRegex(ka.AcceptanceError, "manual review"):
            drills.check_replays(lab, self.directory, [], {"generation": "gen", "commit_counter": 100},
                                 checked, drills.Steps(lab, report), report)
        self.assertEqual(lab.cleared, [])

    def test_detection_failure_still_runs_check_but_never_accepts(self):
        lab = RestoreLab(self.directory, detect=False)
        run = live.Run(lab, ka.SCENARIOS["S0"], 402, "v1")
        run.since, run.offset = at(-1), {"offset_seconds": 0}
        report = {"baseline": {}, "policy": {}}
        with self.assertRaisesRegex(ka.AcceptanceError, "no workers cleared"):
            drills.restore_drill(lab, run, drills.Steps(lab, report), report, self.directory)
        self.assertIn("check", lab.seen)
        self.assertEqual(report["restore_check"]["exit_code"], 1)
        self.assertEqual(lab.cleared, [])

    def test_actual_checker_exit_one_and_unreadable_input_exit_two(self):
        lab = RestoreLab(self.directory)
        # Shell.run executes only Python here; no cluster command is constructed.
        lab.shell = ka.Shell(ka.CONTEXT)
        database = self.directory / "restored.db"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE store_meta(id INTEGER, generation TEXT, commit_counter INTEGER)")
            connection.execute("INSERT INTO store_meta VALUES(1, 'gen', 10)")
        report = {}
        checked = drills.run_check(lab, self.directory, [], {"generation": "gen", "commit_counter": 100},
                                   drills.Steps(lab, report), restored=database)
        self.assertEqual(checked["exit_code"], 1)
        self.assertEqual(checked["report"]["lost_commits"], 90)
        self.assertIn("90 commits behind", checked["listing"])
        with self.assertRaisesRegex(ka.AcceptanceError, "exit 2"):
            drills.run_check(lab, self.directory, [], {"generation": "gen", "commit_counter": 100},
                             drills.Steps(lab, report), restored=self.directory / "missing.db")


class MaintenanceShell:
    context = ka.CONTEXT

    def __init__(self):
        self.replicas, self.pods, self.calls, self.created = 1, ["old-coordinator"], [], None
        self.deployment = {"spec": {"replicas": 1, "template": {"spec": {
            "containers": [{"name": "coordinator", "image": "sentinel-coordinator:test"}],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "sentinel-dist-coordinator-data"}}]}}}}

    def kubectl(self, *args, **options):
        self.calls.append((args, options))
        if args[0] == "scale":
            self.replicas = int(next(value.split("=")[1] for value in args if value.startswith("--replicas=")))
            self.pods = []
        return answer("")

    def json(self, *args, **options):
        self.calls.append((args, options))
        if args[:2] == ("get", "deployment"):
            value = deepcopy(self.deployment)
            value["spec"]["replicas"] = self.replicas
            return value
        return {"items": list(self.pods)}

    def create(self, document, **options):
        self.created = document
        self.calls.append((("create",), options))


class MaintenanceTests(DirectoryTest):
    def test_debug_access_logging_is_a_required_positive_control(self):
        shell = MaintenanceShell()
        shell.deployment["spec"]["strategy"] = {"type": "Recreate"}
        lab = drills.DrillLab(shell, self.directory)
        with self.assertRaisesRegex(ka.AcceptanceError, "SENTINEL_LOG_LEVEL=DEBUG"):
            lab.require_debug()
        self.assertFalse(any(args[0] in {"exec", "scale", "create"} for args, _ in shell.calls))

    def test_zero_replicas_and_no_pods_are_required_before_restore_exec(self):
        shell = MaintenanceShell()
        lab = drills.DrillLab(shell, self.directory)
        with self.assertRaisesRegex(ka.AcceptanceError, "replicas changed"):
            lab.maintenance_exec("p402-restore-test", drills.RESTORE_SCRIPT, drills.BACKUP, drills.DATABASE)
        self.assertFalse(any(args[0] == "exec" for args, _ in shell.calls))
        shell.replicas = 0
        with self.assertRaisesRegex(ka.AcceptanceError, "Pod appeared"):
            lab.maintenance_exec("p402-restore-test", drills.RESTORE_SCRIPT)

    def test_stop_mount_restore_delete_start_order_and_namespace(self):
        shell = MaintenanceShell()
        lab = drills.DrillLab(shell, self.directory)
        lab.stop_coordinator()
        lab.maintenance("p402-restore-test")
        lab.maintenance_exec("p402-restore-test", drills.RESTORE_SCRIPT, drills.BACKUP, drills.DATABASE)
        lab.remove_maintenance("p402-restore-test")
        self.assertTrue(all(options.get("namespace") == ka.NAMESPACE for _, options in shell.calls))
        self.assertEqual(shell.calls[0][0][0], "scale")
        document = shell.created
        self.assertFalse(document["spec"]["automountServiceAccountToken"])
        self.assertEqual(document["spec"]["nodeSelector"], {"sentinel.io/host": "c"})
        self.assertEqual(len(document["spec"]["volumes"]), 1)
        self.assertNotIn("env", document["spec"]["containers"][0])
        self.assertIn("?mode=ro", drills.BACKUP_SCRIPT)

    def test_unexpected_pvc_refused_before_creating_helper(self):
        shell = MaintenanceShell()
        shell.replicas, shell.pods = 0, []
        shell.deployment["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "other"
        lab = drills.DrillLab(shell, self.directory)
        with self.assertRaisesRegex(ka.AcceptanceError, "Unexpected Coordinator PVC"):
            lab.maintenance("p402-restore-test")
        self.assertIsNone(shell.created)

    def test_another_pvc_writer_refused(self):
        shell = MaintenanceShell()
        shell.replicas = 0
        shell.pods = [{"metadata": {"name": "earlier-maintenance"}, "spec": {
            "volumes": [{"persistentVolumeClaim": {"claimName": "sentinel-dist-coordinator-data"}}]}}]
        lab = drills.DrillLab(shell, self.directory)
        with self.assertRaisesRegex(ka.AcceptanceError, "Another Pod mounts"):
            lab.exclusive_volume()

    def test_backup_and_restore_scripts_use_consistent_sqlite_snapshots(self):
        source, backup = self.directory / "live.db", self.directory / "backup.db"
        with closing(sqlite3.connect(source)) as connection, connection:
            connection.execute("CREATE TABLE store_meta(id INTEGER, generation TEXT, commit_counter INTEGER)")
            connection.execute("INSERT INTO store_meta VALUES(1, 'gen', 10)")
        result = subprocess.run([sys.executable, "-c", drills.BACKUP_SCRIPT, str(source), str(backup)],
                                capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(json.loads(result.stdout), {"generation": "gen", "commit_counter": 10})
        with closing(sqlite3.connect(source)) as connection, connection:
            connection.execute("UPDATE store_meta SET commit_counter=100")
        result = subprocess.run([sys.executable, "-c", drills.RESTORE_SCRIPT, str(backup), str(source)],
                                capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(json.loads(result.stdout), {"generation": "gen", "commit_counter": 10})


if __name__ == "__main__":
    unittest.main()
