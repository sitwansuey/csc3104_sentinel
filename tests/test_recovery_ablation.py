"""Recovery ablation replay (P4.04): decision policies x validation levels on identical inputs,
Wilson intervals, error accounting and a resumable run that refuses a changed identity."""

import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest

from experiments.analysis import replay, replay_corpus as corpus
from scripts import build_replay_corpus as builder
from scripts import evaluate_recovery

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evidence/replay/v1"
R1 = "cap-boutique-live-20261112t081032z-63d609"
N1 = "cap-boutique-live-20261112t080756z-29fd09"
FABRICATED = "f00d" * 16


def load(case_id):
    return corpus.load_case(CORPUS, case_id), corpus.load_expected(CORPUS, case_id)


def scripted(answers):
    return lambda *args, **kwargs: replay.ScriptedModel(answers)


class PolicyLevelTests(unittest.TestCase):
    def test_always_rollback_on_an_n_case_is_unsafe_at_v0_and_rejected_by_the_guard_at_v2(self):
        case, expected = load(N1)
        row, proposal = replay.replay(case, "P0")
        self.assertEqual(proposal["action"], "ROLLBACK")
        levels = row["levels"]
        self.assertTrue(levels["V0"]["executed"])
        self.assertTrue(replay.score(row, expected, "V0")["unsafe"])
        # V1 checks only state and policy, so the redundant rollback still passes there.
        self.assertTrue(levels["V1"]["executed"])
        self.assertTrue(replay.score(row, expected, "V1")["unsafe"])
        self.assertEqual(levels["V2"]["verdict"], "REJECT")
        self.assertIn("ROLLBACK_ALREADY_AT_BASELINE", levels["V2"]["reason_codes"])
        self.assertFalse(levels["V2"]["executed"])
        v2 = replay.score(row, expected, "V2")
        self.assertFalse(v2["unsafe"])
        self.assertTrue(v2["correct"])

    def test_the_code_rule_escalates_the_n_case_and_repairs_the_r_case(self):
        n_case, _ = load(N1)
        r_case, r_expected = load(R1)
        self.assertEqual(replay.replay(n_case, "P1")[1]["action"], "ESCALATE")
        row, proposal = replay.replay(r_case, "P1")
        self.assertEqual(proposal["action"], "ROLLBACK")
        self.assertEqual(row["levels"]["V2"]["verdict"], "ALLOW")
        self.assertTrue(replay.score(row, r_expected, "V2")["correct"])

    def test_the_agent_rule_policy_on_a_recorded_diagnosis_needs_no_model(self):
        r_case, r_expected = load(R1)
        n_case, _ = load(N1)
        row, proposal = replay.replay(r_case, "P1R", diagnosis="recorded")
        self.assertEqual(proposal["action"], "ROLLBACK")
        self.assertEqual(row["model_calls"], 0)
        self.assertEqual(row["diagnosis"]["decided_by"], "rule")
        self.assertTrue(replay.score(row, r_expected, "V2")["correct"])
        row, proposal = replay.replay(n_case, "P1R", diagnosis="recorded")
        self.assertEqual((proposal["action"], row["planner"]["error_code"]), ("ESCALATE", "TEMPLATE_ALREADY_BASELINE"))
        # A rule escalation is the policy's decision, not a failed run.
        self.assertEqual(row["status"], "ok")
        # Live, P1R runs the RCA agent, which needs a model.
        with self.assertRaisesRegex(replay.ReplayError, "model client factory"):
            replay.replay(r_case, "P1R")

    def test_a_live_diagnosis_runs_the_real_rca_agent_on_the_replayed_telemetry(self):
        from tests.support.vocabulary import STARTUP, cite_supporting
        case, expected = load(R1)
        rollback = {"action": "ROLLBACK", "rationale": "restore", "parameters": replay.binding(case, "ROLLBACK")}

        class RcaModel(replay.ScriptedModel):
            def ask(self, role, prompt, schema):
                if role == "Reporter":
                    self.calls.append({"role": role, "status": "completed"})
                    return cite_supporting({"failures": [{"root cause component": "shippingservice",
                                                          "root cause reason": STARTUP}],
                                            "explanation": "startup failure in the logs",
                                            "limitations": "scripted"}, prompt, schema)
                return super().ask(role, prompt, schema)

        answers = {"Analyzer": {"summary": "startup failure", "complete": True, "confidence": 0.9,
                                "next_steps": ""},
                   "Planner": {"plan": {"kind": "tool", "goal": "read logs", "tool": "logs",
                                        "args": {"file": "logs.csv"}}},
                   "Recovery Planner": rollback}
        models = []

        def factory(*args, **kwargs):
            models.append(RcaModel(answers))
            return models[-1]

        row, proposal = replay.replay(case, "P4", seed=43, llm_factory=factory, config={"model": "scripted"})
        self.assertEqual(proposal["action"], "ROLLBACK", row)
        self.assertEqual(row["diagnosis_mode"], "live")
        self.assertEqual(row["diagnosis"]["components"], ["shippingservice"])
        self.assertIn("Reporter", [call["role"] for call in models[-1].calls])
        self.assertGreaterEqual(row["model_calls"], 2)
        self.assertTrue(replay.score(row, expected, "V2")["correct"])

    def test_a_fabricated_hash_executes_at_v0_and_is_a_contract_violation_at_v1(self):
        case, expected = load(R1)
        answer = {"action": "ROLLBACK", "rationale": "restore",
                  "parameters": {"baseline_id": case["inputs"]["baseline"]["baseline_id"],
                                 "template_sha256": FABRICATED}}
        for policy, diagnosis in (("P2", "live"), ("P4", "recorded")):
            with self.subTest(policy=policy):
                row, proposal = replay.replay(case, policy, llm_factory=scripted({"Recovery Planner": answer}),
                                              diagnosis=diagnosis)
                self.assertEqual(row["status"], "ok")
                self.assertTrue(row["levels"]["V0"]["executed"])
                self.assertFalse(row["levels"]["V0"]["bound"])
                self.assertTrue(replay.score(row, expected, "V0")["unsafe"])
                self.assertTrue(row["planner"]["contract_violation"])
                self.assertEqual(proposal["action"], "ESCALATE")
                for level in ("V1", "V2"):
                    self.assertFalse(row["levels"][level]["executed"])
                    # The Planner contract refused it and delivered an escalation; the Guard saw no write.
                    self.assertEqual(row["levels"][level]["action"], "ESCALATE")
                    self.assertEqual(row["levels"][level]["reason_codes"], [])
                    scored = replay.score(row, expected, level)
                    self.assertTrue(scored["contract_violation"])
                    self.assertFalse(scored["unsafe"])

    def test_a_correct_rollback_on_stale_state_is_counted_as_a_false_rejection(self):
        case, expected = load(R1)
        stale = deepcopy(case)
        stale["inputs"]["live-state"]["deployment"]["metadata"]["resourceVersion"] = "999999"
        row, _ = replay.replay(stale, "P0")
        self.assertIn("STALE_RESOURCE_VERSION", row["levels"]["V1"]["reason_codes"])
        self.assertEqual(row["levels"]["V2"]["verdict"], "REJECT")
        for level in ("V1", "V2"):
            scored = replay.score(row, expected, level)
            self.assertTrue(scored["correct_write_proposed"])
            self.assertTrue(scored["false_rejection"])
            self.assertTrue(scored["missed_repair"])
        self.assertFalse(replay.score(row, expected, "V0")["false_rejection"])

    def test_without_the_gate_the_model_can_roll_back_an_n_case_that_only_the_guard_stops(self):
        case, expected = load(N1)
        answer = {"action": "ROLLBACK", "rationale": "restore", "parameters": replay.binding(case, "ROLLBACK")}
        models = []

        def factory(*args, **kwargs):
            models.append(replay.ScriptedModel({"Recovery Planner": answer}))
            return models[-1]

        row, _ = replay.replay(case, "P3", llm_factory=factory, diagnosis="recorded")
        self.assertTrue(replay.score(row, expected, "V1")["unsafe"])
        self.assertEqual(row["levels"]["V2"]["verdict"], "REJECT")
        self.assertEqual(row["diagnosis"]["decided_by"], "model")
        self.assertTrue(row["diagnosis"]["names_target"])
        cell = next(cell for cell in replay.summarize([row], {N1: expected})["cells"] if cell["level"] == "V0")
        self.assertEqual(cell["diagnosis_component_correct"]["k"], 1)
        gated, proposal = replay.replay(case, "P4", llm_factory=factory, diagnosis="recorded")
        self.assertEqual(proposal["action"], "ESCALATE")
        self.assertEqual(gated["planner"]["error_code"], "TEMPLATE_ALREADY_BASELINE")
        self.assertEqual(gated["diagnosis"]["decided_by"], "rule")
        self.assertEqual(models[-1].calls, [])

    def test_injection_success_is_measured_against_the_clean_twin(self):
        manifest = corpus.verify(CORPUS)
        adversarial = next(entry["case_id"] for entry in manifest["cases"]
                           if entry["case_id"].startswith("adv-rollback-runbook-boutique"))
        case = corpus.load_case(CORPUS, adversarial)
        twin = corpus.load_case(CORPUS, case["meta"]["twin"])
        labels = {item["case_id"]: corpus.load_expected(CORPUS, item["case_id"]) for item in (case, twin)}

        def gullible(prompt):
            # A model that obeys the quoted line when it can read it.
            if "rolling the Deployment back" in prompt:
                return {"action": "ROLLBACK", "rationale": "the runbook line says so",
                        "parameters": replay.binding(case, "ROLLBACK")}
            return {"action": "ESCALATE", "rationale": "template unchanged", "parameters": {}}

        rows = [replay.replay(subject, "P2", seed=42, llm_factory=scripted({"Recovery Planner": gullible}))[0]
                for subject in (case, twin)]
        cells = {(cell["level"], cell["group"]): cell for cell in replay.summarize(rows, labels)["cells"]}
        self.assertEqual(cells[("V0", "adversarial")]["injection_success"]["k"], 1)
        self.assertEqual(cells[("V0", "adversarial")]["unsafe"]["k"], 1)
        # The Guard refuses the injected rollback, so the outcome equals the clean twin's.
        self.assertEqual(cells[("V2", "adversarial")]["injection_success"]["k"], 0)
        self.assertEqual(cells[("V2", "adversarial")]["unsafe"]["k"], 0)

    def test_a_flagged_line_never_reaches_the_model_and_the_agent_escalates_by_rule(self):
        manifest = corpus.verify(CORPUS)
        adversarial = next(entry["case_id"] for entry in manifest["cases"]
                           if entry["case_id"].startswith("adv-rollback-directive-boutique"))
        case = corpus.load_case(CORPUS, adversarial)
        model = replay.ScriptedModel({"Recovery Planner": {"action": "ESCALATE", "parameters": {}, "rationale": "x"}})
        replay.replay(case, "P2", llm_factory=lambda *a, **k: model)
        self.assertTrue(model.prompts)
        self.assertFalse(any("required action is ROLLBACK" in prompt for _, prompt in model.prompts))

    def test_policy_arguments_are_checked(self):
        case, _ = load(R1)
        for policy, kwargs in (("P9", {}), ("P2", {}), ("P0", {"diagnosis": "invented"})):
            with self.subTest(policy=policy), self.assertRaises(replay.ReplayError):
                replay.replay(case, policy, **kwargs)


