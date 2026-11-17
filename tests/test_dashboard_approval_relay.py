"""Dashboard approval panel relay: `--approval ui` through the lab dashboard server.

The authority is the real `python -m runtime.approval_http` child from
runtime/approval_session.py, against a file-backed Coordinator database; the
dashboard server is the real runtime/dashboard.py server on a free loopback
port. Guard, Coordinator and Executor are the real code; only Kubernetes is
the in-memory fake. The browser side is plain HTTP requests carrying only the
per-session lab token, as the React panel sends them.
"""
from copy import deepcopy
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from runtime.approval_relay import (
    APPROVED, CLOSED, EXPIRED, PENDING, RATIONALE_LIMIT, REVOKE_CONFLICT, REVOKED, ApprovalRelay, RelayRefused,
)
from runtime.approval_session import (
    AUTOMATED_LAB, DASHBOARD_OPERATOR, LAB_EXECUTOR, DashboardApprover, approver_for, operator_identity,
    require_attended,
)
from runtime.dashboard import make_server
from runtime.executor_pipeline import process_executor_requests
from runtime.lab_control import LabControl
from services.coordinator.core import handle
from services.coordinator.store import _digest
from services.executor.core import execute
from shared import log
from tests.support.executor_fake import FakeDeploymentAPI
from tests.test_approval_session import AuthorityCase, real_now, sample, sample_delta


def lab(directory):
    return LabControl(directory, cluster_id="sentinel-boutique-lab", context="kind-sentinel-boutique-lab",
                      namespace="sentinel-boutique", deployment="shippingservice")


def wait_until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(.01)
    return False


class RelayServerCase(AuthorityCase):
    """A real authority, lab session, relay and dashboard server for one escalated action."""
    operator = DASHBOARD_OPERATOR
    wait_seconds = 30

    def setUp(self):
        super().setUp()
        self.control = lab(self.run_dir)
        self.addCleanup(self.control.finish, "STOPPED")
        self.relay = ApprovalRelay()
        self.addCleanup(self.relay.close)
        self.approver = approver_for("ui", self.authority, relay=self.relay, wait_seconds=self.wait_seconds)
        self.server = make_server(self.db, self.run_dir / "no-telemetry.sqlite3", port=0,
                                  lab_control=self.control, approval_relay=self.relay)
        self.serving = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.serving.start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.lab_token = self.control.capabilities()["control_token"]
        self.responses = []
        self.decision = self.store.action(self.proposal["action_id"])["guard_result"]

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.serving.join(2)

    def offer(self):
        """Run the harness side: the ui approver waits on its own thread until decided."""
        self.returned = []
        waiter = threading.Thread(target=lambda: self.returned.append(self.approver(self.proposal, self.decision)),
                                  daemon=True)
        waiter.start()
        self.addCleanup(waiter.join, 5)
        self.addCleanup(self.relay.close)  # Runs before the join: a still-waiting approver is released.
        self.assertTrue(wait_until(lambda: any(item["status"] == PENDING for item in self.relay.requests())))
        return waiter

    def body(self, **changes):
        value = {"action_id": self.proposal["action_id"], "proposal_sha256": _digest(self.proposal),
                 "confirm_action_id": self.proposal["action_id"]}
        value.update(changes)
        return value

    def post(self, route, payload, *, raw=None, **headers):
        """POST as the dashboard page does; returns (status, decoded JSON or None)."""
        sent = {"Content-Type": "application/json", "Origin": self.base, "X-Sentinel-Lab-Token": self.lab_token}
        sent.update(headers)
        data = raw if raw is not None else json.dumps(payload).encode()
        request = Request(self.base + "/api/approvals/" + route, data=data, method="POST", headers=sent)
        try:
            with urlopen(request, timeout=10) as response:
                status, text = response.status, response.read()
        except HTTPError as error:
            status, text = error.code, error.read()
            error.close()
        self.responses.append(text)
        try:
            return status, json.loads(text)
        except ValueError:
            return status, None

    def snapshot(self):
        with urlopen(self.base + "/api/snapshot", timeout=5) as response:
            text = response.read()
        self.responses.append(text)
        return json.loads(text)

    def approved(self):
        self.offer()
        status, answer = self.post("approve", self.body())
        self.assertEqual(status, 200, answer)
        self.assertTrue(wait_until(lambda: self.returned))
        return answer

    def revoke_body(self, approval_id, **changes):
        return self.body(approval_id=approval_id, **changes)


