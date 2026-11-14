"""Configuration-compatibility adapter exposes only UID/version metadata."""

import json
import subprocess
import unittest
from unittest.mock import patch

from services.guard.adapters import AdapterError, certificate_record, read_certificate_status, read_reference_versions


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


def certificate_object(**status):
    """A cert-manager Certificate as kubectl prints it; it names its Secret and holds no key."""
    return {"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
            "metadata": {"name": "security-upstream", "namespace": "boutique", "uid": "cert-uid", "generation": 2,
                         "annotations": {"note": "kept out of the record"}},
            "spec": {"secretName": "security-upstream", "dnsNames": ["upstream.internal"], "duration": "2h0m0s",
                     "issuerRef": {"group": "cert-manager.io", "kind": "ClusterIssuer", "name": "sentinel-lab-ca"}},
            "status": {"conditions": [{"type": "Ready", "status": "True", "observedGeneration": 2,
                                       "message": "Certificate is up to date and has not expired"}],
                       "notBefore": "2026-10-10T11:00:00Z", "notAfter": "2026-10-10T13:00:00Z",
                       "renewalTime": "2026-10-10T12:00:00Z", "revision": 5, **status}}


class CertificateStatusAdapterTests(unittest.TestCase):
    """P5.06: the Guard reader reads Certificate status, never a Secret, and keeps only the reviewed fields."""

    def setUp(self):
        self.target = {"cluster_id": "lab", "namespace": "boutique"}

    def read(self, names=None, **kwargs):
        return read_certificate_status(self.target, ["security-upstream"] if names is None else names,
                                       context="explicit-lab", cluster_id="lab", **kwargs)

    def test_reads_the_named_certificate_and_keeps_only_status_and_identity_fields(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps(certificate_object()).encode(), b"")
        with patch("services.guard.adapters.subprocess.run", return_value=completed) as run:
            records = self.read(["security-upstream", "security-upstream"], kubeconfig="reader.yaml")
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[-4:], ["get", "certificates.cert-manager.io", "security-upstream", "--output=json"])
        for part in ("--context=explicit-lab", "--namespace=boutique", "--kubeconfig=reader.yaml"):
            self.assertIn(part, command)
        self.assertFalse([part for part in command if "secret" in part.lower()])
        self.assertEqual(records, [{
            "kind": "Certificate", "namespace": "boutique", "name": "security-upstream", "uid": "cert-uid",
            "generation": 2, "secret_name": "security-upstream", "dns_names": ["upstream.internal"],
            "issuer": {"group": "cert-manager.io", "kind": "ClusterIssuer", "name": "sentinel-lab-ca"},
            "ready": "True", "ready_observed_generation": 2, "not_before": "2026-10-10T11:00:00Z",
            "not_after": "2026-10-10T13:00:00Z", "revision": 5}])

    def test_a_certificate_cert_manager_has_not_finished_is_recorded_as_unknown(self):
        record = certificate_record(dict(certificate_object(), status={}), "boutique")
        self.assertEqual((record["ready"], record["ready_observed_generation"], record["not_after"], record["revision"]),
                         ("Unknown", None, None, None))
        issuer = certificate_object()
        issuer["spec"]["issuerRef"] = {"name": "local-ca"}
        self.assertEqual(certificate_record(issuer, "boutique")["issuer"],
                         {"group": "cert-manager.io", "kind": "Issuer", "name": "local-ca"})

    def test_another_kind_namespace_or_malformed_object_is_refused(self):
        for change in (lambda item: item.update(kind="Secret"), lambda item: item.update(apiVersion="v1"),
                       lambda item: item["metadata"].update(namespace="other"),
                       lambda item: item["metadata"].update(generation="2"), lambda item: item["spec"].pop("secretName"),
                       lambda item: item["status"].update(notAfter=17), lambda item: item.update(status="ready")):
            item = certificate_object()
            change(item)
            with self.subTest(item=item), self.assertRaises(AdapterError) as caught:
                certificate_record(item, "boutique")
            self.assertEqual(caught.exception.code, "CERTIFICATE_STATE_INVALID")

    def test_failures_never_echo_server_output_and_names_are_checked_first(self):
        for completed, code in ((subprocess.CompletedProcess([], 1, b"PRIVATE", b"PRIVATE-CREDENTIAL"),
                                 "CERTIFICATE_STATE_UNAVAILABLE"),
                                (subprocess.CompletedProcess([], 0, b"PRIVATE not json", b""), "CERTIFICATE_STATE_INVALID"),
                                (subprocess.CompletedProcess([], 0, b" " * (64 * 1024 + 1), b""),
                                 "CERTIFICATE_STATE_INVALID")):
            with self.subTest(code=code), patch("services.guard.adapters.subprocess.run", return_value=completed), \
                    self.assertRaises(AdapterError) as caught:
                self.read()
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("PRIVATE", str(caught.exception))
        other = dict(certificate_object(), metadata=dict(certificate_object()["metadata"], name="other"))
        with patch("services.guard.adapters.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, json.dumps(other).encode(), b"")), \
                self.assertRaises(AdapterError):
            self.read()
        with patch("services.guard.adapters.subprocess.run") as run:
            for names, target in ((["Bad_Name"], self.target), (["ok"], {"cluster_id": "other", "namespace": "boutique"}),
                                  ("security-upstream", self.target), (["ok"] * 65, self.target)):
                with self.subTest(names=names), self.assertRaises(AdapterError):
                    read_certificate_status(target, names, context="explicit-lab", cluster_id="lab")
            run.assert_not_called()

    def test_a_missing_tool_or_timeout_fails_closed(self):
        for error, code in ((OSError("missing"), "CERTIFICATE_STATE_UNAVAILABLE"),
                            (subprocess.TimeoutExpired("kubectl", 15), "CERTIFICATE_STATE_TIMEOUT")):
            with self.subTest(code=code), patch("services.guard.adapters.subprocess.run", side_effect=error), \
                    self.assertRaises(AdapterError) as caught:
                self.read()
            self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