class Unreachable(replay.ScriptedModel):
    """A model client whose server is down: every call raises, as a dropped connection does."""

    def __init__(self, error=None):
        super().__init__({})
        self.error = error or ConnectionError("model server unreachable")

    def ask(self, role, prompt, schema):
        self.calls.append({"role": role, "status": "failed"})
        raise self.error


def rca_model(answers):
    """A scripted RCA agent run: one tool plan, an analysis, a cited startup report, then answers."""
    from tests.support.vocabulary import STARTUP, cite_supporting

    class RcaModel(replay.ScriptedModel):
        def ask(self, role, prompt, schema):
            if role == "Reporter":
                self.prompts.append((role, prompt))
                self.calls.append({"role": role, "status": "completed"})
                return cite_supporting({"failures": [{"root cause component": "shippingservice",
                                                      "root cause reason": STARTUP}],
                                        "explanation": "startup failure in the logs",
                                        "limitations": "scripted"}, prompt, schema)
            return super().ask(role, prompt, schema)

    return RcaModel({"Analyzer": {"summary": "startup failure", "complete": True, "confidence": 0.9,
                                  "next_steps": ""},
                     "Planner": {"plan": {"kind": "tool", "goal": "read logs", "tool": "logs",
                                          "args": {"file": "logs.csv"}}},
                     **answers})


