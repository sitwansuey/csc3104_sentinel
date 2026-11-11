"""Claims registry: every quotable fact has a scope caveat and live, current evidence."""

import json
from pathlib import Path
import tempfile
import unittest

from scripts.evidence import summarize
from tests.support import evidence_fixtures as fx
from tests.test_evidence_summarize import build

ROOT = Path(__file__).resolve().parents[1]


class RepositoryClaimsTests(unittest.TestCase):
    def setUp(self):
        self.claims = summarize.load_claims(ROOT)
        self.index = {entry["bundle_id"]: entry for entry in summarize.load_index(ROOT)["bundles"]}

    def test_every_claim_has_a_caveat_and_existing_current_bundles(self):
        for claim in self.claims:
            with self.subTest(claim=claim.get("id")):
                self.assertTrue(str(claim.get("scope_caveat", "")).strip())
                self.assertTrue(claim.get("bundles"))
                for bundle_id in claim["bundles"]:
                    self.assertIn(bundle_id, self.index)
                    self.assertNotEqual(self.index[bundle_id]["status"], "withdrawn")
                    self.assertTrue((ROOT / "evidence" / self.index[bundle_id]["path"] / "MANIFEST.json").exists())

    def test_ids_are_unique_and_derivations_resolve(self):
        ids = [claim["id"] for claim in self.claims]
        self.assertEqual(len(ids), len(set(ids)))
        for claim in self.claims:
            self.assertIn(claim["derive"], summarize.DERIVATIONS)
            self.assertIn("{value}", claim["text"])

    def test_values_match_the_evidence(self):
        self.assertEqual(summarize.check_claims(ROOT), [])


class ClaimValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = build(self.temp.name)
        self.path = self.root / "evidence/claims.json"

    def tearDown(self):
        self.temp.cleanup()

    def mutate(self, change):
        registry = json.loads(self.path.read_text(encoding="utf-8"))
        change(registry["claims"])
        self.path.write_text(json.dumps(registry), encoding="utf-8")
        return summarize.check_claims(self.root)

    def test_fixture_claims_are_valid(self):
        self.assertEqual(summarize.check_claims(self.root), [])

    def test_missing_scope_caveat_fails(self):
        problems = self.mutate(lambda claims: claims[0].update(scope_caveat="  "))
        self.assertIn("C-AGENT: scope_caveat is required", problems)

    def test_duplicate_id_fails(self):
        problems = self.mutate(lambda claims: claims.append(dict(claims[0])))
        self.assertIn("C-AGENT: claim id is missing or duplicated", problems)

    def test_unknown_derivation_and_missing_bundles_fail(self):
        problems = self.mutate(lambda claims: claims[1].update(derive="guess", bundles=[]))
        self.assertIn("C-REJECT: unknown derivation guess", problems)
        self.assertIn("C-REJECT: at least one bundle is required", problems)

    def test_bundle_not_in_index_fails(self):
        problems = self.mutate(lambda claims: claims[2]["bundles"].append("chaos-never-collected"))
        self.assertIn("C-CHAOS: bundle chaos-never-collected is not in the index", problems)

    def test_text_without_value_placeholder_fails(self):
        problems = self.mutate(lambda claims: claims[0].update(text="Agent mode always recovers"))
        self.assertIn("C-AGENT: text must contain {value}", problems)


if __name__ == "__main__":
    unittest.main()
