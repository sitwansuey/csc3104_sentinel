"""Per-run approval authority and approver strategies for the live harnesses.

LocalApprovalAuthority starts `python -m runtime.approval_http` as a child
process against the run's Coordinator database, on a free loopback port. It
generates one bearer token per identity for this run only. A token exists in
this process's memory and in the child's environment, never in a file, argv,
summary.json or a log line. The identity and permission files written to the
run folder name only the environment variables and one exact target grant.

Approvers decide whether an escalated proposal is approved, and if so create
the approval through the authenticated API:

- interactive: a person reads the code-derived facts and types the action ID.
- auto-lab: explicit opt-in for unattended lab runs. The approval is recorded
  under the automated-lab identity and is never presented as a person's.
- reject: refuses every approval, for negative tests.
- ui: the lab dashboard shows the facts; an operator types the action ID and
  clicks Approve. The dashboard server relays it with this run's operator
  credential (runtime/approval_relay.py); the browser holds no approval token.
"""
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading
import time

from runtime.approval_http import ApprovalClient
from runtime.service_identity import IDENTITY_NAME
from services.coordinator.approvals import TARGET_FIELDS
from services.coordinator.store import _digest

ROOT = Path(__file__).resolve().parents[1]
MODES = ("interactive", "auto-lab", "reject", "ui")
LAB_OPERATOR = "lab-operator"
DASHBOARD_OPERATOR = "dashboard-operator"
AUTOMATED_LAB = "automated-lab"
LAB_GUARD = "lab-guard"
LAB_EXECUTOR = "lab-executor"
TOKEN_ENV = {"operator": "SENTINEL_LAB_APPROVAL_OPERATOR_TOKEN", "guard": "SENTINEL_LAB_APPROVAL_GUARD_TOKEN",
             "executor": "SENTINEL_LAB_APPROVAL_EXECUTOR_TOKEN"}
URL_ENV, IDENTITY_ENV = "SENTINEL_LAB_APPROVAL_URL", "SENTINEL_LAB_APPROVAL_IDENTITY"
OPERATIONS = {"operator": ["create", "read", "revoke", "history"], "guard": ["read"], "executor": ["read"]}
RATIONALE_LIMIT = 500
FIELD_LIMIT = 160


class AuthorityUnavailable(RuntimeError):
    """The approval child did not start, or is not serving."""


