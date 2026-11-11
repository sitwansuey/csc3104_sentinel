"""Broad exception handlers are justified, logged by class name only, and gated by ruff."""
import ast
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch

from agent.llm import Ollama
from agent.progress import notify
from experiments.validation.demo_guard import load_fixtures
from runtime.executor_pipeline import GuardAuthorizer
from runtime.remote_worker import Lease, RemoteWorker
from scripts.ci import except_audit, ruff_baseline
from services.executor.core import ExecutionAdapters, execute
from services.executor.store import LocalTargetCoordinator, SQLiteExecutionLedger
from services.guard.core import proposal_fingerprint
from shared import log
from tests.support.executor_fake import FakeDeploymentAPI

ROOT = Path(__file__).resolve().parents[1]
SECRET = "Bearer PRIVATE-s3cr3t-token-value"
NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=UTC)


def audit(source, path="services/planted.py"):
    return except_audit.problems(except_audit.handlers(path, source=source))


class AuditRuleTests(unittest.TestCase):
    def test_repository_has_no_unjustified_broad_handler(self):
        found = except_audit.scan()
        self.assertEqual(except_audit.problems(found), [])
        counts = except_audit.summary(found)
        self.assertEqual(counts["unjustified"], 0)
        self.assertGreaterEqual(counts["total"], 71)

    def test_silent_pass_is_reported(self):
        problems = audit("try:\n    f()\nexcept Exception:\n    pass\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("without '# noqa: BLE001", problems[0])

    def test_bare_except_is_always_reported(self):
        source = "try:\n    f()\nexcept:  # noqa: BLE001 - a long enough reason here\n    raise\n"
        self.assertIn("bare 'except:'", audit(source)[0])

    def test_reason_and_logging_are_both_required(self):
        no_reason = "try:\n    f()\nexcept Exception as e:  # noqa: BLE001\n    log.failure(LOG, 'x.y', e)\n"
        no_log = "try:\n    f()\nexcept Exception as e:  # noqa: BLE001 - keep serving the other requests\n    g()\n"
        self.assertIn("must say why", audit(no_reason)[0])
        self.assertIn("does not log", audit(no_log)[0])

    def test_tuple_with_exception_and_base_exception_are_broad(self):
        source = ("try:\n    f()\nexcept (ValueError, Exception):\n    g()\n"
                  "try:\n    f()\nexcept BaseException:\n    g()\n")
        self.assertEqual(len(audit(source)), 2)

    def test_reraise_logged_and_narrow_handlers_are_accepted(self):
        source = ("try:\n    f()\nexcept BaseException:\n    close()\n    raise\n"
                  "try:\n    f()\nexcept Exception as e:\n    raise ValueError('x') from e\n"
                  "try:\n    f()\nexcept Exception as e:  # noqa: BLE001 - keep serving the other requests\n"
                  "    log.failure(LOG, 'x.y', e)\n"
                  "try:\n    f()\nexcept (OSError, ValueError):\n    pass\n")
        self.assertEqual(audit(source), [])

    def test_inner_handler_is_not_excused_by_an_outer_reraise(self):
        source = ("try:\n    f()\nexcept Exception:\n    try:\n        g()\n"
                  "    except Exception:\n        pass\n    raise\n")
        problems = audit(source)
        self.assertEqual(len(problems), 1)
        self.assertIn(":6:", problems[0])

    def test_raising_a_new_error_without_the_cause_does_not_count_as_reraise(self):
        source = "try:\n    f()\nexcept Exception as e:\n    raise ValueError('lost cause')\n"
        self.assertEqual(len(audit(source)), 1)


class LoggedFailuresNeverCarryMessagesTests(unittest.TestCase):
    """Statically: every log call in a broad handler passes the exception object or its class name."""

    def test_handlers_log_only_the_exception_class(self):
        checked, problems = 0, []
        for directory in except_audit.DIRECTORIES:
            for path in sorted((ROOT / directory).rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
                    # Calls in this handler only; a nested handler is checked on its own.
                    for call in (node for node in except_audit._walk(handler.body) if isinstance(node, ast.Call)):
                        name = getattr(call.func, "attr", getattr(call.func, "id", None))
                        if name not in {"failure", "emit"}:
                            continue
                        checked += 1
                        where = f"{path.relative_to(ROOT).as_posix()}:{call.lineno}"
                        if name == "failure" and not (len(call.args) >= 3 and isinstance(call.args[2], ast.Name)
                                                      and call.args[2].id == handler.name):
                            problems.append(where + ": failure() must receive the caught exception")
                        for keyword in call.keywords:
                            if self.mentions_error(keyword.value, handler.name):
                                problems.append(where + f": {keyword.arg} is derived from the exception")
        self.assertEqual(problems, [])
        self.assertGreaterEqual(checked, 45)

    @staticmethod
    def mentions_error(node, name):
        """True if node uses the exception other than as type(name).__name__."""
        if name is None:
            return False
        allowed = {id(child.value.args[0]) for child in ast.walk(node)
                   if isinstance(child, ast.Attribute) and child.attr == "__name__"
                   and isinstance(child.value, ast.Call) and getattr(child.value.func, "id", None) == "type"
                   and len(child.value.args) == 1}
        return any(isinstance(child, ast.Name) and child.id == name and id(child) not in allowed
                   for child in ast.walk(node))

    def test_message_derived_fields_are_detected(self):
        def value(text):
            return ast.parse(text, mode="eval").body
        self.assertFalse(self.mentions_error(value("type(error).__name__"), "error"))
        for text in ("str(error)", "f'{type(error).__name__}: {error}'", "error.args[0]", "repr(error)"):
            with self.subTest(text=text):
                self.assertTrue(self.mentions_error(value(text), "error"))


class CapturedLogs(unittest.TestCase):
    def capture(self):
        stream = io.StringIO()
        scope = log.configured("test", level="DEBUG", fmt="json", stream=stream)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        return stream

    def records(self, stream, event):
        text = stream.getvalue()
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn("s3cr3t", text)
        return [record for record in map(json.loads, text.splitlines()) if record["event"] == event]


class FailureHelperTests(CapturedLogs):
    def test_failure_records_the_class_name_and_never_the_message(self):
        stream = self.capture()
        log.failure("demo", "demo.failed", RuntimeError(SECRET), action_id="ACT-1", reason=None)
        (record,) = self.records(stream, "demo.failed")
        self.assertEqual((record["level"], record["error_type"], record["action_id"]),
                         ("warning", "RuntimeError", "ACT-1"))
        self.assertNotIn("msg", record)

    def test_caller_cannot_replace_the_error_type_or_add_a_message_field(self):
        stream = self.capture()
        log.failure("demo", "demo.failed", KeyError(SECRET), error_type=SECRET, message=SECRET, level=log.ERROR)
        (record,) = self.records(stream, "demo.failed")
        self.assertEqual((record["level"], record["error_type"]), ("error", "KeyError"))

    def test_silent_until_configured(self):
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            log.failure("demo", "demo.failed", RuntimeError(SECRET))
        self.assertEqual(stderr.getvalue(), "")


class AuditedSiteLoggingTests(CapturedLogs):
    """Representative sites log the failure; the secret in each message never appears."""

    def executor(self, **changes):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        fixtures = load_fixtures()
        proposal = fixtures["proposal"]
        approval = {"action_id": proposal["action_id"], "proposal_sha256": proposal_fingerprint(proposal),
                    "policy_version": fixtures["policy"]["policy_version"], "operator_id": "fabricated-test-operator",
                    "decision": "APPROVED", "approved_at": (NOW - timedelta(seconds=5)).isoformat(),
                    "expires_at": (NOW + timedelta(seconds=30)).isoformat()}
        authorizer = GuardAuthorizer(
            policy_reader=lambda p: deepcopy(fixtures["policy"]),
            baseline_reader=lambda p: deepcopy(fixtures["baseline"]),
            dependency_reader=lambda p: deepcopy(fixtures["dependencies"]),
            approval_reader=changes.pop("approval_reader", lambda p: deepcopy(approval)))
        ledger = SQLiteExecutionLedger(Path(directory.name) / "executor.db")
        self.addCleanup(ledger.close)
        cluster = changes.pop("cluster", FakeDeploymentAPI)(fixtures["live_state"], clock=lambda: NOW)
        adapters = ExecutionAdapters(ledger=ledger, coordination=LocalTargetCoordinator(Path(directory.name) / "locks"),
                                     cluster=cluster, authorizer=authorizer, clock=lambda: NOW)
        return proposal, adapters, cluster

    def test_executor_prewrite_refusal_logs_the_class_only(self):
        def approval_reader(_proposal):
            raise RuntimeError(SECRET)
        stream = self.capture()
        proposal, adapters, cluster = self.executor(approval_reader=approval_reader)
        result = execute(deepcopy(proposal), adapters)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(cluster.write_attempts, [])
        (record,) = self.records(stream, "executor.prewrite.refused")
        self.assertEqual((record["error_type"], record["action_id"]), ("RuntimeError", proposal["action_id"]))

    def test_executor_indeterminate_write_logs_the_class_only(self):
        class LostWrite(FakeDeploymentAPI):
            def conditional_restore(self, **kwargs):
                raise TimeoutError(SECRET)
        stream = self.capture()
        proposal, adapters, _cluster = self.executor(cluster=LostWrite)
        execute(deepcopy(proposal), adapters)
        (record,) = self.records(stream, "executor.write.indeterminate")
        self.assertEqual(record["error_type"], "TimeoutError")

    def test_lease_loss_is_logged_without_the_lease_token(self):
        class Client:
            calls = 0

            def post(self, operation, body):
                Client.calls += 1
                if Client.calls > 1:
                    raise ConnectionError(SECRET)
                return {}
        stream = self.capture()
        lease = Lease(Client(), {"delivery_id": "D-1", "lease_token": SECRET, "expires_at": time.time() + 60},
                      interval=0.01)
        with lease:
            deadline = time.monotonic() + 5
            while lease.error is None and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertIsInstance(lease.error, ConnectionError)
        (record,) = self.records(stream, "transport.lease.lost")
        self.assertEqual(record["error_type"], "ConnectionError")

    def test_failed_retry_post_is_logged_and_the_original_error_still_raises(self):
        class Client:
            def post(self, operation, body):
                if operation == "retry":
                    raise ConnectionError(SECRET)
                return {}

        class Journal:
            released = []

            def get(self, key):
                return {"result": None}

            def release(self, key):
                self.released.append(key)

        class Adapter:
            def process(self, delivery, lease):
                raise ValueError(SECRET)

        stream = self.capture()
        worker = RemoteWorker(Client(), Journal(), Adapter())
        delivery = {"delivery_id": "D-1", "lease_token": SECRET, "expires_at": time.time() + 60}
        with self.assertRaises(ValueError):
            worker._process("key-1", delivery)
        self.assertEqual(Journal.released, ["key-1"])
        (record,) = self.records(stream, "transport.retry.failed")
        self.assertEqual(record["error_type"], "ConnectionError")

    def test_progress_callback_failure_is_logged_and_swallowed(self):
        def callback(_event):
            raise RuntimeError(SECRET)
        stream = self.capture()
        notify(callback, {"stage": "planning"})
        (record,) = self.records(stream, "progress.notify.failed")
        self.assertEqual(record["error_type"], "RuntimeError")

    def test_optional_model_endpoint_failure_is_logged_with_its_path(self):
        llm = Ollama({"base_url": "http://127.0.0.1:11434", "model": "fixture-model", "timeout": 3})
        stream = self.capture()
        with patch.object(llm, "_request", side_effect=RuntimeError(SECRET)):
            self.assertIsNone(llm._optional("/api/version"))
        (record,) = self.records(stream, "model.optional.failed")
        self.assertEqual((record["error_type"], record["path"]), ("RuntimeError", "/api/version"))


class LintGateTests(unittest.TestCase):
    def config(self):
        return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_blind_except_rules_are_selected(self):
        selected = set(self.config()["tool"]["ruff"]["lint"]["select"])
        self.assertTrue({"BLE001", "S110", "E722"} <= selected)

    def test_blind_except_rules_are_never_baselined_outside_tests(self):
        entries = ruff_baseline.baseline((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertGreater(len(entries), 0)
        self.assertEqual(ruff_baseline.forbidden(entries), [])
        self.assertEqual(ruff_baseline.forbidden({"services/x.py": {"BLE001"}, "tests/t.py": {"BLE001"}}),
                         ["services/x.py: BLE001"])
        parsed = self.config()["tool"]["ruff"]["lint"]["per-file-ignores"]
        self.assertEqual({path: set(codes) for path, codes in parsed.items()}, entries)

    @unittest.skipUnless(importlib.util.find_spec("ruff"), "ruff is not installed (it is in requirements-dev.lock)")
    def test_ruff_rejects_a_new_silent_broad_except(self):
        for path in ("services/planted.py", "services/executor/core.py"):
            with self.subTest(path=path):
                result = subprocess.run(
                    [sys.executable, "-m", "ruff", "check", "--no-cache", "--output-format", "concise",
                     "--stdin-filename", path, "-"],
                    input="def f(g):\n    try:\n        g()\n    except Exception:\n        pass\n",
                    cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("BLE001", result.stdout)
                self.assertIn("S110", result.stdout)


if __name__ == "__main__":
    unittest.main()
