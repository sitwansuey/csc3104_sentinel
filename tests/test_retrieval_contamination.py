"""Retrieval contamination (P4.04): no replay label reaches the runbooks, the agent's knowledge,
or any model context built from a case's inputs."""

from pathlib import Path
import shutil
import tempfile
import unittest

from experiments.analysis import replay, replay_corpus as corpus

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evidence/replay/v1"
# Everything a policy can retrieve or be told besides the case inputs.
SOURCES = ("runbooks", "agent/knowledge")


def texts(paths):
    return {path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
            for folder in paths for path in sorted((ROOT / folder).rglob("*"))
            if path.is_file() and path.suffix in (".md", ".json", ".py")}


class ContaminationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.terms = corpus.label_terms(CORPUS)

    def test_the_terms_cover_scenario_ids_names_classes_and_answer_phrases(self):
        for term in ("R1", "N1", "port-invalid", "node-image-evicted", "rollback_correct", "already-restored",
                     "correct action", "expected action"):
            self.assertIn(term, self.terms)

    def test_runbooks_and_agent_knowledge_hold_no_label(self):
        sources = texts(SOURCES)
        self.assertTrue(any(name.startswith("runbooks/") for name in sources))
        self.assertTrue(any(name.startswith("agent/knowledge/templates/") for name in sources))
        self.assertEqual(corpus.contamination(sources, self.terms), [])

    def test_a_label_added_to_a_runbook_is_found(self):
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / "failed-rollout.md"
            shutil.copyfile(ROOT / "runbooks/failed-rollout.md", copy)
            for added, term in (("\nScenario R1: roll back.\n", "R1"),
                                ("\nFor port-invalid the correct action is ROLLBACK.\n", "correct action")):
                with self.subTest(term=term):
                    text = copy.read_text(encoding="utf-8") + added
                    self.assertIn(("runbooks/failed-rollout.md", term),
                                  corpus.contamination({"runbooks/failed-rollout.md": text}, self.terms))

    def test_word_boundaries_avoid_false_matches(self):
        found = corpus.contamination({"x": "R10 and AN1 and xR1 and port-invalid-2"}, ["R1", "N1", "port-invalid"])
        self.assertEqual(found, [])

    def test_no_model_context_built_from_case_inputs_holds_a_label(self):
        """Every prompt a model would get for every case, under P2 and P4, is free of label terms."""
        manifest = corpus.verify(CORPUS)
        prompts = {}
        answer = {"action": "ESCALATE", "parameters": {}, "rationale": "review"}
        for entry in manifest["cases"]:
            case = corpus.load_case(CORPUS, entry["case_id"])
            runs = [("P2", "live")]
            if "diagnosis" in case["recorded"]:
                runs.append(("P4", "recorded"))
            for policy, diagnosis in runs:
                model = replay.ScriptedModel({"Recovery Planner": answer})
                replay.replay(case, policy, llm_factory=lambda *a, model=model, **k: model, diagnosis=diagnosis)
                for number, (role, prompt) in enumerate(model.prompts):
                    prompts[f"{entry['case_id']}/{policy}/{role}/{number}"] = prompt
        self.assertGreater(len(prompts), len(manifest["cases"]))
        self.assertEqual(corpus.contamination(prompts, self.terms), [])

    def test_a_label_in_the_inputs_would_reach_the_prompt_and_be_found(self):
        case = corpus.load_case(CORPUS, "cap-boutique-live-20261112t081032z-63d609")
        case["inputs"]["incident"]["signals"][0]["detail"] += " (scenario R1, port-invalid)"
        model = replay.ScriptedModel({"Recovery Planner": {"action": "ESCALATE", "parameters": {},
                                                           "rationale": "review"}})
        replay.replay(case, "P2", llm_factory=lambda *a, **k: model)
        found = corpus.contamination({"prompt": model.prompts[0][1]}, self.terms)
        self.assertIn(("prompt", "R1"), found)


if __name__ == "__main__":
    unittest.main()
