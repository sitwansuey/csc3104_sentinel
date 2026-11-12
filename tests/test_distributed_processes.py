"""Fault scenarios run as separate OS processes on one host, against the file cluster.

Each test starts the Coordinator, five workers and the Observer feed as their
own processes (experiments/distributed/topology.py), injects one fault and
checks invariants I1-I6 and X1-X4 from the durable records and logs. These
are processes on one host, not separate machines.

The plan's CI subset (S1b, S1c, S2, S4, S5) and S7 run by default, in about a
minute and a half. Set SENTINEL_DISTRIBUTED_ALL=1 to run every scenario,
including S7b.
Known defects on this code base are asserted exactly (expected_failures), so a
fix makes the test fail until the expectation is updated with the fix.
"""
import os
import unittest

from experiments.distributed.run_scenarios import run_once
from experiments.distributed.scenarios import SCENARIOS

ALL = os.environ.get("SENTINEL_DISTRIBUTED_ALL") == "1"


class ScenarioMixin:
    maxDiff = None

    def run_scenario(self, name):
        summary = run_once(name)
        failed = {item["id"] for item in summary["invariants"] if not item["passed"]}
        details = {item["id"]: item["detail"] for item in summary["invariants"] if not item["passed"]}
        self.assertIsNone(summary["harness_error"])
        self.assertEqual(failed, set(SCENARIOS[name].expected_failures), details)
        self.assertTrue(summary["single_host"] and summary["synthetic_planner"])
        self.assertLessEqual(summary["committed_writes"], 1)
        return summary


class DistributedProcessTests(ScenarioMixin, unittest.TestCase):
    def test_s1b_executor_killed_after_the_write_intent_leaves_its_ledger_in_doubt(self):
        summary = self.run_scenario("S1b")
        self.assertEqual(summary["notes"]["exit_code"], 73)
        self.assertEqual(summary["states"], ["RECOVERED"])

    def test_s1c_executor_killed_after_the_patch_is_adopted_by_the_peer_with_one_write(self):
        summary = self.run_scenario("S1c")
        self.assertEqual((summary["states"], summary["committed_writes"]), (["RECOVERED"], 1))

    def test_s2_a_stalled_lease_holder_never_writes_after_the_peer_took_over(self):
        summary = self.run_scenario("S2")
        self.assertEqual(summary["notes"]["stall"], "block+blackhole" if os.name == "nt" else "suspend")
        self.assertEqual((summary["states"], summary["committed_writes"]), (["RECOVERED"], 1))

    def test_s4_a_dropped_result_response_is_recovered_by_replaying_the_saved_body(self):
        summary = self.run_scenario("S4")
        self.assertTrue(summary["notes"]["replay_receipt"])
        self.assertEqual(summary["states"], ["RECOVERED"])

    def test_s5_coordinator_restarts_between_commit_and_reply_lose_nothing(self):
        summary = self.run_scenario("S5")
        self.assertEqual(summary["notes"]["exit_code"], 73)
        self.assertEqual(summary["states"], ["RECOVERED"])

    def test_s7_executor_clocks_300_s_behind_end_failed_without_a_write_or_a_wedge(self):
        summary = self.run_scenario("S7")
        # Before P3.03 the result was refused with HTTP 400 for ever and the incident stayed READY. Now it is
        # accepted; the Executors' own Guard re-check refuses the approval 300 s in their future, so no write.
        self.assertEqual((summary["states"], summary["committed_writes"]), (["FAILED"], 0))
        self.assertEqual(summary["notes"]["refusals_seen"], 0)
        measured = summary["clock_offsets_measured"]
        for name in ("executor-a", "executor-b"):  # theta = Coordinator - worker: 300 s for a clock 300 s behind.
            self.assertAlmostEqual(measured[name]["theta_seconds"], 300, delta=1)  # Both measure before claiming.
        self.assertLess(abs(measured["guard-1"]["theta_seconds"]), 1)


@unittest.skipUnless(ALL, "set SENTINEL_DISTRIBUTED_ALL=1 to run every scenario")
class AllDistributedScenarioTests(ScenarioMixin, unittest.TestCase):
    def test_s0_no_fault(self):
        self.run_scenario("S0")

    def test_s1a_planner_killed_mid_inference(self):
        self.run_scenario("S1a")

    def test_s1d_verifier_killed_mid_collection(self):
        self.run_scenario("S1d")

    def test_s3_lease_holder_partitioned(self):
        self.run_scenario("S3")

    def test_s6_every_worker_partitioned(self):
        summary = self.run_scenario("S6")
        self.assertEqual(summary["notes"]["during_partition"]["writes"], 0)

    def test_s7b_an_executor_outside_the_skew_bound_fails_closed_and_its_peer_recovers(self):
        summary = self.run_scenario("S7b")
        self.assertEqual((summary["states"], summary["committed_writes"]), (["RECOVERED"], 1))
        self.assertEqual(summary["notes"]["claims_by_skewed"], 0)
        self.assertEqual(summary["notes"]["readyz"]["status"], 503)
        self.assertTrue(summary["notes"]["readyz"]["clock_skew"])
        self.assertAlmostEqual(summary["clock_offsets_measured"]["executor-a"]["theta_seconds"], 5, delta=1)


if __name__ == "__main__":
    unittest.main()