class RelayRefusalTests(RelayServerCase):
    def assert_nothing_decided(self):
        self.assertEqual(self.history(), [])
        self.assertEqual(self.state(), "AWAITING_APPROVAL")
        self.assertEqual(self.returned, [])
        self.assertEqual([item["status"] for item in self.relay.requests()], [PENDING])

    def test_wrong_host_origin_or_lab_token_is_refused_with_403(self):
        self.offer()
        for headers in ({"Origin": "http://evil.example"}, {"Origin": "null"},
                        {"X-Sentinel-Lab-Token": "wrong"}, {"X-Sentinel-Lab-Token": ""},
                        {"Host": "evil.example:8765"}, {"Host": "127.0.0.1.evil.example"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.post("approve", self.body(), **headers)[0], 403)
        self.assert_nothing_decided()

    def test_content_type_size_and_shape_are_checked_before_the_authority_is_called(self):
        self.offer()
        cases = [
            ("text/plain content type", self.body(), None, {"Content-Type": "text/plain"}, 415),
            ("body over 2 KB", None, json.dumps(self.body(pad="x" * 2100)).encode(), {}, 413),
            ("malformed JSON", None, b"{not json", {}, 400),
            ("deeply nested JSON", None, b"[" * 1000 + b"]" * 1000, {}, 400),
            ("JSON array", None, b"[]", {}, 400),
            ("an approval_url key (no SSRF)", self.body(approval_url="http://127.0.0.1:1/"), None, {}, 400),
            ("an operator_id key", self.body(operator_id="someone-else"), None, {}, 400),
            ("an expires_at key", self.body(expires_at="2099-01-01T00:00:00Z"), None, {}, 400),
            ("a missing confirmation", {key: value for key, value in self.body().items()
                                        if key != "confirm_action_id"}, None, {}, 400),
            ("a non-string action ID", self.body(action_id=7), None, {}, 400),
        ]
        for label, payload, raw, headers, status in cases:
            with self.subTest(label):
                self.assertEqual(self.post("approve", payload, raw=raw, **headers)[0], status)
        self.assert_nothing_decided()

    def test_mistyped_action_id_unknown_action_and_stale_digest_create_no_approval(self):
        self.offer()
        action_id = self.proposal["action_id"]
        for label, payload, status in (
                ("mistyped confirmation", self.body(confirm_action_id=action_id[:-1]), 400),
                ("confirmation with spaces", self.body(confirm_action_id=" " + action_id), 400),
                ("confirmation of another case", self.body(confirm_action_id=action_id.lower()), 400),
                ("unknown action", self.body(action_id="ACT-OTHER", confirm_action_id="ACT-OTHER"), 404),
                ("stale proposal digest", self.body(proposal_sha256="0" * 64), 409)):
            with self.subTest(label):
                code, answer = self.post("approve", payload)
                self.assertEqual(code, status)
                self.assertEqual(set(answer), {"error"})
        self.assert_nothing_decided()

    def test_unknown_route_gives_404_and_routes_need_a_relay_and_lab_controls(self):
        self.assertEqual(self.post("delete", self.body())[0], 404)
        with self.assertRaisesRegex(ValueError, "lab controls"):
            make_server(self.db, "missing", port=0, approval_relay=ApprovalRelay())
        server = make_server(self.db, "missing", port=0, lab_control=self.control)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base + "/api/snapshot", timeout=5) as response:
                self.assertNotIn("approval_requests", json.load(response))
            request = Request(base + "/api/approvals/approve", data=json.dumps(self.body()).encode(), method="POST",
                              headers={"Content-Type": "application/json", "Origin": base,
                                       "X-Sentinel-Lab-Token": self.lab_token})
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=5)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_early_refusals_with_a_full_body_reliably_reach_the_client(self):
        body = b'not JSON' + b'x' * (32 * 1024)
        for route, status, token in (('/api/approvals/delete', 404, 'wrong'),
                                    ('/api/approvals/approve', 403, 'wrong'), ('/api/lab/inject', 403, 'wrong'),
                                    ('/api/approvals/approve', 413, self.lab_token),
                                    ('/api/lab/inject', 400, self.lab_token)):
            for attempt in range(3):
                with self.subTest(route=route, attempt=attempt):
                    connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
                    try:
                        connection.request('POST', route, body=body,
                                           headers={'Content-Type': 'application/json', 'Origin': self.base,
                                                    'X-Sentinel-Lab-Token': token})
                        response = connection.getresponse()  # Read only after sending the full refused body.
                        self.assertEqual(response.status, status)
                        response.read()
                    finally:
                        connection.close()
        self.assertEqual(self.history(), [])

    def test_upstream_401_reaches_the_page_and_the_request_stays_pending(self):
        self.offer()
        genuine = self.authority._tokens[DASHBOARD_OPERATOR]
        self.authority._tokens[DASHBOARD_OPERATOR] = "w" * 48
        status, answer = self.post("approve", self.body())
        self.assertEqual(status, 401)
        self.assertIn("credential", answer["error"])
        self.authority._tokens[DASHBOARD_OPERATOR] = genuine
        self.assert_nothing_decided()
        self.assertEqual(self.post("approve", self.body())[0], 200)

    def test_authority_down_gives_503_and_records_nothing(self):
        self.offer()
        self.authority.stop()
        status, answer = self.post("approve", self.body())
        self.assertEqual(status, 503)
        self.assertIn("unavailable", answer["error"])
        self.assertEqual(self.returned, [])
        self.assertEqual([item["status"] for item in self.relay.requests()], [PENDING])
        self.assertIsNone(self.store.approval(self.proposal["action_id"]))


