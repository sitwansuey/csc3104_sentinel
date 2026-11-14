"""An approval or revocation POST that failed in transit is in doubt: reconciled from the store, never sent blind (P4.01).

On 14 Nov 2026 (deploy 13 at 0b5529f, S6 r1) the NetworkChaos that cuts the
Coordinator off from every worker was confirmed in effect, and S6's own
approval POST over the port-forward then failed with SSLEOFError. The
Coordinator's access log shows no create at all: the request never reached
it. S6 raised, its cleanup removed the partition 2 s in, the operator thread
approved afterwards and the run failed
(docs/distributed-acceptance.md#s6-r1-0b5529f-an-approval-post-cut-in-transit).
These tests drive the real Lab against a real Coordinator store (built from
the sample messages, read back through a fake kubectl exec running the
harness's own read-only query helper) and a fake approval client in front of
the real ApprovalAccess, whose transport fails as scripted before or after
the request arrives.
"""
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import ssl
import subprocess
import tempfile
import unittest

from experiments.distributed import kind_acceptance as acceptance
from experiments.distributed import kind_acceptance_live as live
from runtime.service_identity import ProtocolError
from services.coordinator.approvals import ApprovalAccess
from services.coordinator.core import LocalEventBus, handle
from services.coordinator.store import CoordinatorError, SQLiteStore

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 10, 10, 12, 0, 36, tzinfo=UTC)  # Just after the sample Guard decision.
CONTAINER_GONE = 'error: unable to upgrade connection: container not found ("coordinator")'


def sample(name):
    return json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8"))


PROPOSAL = sample("messages/proposal.json")
ACTION = PROPOSAL["action_id"]
POLICY = sample("messages/guard-result.json")["policy_version"]


def done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class StoreKubectl:
    """kubectl whose exec runs the harness's query helper read-only against the Coordinator's real store.

    failing is how many execs from now on fail transiently (the Pod not
    answering); forever makes every one fail. Every exec is counted.
    """

    def __init__(self, database):
        self.database, self.failing, self.forever, self.execs = database, 0, False, 0

    def __call__(self, command, **options):
        if "get" in command and "pods" in command:
            app = next(part for part in command if part.startswith("--selector=app=")).split("=", 2)[2]
            return done(json.dumps({"items": [{"metadata": {"name": app + "-pod"}, "spec": {"nodeName": "n"},
                                               "status": {"phase": "Running"}}]}))
        if "exec" in command and acceptance.QUERY_HELPER in command:
            self.execs += 1
            if self.forever or self.failing:
                self.failing = max(0, self.failing - 1)
                return done(code=1, stderr=CONTAINER_GONE)
            sql, parameters = command[-2], json.loads(command[-1])
            connection = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                return done(json.dumps([dict(row) for row in connection.execute(sql, parameters)]))
            finally:
                connection.close()
        raise AssertionError("unexpected command: " + " ".join(command))


def lost():
    return ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")


class InDoubtLab(acceptance.Lab):
    """The real Lab over a real Coordinator store; the approval API's transport is scripted.

    script holds one outcome per POST, in turn ("ok" once it runs out):
    "ok" (arrives, answered), "lost" (SSL EOF before it reaches the
    Coordinator), "reset" (connection reset before it arrives), "reply_lost"
    (arrives and is recorded, the answer is cut by an SSL EOF), "timeout"
    (arrives, no answer within the client's timeout) or a ProtocolError (the
    approval API's own HTTP error answer). Each POST takes post_seconds of
    virtual time.
    """

    def __init__(self, directory):
        self.now = [0.0]
        self.database = Path(directory) / "coordinator.sqlite3"
        with SQLiteStore(self.database, clock=self.at) as store:
            bus = LocalEventBus()
            for message in (sample("messages/incident.json"), PROPOSAL, sample("messages/guard-result.json")):
                handle(message, store, bus)
        self.kubectl = StoreKubectl(self.database)
        super().__init__(acceptance.Shell("kind-sentinel-dist", runner=self.kubectl), directory, clock=self.at,
                         sleep=self.advance, monotonic=lambda: self.now[0])
        self.script, self.posts, self.forwards, self.post_seconds = [], [], [], 10.0
        self.tokens[acceptance.OPERATOR] = "t" * 43
        self.audit = []

    def at(self):
        return BASE + timedelta(seconds=self.now[0])

    def advance(self, seconds):
        self.now[0] += seconds

    def port_forward(self, port=18767, *, fresh=False):
        self.forwards.append(fresh)
        return port

    def approval_client(self, port):
        lab = self

        class Client:
            def post(self, operation, body):
                lab.posts.append((operation, dict(body)))
                lab.advance(lab.post_seconds)
                outcome = lab.script.pop(0) if lab.script else "ok"
                if isinstance(outcome, ProtocolError):
                    raise outcome
                if outcome == "lost":
                    raise lost()
                if outcome == "reset":
                    raise ConnectionResetError(104, "Connection reset by peer")
                answer = lab.serve(operation, body)
                if outcome == "reply_lost":
                    raise lost()
                if outcome == "timeout":
                    raise TimeoutError("timed out")
                return answer
        return Client()

    def serve(self, operation, body):
        """What the Coordinator's approval endpoint does with a request that reached it."""
        permissions = acceptance.approval_permissions(PROPOSAL["target"])
        with SQLiteStore(self.database, clock=self.at) as store:
            try:
                return ApprovalAccess(store, permissions).call(acceptance.OPERATOR, "operator", operation, body)
            except CoordinatorError:
                raise ProtocolError(409, "Approval conflicts with current lifecycle or binding") from None

    def audit_rows(self, since, until):
        return self.audit, 0

    def rows(self, sql, parameters=()):
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(sql, parameters).fetchall()
        finally:
            connection.close()

    def approvals_recorded(self):
        return self.rows("SELECT id FROM approvals WHERE action_id=?", (ACTION,))

    def revocations_recorded(self):
        return self.rows("SELECT approval_id FROM approval_revocations")


