"""Configuration-compatibility adapter exposes only UID/version metadata."""

import subprocess
import unittest
from unittest.mock import patch

from services.guard.adapters import AdapterError, read_reference_versions


class ReferenceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.target = {"cluster_id": "lab", "namespace": "boutique"}
        self.secret = {"kind": "Secret", "namespace": "boutique", "name": "settings"}

    def read(self, refs=None, **kwargs):
        return read_reference_versions(self.target, [self.secret] if refs is None else refs,
                                       context="explicit-lab", cluster_id="lab", **kwargs)

    def test_reads_only_selected_metadata_with_explicit_context_and_deduplicates(self):
        completed = subprocess.CompletedProcess([], 0, b"secret-uid\topaque:rv", b"")
        with patch("services.guard.adapters.subprocess.run", return_value=completed) as run:
            records = self.read([self.secret, self.secret], kubeconfig="private file.yaml")
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn("--context=explicit-lab", command)
        self.assertIn("--namespace=boutique", command)
        self.assertIn("--kubeconfig=private file.yaml", command)
        self.assertEqual(command[-1], '--output=jsonpath={.metadata.uid}{"\\t"}{.metadata.resourceVersion}')
        self.assertNotIn("--output=json", command)
        self.assertEqual(records, [dict(self.secret, uid="secret-uid", resource_version="opaque:rv")])

    def test_unavailable_secret_never_echoes_server_response(self):
        completed = subprocess.CompletedProcess([], 1, b"PRIVATE-SECRET-DATA", b"PRIVATE-CREDENTIAL")
        with patch("services.guard.adapters.subprocess.run", return_value=completed):
            with self.assertRaises(AdapterError) as caught:
                self.read()
        self.assertEqual(caught.exception.code, "REFERENCE_STATE_UNAVAILABLE")
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_invalid_references_fail_before_any_api_read(self):
        for refs in ([dict(self.secret, namespace="other")], [dict(self.secret, name="--raw=/secret")],
                     [dict(self.secret, kind="Pod")], [dict(self.secret, data="SECRET")], [None]):
            with self.subTest(refs=refs):
                with patch("services.guard.adapters.subprocess.run") as run:
                    with self.assertRaises(AdapterError):
                        self.read(refs)
                    run.assert_not_called()

    def test_all_identities_validated_before_first_read(self):
        with patch("services.guard.adapters.subprocess.run") as run:
            with self.assertRaises(AdapterError):
                self.read([self.secret, dict(self.secret, name="bad..name")])
            run.assert_not_called()

    def test_malformed_metadata_has_no_silent_fallback(self):
        for payload in (b"", b"uid", b"uid\t", b"uid\trv\textra", b"\xff\trv", b"uid\trv\nextra"):
            with self.subTest(payload=payload):
                completed = subprocess.CompletedProcess([], 0, payload, b"")
                with patch("services.guard.adapters.subprocess.run", return_value=completed):
                    with self.assertRaises(AdapterError) as caught:
                        self.read()
                self.assertEqual(caught.exception.code, "REFERENCE_STATE_INVALID")

    def test_missing_tool_and_timeout_fail_closed(self):
        for error, code in ((FileNotFoundError(), "REFERENCE_STATE_UNAVAILABLE"),
                            (subprocess.TimeoutExpired([], 15), "REFERENCE_STATE_TIMEOUT")):
            with self.subTest(code=code):
                with patch("services.guard.adapters.subprocess.run", side_effect=error):
                    with self.assertRaises(AdapterError) as caught:
                        self.read()
                self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