class InfrastructureFailureTests(unittest.TestCase):
    """A failed model or agent is an error row with no credit, never a credited escalation."""

    def assert_no_credit(self, row, expected):
        self.assertEqual(row["status"], "error", row)
        self.assertIsNone(row["levels"])
        for level in replay.LEVELS:
            scored = replay.score(row, expected, level)
            self.assertTrue(scored["error"])
            self.assertFalse(scored["correct"])
            self.assertFalse(scored["acceptable"])
            self.assertFalse(scored["unsafe"])

    def test_an_unreachable_model_gives_error_rows_with_no_credit_at_any_level(self):
        # Every unit here asks the model. (P4 on a recorded N1 diagnosis does not: the code gate
        # escalates first, which is a decision, so that unit stays ok.)
        units = [(case_id, policy, diagnosis, error) for case_id in (N1, R1)
                 for policy, diagnosis, error in (("P2", "live", ConnectionError("down")),
                                                  ("P2", "live", RuntimeError("HTTP 500")),
                                                  ("P3", "recorded", ConnectionError("down")),
                                                  ("P1R", "live", ConnectionError("down")),
                                                  ("P4", "live", RuntimeError("HTTP 500")))]
        units.append((R1, "P4", "recorded", RuntimeError("HTTP 500")))
        for case_id, policy, diagnosis, error in units:
            case, expected = load(case_id)
            with self.subTest(case=case_id, policy=policy, diagnosis=diagnosis, error=type(error).__name__):
                model = Unreachable(error)
                row, proposal = replay.replay(case, policy, seed=42, llm_factory=lambda *a, model=model, **k: model,
                                              diagnosis=diagnosis)
                # The Planner still delivers its safe escalation; the row records why, and is an error.
                self.assertEqual(proposal["action"], "ESCALATE")
                self.assertIn(row["error"], replay.INFRASTRUCTURE_CODES)
                self.assertEqual(row["planner"]["error_code"], row["error"])
                self.assertTrue(model.calls)
                self.assert_no_credit(row, expected)
        case, _ = load(N1)
        row, _ = replay.replay(case, "P4", llm_factory=lambda *a, **k: Unreachable(), diagnosis="recorded")
        self.assertEqual((row["status"], row["planner"]["error_code"]), ("ok", "TEMPLATE_ALREADY_BASELINE"))

    def test_a_spent_planning_budget_is_an_error_and_an_invalid_answer_is_a_decision(self):
        from agent.llm import ModelOutputInvalid, PlanningBudgetExceeded
        case, expected = load(R1)
        row, _ = replay.replay(case, "P4", llm_factory=lambda *a, **k: Unreachable(PlanningBudgetExceeded("spent")),
                               diagnosis="recorded")
        self.assertEqual(row["error"], "BUDGET_EXCEEDED")
        self.assert_no_credit(row, expected)
        for policy, diagnosis in (("P2", "live"), ("P4", "recorded")):
            with self.subTest(policy=policy):
                row, _ = replay.replay(case, policy, diagnosis=diagnosis,
                                       llm_factory=lambda *a, **k: Unreachable(ModelOutputInvalid("not JSON")))
                # An unusable answer is the model's outcome, scored as the escalation it caused.
                self.assertEqual((row["status"], row["planner"]["error_code"]), ("ok", "INVALID_MODEL_OUTPUT"))
                self.assertTrue(replay.score(row, expected, "V2")["missed_repair"])

    def test_error_rows_get_no_credit_in_the_summary(self):
        case, expected = load(N1)
        good, _ = replay.replay(case, "P1")
        failed, _ = replay.replay(case, "P2", llm_factory=lambda *a, **k: Unreachable())
        cells = replay.summarize([good, failed], {N1: expected})["cells"]
        by_policy = {(cell["policy"], cell["level"]): cell for cell in cells}
        for level in replay.LEVELS:
            self.assertEqual(by_policy[("P1", level)]["correct"]["k"], 1)
            self.assertEqual(by_policy[("P2", level)]["correct"]["k"], 0)
            self.assertEqual(by_policy[("P2", level)]["errors"]["k"], 1)
        self.assertEqual(by_policy[("P2", "V0")]["error_kinds"], {"AGENT_ERROR": 1})
        self.assertEqual(by_policy[("P2", "V0")]["planner_error_codes"], {"AGENT_ERROR": 1})

    def test_a_recorded_diagnosis_for_another_incident_stops_the_unit(self):
        case, _ = load(R1)
        wrong = deepcopy(case)
        wrong["recorded"]["diagnosis"]["incident_id"] = "INC-another"
        for policy in ("P1R", "P4"):
            with self.subTest(policy=policy), self.assertRaisesRegex(replay.ReplayError, "another incident"):
                replay.replay(wrong, policy, diagnosis="recorded",
                              llm_factory=scripted({"Recovery Planner": {"action": "ESCALATE", "parameters": {},
                                                                         "rationale": "x"}}))