class Harness(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.lab = InDoubtLab(directory.name)

    def attempts(self, operation="create"):
        return [entry for entry in self.lab.approval_attempts if entry["operation"] == operation]


class ApprovalInDoubtTests(Harness):
    def test_an_eof_before_the_request_arrived_is_reconciled_as_not_recorded_and_sent_once_more(self):
        lab = self.lab
        lab.script = ["lost"]
        lab.approve(ACTION, POLICY)
        self.assertEqual([operation for operation, _ in lab.posts], ["create", "create"])
        self.assertEqual(lab.posts[0][1], lab.posts[1][1])  # The same request again: an exact retry, never a new one.
        self.assertEqual(len(lab.approvals_recorded()), 1)
        first, second = self.attempts()
        self.assertEqual((first["outcome"], first["error_type"], first["reconciled"]),
                         ("in_doubt", "SSLEOFError", "not_recorded"))
        self.assertEqual((second["outcome"], second["attempt"]), ("answered", 2))
        self.assertNotIn("reconciled", second)
        self.assertEqual(lab.forwards, [False, True])  # A fresh port-forward for the resend.
        self.assertIn(ACTION, lab.approved)
        self.assertEqual(lab.approvals[-1]["confirmed_by"], "answer")
        self.assertEqual(lab.in_doubt, {})

    def test_a_reset_connection_is_in_doubt_too(self):
        self.lab.script = ["reset"]
        self.lab.approve(ACTION, POLICY)
        self.assertEqual([entry["error_type"] for entry in self.attempts()[:1]], ["ConnectionResetError"])
        self.assertEqual(len(self.lab.posts), 2)
        self.assertEqual(len(self.lab.approvals_recorded()), 1)

    def test_an_eof_after_the_approval_was_recorded_is_not_sent_again(self):
        for outcome in ("reply_lost", "timeout"):
            with self.subTest(outcome=outcome):
                self.setUp()
                lab = self.lab
                lab.script = [outcome]
                answer = lab.approve(ACTION, POLICY)
                self.assertEqual(len(lab.posts), 1)  # Recorded: never sent twice.
                self.assertEqual(len(lab.approvals_recorded()), 1)
                [entry] = self.attempts()
                self.assertEqual((entry["outcome"], entry["reconciled"]), ("in_doubt", "recorded"))
                self.assertIn(ACTION, lab.approved)
                self.assertEqual(lab.approvals[-1]["confirmed_by"], "store")
                self.assertEqual(answer["approval_id"], lab.approvals_recorded()[0][0])

    def test_reconciliation_reads_through_a_pod_that_does_not_answer_for_a_while(self):
        lab = self.lab
        lab.script = ["reply_lost"]
        original = lab.serve

        def serve(operation, body):
            answer = original(operation, body)
            lab.kubectl.failing = 3  # The reads right after the cut fail, then answer.
            return answer
        lab.serve = serve
        lab.approve(ACTION, POLICY)
        self.assertEqual(len(lab.posts), 1)
        self.assertEqual(self.attempts()[0]["reconciled"], "recorded")
        [outage] = [item for item in lab.read_outages if "recorded" in item["what"]]
        self.assertEqual((outage["attempts"], outage["recovered"]), (3, True))

    def test_an_http_error_answer_is_raised_at_once_and_never_sent_again(self):
        for status, message in ((409, "Proposal precondition changed"), (403, "Approval access denied")):
            with self.subTest(status=status):
                self.setUp()
                lab = self.lab
                lab.script = [ProtocolError(status, message)]
                execs = lab.kubectl.execs
                with self.assertRaises(ProtocolError) as caught:
                    lab.approve(ACTION, POLICY)
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(len(lab.posts), 1)
                self.assertEqual(lab.kubectl.execs, execs + 1)  # The proposal's read only: nothing reconciled.
                [entry] = self.attempts()
                self.assertEqual((entry["outcome"], entry["status"]), ("refused", status))
                self.assertNotIn("reconciled", entry)
                self.assertNotIn(ACTION, lab.approved)
                self.assertEqual(lab.approvals_recorded(), [])

    def test_a_post_that_keeps_failing_in_transit_is_a_harness_error_after_the_bound(self):
        lab = self.lab
        lab.script = ["lost"] * 10
        with self.assertRaises(acceptance.ApprovalInDoubt) as caught:
            lab.approve(ACTION, POLICY)
        self.assertIsInstance(caught.exception, acceptance.AcceptanceError)
        self.assertNotIsInstance(caught.exception, acceptance.TransientRead)  # Never asked again by Lab.read.
        self.assertIn("not recorded after 3 sends", str(caught.exception))
        self.assertEqual(len(lab.posts), acceptance.APPROVAL_SEND_ATTEMPTS)
        self.assertEqual([entry["reconciled"] for entry in self.attempts()], ["not_recorded"] * 3)
        self.assertEqual(lab.approvals_recorded(), [])
        self.assertNotIn(ACTION, lab.approved)

    def test_no_resend_once_the_time_bound_has_passed(self):
        lab = self.lab
        lab.script = ["lost"] * 10
        lab.post_seconds = acceptance.APPROVAL_RESEND_SECONDS  # One POST's timeout uses the whole bound.
        with self.assertRaises(acceptance.ApprovalInDoubt):
            lab.approve(ACTION, POLICY)
        self.assertEqual(len(lab.posts), 1)

    def test_an_unreadable_store_leaves_the_post_in_doubt_and_never_sends_it_again(self):
        lab = self.lab
        lab.script = ["reply_lost"]
        original = lab.serve

        def serve(operation, body):
            answer = original(operation, body)
            lab.kubectl.forever = True
            return answer
        lab.serve = serve
        with self.assertRaises(acceptance.ApprovalInDoubt) as caught:
            lab.approve(ACTION, POLICY)
        self.assertIn("could not be read", str(caught.exception))
        self.assertEqual(len(lab.posts), 1)
        self.assertEqual(self.attempts()[0]["reconciled"], "unreadable")
        self.assertIn(("create", ACTION), lab.in_doubt)
        self.assertLessEqual(lab.now[0], lab.post_seconds + acceptance.APPROVAL_RECONCILE_SECONDS)
        # The next try (the operator's next tick) reconciles the earlier POST first: recorded, so nothing is sent.
        lab.kubectl.forever = False
        lab.approve(ACTION, POLICY)
        self.assertEqual(len(lab.posts), 1)
        self.assertEqual(self.attempts()[-1]["outcome"], "earlier_in_doubt")
        self.assertEqual(self.attempts()[-1]["reconciled"], "recorded")
        self.assertIn(ACTION, lab.approved)
        self.assertEqual(lab.in_doubt, {})
        self.assertEqual(len(lab.approvals_recorded()), 1)


class RevocationInDoubtTests(Harness):
    def setUp(self):
        super().setUp()
        self.lab.approve(ACTION, POLICY)  # Answered: the approval to revoke.
        self.lab.posts.clear()

    def test_a_revocation_cut_before_it_arrived_is_sent_once_more(self):
        self.lab.script = ["lost"]
        self.lab.revoke(ACTION)
        self.assertEqual([operation for operation, _ in self.lab.posts], ["revoke", "revoke"])
        self.assertEqual(self.lab.posts[0][1], self.lab.posts[1][1])
        self.assertEqual([entry["reconciled"] for entry in self.attempts("revoke")[:1]], ["not_recorded"])
        self.assertEqual(len(self.lab.revocations_recorded()), 1)

    def test_a_revocation_recorded_before_its_answer_was_cut_is_not_sent_again(self):
        self.lab.script = ["reply_lost"]
        self.lab.revoke(ACTION)
        self.assertEqual(len(self.lab.posts), 1)
        [entry] = self.attempts("revoke")
        self.assertEqual((entry["outcome"], entry["reconciled"]), ("in_doubt", "recorded"))
        self.assertEqual(len(self.lab.revocations_recorded()), 1)

    def test_a_refused_revocation_is_raised_as_it_is(self):
        self.lab.script = [ProtocolError(409, "Execution already reported; revocation cannot undo a write")]
        with self.assertRaises(ProtocolError):
            self.lab.revoke(ACTION)
        self.assertEqual(len(self.lab.posts), 1)
        self.assertNotIn("reconciled", self.attempts("revoke")[0])


class Ticks:
    def __init__(self, lab, ticks):
        self.lab, self.left = lab, ticks

    def wait(self, timeout):
        self.lab.advance(timeout)
        self.left -= 1
        return self.left < 0


class OperatorInDoubtTests(Harness):
    def test_the_operator_never_approves_twice_after_an_answer_lost_in_transit(self):
        lab = self.lab
        lab.script = ["reply_lost"]
        record = live.operator_record()
        live._operator(lab, POLICY, Ticks(lab, 5), record)
        self.assertEqual(len(lab.posts), 1)
        self.assertEqual(len(lab.approvals_recorded()), 1)
        self.assertEqual(record["fatal"], [])
        self.assertEqual(record["attempts_failed"], 0)
        self.assertEqual(self.attempts()[0]["reconciled"], "recorded")

    def test_the_operator_resends_only_what_the_store_shows_was_not_recorded(self):
        lab = self.lab
        lab.script = ["lost"]
        record = live.operator_record()
        live._operator(lab, POLICY, Ticks(lab, 5), record)
        self.assertEqual(len(lab.posts), 2)
        self.assertEqual(len(lab.approvals_recorded()), 1)
        self.assertEqual(record["fatal"], [])

    def test_an_operator_post_still_in_doubt_after_the_bound_is_fatal(self):
        lab = self.lab
        lab.script = ["lost"] * 3
        record = live.operator_record()
        live._operator(lab, POLICY, Ticks(lab, 1), record)
        [fatal] = record["fatal"]
        self.assertIn("ApprovalInDoubt", fatal)
        self.assertEqual(lab.approvals_recorded(), [])


class S6InDoubtTests(Harness):
    def s6(self):
        lab = self.lab
        run = live.Run(lab, acceptance.SCENARIOS["S6"], 1, POLICY)
        run.since = acceptance.stamp(BASE)
        removed = []

        def create(document, **_):
            run.chaos.append(document)
            run.started[document["metadata"]["name"]] = lab.monotonic()
            return document

        def remove(document, *, check=True):
            removed.append(lab.monotonic())
            run.chaos.remove(document)
        run.create, run.remove = create, remove
        return run, removed

    def test_s6_reconciles_an_eof_resends_once_and_watches_three_leases_with_no_write(self):
        lab = self.lab
        lab.script = ["lost"]
        run, removed = self.s6()
        live.s6(run)
        self.assertEqual(len(lab.posts), 2)
        self.assertEqual(len(lab.approvals_recorded()), 1)
        attempts = run.notes["approval_attempts"]
        self.assertEqual([(entry["outcome"], entry.get("reconciled")) for entry in attempts],
                         [("in_doubt", "not_recorded"), ("answered", None)])
        during = run.notes["during_partition"]
        self.assertEqual(during["template_writes"], 0)
        self.assertEqual(during["seconds"], 3 * acceptance.LEASE_SECONDS)  # Watched in full after the approval.
        self.assertEqual(during["approval_recorded_after_cut_seconds"], 2 * lab.post_seconds)
        # The partition stayed until the watch ended: never removed while the approval was in doubt.
        self.assertEqual(removed, [2 * lab.post_seconds + 3 * acceptance.LEASE_SECONDS])
        self.assertEqual(acceptance.expectation("S6", incidents=[{"state": "RECOVERED"}], rows=[], notes=run.notes),
                         [])

    def test_s6_with_an_approval_that_never_lands_fails_with_its_attempts_in_the_notes(self):
        lab = self.lab
        lab.script = ["lost"] * 10
        run, _ = self.s6()
        with self.assertRaises(acceptance.ApprovalInDoubt):
            live.s6(run)
        self.assertEqual([entry["reconciled"] for entry in run.notes["approval_attempts"]], ["not_recorded"] * 3)
        self.assertNotIn("during_partition", run.notes)
        self.assertIn("template writes during the partition not recorded as 0",
                      acceptance.expectation("S6", incidents=[{"state": "RECOVERED"}], rows=[], notes=run.notes))

    def test_s6_reports_a_write_during_the_partition_after_a_reconciled_approval(self):
        lab = self.lab
        lab.script = ["reply_lost"]
        lab.audit = [{"effect": "template_write", "sentinel": True, "pod": "sentinel-executor-a-1"}]
        run, _ = self.s6()
        with self.assertRaises(AssertionError):
            live.s6(run)
        self.assertEqual(run.notes["during_partition"]["template_writes"], 1)
        self.assertEqual(len(lab.posts), 1)


if __name__ == "__main__":
    unittest.main()