def utc(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class LocalApprovalAuthority:
    """One approval_http child for one run; close() stops it and drops the tokens.

    It grants its identities one target; a scenario-pair run (P4.05) names its
    second target in additional_targets, so each identity may act on exactly
    those two complete identities and nothing else.
    """

    def __init__(self, run_dir, db_path, target, *, operator=LAB_OPERATOR, max_lifetime_seconds=300,
                 startup_timeout=20, python=None, additional_targets=()):
        for item in (target, *additional_targets):
            if (not isinstance(item, dict) or set(item) != TARGET_FIELDS
                    or any(not isinstance(value, str) or not value.strip() for value in item.values())):
                raise ValueError("The authority needs one complete target identity")
        if len(additional_targets) > 1 or any(item == target for item in additional_targets):
            raise ValueError("The authority accepts at most one additional, different target")
        self.additional_targets = [dict(item) for item in additional_targets]
        if operator in {LAB_GUARD, LAB_EXECUTOR}:
            raise ValueError("The operator identity must differ from the reader identities")
        self.directory, self.db_path = Path(run_dir), Path(db_path)
        self.target, self.operator = dict(target), operator
        self.roles = {operator: "operator", LAB_GUARD: "guard", LAB_EXECUTOR: "executor"}
        self.max_lifetime_seconds, self.startup_timeout = max_lifetime_seconds, startup_timeout
        self.python = python or sys.executable
        self.identities_path = self.directory / "approval-identities.json"
        self.permissions_path = self.directory / "approval-permissions.json"
        self.log_path = self.directory / "approval-authority.log"
        self.process = self.url = self.port = self._log = self._reader = None
        self._tokens = {}

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.close()

    def start(self):
        if self.process is not None:
            raise RuntimeError("The approval authority is already started")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._write(self.identities_path, {name: {"role": role, "token_env": TOKEN_ENV[role]}
                                           for name, role in self.roles.items()})
        self._write(self.permissions_path, {name: [{"target": item, "operations": OPERATIONS[role]}
                                                   for item in (self.target, *self.additional_targets)]
                                            for name, role in self.roles.items()})
        self._tokens = {name: secrets.token_urlsafe(48) for name in self.roles}
        environment = dict(os.environ)
        for name in (*TOKEN_ENV.values(), URL_ENV, IDENTITY_ENV):
            environment.pop(name, None)
        environment.update({TOKEN_ENV[role]: self._tokens[name] for name, role in self.roles.items()})
        self._log = self.log_path.open("ab")
        try:
            self.process = subprocess.Popen(
                [self.python, "-m", "runtime.approval_http", "--db", str(self.db_path),
                 "--identities", str(self.identities_path), "--permissions", str(self.permissions_path),
                 "--bind", "127.0.0.1", "--port", "0",
                 "--max-lifetime-seconds", str(self.max_lifetime_seconds)],
                cwd=ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=self._log, text=True, encoding="utf-8")
            self.port = self._bound_port()
            self.url = f"http://127.0.0.1:{self.port}"
            self._wait_ready()
        except BaseException:
            self.close()
            raise
        return self

    @staticmethod
    def _write(path, value):
        path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    def _bound_port(self):
        lines = queue.Queue()

        def drain(stream):
            with stream:
                for line in stream:
                    lines.put(line)
            lines.put(None)
        self._reader = threading.Thread(target=drain, args=(self.process.stdout,), daemon=True)
        self._reader.start()
        try:
            line = lines.get(timeout=self.startup_timeout)
        except queue.Empty:
            raise AuthorityUnavailable("Approval authority did not report its port in time") from None
        try:
            announced = json.loads(line) if line else None
            port = announced["port"] if isinstance(announced, dict) and announced.get("event") == "listening" else None
        except ValueError:
            port = None
        if type(port) is not int or not 0 < port <= 65535:
            raise AuthorityUnavailable("Approval authority stopped before serving; see approval-authority.log")
        return port

    def _wait_ready(self):
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            if self.ready():
                return
            time.sleep(0.1)
        raise AuthorityUnavailable("Approval authority did not become ready; see approval-authority.log")

    def ready(self):
        """True when /readyz answers 200; any failure is not ready."""
        if self.port is None:
            return False
        connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request("GET", "/readyz")
            return connection.getresponse().status == 200
        except OSError:
            return False
        finally:
            connection.close()

    def running(self):
        return self.process is not None and self.process.poll() is None

    def client(self, identity, *, timeout=10):
        if identity not in self._tokens or self.url is None:
            raise AuthorityUnavailable("The approval authority is not running")
        return ApprovalClient(self.url, identity, self._tokens[identity], timeout=timeout)

    def operator_client(self):
        return self.client(self.operator)

    def guard_client(self):
        return self.client(LAB_GUARD)

    def executor_client(self):
        return self.client(LAB_EXECUTOR)

    def executor_environment(self):
        """Environment for a child Executor process; the token never goes in argv or a file."""
        if LAB_EXECUTOR not in self._tokens or self.url is None:
            raise AuthorityUnavailable("The approval authority is not running")
        return {URL_ENV: self.url, IDENTITY_ENV: LAB_EXECUTOR, TOKEN_ENV["executor"]: self._tokens[LAB_EXECUTOR]}

    def stop(self):
        """Stop the child. Clients made earlier now fail, so execution fails closed."""
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self._reader is not None:
            self._reader.join(timeout=5)
        if self._log is not None:
            self._log.close()
            self._log = None

    def close(self):
        self.stop()
        self._tokens.clear()


def executor_client_from_environment(environ=None):
    """Rebuild the Executor's ApprovalClient in a child process from its environment."""
    environ = os.environ if environ is None else environ
    try:
        return ApprovalClient(environ[URL_ENV], environ[IDENTITY_ENV], environ[TOKEN_ENV["executor"]])
    except KeyError:
        raise AuthorityUnavailable("No approval authority was passed to this process") from None


def require_attended(mode, stdin=None):
    """Interactive approval needs a person at a terminal; refuse before any cluster change."""
    if mode not in MODES:
        raise ValueError("Unknown approval mode")
    stdin = sys.stdin if stdin is None else stdin
    if mode == "interactive" and not (stdin is not None and stdin.isatty()):
        raise ValueError("Interactive approval needs a terminal. For an unattended lab run pass "
                         "--approval auto-lab, which is recorded as automated approval.")


def _printable(text, limit):
    text = text if isinstance(text, str) else repr(text)
    cleaned = "".join(char if char.isprintable() else " " for char in text)
    return cleaned[:limit] + (" [truncated]" if len(cleaned) > limit else "")


class Approver:
    """Base strategy: approve(proposal, guard_decision) returns the record or None."""
    mode = None
    decided_by = None

    def __init__(self, authority, *, lifetime_seconds=180, clock=None):
        if not 0 < lifetime_seconds <= 300:
            raise ValueError("Approval lifetime must be at most 300 seconds")
        self.authority, self.lifetime_seconds = authority, lifetime_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.approvals, self.refusals = [], []

    @property
    def identity(self):
        return self.authority.operator if self.authority is not None else None

    def describe(self):
        return {"mode": self.mode, "identity": self.identity, "via": "approval_http",
                "human": self.mode in {"interactive", "ui"}, "decided_by": self.decided_by,
                "approvals": list(self.approvals), "refusals": list(self.refusals)}

    def refuse(self, proposal, reason):
        self.refusals.append({"action_id": proposal["action_id"], "reason": reason})
        return None

    def create(self, proposal, decision):
        expires = self.clock() + timedelta(seconds=self.lifetime_seconds)
        created = self.authority.operator_client().post("create", {
            "action_id": proposal["action_id"], "proposal_sha256": _digest(proposal),
            "policy_version": decision["policy_version"], "expires_at": utc(expires)})
        record = created["approval"]
        self.approvals.append({"action_id": record["action_id"], "approval_id": created["approval_id"],
                               "operator_id": record["operator_id"], "approved_at": record["approved_at"],
                               "expires_at": record["expires_at"]})
        return record

    def __call__(self, proposal, decision):
        raise NotImplementedError


class InteractiveApprover(Approver):
    mode = "interactive"
    decided_by = "a person at the harness prompt typed the action ID"

    def __init__(self, authority, *, ask=input, say=print, **options):
        super().__init__(authority, **options)
        self.ask, self.say = ask, say

    def facts(self, proposal, decision):
        # Every proposal field is printed through _printable: in agent mode the record comes from model output.
        target = {key: _printable(value, FIELD_LIMIT) for key, value in proposal["target"].items()}
        parameters = proposal.get("parameters", {})
        # P5.07: a SCALE's operator sees the replica count it would write; other actions print as before.
        replicas = (["  desired_replicas: " + _printable(str(parameters.get("desired_replicas")), FIELD_LIMIT)]
                    if proposal.get("action") == "SCALE" else [])
        return [
            "Approval requested. Facts below come from the proposal record and the Guard decision.",
            "  target:          {cluster_id}/{namespace} {kind}/{name} uid={uid}".format(**target),
            "  action:          " + _printable(proposal["action"], FIELD_LIMIT),
            "  action ID:       " + _printable(proposal["action_id"], FIELD_LIMIT),
            "  baseline_id:     " + _printable(str(parameters.get("baseline_id")), FIELD_LIMIT),
            "  template_sha256: " + _printable(str(parameters.get("template_sha256", ""))[:12], FIELD_LIMIT),
            *replicas,
            "  proposal digest: " + _digest(proposal),
            "  Guard reasons:   " + _printable(", ".join(decision.get("reason_codes", [])), FIELD_LIMIT * 4),
            "  UNTRUSTED model text: " + _printable(proposal.get("rationale", ""), RATIONALE_LIMIT),
        ]

    def __call__(self, proposal, decision):
        for line in self.facts(proposal, decision):
            self.say(line)
        try:
            typed = self.ask("Type the action ID to approve (anything else refuses): ")
        except EOFError:
            typed = ""
        if not isinstance(typed, str) or typed.strip() != proposal["action_id"]:
            self.say("Approval refused: the typed text is not this action ID. No approval was recorded.")
            return self.refuse(proposal, "typed action ID did not match")
        return self.create(proposal, decision)


class AutoLabApprover(Approver):
    mode = "auto-lab"
    decided_by = "automated-lab approval: explicit opt-in for an unattended lab run; no person decided"

    def __init__(self, authority, **options):
        if authority.operator != AUTOMATED_LAB:
            raise ValueError("Automated lab approval must use the automated-lab identity")
        super().__init__(authority, **options)

    def __call__(self, proposal, decision):
        return self.create(proposal, decision)


class RejectingApprover(Approver):
    mode = "reject"
    decided_by = "every approval refused (negative test)"

    @property
    def identity(self):
        return None

    def __call__(self, proposal, decision):
        return self.refuse(proposal, "reject mode")


class DashboardApprover(Approver):
    """Waits for a decision made in the lab dashboard and relayed by its server.

    The dashboard server calls approve_offer and revoke_offer on its HTTP
    threads; both use this run's operator credential, which stays in memory.
    """
    mode = "ui"
    decided_by = ("an operator typed the action ID and clicked Approve in the lab dashboard; the request "
                  "carried the per-session lab token, which shows the click came through the dashboard, "
                  "not which person made it")

    def __init__(self, authority, *, relay, wait_seconds=600, **options):
        if relay is None:
            raise ValueError("Dashboard approval needs the dashboard's approval relay")
        if not 0 < wait_seconds <= 3600:
            raise ValueError("Dashboard approval wait must be positive and at most 1 hour")
        super().__init__(authority, **options)
        self.relay, self.wait_seconds = relay, wait_seconds
        self.revocations = []

    def describe(self):
        return {**super().describe(), "revocations": list(self.revocations)}

    def __call__(self, proposal, decision):
        record = self.relay.offer(self, proposal, decision, wait_seconds=self.wait_seconds)
        if record is None:
            return self.refuse(proposal, "no dashboard approval before the request expired or the dashboard stopped")
        return record

    def approve_offer(self, proposal, decision):
        record = self.create(proposal, decision)
        return record, self.approvals[-1]["approval_id"]

    def revoke_offer(self, proposal, approval_id, reason):
        revocation = self.authority.operator_client().post("revoke", {
            "action_id": proposal["action_id"], "proposal_sha256": _digest(proposal),
            "approval_id": approval_id, "reason": reason})["revocation"]
        self.revocations.append({"action_id": revocation["action_id"], "approval_id": revocation["approval_id"],
                                 "operator_id": revocation["operator_id"], "revoked_at": revocation["revoked_at"]})
        return revocation


def operator_identity(mode, operator=None):
    """The operator identity the authority grants for this approval mode."""
    if mode not in MODES:
        raise ValueError("Unknown approval mode")
    if mode == "auto-lab":
        if operator not in {None, AUTOMATED_LAB}:
            raise ValueError("Automated lab approval always uses the automated-lab identity")
        return AUTOMATED_LAB
    if operator == AUTOMATED_LAB:
        raise ValueError("The automated-lab identity is reserved for --approval auto-lab")
    if operator in {LAB_GUARD, LAB_EXECUTOR}:
        raise ValueError("The lab-guard and lab-executor identities are reserved for the read-only readers")
    if operator is not None and not (isinstance(operator, str) and IDENTITY_NAME.fullmatch(operator)):
        raise ValueError("An operator identity is 1 to 128 letters, digits, '.', '_' or '-', "
                         "starting with a letter or digit")
    return operator or (DASHBOARD_OPERATOR if mode == "ui" else LAB_OPERATOR)


def approver_for(mode, authority, *, relay=None, **options):
    """The approver for a mode; relay is the dashboard's ApprovalRelay and is used only by ui."""
    plain = {k: v for k, v in options.items() if k not in {"ask", "say"}}
    if mode == "interactive":
        return InteractiveApprover(authority, **options)
    if mode == "auto-lab":
        return AutoLabApprover(authority, **plain)
    if mode == "reject":
        return RejectingApprover(authority, **plain)
    if mode == "ui":
        return DashboardApprover(authority, relay=relay, **plain)
    raise ValueError("Unknown approval mode")