class RelayDecisionTests(RelayServerCase):
    def test_snapshot_lists_the_pending_facts_and_no_credential(self):
        self.offer()
        (shown,) = self.snapshot()["approval_requests"]
        self.assertEqual((shown["action_id"], shown["status"], shown["mode"], shown["operator_id"]),
                         (self.proposal["action_id"], PENDING, "ui", DASHBOARD_OPERATOR))
        self.assertEqual(shown["target"], self.proposal["target"])
        self.assertEqual((shown["action"], shown["baseline_id"]), ("ROLLBACK", "BASE-DEMO-001"))
        self.assertEqual(shown["template_sha256_prefix"], self.proposal["parameters"]["template_sha256"][:12])
        self.assertNotIn("desired_replicas", shown)  # Only a SCALE names a replica count (P5.07).
        self.assertEqual(shown["proposal_sha256"], _digest(self.proposal))
        self.assertEqual(shown["guard"], {"verdict": "ESCALATE", "reason_codes": ["OPERATOR_APPROVAL_REQUIRED"]})
        self.assertEqual(shown["policy_version"], self.decision["policy_version"])
        self.assertEqual(shown["rationale"], self.proposal["rationale"])
        self.assertFalse(shown["rationale_truncated"])
        self.assertIsNone(shown["approval"])
        self.assertGreater(shown["expires_at"], shown["requested_at"])

    def test_a_scale_request_shows_the_replica_count_it_would_write(self):
        # P5.07: the operator approving a SCALE sees its desired_replicas; a malformed count is never shown.
        entry = {"proposal": {**deepcopy(self.proposal), "action": "SCALE",
                              "parameters": {"baseline_id": "BASE-DEMO-001", "desired_replicas": 2,
                                             "template_sha256": "a" * 64}},
                 "decision": self.decision, "record": None, "approver": Mock(identity=DASHBOARD_OPERATOR),
                 "status": PENDING, "digest": "d" * 64, "requested_at": "t0", "expires_at": "t1",
                 "approval_id": None, "revocation": None}
        self.assertEqual(ApprovalRelay._public(entry)["desired_replicas"], 2)
        entry["proposal"]["parameters"]["desired_replicas"] = "2"
        self.assertIsNone(ApprovalRelay._public(entry)["desired_replicas"])
        entry["proposal"].update(action="ROLLBACK", parameters={"desired_replicas": 2})
        self.assertNotIn("desired_replicas", ApprovalRelay._public(entry))

    def test_typed_approval_is_relayed_as_the_dashboard_operator_and_executes_once(self):
        answer = self.approved()
        (entry,) = self.history()
        self.assertEqual(entry["approval"]["operator_id"], DASHBOARD_OPERATOR)
        self.assertEqual(answer, {"action_id": self.proposal["action_id"], "approval_id": entry["approval_id"],
                                  "operator_id": DASHBOARD_OPERATOR, "approved_at": entry["approval"]["approved_at"],
                                  "expires_at": entry["approval"]["expires_at"], "mode": "ui"})
        self.assertEqual(self.returned, [entry["approval"]])
        self.assertTrue(self.store.has_managed_approval(self.proposal["action_id"]))
        summary = self.approver.describe()
        self.assertEqual((summary["mode"], summary["identity"], summary["via"], summary["human"]),
                         ("ui", DASHBOARD_OPERATOR, "approval_http", True))
        self.assertIn("not which person", summary["decided_by"])
        self.assertEqual(summary["approvals"][0]["approval_id"], entry["approval_id"])
        self.assertEqual(self.post("approve", self.body())[0], 409)  # A second click creates nothing.
        self.assertEqual(self.guard()[0]["verdict"], "ALLOW")
        results = process_executor_requests(self.store, self.bus, adapters=self.adapters())
        self.assertEqual([result["status"] for result in results], ["APPLIED"])
        self.assertEqual(len(self.cluster.writes), 1)
        self.assertEqual(len(self.history()), 1)
        (shown,) = self.snapshot()["approval_requests"]
        self.assertEqual((shown["status"], shown["approval"]["approval_id"]), (APPROVED, entry["approval_id"]))

    def test_revocation_before_the_executor_gives_zero_writes(self):
        answer = self.approved()
        self.assertEqual(self.guard()[0]["verdict"], "ALLOW")
        self.assertEqual(self.state(), "READY")
        adapters = self.adapters()
        for label, payload, status in (
                ("older approval ID", self.revoke_body(answer["approval_id"] + 1), 409),
                ("boolean approval ID", self.revoke_body(True), 400),
                ("mistyped confirmation", self.revoke_body(answer["approval_id"], confirm_action_id="ACT"), 400),
                ("stale digest", self.revoke_body(answer["approval_id"], proposal_sha256="f" * 64), 409),
                ("reason chosen by the page", self.revoke_body(answer["approval_id"], reason="other"), 400)):
            with self.subTest(label):
                self.assertEqual(self.post("revoke", payload)[0], status)
        self.assertIsNone(self.history()[0]["revocation"])
        status, revoked = self.post("revoke", self.revoke_body(answer["approval_id"]))
        self.assertEqual(status, 200, revoked)
        self.assertEqual((revoked["operator_id"], revoked["approval_id"], revoked["mode"]),
                         (DASHBOARD_OPERATOR, answer["approval_id"], "ui"))
        self.assertEqual(self.state(), "VALIDATING")
        self.assertEqual(self.history()[0]["revocation"]["operator_id"], DASHBOARD_OPERATOR)
        self.assertEqual(process_executor_requests(self.store, self.bus, adapters=adapters), [])
        self.assertEqual(execute(self.proposal, adapters)["status"], "FAILED")
        self.assertEqual(self.cluster.write_attempts, [])
        self.assertEqual(self.post("revoke", self.revoke_body(answer["approval_id"]))[0], 409)
        (shown,) = self.snapshot()["approval_requests"]
        self.assertEqual((shown["status"], shown["revocation"]["operator_id"]), (REVOKED, DASHBOARD_OPERATOR))
        self.assertEqual(self.approver.describe()["revocations"][0]["approval_id"], answer["approval_id"])

    def test_revocation_after_execution_is_reported_is_refused(self):
        answer = self.approved()
        self.guard()
        results = process_executor_requests(self.store, self.bus, adapters=self.adapters())
        self.assertEqual([result["status"] for result in results], ["APPLIED"])
        status, refused = self.post("revoke", self.revoke_body(answer["approval_id"]))
        self.assertEqual(status, 409)
        self.assertEqual(refused["error"], REVOKE_CONFLICT)
        self.assertIsNone(self.history()[0]["revocation"])
        self.assertEqual([item["status"] for item in self.relay.requests()], [APPROVED])

    def test_revoking_a_pending_request_has_nothing_to_revoke(self):
        self.offer()
        self.assertEqual(self.post("revoke", self.revoke_body(1))[0], 409)
        self.assertEqual(self.history(), [])

    def test_no_credential_in_responses_snapshot_logs_or_run_folder(self):
        stream = io.StringIO()
        with log.configured("test", level="DEBUG", stream=stream):
            self.offer()
            self.snapshot()
            self.post("approve", self.body(confirm_action_id="wrong"))
            self.post("approve", self.body(), Origin="http://evil.example")
            answer = self.post("approve", self.body())[1]
            self.snapshot()
            self.guard()
            self.post("revoke", self.revoke_body(answer["approval_id"]))
            self.snapshot()
        logged = stream.getvalue()
        self.assertIn("dashboard.approval.relayed", logged)
        self.assertIn("dashboard.approval.refused", logged)
        summary = json.dumps(self.approver.describe())
        tokens = {**self.tokens, "lab": self.lab_token}
        self.authority.close()
        self.store.close()
        self.control.finish("STOPPED")
        files = [path for path in self.run_dir.rglob("*") if path.is_file()]
        self.assertTrue(any(path.name == "lab-session.json" for path in files))
        for name, token in tokens.items():
            with self.subTest(credential=name):
                for text in self.responses:
                    self.assertNotIn(token.encode(), text)
                self.assertNotIn(token, logged)
                self.assertNotIn(token, summary)
                for path in files:
                    self.assertNotIn(token.encode(), path.read_bytes(), path.name)


