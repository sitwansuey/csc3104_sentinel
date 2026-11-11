"""Sanitiser: redact secrets, refuse credential material, rewrite local paths, cap sizes."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.evidence import collect, sanitise
from scripts.evidence.sanitise import Context, SanitiseError, sanitise_bytes
from tests.support.evidence_fixtures import context, write

PROFILES = {"lab": {"include": ["*.json", "*.log", "*/*.json"]}}


def clean(value, name="file.json", ctx=None):
    data = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
    output, count = sanitise_bytes(data, name, ctx or context())
    return output.decode("utf-8"), count


def jwt():
    return "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "c2lnbmF0dXJlLXZhbHVl"


class RedactionTests(unittest.TestCase):
    def test_token_under_sensitive_key_is_redacted_to_a_short_hash(self):
        text, count = clean({"token": "abc123secretvalue", "nested": {"api_key": "k-1", "Authorization": "x"}})
        value = json.loads(text)
        self.assertEqual(value["token"], sanitise.redacted("abc123secretvalue"))
        self.assertRegex(value["token"], r"^<redacted:[0-9a-f]{8}>$")
        self.assertTrue(value["nested"]["api_key"].startswith("<redacted:"))
        self.assertTrue(value["nested"]["Authorization"].startswith("<redacted:"))
        self.assertEqual(count, 3)
        self.assertNotIn("abc123secretvalue", text)

    def test_resource_names_counts_and_flags_are_kept(self):
        text, count = clean({"secretName": "shipping-tls", "prompt_tokens": 3124,
                             "automountServiceAccountToken": False, "reason_code": "ROLLBACK_ALREADY_AT_BASELINE"})
        self.assertEqual(json.loads(text), {"secretName": "shipping-tls", "prompt_tokens": 3124,
                                            "automountServiceAccountToken": False,
                                            "reason_code": "ROLLBACK_ALREADY_AT_BASELINE"})
        self.assertEqual(count, 0)

    def test_inline_bearer_and_text_secrets_are_redacted(self):
        text, count = clean({"detail": "sent Bearer abcdefghijklmnop to the API"})
        self.assertNotIn("abcdefghijklmnop", text)
        self.assertIn("Bearer <redacted:", text)
        log, log_count = clean(b"connect token=s3cr3tvalue password: hunter2hunter\nok\n", "worker.log")
        self.assertNotIn("s3cr3tvalue", log)
        self.assertNotIn("hunter2hunter", log)
        self.assertEqual((count, log_count), (1, 2))

    def assertGone(self, value, secret, name="file.json"):
        text, count = clean(value, name)
        self.assertNotIn(secret, text)
        self.assertIn("<redacted:", text)
        self.assertGreaterEqual(count, 1)
        return text

    def test_basic_authorization_and_cookie_headers_in_text_are_redacted(self):
        log = self.assertGone(b'curl -H "Authorization: Basic dXNlcjpwYXNzd29yZA==" http://x\n',
                              "dXNlcjpwYXNzd29yZA", "a.log")
        self.assertIn('"Authorization: <redacted:', log)
        self.assertTrue(log.rstrip().endswith('" http://x'))
        self.assertGone(b"Cookie: session=abcdef1234567890\n", "abcdef1234567890", "b.log")
        self.assertGone(b"Set-Cookie: sid=abcdef1234567890; HttpOnly\n", "abcdef1234567890", "c.log")
        self.assertGone(b"Proxy-Authorization: Digest abcdef1234567890\n", "abcdef1234567890", "d.log")

    def test_inline_secrets_inside_json_strings_are_redacted(self):
        self.assertGone({"stdout": "users:\n- user:\n    token: zzzzzzzzzzzzzzzz\n"}, "zzzzzzzzzzzzzzzz")
        self.assertGone({"args": "--token=abcdefghijklmnop"}, "abcdefghijklmnop")
        self.assertGone({"detail": 'curl -H "Authorization: Basic dXNlcjpwYXNzd29yZA=="'}, "dXNlcjpwYXNzd29yZA")
        self.assertGone({"detail": "Cookie: session=abcdef1234567890"}, "abcdef1234567890")
        self.assertGone(b'{"note": "password: hunter2hunter"}\n', "hunter2hunter", "events.jsonl")

    def test_quoted_header_values_are_redacted_whole(self):
        for raw in (b'Authorization: "Basic SEKRETVALUE1234"\n', b"Cookie: 'SEKRETVALUE1234'\n",
                    b'{"Authorization": "Basic SEKRETVALUE1234"}\n'):
            log = self.assertGone(raw, "SEKRETVALUE1234", "a.log")
            self.assertNotIn("Basic", log)
        self.assertGone({"detail": 'Authorization: "Basic SEKRETVALUE1234"'}, "SEKRETVALUE1234")
        self.assertGone({"detail": "Cookie: 'SEKRETVALUE1234'"}, "SEKRETVALUE1234")
        self.assertGone(b'{"stdout": "{\\"Cookie\\": \\"sid SEKRETVALUE1234\\"}"}\n', "SEKRETVALUE1234", "a.log")

    def test_header_with_no_value_is_left_alone_and_not_counted(self):
        raw = "Authorization: \nCookie:\nnext line\n"
        text, count = clean(raw.encode("utf-8"), "a.log")
        self.assertEqual((text, count), (raw, 0))

    def test_quoted_keys_serialised_json_and_reprs_inside_strings_are_redacted(self):
        secret = "SEKRETVALUE1234"
        self.assertGone({"stdout": json.dumps({"token": secret})}, secret)
        self.assertGone({"detail": str({"api_key": secret})}, secret)
        self.assertGone(('{"password": "' + secret + '"}\n').encode(), secret, "worker.log")
        self.assertGone(('{"password": "' + secret + '", broken\n').encode(), secret, "events.jsonl")
        self.assertGone(('{"stdout": ' + json.dumps(json.dumps({"token": "a b " + secret})) + '}\n').encode(),
                        secret, "raw.log")

    def test_quoted_inline_values_keep_their_quotes_and_the_key_hash(self):
        log, _ = clean(b'{"password": "hunter2hunter", "n": 1}\n', "worker.log")
        self.assertEqual(json.loads(log), {"password": sanitise.redacted("hunter2hunter"), "n": 1})
        repr_text, _ = clean(b"{'api_key': 'k-abcdef'}\n", "worker.log")
        self.assertEqual(repr_text, "{'api_key': '" + sanitise.redacted("k-abcdef") + "'}\n")

    def test_name_value_pairs_in_serialised_json_inside_a_string_are_redacted(self):
        secret = "SEKRETVALUE1234"
        stdout = json.dumps({"env": [{"name": "API_KEY", "value": secret}, {"name": "PORT", "value": "8080"}]})
        text = self.assertGone({"stdout": stdout, "rc": 0}, secret)
        inner = json.loads(json.loads(text)["stdout"])
        self.assertEqual(inner["env"][1], {"name": "PORT", "value": "8080"})
        self.assertEqual(json.loads(text)["rc"], 0)

    def test_serialised_json_with_nothing_to_redact_is_left_as_written(self):
        stdout = '{"items":[{"name":"PORT","value":"8080"}],  "kind": "List"}'
        text, count = clean({"stdout": stdout, "note": "[not json"})
        self.assertEqual(json.loads(text), {"stdout": stdout, "note": "[not json"})
        self.assertEqual(count, 0)

    def test_inline_flags_nulls_counts_and_resource_names_are_kept(self):
        raw = ('{"max_tokens": 512, "automountServiceAccountToken": false, "token": null, '
               '"secretName": "shipping-tls", "secretKeyRef": {"name": "api"}}\n'
               "prompt_tokens=3124 secretName: shipping-tls\n")
        text, count = clean(raw.encode("utf-8"), "a.log")
        self.assertEqual((text, count), (raw, 0))
        self.assertGone(b'{"max_tokens": "SEKRETVALUE1234"}\n', "SEKRETVALUE1234", "a.log")

    def test_generic_secret_names_and_keys_are_redacted(self):
        secret = "SEKRETVALUE1234"
        self.assertGone({"env": [{"name": "AWS_SECRET_ACCESS_KEY", "value": secret}]}, secret)
        self.assertGone({"env": [{"name": "SECRET_KEY", "value": secret}]}, secret)
        self.assertGone({"SECRET_KEY": secret}, secret)
        self.assertGone({"secret": secret}, secret)
        self.assertGone({"data": {"webhookSecret": secret}}, secret)

    def test_secret_resource_references_are_kept(self):
        value = {"env": [{"name": "API_KEY", "valueFrom": {"secretKeyRef": {"name": "api-secret", "key": "k"}}}],
                 "envFrom": [{"secretRef": {"name": "app-secrets"}}],
                 "volumes": [{"name": "tls", "projected": {"sources": [{"secretName": "shipping-tls"}]}}],
                 "rules": [{"resource": "secrets", "verb": "get"}]}
        text, count = clean(value)
        self.assertEqual(json.loads(text), value)
        self.assertEqual(count, 0)

    def test_name_value_pairs_with_a_sensitive_name_are_redacted(self):
        text = self.assertGone({"headers": [{"name": "Authorization", "value": "Basic dXNlcjpwYXNzd29yZA=="}]},
                               "dXNlcjpwYXNzd29yZA")
        self.assertEqual(json.loads(text)["headers"][0]["name"], "Authorization")
        self.assertGone({"env": [{"name": "API_KEY", "value": "sk-abcdef0123456789"}]}, "sk-abcdef0123456789")
        self.assertGone({"env": [{"name": "DB_PASSWORD", "value": 123456789}]}, "123456789")

    def test_argument_after_a_secret_flag_is_redacted(self):
        text = self.assertGone({"cmd": ["kubectl", "--token", "abcdefghijklmnop", "get", "pods"]},
                               "abcdefghijklmnop")
        self.assertEqual(json.loads(text)["cmd"][3:], ["get", "pods"])
        self.assertGone({"cmd": ["login", "-password", "hunter2hunter"]}, "hunter2hunter")

    def test_any_non_null_value_under_a_sensitive_key_is_redacted(self):
        self.assertGone({"token": {"value": "s3cr3tvalue"}}, "s3cr3tvalue")
        self.assertGone({"password": 123456789}, "123456789")
        self.assertGone({"api_key": ["k-abcdef", "k-ghijkl"]}, "k-abcdef")
        self.assertGone({"client_secret": 1.5e10}, "15000000000")

    def test_ordinary_pairs_flags_nulls_and_counts_are_kept(self):
        value = {"env": [{"name": "PORT", "value": "8080"}, {"name": "API_KEY", "valueFrom": {"secretKeyRef":
                 {"name": "api", "key": "k"}}}], "cmd": ["kubectl", "--context", "kind-x", "get"],
                 "token": None, "password": "", "max_tokens": 512, "automountServiceAccountToken": False,
                 "detail": "Guard decision recorded with reason OPERATOR_APPROVAL_REQUIRED"}
        text, count = clean(value)
        self.assertEqual(json.loads(text), value)
        self.assertEqual(count, 0)

    def test_model_io_is_dropped_unless_requested_then_truncated(self):
        record = {"role": "Analyzer", "seconds": 3.2, "prompt": "word " * 1000, "response": "short"}
        dropped, _ = clean(record)
        self.assertEqual(json.loads(dropped), {"role": "Analyzer", "seconds": 3.2})
        kept, _ = clean(record, ctx=Context(include_model_io=True))
        value = json.loads(kept)
        self.assertEqual(value["response"], "short")
        self.assertEqual(len(value["prompt"]), sanitise.MODEL_IO_CAP + len("...<truncated>"))

    def test_byte_order_mark_and_jsonl_are_supported(self):
        text, _ = clean(b'\xef\xbb\xbf{"status": "PASS"}', "summary.json")
        self.assertEqual(json.loads(text), {"status": "PASS"})
        lines, _ = clean(b'{"token": "abcdef"}\n\n{"phase": "x"}\n', "timeline.jsonl")
        self.assertEqual(len(lines.splitlines()), 2)
        self.assertNotIn("abcdef", lines)


class HardFailureTests(unittest.TestCase):
    def assertRefused(self, value, name="file.json"):
        with self.assertRaises(SanitiseError):
            clean(value, name)

    def test_pem_block_is_refused(self):
        self.assertRefused({"cert": "-----BEGIN " + "CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"})
        self.assertRefused(("-----BEGIN " + "RSA PRIVATE KEY-----\n").encode(), "notes.txt")

    def test_jwt_is_refused_even_under_a_redacted_key(self):
        self.assertRefused({"detail": "header " + jwt()})
        self.assertRefused({"token": jwt()})

    def test_kubeconfig_key_data_and_documents_are_refused(self):
        self.assertRefused({"user": {"client-key-data": "LS0tLS1"}})
        self.assertRefused(b"users:\n- user:\n    client-certificate-data: abc\n", "config.yaml")
        self.assertRefused({"apiVersion": "v1", "clusters": [], "users": [{"name": "x"}]})

    def test_long_base64_and_known_token_shapes_are_refused(self):
        self.assertRefused({"blob": "QUJD" * 200})
        self.assertRefused({"note": "ghp_" + "a" * 36})
        self.assertRefused({"note": "AKIA" + "ABCDEFGHIJKLMNOP"})

    def test_binary_and_invalid_json_are_refused(self):
        self.assertRefused(b"\xff\xfe\x00binary", "image.json")
        self.assertRefused(b"{not json", "summary.json")


class PathRewriteTests(unittest.TestCase):
    def test_windows_repository_home_and_other_profiles_become_placeholders(self):
        text, _ = clean({"coordinator": "C:\\work\\repo\\data\\runs\\x\\coordinator.sqlite",
                         "home": "C:\\Users\\tester\\AppData\\Local\\Temp\\a",
                         "other": "D:\\Users\\someone\\file.txt", "slashed": "c:/users/another/x"})
        value = json.loads(text)
        self.assertEqual(value["coordinator"], "<repo>\\data\\runs\\x\\coordinator.sqlite")
        self.assertEqual(value["home"], "<home>\\AppData\\Local\\Temp\\a")
        self.assertEqual(value["other"], "<home>\\file.txt")
        self.assertEqual(value["slashed"], "<home>/x")

    def test_posix_homes_and_escaped_windows_paths_in_text(self):
        text, _ = clean(b'cwd=/home/bob/run and /Users/carol/x and "C:\\\\Users\\\\dave\\\\y"\n', "run.log")
        self.assertNotIn("bob", text)
        self.assertNotIn("carol", text)
        self.assertNotIn("dave", text)
        self.assertNotIn("Users\\", text)
        self.assertIn("<home>/run", text)

    def test_local_user_names_are_replaced_as_whole_words(self):
        text, _ = clean({"operator": "tester ran it", "word": "protesters"})
        value = json.loads(text)
        self.assertEqual(value["operator"], "<user> ran it")
        self.assertEqual(value["word"], "protesters")


class CapAndAtomicityTests(unittest.TestCase):
    def test_per_file_cap_omits_the_file_and_bundle_cap_refuses(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run-20261111T100000Z"
            write(run / "summary.json", {"status": "PASS", "started_at": "2026-11-11T10:00:00Z"})
            write(run / "big.json", {"rows": ["x" * 100] * 50})
            manifest = collect.collect(run, "lab", "capped", evidence_root=Path(temp) / "evidence",
                                       profiles=PROFILES, context=context(), file_cap=1000)
            self.assertEqual([f["path"] for f in manifest["files"]], ["summary.json"])
            self.assertEqual(manifest["omitted"][0]["path"], "big.json")
            self.assertEqual(manifest["omitted"][0]["reason"], "over per-file cap")
            with self.assertRaises(SanitiseError):
                collect.collect(run, "lab", "too-big", evidence_root=Path(temp) / "evidence", profiles=PROFILES,
                                context=context(), bundle_cap=1000)
            self.assertFalse((Path(temp) / "evidence/2026-11-11/too-big").exists())

    def test_hard_failure_writes_no_partial_bundle_and_leaves_the_index(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            write(run / "summary.json", {"status": "PASS", "started_at": "2026-11-11T10:00:00Z"})
            write(run / "z/leak.json", {"key": "-----BEGIN " + "PRIVATE KEY-----"})
            evidence = Path(temp) / "evidence"
            with self.assertRaises(SanitiseError):
                collect.collect(run, "lab", "leaky", evidence_root=evidence, profiles=PROFILES, context=context())
            self.assertFalse(evidence.exists() and any(evidence.rglob("*")))

    def test_command_line_failure_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            write(run / "summary.json", {"status": "PASS", "started_at": "2026-11-11T10:00:00Z",
                                         "note": "-----BEGIN " + "CERTIFICATE-----"})
            evidence = Path(temp) / "evidence"
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                code = collect.main(["--run", str(run), "--class", "connected-harness", "--bundle-id", "leaky",
                                     "--evidence-root", str(evidence)])
            self.assertEqual(code, 1)
            self.assertIn("PEM block", errors.getvalue())
            self.assertFalse((evidence / "2026-11-11/leaky").exists())
            self.assertFalse((evidence / "index.json").exists())


if __name__ == "__main__":
    unittest.main()
