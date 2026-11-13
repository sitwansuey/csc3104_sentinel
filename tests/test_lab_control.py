"""Offline tests: UI gates cannot choose targets, repeat work or bypass a gate."""

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from runtime.dashboard import make_server
from runtime.lab_control import LabConflict, LabControl


def control(directory, **changes):
    return LabControl(directory, **{**dict(cluster_id="sentinel-boutique-lab", context="kind-sentinel-boutique-lab",
                                          namespace="sentinel-boutique", deployment="shippingservice"), **changes})


class LabFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.control = control(self.directory.name)
        self.addCleanup(self.control.finish, "STOPPED")

    def ready(self, scenario="primary"):
        completed = []
        def wait():
            try:
                self.control.wait_for_injection(scenario)
                completed.append(scenario)
            except InterruptedError:
                completed.append("stopped")
        worker = threading.Thread(target=wait, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        # Close before join: unittest cleanup runs in reverse order.
        self.addCleanup(self.control.finish, "STOPPED")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.control.snapshot()
            if state["state"] == "READY":
                return worker, completed, state
            time.sleep(.005)
        self.fail("Harness did not reach the ready gate")

class LabControlTests(LabFixture):
    def test_scope_is_fixed_and_token_is_not_persisted(self):
        for values in (dict(cluster_id="production"), dict(context="admin"), dict(deployment="other")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                control(self.directory.name, **values)
        access = self.control.capabilities()
        audit = (Path(self.directory.name) / "lab-session.json").read_text()
        self.assertNotIn(access["control_token"], audit)
        self.assertNotIn("control_token", audit)

    def test_no_request_is_accepted_during_setup_or_for_a_stale_session(self):
        with self.assertRaises(LabConflict):
            self.control.request_injection(self.control.snapshot()["session_id"], "not-ready")
        _, completed, state = self.ready()
        for session, gate in (("old-session", state["gate_id"]), (state["session_id"], "old-gate")):
            with self.assertRaises(LabConflict):
                self.control.request_injection(session, gate)
        self.assertEqual(completed, [])

    def test_duplicate_clicks_release_only_one_scenario_and_not_the_next(self):
        worker, completed, first = self.ready()
        results = []
        def click():
            results.append(self.control.request_injection(first["session_id"], first["gate_id"]))
        clicks = [threading.Thread(target=click) for _ in range(2)]
        for thread in clicks: thread.start()
        for thread in clicks: thread.join(2)
        worker.join(2)
        self.assertEqual(completed, ["primary"])
        self.assertEqual(sorted(item["duplicate"] for item in results), [False, True])
        self.control.complete_scenario("RECOVERED", incident_id="INC-1", action_id="ACT-1")
        _, next_completed, second = self.ready("crash")
        self.assertTrue(self.control.request_injection(first["session_id"], first["gate_id"])["duplicate"])
        self.assertEqual(next_completed, [])
        self.assertEqual(self.control.snapshot()["gate_id"], second["gate_id"])
        self.assertTrue(self.control.snapshot()["can_inject"])

    def test_failed_audit_write_does_not_release_the_waiting_harness(self):
        _, completed, state = self.ready()
        with patch.object(self.control, "_persist", side_effect=OSError("storage unavailable")):
            with self.assertRaises(OSError):
                self.control.request_injection(state["session_id"], state["gate_id"])
        self.assertEqual(completed, [])
        self.assertTrue(self.control.snapshot()["can_inject"])

    def test_close_unblocks_waiter_and_prevents_later_mutation(self):
        worker, completed, state = self.ready()
        self.control.finish("STOPPED")
        worker.join(2)
        self.assertEqual(completed, ["stopped"])
        with self.assertRaises(LabConflict):
            self.control.request_injection(state["session_id"], state["gate_id"])

    def test_outcomes_and_full_sequence_are_required_before_completion(self):
        with self.assertRaises(LabConflict):
            self.control.wait_for_injection("crash")
        with self.assertRaises(LabConflict):
            self.control.finish("COMPLETED")
        for scenario, expected in (("primary", "RECOVERED"), ("crash", "RECOVERED"), ("unnecessary-rollback", "REJECTED")):
            worker, _, state = self.ready(scenario)
            self.control.request_injection(state["session_id"], state["gate_id"])
            worker.join(2)
            with self.assertRaises(LabConflict):
                self.control.complete_scenario("APPLIED", incident_id="INC", action_id="ACT")
            self.control.complete_scenario(expected, incident_id="INC-" + scenario, action_id="ACT-" + scenario)
        self.control.finish("COMPLETED")
        self.assertEqual(self.control.snapshot()["state"], "COMPLETED")
        self.assertFalse(self.control.snapshot()["can_inject"])


class CatalogScopeTests(unittest.TestCase):
    """SCOPES and the step order come from experiments/boutique/catalog.json, not from constants."""

    CONNECTED = dict(cluster_id="sentinel-boutique-connected", context="kind-sentinel-boutique-connected",
                     namespace="sentinel-boutique-connected")

    def test_scopes_are_every_catalog_target_in_each_of_its_scopes(self):
        from experiments.boutique.harness import scenarios
        from runtime import lab_control
        catalog = scenarios.load()
        expected = {(catalog["scopes"][scope]["cluster_id"], catalog["scopes"][scope]["context"],
                     catalog["scopes"][scope]["namespace"], name)
                    for name, target in catalog["targets"].items() if not target.get("pair_only")
                    for scope in target["scopes"]}
        self.assertEqual(set(lab_control.SCOPES), expected)
        # redis-cart is repaired only inside a scenario pair (P4.05); the lab controls never drive it.
        self.assertNotIn("redis-cart", {scope[3] for scope in lab_control.SCOPES})
        self.assertEqual([step[0] for step in lab_control.SCENARIOS], [step["id"] for step in catalog["lab_sequence"]])

    def test_a_second_catalog_target_is_allowed_only_in_its_own_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            cart = control(directory, deployment="cartservice", **self.CONNECTED)
            try:
                self.assertEqual(cart.snapshot()["target"]["name"], "cartservice")
            finally:
                cart.finish("STOPPED")
            # cartservice has no isolated lab manifest, so the single-service lab may not name it.
            for values in (dict(deployment="cartservice"), dict(deployment="paymentservice", **self.CONNECTED)):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    control(directory, **values)

    def test_scopes_follow_a_changed_catalog(self):
        from experiments.boutique.harness import scenarios
        catalog = scenarios.load()
        for name in ("cartservice", "redis-cart"):
            catalog["targets"].pop(name)
        catalog["scenarios"] = [item for item in catalog["scenarios"]
                                if item["target"] not in {"cartservice", "redis-cart"}]
        catalog["pairs"] = []
        names = {scope[3] for scope in scenarios.lab_scopes(scenarios.validate(catalog))}
        self.assertEqual(names, {"shippingservice"})


class LabControlHTTPTests(LabFixture):
    def setUp(self):
        super().setUp()
        self.server = make_server("missing", "missing", port=0, lab_control=self.control)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, payload, **changes):
        headers = {"Content-Type": "application/json", "Origin": self.base,
                   "X-Sentinel-Lab-Token": self.control.capabilities()["control_token"]}
        headers.update(changes)
        return Request(self.base + "/api/lab/inject", data=json.dumps(payload).encode(), method="POST", headers=headers)

    def test_get_snapshot_exposes_state_without_control_secret(self):
        with urlopen(self.base + "/api/snapshot", timeout=3) as response:
            snapshot = json.load(response)
        self.assertEqual(snapshot["lab"]["state"], "PREPARING")
        self.assertNotIn("control_token", snapshot["lab"])
        with urlopen(self.base + "/api/lab", timeout=3) as response:
            access = json.load(response)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        self.assertTrue(self.control.authenticates(access["control_token"]))

    def test_default_dashboard_does_not_expose_the_control_routes(self):
        server = make_server("missing", "missing", port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base + "/api/snapshot", timeout=3) as response:
                self.assertNotIn("lab", json.load(response))
            for request, status in ((base + "/api/lab", 404),
                    (Request(base + "/api/lab/inject", data=b"{}", method="POST"), 501)):
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=3)
                self.assertEqual(error.exception.code, status)
                error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_only_current_authenticated_same_origin_request_releases_gate(self):
        worker, completed, state = self.ready()
        payload = {"session_id": state["session_id"], "gate_id": state["gate_id"]}
        for headers in ({"Origin": "http://other.example"}, {"X-Sentinel-Lab-Token": "wrong"},
                        {"Host": "other.example:8765"}):
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as error:
                urlopen(self.request(payload, **headers), timeout=3)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        with self.assertRaises(HTTPError) as error:
            urlopen(self.request({**payload, "target": "production"}), timeout=3)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(completed, [])
        with urlopen(self.request(payload), timeout=3) as response:
            self.assertEqual(response.status, 202)
        worker.join(2)
        self.assertEqual(completed, ["primary"])
        with urlopen(self.request(payload), timeout=3) as response:
            self.assertTrue(json.load(response)["duplicate"])

    def test_wrong_gate_and_wrong_content_type_cannot_start_a_scenario(self):
        _, completed, state = self.ready()
        for payload, headers, expected in (({"session_id": state["session_id"], "gate_id": "stale"}, {}, 409),
                ({"session_id": state["session_id"], "gate_id": state["gate_id"]}, {"Content-Type": "text/plain"}, 415)):
            with self.assertRaises(HTTPError) as error:
                urlopen(self.request(payload, **headers), timeout=3)
            self.assertEqual(error.exception.code, expected)
            error.exception.close()
        self.assertEqual(completed, [])


if __name__ == "__main__":
    unittest.main()
