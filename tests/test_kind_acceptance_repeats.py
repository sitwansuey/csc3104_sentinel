"""Repeated runs in one sentinel-dist deployment are independent of each other (P4.01).

The Coordinator, the Observer and the workers keep their stores (PVCs) across
the runs of a deployment. On 13 Nov 2026 the first S0 run passed and the S0
runs after it in the same deployment each ended in about 15 s with no
incident: the store still held the first run's RECOVERED incident, so the
harness took every incident to be terminal at once and restored the target
before the Observer's 15 s failure window could open a new one
(docs/distributed-acceptance.md#first-live-attempts). These tests drive the
real Lab's waiting, approval, settling and scoping over a local Coordinator
database whose incidents a scripted world changes as virtual time passes.
"""
from datetime import UTC, datetime, timedelta
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from experiments.distributed import audit
from experiments.distributed import kind_acceptance as acceptance
from experiments.distributed import kind_acceptance_live as live
from services.executor.store import fingerprint
from services.observer.core import Observer

BASE = datetime(2026, 11, 13, 13, 0, tzinfo=UTC)
TEMPLATE = {"metadata": {"labels": {"app": "demo-api"}},
            "spec": {"containers": [{"name": "demo-api", "image": "docker.io/library/sentinel-demo@sha256:" + "a" * 64,
                                     "env": [{"name": "STARTUP_FAILURE", "value": "false"}]}]}}
FAULTED = {"metadata": {"labels": {"app": "demo-api"}},
           "spec": {"containers": [{"name": "demo-api", "image": "docker.io/library/sentinel-demo@sha256:" + "a" * 64,
                                    "env": [{"name": "STARTUP_FAILURE", "value": "true"}]}]}}
BASELINE = {"target": {"uid": "uid-demo"}, "template_sha256": fingerprint(TEMPLATE), "pod_template": TEMPLATE,
            "healthy_observed_at": "2026-11-13T12:55:00Z"}
POLICY = {"policy_version": "v1", "baseline_constraints": {"max_age_seconds": 14400}}
SCHEMA = """
    CREATE TABLE incidents(incident_id TEXT PRIMARY KEY, state TEXT, active_action_id TEXT);
    CREATE TABLE actions(action_id TEXT PRIMARY KEY, incident_id TEXT, state TEXT);
    CREATE TABLE transitions(seq INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, action_id TEXT,
                             from_state TEXT, to_state TEXT, cause TEXT, recorded_at TEXT);
    CREATE TABLE target_ownership(uid TEXT, epoch INTEGER, holder TEXT, seal_required INTEGER, sealed_epoch INTEGER);
"""


def audit_rows():
    """One operator injection and one Sentinel template write (generations 2-4), as in the other run tests."""
    fixture = Path(__file__).resolve().parent / "fixtures" / "distributed_audit" / "one-write.jsonl"
    events, _ = audit.parse(fixture.read_text(encoding="utf-8"))
    return audit.extract(events)