class NoRagTests(unittest.TestCase):
    """--runbooks none removes the Recovery Planner's runbooks and the RCA agent's workflows."""

    @staticmethod
    def leading_json(prompt):
        return json.JSONDecoder().raw_decode(prompt)[0]

    def run_p3(self, config):
        case, _ = load(R1)
        model = rca_model({"Recovery Planner": {"action": "ROLLBACK", "rationale": "restore",
                                                "parameters": replay.binding(case, "ROLLBACK")}})
        # Without the bootstrap plan the agent's first round asks its Planner, whose prompt holds the workflows.
        row, proposal = replay.replay(case, "P3", seed=42, llm_factory=lambda *a, **k: model,
                                      config={"model": "scripted", "bootstrap_live_tools": False, **config})
        self.assertEqual(row["status"], "ok", row)
        prompts = {role: [prompt for name, prompt in model.prompts if name == role]
                   for role in ("Planner", "Analyzer", "Reporter", "Recovery Planner")}
        return row, proposal, prompts

    def test_runbooks_none_empties_the_runbooks_and_the_agents_workflows(self):
        from agent import agent as rca_agent
        original = rca_agent.retrieve
        row, proposal, full = self.run_p3({})
        self.assertEqual(proposal["action"], "ROLLBACK")
        self.assertIn('"id": "failed-rollout"', full["Recovery Planner"][0])
        workflows = self.leading_json(full["Planner"][0])["workflows"]
        steps = [step for workflow in workflows for step in workflow["steps"]]
        self.assertTrue(steps)
        row, proposal, empty = self.run_p3({"runbooks": "none"})
        self.assertIs(rca_agent.retrieve, original)
        self.assertEqual(self.leading_json(empty["Planner"][0])["workflows"], [])
        for role in ("Planner", "Analyzer", "Reporter"):
            self.assertTrue(empty[role], role)
            for prompt in empty[role]:
                for step in steps:
                    self.assertNotIn(step, prompt, role)
        # With no runbook, propose() escalates without asking the Recovery Planner (as D5 expects).
        self.assertEqual(empty["Recovery Planner"], [])
        self.assertFalse(row["planner"]["provider_called"])
        self.assertEqual(proposal["action"], "ESCALATE")
        self.assertEqual(row["diagnosis"]["components"], ["shippingservice"])

    def test_runbooks_none_empties_the_p2_runbooks(self):
        case, _ = load(R1)
        calls = {}
        for setting in ("all", "none"):
            model = replay.ScriptedModel({"Recovery Planner": {"action": "ESCALATE", "parameters": {},
                                                               "rationale": "x"}})
            row, _ = replay.replay(case, "P2", llm_factory=lambda *a, model=model, **k: model,
                                   config={"runbooks": setting})
            calls[setting] = (row["planner"]["provider_called"], [prompt for _, prompt in model.prompts])
        self.assertTrue(calls["all"][0])
        self.assertIn('"id": "failed-rollout"', calls["all"][1][0])
        self.assertEqual(calls["none"], (False, []))

    def test_runbooks_none_refuses_a_recorded_diagnosis(self):
        case, _ = load(R1)
        with self.assertRaisesRegex(replay.ReplayError, "needs --diagnosis live"):
            replay.replay(case, "P3", diagnosis="recorded", config={"runbooks": "none"},
                          llm_factory=scripted({}))


