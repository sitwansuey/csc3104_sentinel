"""Golden vectors for the one canonical JSON encoding and digest.

The expected strings and hexes were captured from every hand-copied helper on
main before they were merged into shared/canonical.py. A change here breaks the
digest binding between Guard, Executor, Coordinator and stored records.
"""

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shared.canonical import (
    CanonicalError, CanonicalRecursionError, CanonicalTypeError, CanonicalValueError,
    canonical_bytes, canonical_json, sha256_hex,
)
from runtime.observer_pipeline import encoded as pipeline_encoded
from runtime.remote_worker import encoded as remote_encoded
from runtime.worker_transport import digest as transport_digest
from services.coordinator.store import _digest as coordinator_digest, _json as coordinator_json
from services.executor.delivery_store import _ensure_reviews, _record_review
from services.executor.store import canonical_json as executor_canonical_json, fingerprint
from services.guard.common import GuardInputError, json_sha256
from services.guard.core import template_sha256
from services.guard.preview import server_dry_run
from services.observer.core import ObservationError, _json_sha256 as observer_sha256
from services.observer.telemetry import encoded as telemetry_encoded
from services.planner.store import _json as planner_json
from services.verifier.store import _json as verifier_json


def fixture(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


NESTED = {
    "zeta": [3, 1.0, 1, -0.0, 2.5e-07, 10**20, True, False, None],
    "alpha": {"b": {"d": [], "c": {}}, "a": "", "B": "upper"},
    "unicode": "café 中文 \U0001F600 \"quote\" back\\slash \n\t\u0000",
    "ékey": "non-ascii key",
    "10": "string key sorts as text", "9": "nine",
    "tuple": (1, "two", (3,)),
}

# name -> (value, expected canonical JSON or None, expected SHA-256 hex)
GOLDEN = {
    "nested": (NESTED,
               '{"10":"string key sorts as text","9":"nine","alpha":{"B":"upper","a":"","b":{"c":{},"d":[]}},'
               '"tuple":[1,"two",[3]],"unicode":"caf\\u00e9 \\u4e2d\\u6587 \\ud83d\\ude00 \\"quote\\" '
               'back\\\\slash \\n\\t\\u0000","zeta":[3,1.0,1,-0.0,2.5e-07,100000000000000000000,true,false,null],'
               '"\\u00e9key":"non-ascii key"}',
               "65c403feb4db3bf639ef4985830ab2ba029299b3536789a85612f99fe01ee058"),
    "int_one": (1, "1", "6b86b273ff34fce19d6b804eff5a3f5747ada4eaa22f1d49c01e52ddb7875b4b"),
    "float_one": (1.0, "1.0", "d0ff5974b6aa52cf562bea5921840c032a860a91a3512f7fe8f768f6bbe005f6"),
    "empty_object": ({}, "{}", "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
    "empty_list": ([], "[]", "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "string": ("plain", '"plain"', "945603a8f587786b463c3f94fce115c0fae88fac2728cc96ddf5981cf7f61741"),
    "null": (None, "null", "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"),
    "proposal": (fixture("data/sentinel/samples/messages/proposal.json"), None,
                 "d4c8924c42fd67b98f95fe1f9324c8c1ddea69b6c3b0a3252a7fdb0e01e87b74"),
    "pod_template": (fixture("data/sentinel/samples/baseline.json")["pod_template"], None,
                     "a9091159ce70bcf4679f6fa7a70c33ce7169e8dd3296ec647d9bfee47c9445bb"),
    "guard_decision": (fixture("data/sentinel/samples/messages/guard-result.json"), None,
                       "b529be8d9da7f72ec203e6a2f0703f8c62f380befecb35d9c3b894a7c37620e0"),
}
# Captured on main from the inline copies in guard/preview.py and executor/delivery_store.py.
PREVIEW_PATCH_SHA256 = "e3dd04453e99ae009226d00483fa2221eb2f272d3c172430d52855ed9aee622c"
DELIVERY_REVIEW_SHA256 = "aa4d04601fea5d5811f242c3f239530b0eff6ca4d1954766f24ddd84b881dc64"

LEGACY_STRINGS = {
    "services.executor.store.canonical_json": executor_canonical_json,
    "services.coordinator.store._json": coordinator_json,
    "services.planner.store._json": planner_json,
    "services.verifier.store._json": verifier_json,
    "services.observer.telemetry.encoded": telemetry_encoded,
    "runtime.observer_pipeline.encoded": pipeline_encoded,
    "runtime.remote_worker.encoded": remote_encoded,
}
LEGACY_DIGESTS = {
    "services.executor.store.fingerprint": fingerprint,
    "services.coordinator.store._digest": coordinator_digest,
    "runtime.worker_transport.digest": transport_digest,
    "services.guard.common.json_sha256": json_sha256,
    "services.observer.core._json_sha256": observer_sha256,
}


def cyclic_dict():
    value = {"a": 1}
    value["self"] = value
    return value


def cyclic_list():
    value = [1]
    value.append(value)
    return value


def deeply_nested():
    # Far past both the Python and C encoder recursion limits on 3.12 and 3.13.
    value = []
    for _ in range(100_000):
        value = [value]
    return value


class GoldenVectorTests(unittest.TestCase):
    def test_canonical_json_and_digest_match_frozen_vectors(self):
        for name, (value, expected_json, expected_hex) in GOLDEN.items():
            with self.subTest(name=name):
                if expected_json is not None:
                    self.assertEqual(canonical_json(value), expected_json)
                self.assertEqual(canonical_bytes(value), canonical_json(value).encode("utf-8"))
                self.assertEqual(sha256_hex(value), expected_hex)
                self.assertEqual(hashlib.sha256(canonical_bytes(value)).hexdigest(), expected_hex)

    def test_every_legacy_entry_point_equals_the_shared_module(self):
        for name, (value, _, expected_hex) in GOLDEN.items():
            for legacy, function in LEGACY_STRINGS.items():
                with self.subTest(name=name, legacy=legacy):
                    self.assertEqual(function(value), canonical_json(value))
            for legacy, function in LEGACY_DIGESTS.items():
                with self.subTest(name=name, legacy=legacy):
                    self.assertEqual(function(value), expected_hex)

    def test_guard_template_hash_matches_the_recorded_baseline(self):
        baseline = fixture("data/sentinel/samples/baseline.json")
        self.assertEqual(template_sha256(baseline["pod_template"]), baseline["template_sha256"])
        self.assertEqual(GOLDEN["pod_template"][2], baseline["template_sha256"])

    def test_output_is_ascii_sorted_and_compact(self):
        text = canonical_json({"b": "ü", "a": {"d": 1, "c": [1, 2]}})
        self.assertEqual(text, '{"a":{"c":[1,2],"d":1},"b":"\\u00fc"}')
        self.assertTrue(text.isascii())

    def test_integer_and_float_stay_distinct(self):
        self.assertNotEqual(sha256_hex(1), sha256_hex(1.0))
        self.assertNotEqual(sha256_hex({"replicas": 1}), sha256_hex({"replicas": 1.0}))

    def test_preview_patch_file_bytes_are_unchanged(self):
        proposal = fixture("data/sentinel/samples/messages/proposal.json")
        live = fixture("data/sentinel/samples/observations.json")["failed-rollout"][-1]
        baseline = fixture("data/sentinel/samples/baseline.json")
        response = copy.deepcopy(live["deployment"])
        response["spec"]["template"] = copy.deepcopy(baseline["pod_template"])
        seen = {}

        def run(command, **kwargs):
            seen["bytes"] = Path(command[command.index("--patch-file") + 1]).read_bytes()
            return subprocess.CompletedProcess([], 0, json.dumps(response).encode("utf-8"), b"")

        with patch("services.guard.preview.subprocess.run", side_effect=run):
            result = server_dry_run(proposal, live, baseline, context="c", cluster_id="sentinel-lab")
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(hashlib.sha256(seen["bytes"]).hexdigest(), PREVIEW_PATCH_SHA256)
        expected = [
            {"op": "test", "path": "/metadata/uid", "value": proposal["target"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": result["resource_version"]},
            {"op": "replace", "path": "/spec/template", "value": baseline["pod_template"]},
        ]
        self.assertEqual(seen["bytes"], canonical_bytes(expected))

    def test_delivery_review_record_is_unchanged(self):
        class Store:
            def __init__(self):
                self.db = sqlite3.connect(":memory:")

            def action(self, action_id):
                return None

        store = Store()
        self.addCleanup(store.db.close)
        _ensure_reviews(store)
        execution = fixture("data/sentinel/samples/messages/execution-result.json")
        _record_review(store, 7, execution, "test")
        stored = store.db.execute("SELECT result FROM executor_delivery_reviews").fetchone()[0]
        self.assertEqual(stored, canonical_json(execution))
        self.assertEqual(hashlib.sha256(stored.encode("utf-8")).hexdigest(), DELIVERY_REVIEW_SHA256)


class RejectionTests(unittest.TestCase):
    BAD = {
        "nan": (float("nan"), CanonicalValueError, ValueError),
        "infinity": (float("inf"), CanonicalValueError, ValueError),
        "negative_infinity": ({"x": [float("-inf")]}, CanonicalValueError, ValueError),
        "set": ({"x": {1, 2}}, CanonicalTypeError, TypeError),
        "frozenset": (frozenset({1}), CanonicalTypeError, TypeError),
        "bytes": ({"x": b"raw"}, CanonicalTypeError, TypeError),
        "object": (object(), CanonicalTypeError, TypeError),
        "mixed_key_types": ({1: "a", "b": 2}, CanonicalTypeError, TypeError),
        "cyclic_dict": (cyclic_dict(), CanonicalValueError, ValueError),
        "cyclic_list": (cyclic_list(), CanonicalValueError, ValueError),
    }

    def test_unencodable_values_raise_canonical_error_keeping_the_builtin_type(self):
        for name, (value, specific, builtin) in self.BAD.items():
            for function in (canonical_json, canonical_bytes, sha256_hex):
                with self.subTest(name=name, function=function.__name__):
                    with self.assertRaises(specific) as caught:
                        function(value)
                    self.assertIsInstance(caught.exception, CanonicalError)
                    self.assertIsInstance(caught.exception, builtin)
                    self.assertIsNotNone(caught.exception.__cause__)

    def test_error_message_matches_the_json_module(self):
        with self.assertRaises(CanonicalValueError) as caught:
            canonical_json(float("nan"))
        self.assertIn("Out of range float values are not JSON compliant", str(caught.exception))

    def test_nesting_past_the_recursion_limit_raises(self):
        with self.assertRaises(CanonicalRecursionError) as caught:
            canonical_json(deeply_nested())
        self.assertIsInstance(caught.exception, RecursionError)
        self.assertIsInstance(caught.exception, CanonicalError)

    def test_legacy_entry_points_reject_the_same_values(self):
        for name, (value, _, builtin) in self.BAD.items():
            for legacy, function in {**LEGACY_STRINGS, "services.executor.store.fingerprint": fingerprint,
                                     "services.coordinator.store._digest": coordinator_digest,
                                     "runtime.worker_transport.digest": transport_digest}.items():
                with self.subTest(name=name, legacy=legacy):
                    with self.assertRaises(builtin):
                        function(value)

    def test_domain_wrappers_keep_their_error_types(self):
        for name, (value, _, _) in self.BAD.items():
            with self.subTest(name=name):
                with self.assertRaises(GuardInputError):
                    json_sha256(value)
                with self.assertRaises(ObservationError):
                    observer_sha256(value)
        with self.assertRaises(GuardInputError):
            json_sha256(deeply_nested())


def load_check_contracts():
    spec = importlib.util.spec_from_file_location("check_contracts_under_test", ROOT / "scripts/check_contracts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CheckContractsTests(unittest.TestCase):
    def setUp(self):
        self.module = load_check_contracts()
        self.fresh_samples()

    def fresh_samples(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        samples = self.root / "data/sentinel/samples"
        shutil.copytree(ROOT / "data/sentinel/samples/messages", samples / "messages")
        shutil.copyfile(ROOT / "data/sentinel/samples/baseline.json", samples / "baseline.json")
        self.samples = samples

    def run_main(self):
        with patch.object(self.module, "ROOT", self.root), contextlib.redirect_stdout(io.StringIO()) as out:
            self.module.main()
        return out.getvalue()

    def plant_template_value(self, value):
        """Plant a value and rebind both digests with the old NaN-tolerant encoding."""
        baseline = json.loads((self.samples / "baseline.json").read_text(encoding="utf-8"))
        baseline["pod_template"]["metadata"]["annotations"] = {"weight": value}
        tolerant = json.dumps(baseline["pod_template"], sort_keys=True, separators=(",", ":"), allow_nan=True)
        digest = hashlib.sha256(tolerant.encode()).hexdigest()
        baseline["template_sha256"] = digest
        proposal_path = self.samples / "messages/proposal.json"
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        proposal["parameters"]["template_sha256"] = digest
        (self.samples / "baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")

    def test_fixtures_still_validate_with_the_shared_digest(self):
        self.assertIn("Validated", self.run_main())

    def test_nan_and_infinity_in_the_baseline_template_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.fresh_samples()
                self.plant_template_value(value)
                self.assertRegex((self.samples / "baseline.json").read_text(encoding="utf-8"),
                                 r"NaN|Infinity")
                with self.assertRaises(CanonicalValueError):
                    self.run_main()

    def test_changed_template_is_still_rejected(self):
        baseline = json.loads((self.samples / "baseline.json").read_text(encoding="utf-8"))
        baseline["pod_template"]["metadata"]["labels"]["app"] = "changed"
        (self.samples / "baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exact saved baseline template"):
            self.run_main()

    def test_a_scale_sample_bound_to_another_template_or_baseline_is_rejected(self):
        # P5.07: the SCALE sample must name the saved baseline and the template it leaves in place.
        path = self.samples / "messages/scale-proposal.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for change in ({"template_sha256": "b" * 64}, {"baseline_id": "BASE-OTHER"}):
            with self.subTest(change=change):
                path.write_text(json.dumps({**original, "parameters": {**original["parameters"], **change}}),
                                encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "SCALE sample is not bound"):
                    self.run_main()


class DockerfileCopyTests(unittest.TestCase):
    """Images that copy explicit files must include shared/canonical.py when their code imports it."""

    def copied_sources(self, dockerfile):
        sources = []
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "COPY" and not parts[1].startswith("--"):
                sources += parts[1:-1]
        return sources

    def test_images_that_import_shared_canonical_copy_it(self):
        checked = 0
        for dockerfile in sorted((ROOT / "deploy/docker").glob("*.Dockerfile")):
            sources = self.copied_sources(dockerfile)
            files = []
            for source in sources:
                path = ROOT / source
                files += sorted(path.rglob("*.py")) if path.is_dir() else [path] if path.suffix == ".py" else []
            uses = any(re.search(r"\bshared\.canonical\b", path.read_text(encoding="utf-8")) for path in files)
            if uses:
                checked += 1
                with self.subTest(dockerfile=dockerfile.name):
                    self.assertTrue({"shared/", "shared/canonical.py"} & set(sources),
                                    f"{dockerfile.name} imports shared.canonical but does not copy it")
        self.assertGreaterEqual(checked, 3)


if __name__ == "__main__":
    unittest.main()
