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
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import unittest

from experiments.distributed import kind_acceptance as acceptance
from experiments.distributed import kind_acceptance_live as live
from runtime.approval_http import ApprovalClient
from runtime.service_identity import ProtocolError
from runtime.worker_transport import digest
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
    answering); forever makes every one fail; raising, when set, is raised
    by the next exec instead (kubectl that cannot be spawned), then cleared.
    Every exec is counted.
    """

    def __init__(self, database):
        self.database, self.failing, self.forever, self.execs = database, 0, False, 0
        self.raising = None

    def __call__(self, command, **options):
        if "get" in command and "pods" in command:
            app = next(part for part in command if part.startswith("--selector=app=")).split("=", 2)[2]
            return done(json.dumps({"items": [{"metadata": {"name": app + "-pod"}, "spec": {"nodeName": "n"},
                                               "status": {"phase": "Running"}}]}))
        if "exec" in command and acceptance.QUERY_HELPER in command:
            self.execs += 1
            if self.raising is not None:
                error, self.raising = self.raising, None
                raise error
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
    (arrives and is recorded, the answer is cut by an SSL EOF), "reply_cut"
    (arrives and is recorded, the reply is cut after its headers, so the
    client reads no JSON body and makes up its own ProtocolError 502),
    "timeout" (arrives, no answer within the client's timeout) or a
    ProtocolError (the approval API's own HTTP error answer, or one the
    client makes up). Each POST takes post_seconds of virtual time.
    forward_error, when set, is raised by the next fresh port-forward
    (kubectl that cannot be spawned), then cleared.
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
        self.forward_error = None
        self.tokens[acceptance.OPERATOR] = "t" * 43
        self.audit = []

    def at(self):
        return BASE + timedelta(seconds=self.now[0])

    def advance(self, seconds):
        self.now[0] += seconds

    def port_forward(self, port=18767, *, fresh=False):
        self.forwards.append(fresh)
        if fresh and self.forward_error is not None:
            error, self.forward_error = self.forward_error, None
            raise error
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
                if outcome == "reply_cut":  # What ApprovalClient.post raises when the body after 200 never came.
                    try:
                        json.loads(b"")
                    except ValueError as error:
                        raise ProtocolError(502, "Approval authority returned invalid JSON") from error
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

    def unreadable_after_the_next_post(self):
        """The next POST goes as scripted; the kubectl exec right after it cannot be spawned (OSError)."""
        lab, original = self.lab, self.lab.approval_client

        def client(port):
            inner = original(port)

            class Client:
                def post(self, operation, body):
                    try:
                        return inner.post(operation, body)
                    finally:
                        lab.kubectl.raising = OSError(24, "Too many open files")
                        lab.approval_client = original
            return Client()
        lab.approval_client = client


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

    def test_a_reply_cut_after_its_headers_is_in_doubt_not_a_refusal(self):
        # The client reads no JSON body and makes up a 502, a status the approval API never answers.
        lab = self.lab
        lab.script = ["reply_cut"]
        answer = lab.approve(ACTION, POLICY)
        self.assertEqual(len(lab.posts), 1)  # Recorded: never sent twice.
        self.assertEqual(len(lab.approvals_recorded()), 1)
        [entry] = self.attempts()
        self.assertEqual((entry["outcome"], entry["error_type"], entry["reconciled"]),
                         ("in_doubt", "ProtocolError502", "recorded"))
        self.assertNotIn("status", entry)
        self.assertIn(ACTION, lab.approved)
        self.assertEqual(lab.approvals[-1]["confirmed_by"], "store")
        self.assertEqual(answer["approval_id"], lab.approvals_recorded()[0][0])
        self.assertEqual(lab.in_doubt, {})

    def test_an_unreadable_reply_to_a_post_that_never_landed_is_reconciled_and_sent_once_more(self):
        lab = self.lab
        lab.script = [ProtocolError(502, "Approval authority returned invalid JSON")]  # Nothing reached the store.
        lab.approve(ACTION, POLICY)
        self.assertEqual(len(lab.posts), 2)
        self.assertEqual(lab.posts[0][1], lab.posts[1][1])
        self.assertEqual([(entry["outcome"], entry.get("reconciled")) for entry in self.attempts()],
                         [("in_doubt", "not_recorded"), ("answered", None)])
        self.assertEqual(len(lab.approvals_recorded()), 1)

    def test_a_reconciliation_that_fails_any_way_keeps_the_post_in_doubt_for_the_next_try(self):
        lab = self.lab
        lab.script = ["lost"]
        reads = lab.kubectl.execs
        self.unreadable_after_the_next_post()
        with self.assertRaises(acceptance.ApprovalInDoubt) as caught:
            lab.approve(ACTION, POLICY)
        self.assertIn("could not be read", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertGreater(lab.kubectl.execs, reads + 1)  # The proposal, then the failed reconciliation.
        [entry] = self.attempts()
        self.assertEqual((entry["outcome"], entry["reconciled"]), ("in_doubt", "unreadable"))
        self.assertIn(("create", ACTION), lab.in_doubt)
        self.assertEqual(len(lab.posts), 1)
        # The next try (the operator's next tick) reconciles the earlier POST first: not recorded, so that very
        # request is sent again, an exact retry, never a new approval sent blind.
        lab.approve(ACTION, POLICY)
        self.assertEqual(len(lab.posts), 2)
        self.assertEqual(lab.posts[0][1], lab.posts[1][1])
        self.assertEqual([(entry["outcome"], entry.get("reconciled")) for entry in self.attempts()],
                         [("in_doubt", "unreadable"), ("earlier_in_doubt", "not_recorded"), ("answered", None)])
        self.assertEqual(len(lab.approvals_recorded()), 1)
        self.assertEqual(lab.in_doubt, {})

    def test_a_resend_whose_port_forward_cannot_start_is_recorded_and_stays_in_doubt(self):
        lab = self.lab
        lab.script = ["lost"]
        lab.forward_error = FileNotFoundError(2, "kubectl not found")
        with self.assertRaises(FileNotFoundError):
            lab.approve(ACTION, POLICY)
        self.assertEqual(len(lab.posts), 1)
        self.assertEqual([(entry["outcome"], entry.get("reconciled"), entry.get("error_type"))
                          for entry in self.attempts()],
                         [("in_doubt", "not_recorded", "SSLEOFError"), ("not_sent", None, "FileNotFoundError")])
        self.assertIn(("create", ACTION), lab.in_doubt)  # The cut POST may still land: read first next time.
        lab.approve(ACTION, POLICY)
        self.assertEqual(len(lab.posts), 2)
        self.assertEqual(lab.posts[0][1], lab.posts[1][1])
        self.assertEqual(self.attempts()[2]["outcome"], "earlier_in_doubt")
        self.assertEqual(len(lab.approvals_recorded()), 1)
        self.assertEqual(lab.in_doubt, {})

    def test_an_earlier_post_too_close_to_its_expiry_is_not_sent_again_and_a_new_one_is(self):
        lab = self.lab
        lab.script = ["lost"]
        self.unreadable_after_the_next_post()  # Kept in doubt.
        with self.assertRaises(acceptance.ApprovalInDoubt):
            lab.approve(ACTION, POLICY)
        lab.advance(200)  # Its four minutes have less than two left.
        lab.approve(ACTION, POLICY)
        self.assertEqual(len(lab.posts), 2)
        self.assertNotEqual(lab.posts[0][1]["expires_at"], lab.posts[1][1]["expires_at"])
        self.assertEqual(self.attempts()[1]["reconciled"], "not_recorded")
        self.assertEqual(len(lab.approvals_recorded()), 1)
        self.assertEqual(lab.in_doubt, {})

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

    def test_a_revocation_whose_reply_was_cut_after_its_headers_is_not_sent_again(self):
        self.lab.script = ["reply_cut"]
        self.lab.revoke(ACTION)
        self.assertEqual(len(self.lab.posts), 1)
        [entry] = self.attempts("revoke")
        self.assertEqual((entry["outcome"], entry["error_type"], entry["reconciled"]),
                         ("in_doubt", "ProtocolError502", "recorded"))
        self.assertEqual(len(self.lab.revocations_recorded()), 1)

    def test_a_refused_revocation_is_raised_as_it_is(self):
        self.lab.script = [ProtocolError(409, "Execution already reported; revocation cannot undo a write")]
        with self.assertRaises(ProtocolError):
            self.lab.revoke(ACTION)
        self.assertEqual(len(self.lab.posts), 1)
        self.assertNotIn("reconciled", self.attempts("revoke")[0])


class CutReply:
    """A loopback approval endpoint that records each request it reads, then cuts its reply after the headers.

    The real ApprovalClient talks to it (plain HTTP on loopback): each POST
    is served by the real ApprovalAccess (InDoubtLab.serve), and the reply
    is "200 OK" with a Content-Length its body never reaches (body is the
    part sent, empty by default) before the write side is shut.
    """

    def __init__(self, lab, body=b""):
        self.lab, self.body, self.served = lab, body, 0
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        with self.listener:
            while True:
                try:
                    connection, _ = self.listener.accept()
                except OSError:
                    return
                with connection:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        data += connection.recv(65536)
                    head, _, body = data.partition(b"\r\n\r\n")
                    lines = head.decode("ascii").split("\r\n")
                    length = next(int(line.split(":", 1)[1]) for line in lines
                                  if line.lower().startswith("content-length:"))
                    while len(body) < length:
                        body += connection.recv(65536)
                    operation = lines[0].split()[1].rsplit("/", 1)[1]
                    self.lab.serve(operation, json.loads(body))
                    self.served += 1
                    connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                       b"Content-Length: 80\r\n\r\n" + self.body)
                    connection.shutdown(socket.SHUT_WR)

    def close(self):
        self.listener.close()
        self.thread.join(5)


class CutReplyOverASocketTests(Harness):
    """The real ApprovalClient against a reply cut after its headers: its own 502, reconciled as in doubt."""

    def test_the_client_makes_up_a_502_for_a_reply_cut_after_its_headers(self):
        for sent in (b"", b'{"approval_id": 1, "appr'):
            with self.subTest(sent=sent):
                self.setUp()
                endpoint = CutReply(self.lab, sent)
                self.addCleanup(endpoint.close)
                client = ApprovalClient(f"http://127.0.0.1:{endpoint.port}", acceptance.OPERATOR, "t" * 43,
                                        timeout=5)
                (row,) = self.lab.rows("SELECT proposal FROM actions WHERE action_id=?", (ACTION,))
                body = {"action_id": ACTION, "proposal_sha256": digest(json.loads(row[0])), "policy_version": POLICY,
                        "expires_at": (BASE + timedelta(seconds=240)).isoformat()}
                with self.assertRaises(ProtocolError) as caught:
                    client.post("create", body)
                self.assertEqual(caught.exception.status, acceptance.CLIENT_MADE_STATUS)
                self.assertNotIsInstance(caught.exception, acceptance.IN_TRANSIT)
                self.assertEqual(endpoint.served, 1)
                self.assertEqual(len(self.lab.approvals_recorded()), 1)  # Recorded before the reply was cut.

    def test_lab_approve_through_the_real_client_reconciles_a_cut_reply_as_recorded(self):
        lab = self.lab
        endpoint = CutReply(lab)
        self.addCleanup(endpoint.close)
        lab.approval_client = lambda port: ApprovalClient(f"http://127.0.0.1:{endpoint.port}", acceptance.OPERATOR,
                                                          "t" * 43, timeout=5)
        answer = lab.approve(ACTION, POLICY)
        self.assertEqual(endpoint.served, 1)  # Never sent twice.
        self.assertEqual(len(lab.approvals_recorded()), 1)
        [entry] = self.attempts()
        self.assertEqual((entry["outcome"], entry["error_type"], entry["reconciled"]),
                         ("in_doubt", "ProtocolError502", "recorded"))
        self.assertEqual(answer["approval_id"], lab.approvals_recorded()[0][0])


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

    def test_an_operator_tick_whose_reconciliation_fails_is_fatal_and_the_next_tick_reads_first(self):
        lab = self.lab
        lab.script = ["lost"]
        self.unreadable_after_the_next_post()
        record = live.operator_record()
        live._operator(lab, POLICY, Ticks(lab, 3), record)
        self.assertEqual(len(lab.posts), 2)
        self.assertEqual(lab.posts[0][1], lab.posts[1][1])  # The in-doubt request again, never a new one.
        self.assertEqual(len(lab.approvals_recorded()), 1)
        [fatal] = record["fatal"]
        self.assertIn("ApprovalInDoubt", fatal)
        self.assertEqual([(entry["outcome"], entry.get("reconciled")) for entry in self.attempts()],
                         [("in_doubt", "unreadable"), ("earlier_in_doubt", "not_recorded"), ("answered", None)])

    def test_an_operator_post_whose_reply_was_cut_after_its_headers_is_not_sent_again(self):
        lab = self.lab
        lab.script = ["reply_cut"]
        record = live.operator_record()
        live._operator(lab, POLICY, Ticks(lab, 5), record)
        self.assertEqual(len(lab.posts), 1)
        self.assertEqual(len(lab.approvals_recorded()), 1)
        self.assertEqual((record["fatal"], record["attempts_failed"]), ([], 0))

    def test_an_operator_post_still_in_doubt_after_the_bound_is_fatal(self):
        lab = self.lab
        lab.script = ["lost"] * 3
        record = live.operator_record()
        live._operator(lab, POLICY, Ticks(lab, 1), record)
        [fatal] = record["fatal"]
        self.assertIn("ApprovalInDoubt", fatal)
        self.assertEqual(lab.approvals_recorded(), [])


class S6InDoubtTests(Harness):
    CONFIRM = 7.0  # Seconds Chaos Mesh takes to confirm the partition in effect.

    def s6(self):
        lab = self.lab
        run = live.Run(lab, acceptance.SCENARIOS["S6"], 1, POLICY)
        run.since = acceptance.stamp(BASE)
        removed = []

        def create(document, **_):
            run.chaos.append(document)
            run.started[document["metadata"]["name"]] = lab.monotonic()
            lab.advance(self.CONFIRM)
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
        # Counted from the cut's creation, as the partition's own duration is (Run.window_left).
        self.assertEqual(during["approval_recorded_after_cut_seconds"], self.CONFIRM + 2 * lab.post_seconds)
        # The partition stayed until the watch ended: never removed while the approval was in doubt.
        self.assertEqual(removed, [self.CONFIRM + 2 * lab.post_seconds + 3 * acceptance.LEASE_SECONDS])
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

    def test_s6_reconciles_a_reply_cut_after_its_headers_and_watches_three_leases_with_no_write(self):
        lab = self.lab
        lab.script = ["reply_cut"]
        run, _ = self.s6()
        live.s6(run)
        self.assertEqual(len(lab.posts), 1)
        self.assertEqual([(entry["outcome"], entry["error_type"], entry["reconciled"])
                          for entry in run.notes["approval_attempts"]],
                         [("in_doubt", "ProtocolError502", "recorded")])
        self.assertEqual(run.notes["during_partition"]["template_writes"], 0)
        self.assertEqual(run.notes["during_partition"]["seconds"], 3 * acceptance.LEASE_SECONDS)
        self.assertEqual(acceptance.expectation("S6", incidents=[{"state": "RECOVERED"}], rows=[], notes=run.notes),
                         [])

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
