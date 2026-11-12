"""Invariant checks over synthetic records: each check passes a clean run and fails its defect."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from experiments.distributed import invariants

ACTION, INCIDENT = "ACT-1", "INC-1"


def clean():
    """Records of one recovered incident with one write and one acknowledged result per command."""
    return {
        "lease_seconds": 4, "terminal_in_time": True,
        "incidents": [{"incident_id": INCIDENT, "state": "RECOVERED", "active_action_id": ACTION}],
        "actions": [{"action_id": ACTION, "incident_id": INCIDENT, "state": "RECOVERED"}],
        "transitions": [
            {"seq": 1, "incident_id": INCIDENT, "action_id": None, "from_state": None, "to_state": "OPEN"},
            {"seq": 2, "incident_id": INCIDENT, "action_id": ACTION, "from_state": "OPEN", "to_state": "READY"},
            {"seq": 3, "incident_id": INCIDENT, "action_id": ACTION, "from_state": "READY", "to_state": "VERIFYING"},
            {"seq": 4, "incident_id": INCIDENT, "action_id": ACTION, "from_state": "VERIFYING", "to_state": "RECOVERED"},
        ],
        "outbox": [{"id": 1, "topic": "executor.request", "action_id": ACTION, "delivered": 1},
                   {"id": 2, "topic": "verifier.request", "action_id": ACTION, "delivered": 1}],
        "receipts": [{"event_id": 1, "delivery_id": "d1", "owner": "executor-a", "attempts": 1,
                      "finished": 10.0, "result_digest": "r1"}],
        "message_digests": ["m1"],
        "revocations": [],
        "cluster_writes": [{"action_id": ACTION, "host": "executor-a", "decided_at": 100.0, "marker": "write"}],
        "executor_ledgers": {"executor-a": [{"action_id": ACTION, "phase": "DONE"}], "executor-b": []},
        "worker_results": [{"identity": "executor-a", "event_id": 1, "accepted": True,
                            "result_digest": "r1", "message_digest": "m1"}],
        "observer_outbox": [{"incident_id": INCIDENT, "delivered": 1}],
        "access": [
            {"ts": "2026-11-12T10:00:00Z", "identity": "executor-a", "operation": "claim", "http_status": 200,
             "outcome": "claimed", "delivery_id": "d1"},
            {"ts": "2026-11-12T10:00:02Z", "identity": "executor-a", "operation": "result", "http_status": 200,
             "outcome": "accepted", "delivery_id": "d1"},
            {"ts": "2026-11-12T10:00:03Z", "identity": "executor-b", "operation": "claim", "http_status": 200,
             "outcome": "claimed", "delivery_id": "d1"},
        ],
        "secret_scan": {"files_scanned": 3, "argv_scanned": 7, "hits": []},
    }


def failed(records):
    return {item["id"] for item in invariants.check(records) if not item["passed"]}


class InvariantTests(unittest.TestCase):
    def test_a_clean_run_passes_every_check(self):
        results = invariants.check(clean())
        self.assertEqual([item["id"] for item in results],
                         ["I1", "I2", "I3", "I4", "I5", "I6", "X1", "X2", "X3", "X4"])
        self.assertEqual(failed(clean()), set())

    def test_two_committed_writes_for_one_action_fail_i1(self):
        records = clean()
        records["cluster_writes"].append({"action_id": ACTION, "host": "executor-b", "decided_at": 101.0,
                                          "marker": "write"})
        self.assertEqual(failed(records), {"I1"})

    def test_an_unannotated_malformed_or_fence_template_write_fails_i1(self):
        for row in ({"action_id": "unannotated", "marker": "none"}, {"action_id": "unknown", "marker": "write"},
                    {"action_id": ACTION, "marker": "fence"}):
            records = clean()
            records["cluster_writes"] = [dict(row, host="executor-b", decided_at=101.0)]
            with self.subTest(row=row):
                self.assertEqual(failed(records), {"I1"})

    def test_a_write_after_revocation_fails_i2_unless_the_action_went_to_review(self):
        records = clean()
        records["revocations"] = [{"action_id": ACTION, "revoked_at": "1970-01-01T00:01:00Z"}]
        self.assertEqual(failed(records), {"I2"})
        records["actions"][0]["state"] = "NEEDS_REVIEW"
        self.assertNotIn("I2", failed(records))
        records = clean()
        records["revocations"] = [{"action_id": ACTION, "revoked_at": "1970-01-01T00:02:00Z"}]
        self.assertEqual(failed(records), set())

    def test_a_non_terminal_or_late_incident_fails_i3(self):
        records = clean()
        records["incidents"][0]["state"] = "READY"
        records["transitions"].append({"seq": 5, "incident_id": INCIDENT, "action_id": ACTION,
                                       "from_state": "RECOVERED", "to_state": "READY"})
        self.assertEqual(failed(records), {"I3"})
        late = clean()
        late["terminal_in_time"] = False
        self.assertEqual(failed(late), {"I3"})
        empty = clean()
        empty["incidents"], empty["transitions"], empty["observer_outbox"] = [], [], []
        self.assertIn("I3", failed(empty))

    def test_two_acknowledged_results_for_one_command_fail_i4(self):
        records = clean()
        records["worker_results"].append({"identity": "executor-b", "event_id": 1, "accepted": True,
                                          "result_digest": "r2", "message_digest": "m1"})
        self.assertEqual(failed(records), {"I4"})
        differs = clean()
        differs["receipts"][0]["result_digest"] = "other"
        self.assertEqual(failed(differs), {"I4"})

    def test_repeated_http_400_refusals_or_unbounded_attempts_fail_i5(self):
        records = clean()
        records["access"] += [{"ts": f"2026-11-12T10:00:0{n}Z", "identity": "executor-a", "operation": "result",
                               "http_status": 400, "outcome": "invalid", "delivery_id": "d1"} for n in range(3)]
        self.assertEqual(failed(records), {"I5"})
        two = clean()
        two["access"] += records["access"][-2:]
        self.assertEqual(failed(two), set())
        attempts = clean()
        attempts["receipts"][0]["attempts"] = invariants.MAX_ATTEMPTS + 1
        self.assertEqual(failed(attempts), {"I5"})

    def test_an_uncertain_or_started_ledger_row_fails_i6(self):
        for phase in ("UNCERTAIN", "WRITE_STARTED"):
            with self.subTest(phase=phase):
                records = clean()
                records["executor_ledgers"]["executor-b"] = [{"action_id": ACTION, "phase": phase}]
                self.assertEqual(failed(records), {"I6"})

    def test_a_second_claim_during_a_current_lease_fails_x1(self):
        records = clean()
        records["access"] = [records["access"][0], {**records["access"][2], "ts": "2026-11-12T10:00:01Z"}]
        self.assertEqual(failed(records), {"X1"})
        renewed = clean()
        renewed["access"] = [renewed["access"][0],
                             {**renewed["access"][0], "operation": "renew", "outcome": "renewed",
                              "ts": "2026-11-12T10:00:03Z"},
                             {**renewed["access"][2], "ts": "2026-11-12T10:00:06Z"}]
        self.assertEqual(failed(renewed), {"X1"})
        expired = deepcopy(renewed)
        expired["access"][2]["ts"] = "2026-11-12T10:00:08Z"
        self.assertEqual(failed(expired), set())

    def test_a_secret_hit_fails_x2(self):
        records = clean()
        records["secret_scan"]["hits"] = ["logs/executor-a.stderr.log"]
        self.assertEqual(failed(records), {"X2"})

    def test_a_lost_acknowledgement_fails_x3(self):
        records = clean()
        records["message_digests"] = []
        self.assertEqual(failed(records), {"X3"})
        observer = clean()
        observer["observer_outbox"].append({"incident_id": "INC-LOST", "delivered": 1})
        self.assertEqual(failed(observer), {"X3"})

    def test_a_broken_journal_or_unjournalled_command_fails_x4(self):
        records = clean()
        records["transitions"][2]["from_state"] = "VALIDATING"
        self.assertEqual(failed(records), {"X4"})
        command = clean()
        command["transitions"] = [row for row in command["transitions"] if row["to_state"] != "READY"]
        command["transitions"][1]["from_state"] = "OPEN"
        self.assertEqual(failed(command), {"X4"})
        state = clean()
        state["incidents"][0]["state"] = "FAILED"
        self.assertEqual(failed(state), {"X4"})

    def test_details_name_counts_and_identifiers_only(self):
        records = clean()
        records["cluster_writes"].append({"action_id": ACTION, "host": "executor-b", "decided_at": 101.0,
                                          "marker": "write"})
        (i1,) = [item for item in invariants.check(records) if item["id"] == "I1"]
        self.assertEqual(i1["detail"], "ACT-1: 2 committed writes")


class SecretScanTests(unittest.TestCase):
    def test_scan_reports_file_and_argv_names_never_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / name for name in ("logs", "config")}
            for path in paths.values():
                path.mkdir()
            secret = "s" * 43
            (paths["logs"] / "clean.log").write_text("nothing here", encoding="utf-8")
            (paths["config"] / "leaky.json").write_text('{"token": "' + secret + '"}', encoding="utf-8")
            topology = SimpleNamespace(run_dir=root, paths=paths, processes={
                "worker": SimpleNamespace(command=["python", "-m", "x", "--token", secret]),
                "other": SimpleNamespace(command=["python", "-m", "y"])})
            scan = invariants.scan_secrets(topology, [secret, ""])
        self.assertEqual(scan["files_scanned"], 2)
        self.assertEqual(scan["argv_scanned"], 2)
        self.assertEqual(sorted(scan["hits"]), ["argv of worker", str(Path("config") / "leaky.json")])
        self.assertNotIn(secret, repr(scan))


if __name__ == "__main__":
    unittest.main()