class ExpiringRelayTests(RelayServerCase):
    wait_seconds = 1

    def test_expired_request_is_refused_and_the_harness_records_a_refusal(self):
        self.offer()
        self.assertTrue(wait_until(lambda: self.returned, timeout=5))
        self.assertEqual(self.returned, [None])
        status, answer = self.post("approve", self.body())
        self.assertEqual(status, 410)
        self.assertIn("expired", answer["error"])
        self.assertEqual(self.history(), [])
        self.assertEqual(self.state(), "AWAITING_APPROVAL")
        self.assertEqual([item["status"] for item in self.relay.requests()], [EXPIRED])
        self.assertEqual(len(self.approver.describe()["refusals"]), 1)


class FakeApprover:
    identity = DASHBOARD_OPERATOR

    def __init__(self):
        self.approve_offer = Mock(return_value=({"operator_id": DASHBOARD_OPERATOR, "approved_at": "2026-11-12T00:00:00Z",
                                                 "expires_at": "2099-01-01T00:00:00Z"}, 3))
        self.revoke_offer = Mock()


class RelayUnitTests(unittest.TestCase):
    """Relay behaviour that needs no authority child."""

    def setUp(self):
        self.relay = ApprovalRelay()
        self.addCleanup(self.relay.close)
        self.proposal = sample("messages/proposal.json", sample_delta())
        self.decision = {"verdict": "ESCALATE", "reason_codes": ["OPERATOR_APPROVAL_REQUIRED"], "policy_version": "p"}

    def waiting(self, approver, proposal=None, wait_seconds=30):
        returned = []
        worker = threading.Thread(target=lambda: returned.append(self.relay.offer(
            approver, proposal or self.proposal, self.decision, wait_seconds=wait_seconds)), daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.assertTrue(wait_until(lambda: self.relay.requests()))
        return worker, returned

    def payload(self, **changes):
        return {"action_id": self.proposal["action_id"], "proposal_sha256": _digest(self.proposal),
                "confirm_action_id": self.proposal["action_id"], **changes}

    def test_model_text_is_labelled_data_truncated_and_kept_literal(self):
        proposal = deepcopy(self.proposal)
        proposal["rationale"] = "<script>alert(1)</script>\x1b[2J" + "y" * 2000
        proposal["target"]["name"] = "demo\r\napi"
        self.waiting(FakeApprover(), proposal)
        (shown,) = self.relay.requests()
        self.assertTrue(shown["rationale"].startswith("<script>alert(1)</script> [2J"))
        self.assertEqual(len(shown["rationale"]), RATIONALE_LIMIT)
        self.assertTrue(shown["rationale_truncated"])
        self.assertEqual(shown["target"]["name"], "demo  api")

    def test_close_releases_the_waiting_approver_and_refuses_later_clicks(self):
        worker, returned = self.waiting(FakeApprover())
        self.relay.close()
        worker.join(5)
        self.assertEqual(returned, [None])
        with self.assertRaises(RelayRefused) as refused:
            self.relay.approve(self.payload())
        self.assertEqual(refused.exception.status, 404)
        self.assertEqual(self.relay.requests()[0]["status"], CLOSED)
        self.assertIsNone(self.relay.offer(FakeApprover(), self.proposal, self.decision, wait_seconds=5))

    def test_an_interrupted_wait_cannot_be_approved_later(self):
        approver = FakeApprover()
        with patch.object(self.relay._condition, "wait", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.relay.offer(approver, self.proposal, self.decision, wait_seconds=30)
        with self.assertRaises(RelayRefused) as refused:
            self.relay.approve(self.payload())
        self.assertEqual(refused.exception.status, 409)
        approver.approve_offer.assert_not_called()

    def test_one_open_request_per_action_and_bounded_wait(self):
        self.waiting(FakeApprover())
        with self.assertRaisesRegex(RuntimeError, "already has an open"):
            self.relay.offer(FakeApprover(), self.proposal, self.decision, wait_seconds=5)
        for seconds in (0, -1, 3601):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                self.relay.offer(FakeApprover(), self.proposal, self.decision, wait_seconds=seconds)

    def test_an_upstream_bug_is_not_hidden_and_the_request_stays_pending(self):
        approver = FakeApprover()
        approver.approve_offer.side_effect = RuntimeError("bug")
        self.waiting(approver)
        with self.assertRaisesRegex(RuntimeError, "bug"):
            self.relay.approve(self.payload())
        self.assertEqual(self.relay.requests()[0]["status"], PENDING)

    def test_approval_waits_for_a_decision_already_on_its_way(self):
        approver = FakeApprover()
        release = threading.Event()

        def slow(*_args):
            release.wait(5)
            return approver.approve_offer.return_value
        approver.approve_offer.side_effect = slow
        worker, returned = self.waiting(approver, wait_seconds=1)
        clicked = threading.Thread(target=self.relay.approve, args=(self.payload(),), daemon=True)
        clicked.start()
        time.sleep(1.5)  # The request's own wait has passed while the authority call is in flight.
        self.assertEqual(returned, [])
        release.set()
        clicked.join(5)
        worker.join(5)
        self.assertEqual(returned[0]["operator_id"], DASHBOARD_OPERATOR)

    def test_relay_keeps_a_bounded_number_of_decided_requests(self):
        for index in range(25):
            proposal = dict(self.proposal, action_id=f"ACT-{index}")
            self.relay.offer(FakeApprover(), proposal, self.decision, wait_seconds=0.01)
        self.assertEqual(len(self.relay.requests()), 20)
        self.assertEqual(self.relay.requests()[0]["action_id"], "ACT-24")


class DashboardModeTests(unittest.TestCase):
    def test_ui_mode_identity_attendance_and_relay_requirements(self):
        self.assertEqual(operator_identity("ui"), DASHBOARD_OPERATOR)
        self.assertEqual(operator_identity("ui", "ops-1"), "ops-1")
        for identity in (AUTOMATED_LAB, LAB_EXECUTOR, "has space"):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                operator_identity("ui", identity)
        require_attended("ui", io.StringIO())  # The decision is made in the browser, not at a terminal.
        authority = Mock(operator=DASHBOARD_OPERATOR)
        with self.assertRaisesRegex(ValueError, "relay"):
            approver_for("ui", authority)
        for seconds in (0, 3601):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                DashboardApprover(authority, relay=ApprovalRelay(), wait_seconds=seconds)
        approver = approver_for("ui", authority, relay=ApprovalRelay(), ask=input, say=print)
        self.assertIsInstance(approver, DashboardApprover)


class HarnessWiringTests(unittest.TestCase):
    """The Boutique and Executor harnesses route --approval ui through the relay, or refuse it."""

    def run_main(self, module, arguments):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run_dir = Path(temp.name) / "run"
        with patch("sys.argv", ["harness", "--run-dir", str(run_dir), *arguments]), \
                patch("sys.stdin", io.StringIO()), patch("sys.stderr", io.StringIO()) as stderr, \
                patch("subprocess.run", side_effect=AssertionError("cluster command attempted")):
            with self.assertRaises(SystemExit) as stopped:
                module.main()
        self.assertNotEqual(stopped.exception.code, 0)
        self.assertFalse(run_dir.exists())
        return stderr.getvalue()

    def test_ui_approval_needs_the_dashboard_lab_controls_and_the_boutique_harness(self):
        from experiments.boutique.harness import live as boutique
        from experiments.recovery import live as recovery
        self.assertIn("requires --dashboard --ui-controls", self.run_main(boutique, ["--approval", "ui"]))
        self.assertIn("requires --dashboard --ui-controls",
                      self.run_main(boutique, ["--approval", "ui", "--dashboard"]))
        self.assertIn("invalid choice", self.run_main(recovery, ["--approval", "ui"]))

    def test_boutique_main_gives_the_dashboard_a_relay_and_closing_it_releases_the_approver(self):
        from experiments.boutique.harness import live
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run_dir = Path(temp.name) / "run"
        made = []

        class Dashboard:
            def __init__(self, directory, **options):
                made.append(options)

            def start(self):
                raise RuntimeError("stop before the cluster")

            def close(self):
                made[-1]["approval_relay"].close()

        with patch("sys.argv", ["harness", "--run-dir", str(run_dir), "--approval", "ui", "--dashboard",
                                "--ui-controls"]), \
                patch("sys.stdin", io.StringIO()), patch("sys.stderr", io.StringIO()), \
                patch("sys.stdout", io.StringIO()), \
                patch("subprocess.run", side_effect=AssertionError("cluster command attempted")), \
                patch("shared.provenance.collect", return_value={"source_revision": "test"}), \
                patch.object(live, "tool_versions", return_value={}), \
                patch("experiments.boutique.harness.dashboard.BoutiqueDashboard", Dashboard):
            self.assertEqual(live.main(), 1)
        relay = made[0]["approval_relay"]
        self.assertIsInstance(relay, ApprovalRelay)
        self.assertIsNotNone(made[0]["lab_control"])
        self.assertIsNone(relay.offer(FakeApprover(), sample("messages/proposal.json", sample_delta()),
                                      {"reason_codes": []}, wait_seconds=5))  # Closed: returns at once.
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))["operator_approval"]
        self.assertEqual((summary["mode"], summary["identity"]), ("ui", DASHBOARD_OPERATOR))

    def test_boutique_dashboard_close_closes_the_relay(self):
        from experiments.boutique.harness.dashboard import BoutiqueDashboard
        relay = ApprovalRelay()
        with tempfile.TemporaryDirectory() as directory:
            BoutiqueDashboard(directory, context="c", cluster_id="i", namespace="n", approval_relay=relay).close()
        self.assertIsNone(relay.offer(FakeApprover(), sample("messages/proposal.json", sample_delta()),
                                      {"reason_codes": []}, wait_seconds=5))

    def test_case_waits_for_the_dashboard_decision_and_saves_the_relayed_approval(self):
        from experiments.recovery.live import Experiment
        delta = sample_delta()
        cluster = FakeDeploymentAPI(sample("observations.json", delta)["failed-rollout"][-1], clock=real_now)
        proposal = sample("messages/proposal.json", delta)
        incident = sample("messages/incident.json", delta)

        class OfflineExperiment(Experiment):
            def deployment(self):
                return cluster.read(proposal["target"])["deployment"]

            def build_proposal(self, incident, folder, rationale=None):
                return deepcopy(proposal)

            def dependency_result(self, current):
                return sample("messages/dependency-result.json", delta)

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch("subprocess.run", side_effect=AssertionError("cluster command attempted")), \
                patch("shared.provenance.collect", return_value={"source_revision": "test"}):
            experiment = OfflineExperiment(Path(temp.name) / "run")
        experiment.target, experiment.policy = proposal["target"], sample("policy.json", delta)
        experiment.baseline = sample("baseline.json", delta)
        experiment.approval_relay = relay = ApprovalRelay()
        self.addCleanup(experiment.store.close)
        self.addCleanup(experiment.close_approval_authority)
        self.addCleanup(relay.close)
        handle(incident, experiment.store, experiment.bus)
        experiment.configure_approval("ui")

        def operator():
            wait_until(lambda: relay.requests(), timeout=20)
            relay.approve({"action_id": proposal["action_id"], "proposal_sha256": _digest(proposal),
                           "confirm_action_id": proposal["action_id"]})
        clicker = threading.Thread(target=operator, daemon=True)
        clicker.start()
        self.addCleanup(clicker.join, 5)
        folder, _ = experiment.case("approved-rollback", incident, coordinator=True)
        saved = json.loads((folder / "approval.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["operator_id"], DASHBOARD_OPERATOR)
        self.assertTrue(experiment.store.has_managed_approval(proposal["action_id"]))
        self.assertEqual(experiment.store.action(proposal["action_id"])["state"], "READY")
        summary = experiment.summary["operator_approval"]
        self.assertEqual((summary["mode"], summary["identity"], summary["human"]), ("ui", DASHBOARD_OPERATOR, True))


if __name__ == "__main__":
    unittest.main()