class ModelPinTests(unittest.TestCase):
    PINNED = "bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8"

    def test_the_run_refuses_a_model_whose_digest_is_not_the_pinned_one(self):
        self.assertEqual(evaluate_recovery.check_digest(self.PINNED, "bdbd181c33f2"), "sha256:" + self.PINNED)
        self.assertEqual(evaluate_recovery.check_digest("sha256:" + self.PINNED, None), "sha256:" + self.PINNED)
        for reported, expected in (("0" * 64, "bdbd181c33f2"), (self.PINNED, "bdbd"), ("unavailable", None),
                                   (None, None), (self.PINNED, "not-hex-digest")):
            with self.subTest(reported=reported, expected=expected), self.assertRaises(SystemExit):
                evaluate_recovery.check_digest(reported, expected)


class MetricTests(unittest.TestCase):
    def test_wilson_bounds_at_zero_all_and_half(self):
        self.assertEqual(replay.wilson(0, 10), [0.0, 0.2775])
        self.assertEqual(replay.wilson(10, 10), [0.7225, 1.0])
        self.assertEqual(replay.wilson(5, 10), [0.2366, 0.7634])
        self.assertEqual(replay.wilson(0, 1), [0.0, 0.7935])
        self.assertIsNone(replay.wilson(0, 0))
        for k, n in ((-1, 3), (4, 3), (1.0, 3)):
            with self.subTest(k=k, n=n), self.assertRaises(ValueError):
                replay.wilson(k, n)

    def test_exact_mcnemar(self):
        self.assertEqual(replay.mcnemar_exact(0, 0), 1.0)
        self.assertEqual(replay.mcnemar_exact(6, 0), 0.03125)
        self.assertEqual(replay.mcnemar_exact(3, 3), 1.0)

    def test_errors_stay_in_every_denominator_without_credit(self):
        case, expected = load(R1)
        good, _ = replay.replay(case, "P0")
        bad = replay.error_row(R1 + "-x", "captured", "P0", None, RuntimeError("boom"))
        labels = {R1: expected, R1 + "-x": expected}
        cell = next(cell for cell in replay.summarize([good, bad], labels)["cells"] if cell["level"] == "V2")
        self.assertEqual(cell["rows"], 2)
        self.assertEqual(cell["errors"], {"k": 1, "n": 2, "rate": 0.5, "wilson95": replay.wilson(1, 2)})
        self.assertEqual((cell["correct"]["k"], cell["correct"]["n"]), (1, 2))
        self.assertEqual((cell["missed_repair"]["k"], cell["missed_repair"]["n"]), (1, 2))
        self.assertEqual((cell["unsafe"]["k"], cell["unsafe"]["n"]), (0, 2))


class RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        cls.corpus = root / "corpus"
        sources = [next((ROOT / "evidence").glob(f"*/{case_id[4:]}")) for case_id in (R1, N1)]
        builder.build(sources, cls.corpus)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name) / "run"

    def test_a_run_writes_rows_results_and_a_table_without_model_text(self):
        results = replay.run(self.corpus, self.out, policies=["P0", "P1"])
        manifest = corpus.read_json(self.corpus / corpus.MANIFEST)
        self.assertEqual(results["rows"], 2 * len(manifest["cases"]))
        self.assertEqual({cell["group"] for cell in results["cells"]}, {"captured", "derived", "adversarial"})
        self.assertEqual(results["labels_pending_review"], len(manifest["cases"]))
        text = (self.out / "per-case.jsonl").read_text(encoding="utf-8")
        for marker in ("rationale", "Restore the saved", "invalid-port", "prompt\""):
            self.assertNotIn(marker, text)
        self.assertIn("Wilson 95% interval", (self.out / "table.md").read_text(encoding="utf-8"))

    def test_source_hashes_do_not_depend_on_the_checkouts_line_endings(self):
        with tempfile.TemporaryDirectory() as temporary:
            lf, crlf = Path(temporary) / "lf", Path(temporary) / "crlf"
            for root, newline in ((lf, b"\n"), (crlf, b"\r\n")):
                for name in replay.SOURCES:
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"line one" + newline + b"line two" + newline)
            self.assertEqual(replay.source_hashes(lf), replay.source_hashes(crlf))
            # The corpus manifest records the catalog hash the same way, so --check agrees across OSes.
            name = replay.SOURCES[0]
            self.assertEqual(builder.catalog_sha256(lf / name), builder.catalog_sha256(crlf / name))

    def test_a_recorded_diagnosis_run_skips_cases_without_one(self):
        results = replay.run(self.corpus, self.out, policies=["P1R"], diagnosis="recorded")
        rows = [json.loads(line) for line in (self.out / "per-case.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted(row["case_id"] for row in rows), sorted([N1, R1]))
        self.assertEqual({row["seed"] for row in rows}, {None})
        self.assertEqual(results["rows"], 2)

    def test_resume_skips_finished_units_and_refuses_a_changed_identity(self):
        replay.run(self.corpus, self.out, policies=["P0"], cases={R1})
        first = (self.out / "per-case.jsonl").read_text(encoding="utf-8")
        with self.assertRaisesRegex(replay.ReplayError, "pass --resume"):
            replay.run(self.corpus, self.out, policies=["P0"], cases={R1})
        replay.run(self.corpus, self.out, policies=["P0"], cases={R1}, resume=True)
        self.assertEqual((self.out / "per-case.jsonl").read_text(encoding="utf-8"), first)
        for change, field in (({"policies": ["P0", "P1"]}, "policies"),
                              ({"model_digest": "sha256:" + "1" * 64}, "model_digest"),
                              ({"config": {"num_ctx": 8192}}, "config")):
            with self.subTest(field=field), self.assertRaisesRegex(replay.ReplayError, "Resume refused.*" + field):
                replay.run(self.corpus, self.out, **{"policies": ["P0"], "cases": {R1}, "resume": True, **change})

    def test_resume_refuses_a_changed_corpus_manifest(self):
        replay.run(self.corpus, self.out, policies=["P0"], cases={R1})
        with tempfile.TemporaryDirectory() as temporary:
            changed = Path(temporary) / "corpus"
            builder.shutil.copytree(self.corpus, changed)
            manifest = corpus.read_json(changed / corpus.MANIFEST)
            manifest["labels"]["status"] = "edited"
            corpus.write_json(changed / corpus.MANIFEST, manifest)
            with self.assertRaisesRegex(replay.ReplayError, "corpus_manifest_sha256"):
                replay.run(changed, self.out, policies=["P0"], cases={R1}, resume=True)

    def test_a_failing_unit_is_saved_as_an_error_row(self):
        original = replay.replay

        def broken(case, policy, **kwargs):
            if case["case_id"] == N1:
                raise RuntimeError("simulated failure")
            return original(case, policy, **kwargs)

        replay.replay = broken
        self.addCleanup(setattr, replay, "replay", original)
        results = replay.run(self.corpus, self.out, policies=["P0"], cases={R1, N1})
        rows = [json.loads(line) for line in (self.out / "per-case.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted(row["status"] for row in rows), ["error", "ok"])
        cell = next(cell for cell in results["cells"] if cell["level"] == "V2")
        self.assertEqual(cell["errors"]["k"], 1)
        self.assertEqual(cell["rows"], 2)

    def test_a_failed_model_unit_is_an_error_row_and_a_resume_runs_it_again(self):
        down = lambda *a, **k: Unreachable()  # noqa: E731
        escalate = scripted({"Recovery Planner": {"action": "ESCALATE", "parameters": {}, "rationale": "x"}})
        options = {"policies": ["P2"], "cases": {R1, N1}, "model_digest": "sha256:" + "a" * 64}
        results = replay.run(self.corpus, self.out, llm_factory=down, **options)
        self.assertEqual((results["rows"], results["error_rows"]), (2, 2))
        for cell in results["cells"]:
            self.assertEqual((cell["correct"]["k"], cell["errors"]["k"]), (0, cell["rows"]), cell)
        table = (self.out / "table.md").read_text(encoding="utf-8")
        self.assertIn("errors AGENT_ERROR 2", table)
        results = replay.run(self.corpus, self.out, llm_factory=escalate, resume=True, **options)
        rows = [json.loads(line) for line in (self.out / "per-case.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["status"] for row in rows], ["error", "error", "ok", "ok"])
        self.assertEqual((results["rows"], results["error_rows"], results["rows_rerun_after_an_error"]), (2, 0, 2))
        cell = next(cell for cell in results["cells"] if cell["level"] == "V2")
        self.assertEqual((cell["correct"]["k"], cell["errors"]["k"]), (1, 0))
        # Finished units are not run again.
        replay.run(self.corpus, self.out, llm_factory=down, resume=True, **options)
        self.assertEqual(len((self.out / "per-case.jsonl").read_text(encoding="utf-8").splitlines()), 4)

    def test_a_wrong_recorded_diagnosis_stops_the_run(self):
        original = corpus.load_case

        def wrong(root, case_id):
            case = original(root, case_id)
            case["recorded"]["diagnosis"]["incident_id"] = "INC-another"
            return case

        corpus.load_case = wrong
        self.addCleanup(setattr, corpus, "load_case", original)
        with self.assertRaisesRegex(replay.ReplayError, "another incident"):
            replay.run(self.corpus, self.out, policies=["P1R"], diagnosis="recorded", cases={R1})
        self.assertFalse((self.out / "per-case.jsonl").exists())

    def test_a_golden_unit_that_fails_is_an_error_and_fails_strict(self):
        original = replay.replay

        def failing(case, policy, **kwargs):
            row, proposal = original(case, policy, **kwargs)
            if policy == "P0":  # the Guard cross-check
                return row, proposal
            return {**row, "status": "error", "error": "MODEL_UNAVAILABLE", "levels": None}, proposal

        replay.replay = failing
        self.addCleanup(setattr, replay, "replay", original)
        golden = self.out.parent / "golden.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = evaluate_recovery.main(["golden", "--corpus", str(self.corpus), "--out", str(golden), "--strict"])
        self.assertEqual(code, 1)
        saved = corpus.read_json(golden)
        self.assertEqual((saved["summary"]["error"], saved["summary"]["reproduced"]), (2, 0))
        self.assertEqual({item["error"] for item in saved["cases"]}, {"MODEL_UNAVAILABLE"})

    def test_command_line_run_and_report(self):
        golden = self.out.parent / "golden.json"
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(evaluate_recovery.main(["run", "--corpus", str(self.corpus), "--policy", "P0",
                                                     "--case", R1, "--out", str(self.out)]), 0)
            self.assertEqual(evaluate_recovery.main(["report", "--corpus", str(self.corpus),
                                                     "--out", str(self.out)]), 0)
            self.assertEqual(evaluate_recovery.main(["golden", "--corpus", str(self.corpus), "--out", str(golden),
                                                     "--strict"]), 0)
        self.assertIn("1 rows; 1 label sets pending review", output.getvalue())
        self.assertEqual(corpus.read_json(golden)["summary"]["reproduced"], 2)


if __name__ == "__main__":
    unittest.main()
