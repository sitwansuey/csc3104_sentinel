"""The API server audit extract and I7 (P4.01): sanitised rows, and writes counted by effect.

The fixtures in tests/fixtures/distributed_audit are audit events in the
shape the API server writes with deploy/kind/audit-policy.yaml. They are
written for these tests, not captured from a cluster.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from experiments.distributed import audit, invariants

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "distributed_audit"
EXECUTOR = audit.EXECUTOR_USER


def events(name):
    found, skipped = audit.parse((FIXTURES / name).read_text(encoding="utf-8"))
    return found, skipped


def jwt():
    return "eyJ" + "hbGciOiJSUzI1NiJ9" + "." + "eyJzdWIiOiJzeXN0ZW06c2EifQ" + "." + "c2lnbmF0dXJlLXZhbHVlLXgx"


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.events, self.skipped = events("one-write.jsonl")
        self.rows = audit.extract(self.events)

    def by_id(self, number):
        [row] = [row for row in self.rows if row["audit_id"].endswith(f"{number:012d}")]
        return row

    def test_a_partial_last_line_is_skipped_and_counted(self):
        self.assertEqual((len(self.events), self.skipped), (9, 1))

    def test_one_executor_template_write_passes_i7(self):
        result = invariants.i7({"audit": self.rows})
        self.assertTrue(result["passed"], result)
        self.assertEqual(audit.summary(self.rows)["sentinel_template_writes_per_action"], {"ACT-1": 1})
        row = self.by_id(4)
        self.assertEqual((row["effect"], row["marker"], row["action_id"], row["pod"], row["node"]),
                         ("template_write", "write", "ACT-1", "sentinel-executor-a-7c8d9e6f5-a1a1a",
                          "sentinel-dist-worker"))
        self.assertEqual((row["resource_version"], row["generation"]), ("1002", 3))

    def test_a_seal_patch_is_a_fence_not_a_template_write(self):
        row = self.by_id(5)
        self.assertEqual((row["effect"], row["marker"], row["action_id"], row["fence_epoch"]),
                         ("fence", "fence", "ACT-2", "3"))
        self.assertNotIn("/spec/template", row["paths"])

    def test_denied_patches_are_not_writes(self):
        stale, admission, other = self.by_id(6), self.by_id(7), self.by_id(9)
        self.assertEqual([row["effect"] for row in (stale, admission, other)], ["denied"] * 3)
        self.assertEqual((stale["code"], admission["code"], other["code"]), (422, 403, 403))
        self.assertTrue(admission["admission_denied"])
        self.assertFalse(other["admission_denied"])
        self.assertIsNone(stale["resource_version"])
        only_denied = [row for row in self.rows if row["effect"] == "denied"]
        self.assertEqual(audit.template_writes(only_denied), [])
        self.assertTrue(invariants.i7({"audit": only_denied})["passed"])

    def test_a_dry_run_is_not_a_write(self):
        self.assertEqual(self.by_id(3)["effect"], "dry_run")
        self.assertTrue(self.by_id(3)["dry_run"])

    def test_the_operator_injection_is_counted_apart_and_not_against_any_action(self):
        row = self.by_id(1)
        self.assertEqual((row["effect"], row["user"], row["sentinel"]), ("template_write", "<operator>", False))
        self.assertEqual(audit.summary(self.rows)["operator_template_writes"], 1)
        self.assertEqual([item["action_id"] for item in audit.template_writes(self.rows)], ["ACT-1"])

    def test_two_successful_template_patches_for_one_action_fail_i7_and_i1(self):
        found, _ = events("two-writes.jsonl")
        rows = audit.extract(found)
        result = invariants.i7({"audit": rows})
        self.assertFalse(result["passed"])
        self.assertIn("ACT-1: 2 successful template PATCHes", result["detail"])
        self.assertFalse(invariants.i1({"cluster_writes": audit.template_writes(rows)})["passed"])
        self.assertEqual(audit.summary(rows)["max_template_writes_per_action"], 2)

    def test_a_template_write_without_evidence_or_by_another_identity_fails_i7(self):
        unannotated = deepcopy(self.events[3])
        unannotated["requestObject"] = [op for op in unannotated["requestObject"]
                                        if "sentinel.io" not in op.get("path", "")]
        by_guard = deepcopy(self.events[3])
        by_guard["user"]["username"] = "system:serviceaccount:sentinel-dist:sentinel-guard"
        for event, message in ((unannotated, "without valid action evidence"), (by_guard, "not the Executor")):
            with self.subTest(message=message):
                result = invariants.i7({"audit": audit.extract([event])})
                self.assertFalse(result["passed"])
                self.assertIn(message, result["detail"])

    def test_a_merge_patch_or_update_of_the_template_is_a_write_and_a_status_patch_is_not(self):
        merge = deepcopy(self.events[0])
        merge["user"] = dict(self.events[3]["user"])
        self.assertEqual(audit.extract([merge])[0]["effect"], "template_write")
        update = deepcopy(merge)
        update.update(verb="update", requestObject={"metadata": {}, "spec": {}})
        self.assertEqual(audit.extract([update])[0]["effect"], "template_write")
        status = deepcopy(self.events[3])
        status["objectRef"]["subresource"] = "status"
        self.assertEqual(audit.extract([status])[0]["effect"], "request")

    def test_an_unfinished_or_panicked_request_is_never_a_write(self):
        started = dict(self.events[3], stage="ResponseStarted")
        panic = dict(self.events[3], stage="Panic")
        self.assertEqual(audit.extract([started]), [])
        self.assertEqual(audit.extract([panic])[0]["effect"], "failed")

    def test_without_the_audit_log_i7_fails_rather_than_passing_on_nothing(self):
        self.assertFalse(invariants.i7({})["passed"])
        self.assertTrue(invariants.i7({"audit": []})["passed"])

    def test_the_time_window_and_namespaces_bound_the_extract(self):
        windowed = audit.extract(self.events, since="2026-11-13T10:00:30Z", until="2026-11-13T10:01:00.5Z")
        self.assertEqual([row["audit_id"][-2:] for row in windowed], ["03", "04", "05"])
        elsewhere = deepcopy(self.events[0])
        elsewhere["objectRef"]["namespace"] = "kube-system"
        self.assertEqual(audit.extract([elsewhere]), [])


class SanitiserTests(unittest.TestCase):
    def setUp(self):
        self.events, _ = events("one-write.jsonl")
        self.secret_names = ["sentinel-dist-executor-a", "sentinel-dist-operator-1"]

    def test_bodies_ips_agents_credential_ids_and_names_of_people_and_secrets_are_dropped(self):
        poisoned = deepcopy(self.events)
        poisoned[3]["annotations"]["note"] = "Bearer " + jwt()
        poisoned[3]["userAgent"] = "kubectl " + jwt()
        poisoned[7]["requestObject"] = {"data": {"delivery-token": jwt()}}
        poisoned[0]["user"]["username"] = "someone@example.com"
        text = audit.dumps(audit.extract(poisoned), secret_names=self.secret_names)
        for value in (jwt(), "JTI=", "172.18.0.3", "kubectl/v1.37.1", "someone@", "sentinel-dist-executor-a",
                      "STARTUP_FAILURE", "uid-demo", "requestURI", "sourceIPs"):
            with self.subTest(value=value):
                self.assertNotIn(value, text)
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(rows[7]["name"], "<secret>")
        self.assertEqual(set(rows[0]), {
            "audit_id", "at", "verb", "user", "sentinel", "pod", "node", "resource", "subresource", "namespace",
            "name", "code", "reason", "decision", "admission_denied", "dry_run", "effect", "paths", "marker",
            "action_id", "fence_epoch", "resource_version", "generation"})

    def test_assert_clean_refuses_credential_material_and_secret_names(self):
        for text in ('{"x": "' + jwt() + '"}', "-----BEGIN PRIVATE KEY-----", '{"name": "sentinel-dist-operator-1"}'):
            with self.subTest(text=text[:20]), self.assertRaises(audit.AuditError):
                audit.assert_clean(text, secret_names=self.secret_names)
        self.assertEqual(audit.assert_clean("{}", secret_names=self.secret_names), "{}")

    def test_a_malformed_pod_name_is_not_kept(self):
        event = deepcopy(self.events[3])
        event["user"]["extra"][audit.POD_NAME] = ["../../" + jwt()]
        self.assertIsNone(audit.extract([event])[0]["pod"])


if __name__ == "__main__":
    unittest.main()