class StoreLab(acceptance.Lab):
    """The real Lab over local Coordinator and Observer databases, a scripted target and virtual time.

    events are (seconds after the injection, function(lab)): what the Observer,
    the Coordinator and the Deployment controller do while the harness waits.
    With operator set, each sleep lets the operator approve what waits for
    approval (the approver thread, made deterministic); approve() stands in
    for the approval API and hands the decision to on_approve.
    """

    def __init__(self, directory, events=(), *, operator=True, on_approve=None):
        self.now = 0.0
        super().__init__(None, directory, clock=lambda: BASE + timedelta(seconds=self.now), sleep=self.advance,
                         monotonic=lambda: self.now)
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.database = Path(directory) / "coordinator.sqlite3"
        self.observer_database = Path(directory) / "observer.sqlite3"
        self.execute_script(SCHEMA)
        self.execute("CREATE TABLE observer_outbox(incident_id TEXT PRIMARY KEY, delivered INTEGER)",
                     database=self.observer_database)
        self.events, self.injected_at, self.operator = sorted(events, key=lambda item: item[0]), None, operator
        self.on_approve = on_approve
        self.generation, self.template, self.healthy = 1, TEMPLATE, True
        self.rows = audit_rows()

    # The world -----------------------------------------------------------------------------------------

    def advance(self, seconds):
        self.now += seconds
        while self.events and self.injected_at is not None and self.now >= self.injected_at + self.events[0][0]:
            self.events.pop(0)[1](self)
        if self.operator:
            self.approve_pending("v1")

    def execute(self, sql, parameters=(), *, database=None):
        connection = sqlite3.connect(database or self.database)
        try:
            with connection:
                connection.execute(sql, parameters)
        finally:
            connection.close()

    def execute_script(self, script):
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(script)
        finally:
            connection.close()

    def detect(self, incident_id):
        """The Observer opens an incident into its outbox, not yet delivered (the Coordinator is unreachable)."""
        self.execute("INSERT INTO observer_outbox VALUES(?,0)", (incident_id,), database=self.observer_database)

    def open_incident(self, incident_id, at=None):
        """The Observer delivers an incident and the Coordinator journals it OPEN."""
        at = at or acceptance.stamp(self.clock())
        self.execute("INSERT INTO incidents VALUES(?,?,NULL)", (incident_id, "OPEN"))
        self.execute("INSERT INTO transitions(incident_id,action_id,from_state,to_state,cause,recorded_at) "
                     "VALUES(?,NULL,NULL,'OPEN','incident',?)", (incident_id, at))
        self.execute("INSERT INTO observer_outbox VALUES(?,1) ON CONFLICT(incident_id) DO UPDATE SET delivered=1",
                     (incident_id,), database=self.observer_database)

    def move(self, incident_id, action_id, state, at=None):
        at = at or acceptance.stamp(self.clock())
        (before,) = self.query("SELECT state FROM incidents WHERE incident_id=?", (incident_id,))
        self.execute("INSERT OR IGNORE INTO actions VALUES(?,?,?)", (action_id, incident_id, state))
        self.execute("UPDATE actions SET state=? WHERE action_id=?", (state, action_id))
        self.execute("UPDATE incidents SET state=?, active_action_id=? WHERE incident_id=?",
                     (state, action_id, incident_id))
        self.execute("INSERT INTO transitions(incident_id,action_id,from_state,to_state,cause,recorded_at) "
                     "VALUES(?,?,?,?,'step',?)", (incident_id, action_id, before["state"], state, at))

    # The Lab's cluster primitives ----------------------------------------------------------------------

    def query(self, sql, parameters=(), *, strict=False, identity="coordinator"):
        connection = sqlite3.connect(self.database if identity == "coordinator" else self.observer_database)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(sql, parameters)]
        finally:
            connection.close()

    def target(self):
        ready = 1 if self.healthy else 0
        return {"metadata": {"uid": "uid-demo", "generation": self.generation, "resourceVersion": "1"},
                "spec": {"replicas": 1, "template": self.template},
                "status": {"observedGeneration": self.generation, "replicas": 1, "updatedReplicas": 1,
                           "readyReplicas": ready, "availableReplicas": ready}}

    def inject(self):
        self.injected_at = self.now
        self.generation, self.template, self.healthy = 2, FAULTED, False
        return {"operator_action": "inject STARTUP_FAILURE=true", "at": acceptance.stamp(self.clock())}

    def rollback(self):
        """Sentinel's rollback: the baseline template back (generation 4 in the audit rows)."""
        self.generation, self.template = 4, TEMPLATE

    def restore(self, baseline):
        if fingerprint(self.template) == baseline["template_sha256"]:
            return {"operator_restore": False}
        self.generation, self.template, self.healthy = self.generation + 1, TEMPLATE, True
        return {"operator_restore": True, "at": acceptance.stamp(self.clock())}

    def approve(self, action_id, policy_version):
        self.approved.add(action_id)
        self.approvals.append({"action_id": action_id, "at": acceptance.stamp(self.clock())})
        if self.on_approve:
            self.on_approve(self, action_id)

    def clock_offset(self):
        return {"offset_seconds": 0.0, "uncertainty_seconds": 0.01, "at": acceptance.stamp(self.clock())}

    def copy_database(self, identity, path, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        if identity in {"coordinator", "observer-1"}:
            shutil.copyfile(self.database if identity == "coordinator" else self.observer_database, destination)
            return True
        return False

    def logs(self, identity, since, destination, *, prefix=""):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(prefix, encoding="utf-8")

    def audit_log(self, since, until):
        return {"rows": self.rows, "skipped": 0, "first_at": "2026-11-13T09:00:00Z", "files": 1}


def earlier_recovered_run(lab):
    """What the first S0 run of 13 Nov 2026 left in the store: one RECOVERED incident, delivered by the Observer."""
    lab.open_incident("INC-EARLIER", at="2026-11-13T12:01:32Z")
    lab.move("INC-EARLIER", "ACT-EARLIER", "RECOVERED", at="2026-11-13T12:02:05Z")


def recovers(incident="INC-NEW", action="ACT-1", *, pod_less=2):
    """The Observer opens this run's incident 16 s after the injection; Sentinel rolls back and verifies it.

    pod_less is how long the rollback's Recreate rollout leaves the target without a ready Pod.
    """
    return [(16, lambda world: world.open_incident(incident)),
            (18, lambda world: world.move(incident, action, "VALIDATING")),
            (30, lambda world: (world.rollback(), world.move(incident, action, "VERIFYING"))),
            (30 + pod_less, lambda world: setattr(world, "healthy", True)),
            (45, lambda world: world.move(incident, action, "RECOVERED"))]


class RepeatedRunTests(unittest.TestCase):
    def setUp(self):
        self.tokens = {"executor-a": {"delivery-token": "q" * 43}}
        self.deployed = {"policy_version": "v1", "versions": {}, "api_server": {"endpoint": "172.18.0.4"}}
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        patcher = patch.object(live, "git_revision", lambda: "abc123")
        patcher.start()
        self.addCleanup(patcher.stop)
        # The operator is the deterministic one in StoreLab.advance, not a thread on the wall clock.
        operator = patch.object(live, "_operator", lambda lab, version, stopping, record: None)
        operator.start()
        self.addCleanup(operator.stop)

    def run_s0(self, lab, out="out"):
        return live.run_one(lab, acceptance.SCENARIOS["S0"], 1, self.deployed, BASELINE, self.tokens,
                            self.directory / out)  # run() passes the policy; the age check is tested below.

    def test_a_run_waits_for_its_own_incident_when_the_store_holds_an_earlier_recovered_one(self):
        """The 13 Nov 2026 symptom: states [] in about 15 s, X3 failing on the earlier run's delivered incident."""
        lab = StoreLab(self.directory / "lab", recovers())
        earlier_recovered_run(lab)
        summary = self.run_s0(lab)
        self.assertEqual(summary["states"], ["RECOVERED"], summary["expectation"])
        self.assertEqual([item["incident_id"] for item in summary["incidents"]], ["INC-NEW"])
        self.assertGreaterEqual(summary["duration_seconds"], 45)  # It waited for the Observer and the recovery.
        self.assertEqual(summary["operator_actions"][1:], [{"operator_restore": False}])
        results = {item["id"]: item for item in summary["invariants"]}
        self.assertTrue(results["X3"]["passed"], results["X3"]["detail"])
        self.assertTrue(results["I3"]["passed"], results["I3"]["detail"])
        self.assertTrue(summary["expectation"]["met"], summary["expectation"]["failures"])
        self.assertEqual(summary["earlier_incidents"], 1)
        self.assertTrue(summary["settle"]["settled"])
        self.assertTrue(summary["passed"], summary)

    def test_without_its_own_incident_a_run_times_out_rather_than_passing_on_an_earlier_one(self):
        lab = StoreLab(self.directory / "lab", [])  # The Observer never opens an incident for this fault.
        earlier_recovered_run(lab)
        summary = self.run_s0(lab)
        self.assertFalse(summary["terminal_in_time"])
        self.assertEqual(summary["states"], [])
        self.assertGreaterEqual(summary["duration_seconds"], acceptance.DEADLINE_SECONDS)
        self.assertFalse(summary["passed"])

    def test_waiting_for_a_state_ignores_earlier_incidents(self):
        """S3, S4 and S5 wait for VERIFYING or a terminal state; an earlier RECOVERED incident must not end that."""
        lab = StoreLab(self.directory / "lab", [])
        earlier_recovered_run(lab)
        lab.begin_run(BASELINE, POLICY)
        with self.assertRaises(TimeoutError):
            lab.wait_state({"VERIFYING", *acceptance.invariants.TERMINAL}, 30)
        self.assertFalse(lab.terminal())
        lab.open_incident("INC-NEW")
        lab.move("INC-NEW", "ACT-1", "AWAITING_APPROVAL")
        self.assertEqual(lab.wait_state({"AWAITING_APPROVAL"}, 5)["incident_id"], "INC-NEW")

    def test_an_incident_the_rollback_rollout_opens_ends_inside_the_run(self):
        """Sentinel's own Recreate rollout leaves no Pod for 20 s; the Observer opens a second incident."""
        # The Guard refuses the second incident's rollback: the target is already at its baseline.
        events = recovers(pod_less=20) + [(47, lambda world: world.open_incident("INC-ROLLOUT")),
                                          (49, lambda world: world.move("INC-ROLLOUT", "ACT-2", "REJECTED"))]
        lab = StoreLab(self.directory / "lab", events)
        summary = self.run_s0(lab)
        self.assertEqual([(item["incident_id"], item["state"]) for item in summary["incidents"]],
                         [("INC-NEW", "RECOVERED"), ("INC-ROLLOUT", "REJECTED")])
        self.assertTrue(summary["settle"]["settled"])
        self.assertTrue(summary["passed"], summary)
        # The next run starts from a settled lab, counts neither incident and waits for its own.
        lab.events = recovers("INC-AGAIN", "ACT-3")
        second = self.run_s0(lab, out="second")
        self.assertEqual(second["earlier_incidents"], 2)
        self.assertEqual([(item["incident_id"], item["state"]) for item in second["incidents"]],
                         [("INC-AGAIN", "RECOVERED")])

    def test_an_incident_escalated_before_the_restore_is_decided_by_the_operator_while_settling(self):
        """The 13 Nov 2026 leftover: an incident AWAITING_APPROVAL when the target was put back."""
        def stale(world, action_id):  # The Guard re-checks live state after the approval: the target moved.
            world.move("INC-LATE", action_id, "REJECTED")
        events = [(16, lambda world: world.open_incident("INC-NEW")),
                  (40, lambda world: world.move("INC-NEW", "ACT-1", "FAILED"))]
        lab = StoreLab(self.directory / "lab", events, operator=False, on_approve=stale)
        restore = StoreLab.restore

        def escalate_then_restore(world, baseline):
            world.open_incident("INC-LATE")  # Opened, planned and escalated on the faulted template.
            world.move("INC-LATE", "ACT-2", "AWAITING_APPROVAL")
            world.operator = True  # The operator is still approving while the lab settles.
            return restore(world, baseline)
        with patch.object(StoreLab, "restore", escalate_then_restore):
            summary = self.run_s0(lab)
        self.assertTrue(summary["settle"]["settled"], summary["settle"])
        self.assertEqual(summary["states"], ["FAILED", "REJECTED"])
        inject, restored, approval = summary["operator_actions"]
        self.assertEqual(inject["operator_action"], "inject STARTUP_FAILURE=true")
        self.assertTrue(restored["operator_restore"])
        self.assertEqual((approval["operator_action"], approval["action_id"]), ("approve", "ACT-2"))

    def test_an_incident_the_observer_has_not_delivered_holds_the_settle_and_refuses_the_next_run(self):
        """Delivered later, it would reach the Coordinator during the next run and count as that run's."""
        events = recovers() + [(46, lambda world: world.detect("INC-UNDELIVERED"))]
        lab = StoreLab(self.directory / "lab", events)
        summary = self.run_s0(lab)
        self.assertFalse(summary["settle"]["settled"])
        self.assertEqual(summary["settle"]["open"], ["INC-UNDELIVERED: not yet delivered by the Observer"])
        self.assertFalse(summary["passed"])
        with self.assertRaisesRegex(acceptance.AcceptanceError, "not yet delivered incident INC-UNDELIVERED"):
            self.run_s0(lab, out="second")
        # Once the Observer delivers it and it ends, the next run may start and leaves it out.
        lab.open_incident("INC-UNDELIVERED")
        lab.move("INC-UNDELIVERED", "ACT-9", "REJECTED")
        lab.events = recovers("INC-AGAIN", "ACT-3")
        third = self.run_s0(lab, out="third")
        self.assertEqual([item["incident_id"] for item in third["incidents"]], ["INC-AGAIN"])
        self.assertTrue(third["passed"], third)

    def test_a_run_that_leaves_an_incident_waiting_does_not_pass_and_the_next_run_is_refused(self):
        events = recovers() + [(46, lambda world: world.open_incident("INC-LATE")),
                                   (47, lambda world: world.move("INC-LATE", "ACT-2", "AWAITING_APPROVAL"))]
        lab = StoreLab(self.directory / "lab", events, operator=False)
        summary = self.run_s0(lab)
        self.assertFalse(summary["settle"]["settled"])
        self.assertEqual(summary["settle"]["open"], ["INC-LATE: AWAITING_APPROVAL"])
        self.assertTrue(summary["expectation"]["met"])  # Its own incident recovered, but the lab was left unsettled.
        self.assertFalse(summary["passed"])
        with self.assertRaisesRegex(acceptance.AcceptanceError, "INC-LATE is AWAITING_APPROVAL.*planning_blocker"):
            self.run_s0(lab, out="second")


class SettledTests(unittest.TestCase):
    def deployment(self, template=TEMPLATE, **status):
        values = {"observedGeneration": 4, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1,
                  "availableReplicas": 1, **status}
        return {"metadata": {"generation": 4}, "spec": {"replicas": 1, "template": template}, "status": values}

    def test_a_settled_lab_may_run(self):
        incidents = [{"incident_id": "INC-1", "state": "RECOVERED"}, {"incident_id": "INC-2", "state": "REJECTED"}]
        ownership = [{"epoch": 3, "seal_required": 0, "sealed_epoch": 2}]
        self.assertEqual(acceptance.run_unsettled(incidents, ownership, self.deployment(), BASELINE, policy=POLICY,
                                                  now=BASE), [])

    def test_what_an_earlier_run_left_behind_refuses_a_new_run(self):
        cases = {
            "earlier incident INC-1 is AWAITING_APPROVAL": ([{"incident_id": "INC-1", "state": "AWAITING_APPROVAL"}],
                                                           [], self.deployment(), BASE),
            "the Observer has not yet delivered incident INC-2": ([], [], self.deployment(), BASE),
            "a seal is still required on the target (epoch 3)": ([], [{"epoch": 3, "seal_required": 1}],
                                                                 self.deployment(), BASE),
            "the target: its template is not the baseline's": ([], [], self.deployment(FAULTED), BASE),
            "the target: not every one of its 1 replica(s)": ([], [], self.deployment(availableReplicas=0), BASE),
            "the target: its latest generation is not observed": ([], [], self.deployment(observedGeneration=3),
                                                                  BASE),
            "would be older than the policy's max_age_seconds": ([], [], self.deployment(),
                                                                BASE + timedelta(hours=3, minutes=55)),
        }
        for expected, (incidents, ownership, deployment, now) in cases.items():
            with self.subTest(expected=expected):
                undelivered = ["INC-2"] if "delivered" in expected else []
                problems = acceptance.run_unsettled(incidents, ownership, deployment, BASELINE, policy=POLICY,
                                                    now=now, undelivered=undelivered)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(expected, problems[0])

    def test_the_coordinator_must_be_readable_to_begin_a_run(self):
        class NoPods:
            context = acceptance.CONTEXT

            def json(self, *args, namespace=None):
                return {"items": []}
        now = [0.0]
        lab = acceptance.Lab(NoPods(), tempfile.gettempdir(), sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                             monotonic=lambda: now[0])
        with self.assertRaises(acceptance.TransientRead):  # A Coordinator that is down never reads as nothing.
            lab.query("SELECT 1")
        with self.assertRaisesRegex(acceptance.ReadUnavailable, "Coordinator's database could not be read"):
            lab.begin_run(BASELINE, POLICY)  # Before a run, it must not read as "no earlier incident".
        self.assertGreaterEqual(now[0], acceptance.READ_RETRY_SECONDS)  # Asked again until its bound, not once.
        with self.assertRaisesRegex(acceptance.AcceptanceError, "Observer's database could not be read"):
            lab.undelivered(strict=True)  # Nor may an unreadable Observer outbox read as empty.


class CrossRunRecordTests(unittest.TestCase):
    def test_x3_compares_the_observers_outbox_with_every_recorded_incident(self):
        records = {"transitions": [
            {"seq": 1, "incident_id": "INC-EARLIER", "from_state": None, "to_state": "OPEN",
             "recorded_at": "2026-11-13T12:01:32Z"},
            {"seq": 2, "incident_id": "INC-NEW", "from_state": None, "to_state": "OPEN",
             "recorded_at": "2026-11-13T13:00:16Z"}],
            "incidents": [{"incident_id": "INC-EARLIER", "state": "RECOVERED"},
                          {"incident_id": "INC-NEW", "state": "RECOVERED"}],
            "observer_outbox": [{"incident_id": "INC-EARLIER", "delivered": 1},
                                {"incident_id": "INC-NEW", "delivered": 1}]}
        scoped = acceptance.scope_records(records, "2026-11-13T13:00:00Z", {"INC-EARLIER"})
        self.assertEqual([row["incident_id"] for row in scoped["incidents"]], ["INC-NEW"])
        self.assertTrue(acceptance.invariants.x3(scoped)["passed"])
        lost = dict(scoped, observer_outbox=[*scoped["observer_outbox"], {"incident_id": "INC-LOST", "delivered": 1}])
        self.assertIn("INC-LOST: acknowledged to the Observer but not recorded",
                      acceptance.invariants.x3(lost)["detail"])
        # File mode passes no recorded_incident_ids and keeps comparing with the incidents it has.
        self.assertFalse(acceptance.invariants.x3({"incidents": [], "observer_outbox": [
            {"incident_id": "INC-NEW", "delivered": 1}]})["passed"])

    def test_an_earlier_incident_is_never_counted_however_close_the_clocks_put_it(self):
        records = {"transitions": [{"seq": 1, "incident_id": "INC-EARLIER", "from_state": None, "to_state": "OPEN",
                                    "recorded_at": "2026-11-13T13:00:00.500000Z"}],
                   "incidents": [{"incident_id": "INC-EARLIER", "state": "RECOVERED"}]}
        self.assertEqual(acceptance.scope_records(records, "2026-11-13T13:00:00Z")["incidents"],
                         records["incidents"])
        self.assertEqual(acceptance.scope_records(records, "2026-11-13T13:00:00Z", {"INC-EARLIER"})["incidents"], [])

    def test_s8_waits_for_a_seal_newer_than_the_ones_earlier_runs_left(self):
        earlier = [{"uid": "uid-demo", "epoch": 1, "seal_required": 0, "sealed_epoch": 1}]
        self.assertIsNone(live.newer_seal(earlier, 1))  # The earlier S8's seal is not this run's.
        self.assertIsNone(live.newer_seal([{**earlier[0], "epoch": 2, "seal_required": 1}], 1))  # Still sealing.
        sealed = [{**earlier[0], "epoch": 2, "sealed_epoch": 2}]
        self.assertEqual(live.newer_seal(sealed, 1), sealed[0])


def observation(at, generation, healthy):
    ready = 1 if healthy else 0
    return {"source": "kubernetes", "captured_at": acceptance.stamp(BASE + timedelta(seconds=at)),
            "cluster_id": "sentinel-dist",
            "deployment": {"kind": "Deployment",
                           "metadata": {"namespace": "sentinel-dist-workload", "name": "demo-api", "uid": "uid-demo",
                                        "resourceVersion": str(100 + at), "generation": generation},
                           "spec": {"replicas": 1},
                           "status": {"observedGeneration": generation, "readyReplicas": ready,
                                      "availableReplicas": ready}},
            "pods": [], "events": [], "logs": []}


class ObserverAcrossRunsTests(unittest.TestCase):
    """The Observer opens a new incident for a new fault on the same target after an earlier one ended."""

    def feed(self, observer, samples):
        incidents = []
        for at, generation, healthy in samples:
            incident = observer.observe(observation(at, generation, healthy))
            if incident:
                incidents.append(incident)
        return incidents

    def setUp(self):
        self.observer = Observer({"detection": {"failure_duration_seconds": 15}})

    def failing(self, start, generation, seconds):
        return [(start + step, generation, False) for step in range(0, seconds + 1, 2)]

    def test_a_new_fault_after_a_recovered_incident_opens_a_new_incident(self):
        first = self.feed(self.observer, self.failing(0, 2, 16))
        self.feed(self.observer, [(40, 4, True), (42, 4, True)])  # Recovered: healthy again.
        second = self.feed(self.observer, self.failing(60, 5, 16))
        self.assertEqual((len(first), len(second)), (1, 1))
        self.assertNotEqual(first[0]["incident_id"], second[0]["incident_id"])
        self.assertEqual(second[0]["observed_generation"], 5)

    def test_a_new_generation_opens_a_new_episode_without_a_healthy_sample_between(self):
        first = self.feed(self.observer, self.failing(0, 2, 16))
        second = self.feed(self.observer, self.failing(18, 3, 16))
        self.assertEqual(len(first) + len(second), 2)

    def test_a_fault_put_back_before_the_failure_window_opens_no_incident(self):
        """Runs 2-5 of 13 Nov 2026: restored about 13 s after the injection, so no incident could open."""
        self.assertEqual(self.feed(self.observer, self.failing(0, 5, 12) + [(14, 6, True)]), [])
        self.assertEqual(len(self.feed(self.observer, self.failing(20, 7, 16))), 1)


if __name__ == "__main__":
    unittest.main()
