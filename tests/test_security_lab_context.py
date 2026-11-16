"""The protected security lab: its kubectl context, and its certificate lifecycle (P5.06).

The lab must not change the user's kubectl current-context. Every Envoy
loads its key pair and trust bundle through file-based SDS with
watched_directory, with no inline or static keys, exact SAN matchers in the
reviewed static configuration and whole Secret volumes (never subPath); and
cert-manager, pinned by version and digest, issues every identity.
"""

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.security import boutique_security as lab, cert_lifecycle
from scripts.security.boutique_security_manifest import NAMESPACE, TRUST_BUNDLE, VERIFIER_IDENTITY, overlay
from scripts.security.security_lab import ROUTES, envoy_config, sds_names, sds_resource


class SecurityLabContextTests(unittest.TestCase):
    def fake(self, previous, *, create_fails=False, delete_fails=False):
        calls = []

        def command(args, **_):
            args = [str(arg) for arg in args]
            calls.append(args)
            if args[:3] == ["kubectl", "config", "current-context"]:
                return SimpleNamespace(stdout=(previous or "") + "\n", returncode=0 if previous else 1)
            if args[:3] == ["kind", "get", "clusters"]:
                return SimpleNamespace(stdout="sentinel-boutique-connected\n", returncode=0)
            if args[:3] == ["kind", "create", "cluster"] and create_fails:
                raise RuntimeError("kind failed")
            if args[:3] == ["kind", "delete", "cluster"] and delete_fails:
                raise RuntimeError("kind failed")
            if args[:3] == ["kind", "get", "nodes"]:
                raise RuntimeError("stop after cluster creation")
            return SimpleNamespace(stdout="", returncode=0)
        return calls, command

    def setup(self, previous, **kwargs):
        calls, command = self.fake(previous, **kwargs)
        with patch.object(lab, "command", side_effect=command), tempfile.TemporaryDirectory() as temp, \
                self.assertRaises(RuntimeError):
            lab.setup_cluster(Path(temp))
        create = next(i for i, call in enumerate(calls) if call[:3] == ["kind", "create", "cluster"])
        return calls[create + 1:]

    def stop(self, previous, **kwargs):
        calls, command = self.fake(previous, **kwargs)
        with patch.object(lab, "command", side_effect=command), tempfile.TemporaryDirectory() as temp, \
                patch.object(sys, "argv", ["boutique_security.py", "stop", "--directory", temp]):
            if kwargs.get("delete_fails"):
                with self.assertRaises(RuntimeError):
                    lab.main()
            else:
                lab.main()
        delete = next(i for i, call in enumerate(calls) if call[:3] == ["kind", "delete", "cluster"])
        return calls[delete + 1:]

    def test_create_restores_the_previous_context(self):
        after = self.setup("kind-sentinel-boutique-connected")
        self.assertIn(["kubectl", "config", "use-context", "kind-sentinel-boutique-connected"], after)

    def test_create_leaves_an_unset_context_unset(self):
        self.assertIn(["kubectl", "config", "unset", "current-context"], self.setup(None))

    def test_create_failure_still_restores(self):
        after = self.setup("my-context", create_fails=True)
        self.assertIn(["kubectl", "config", "use-context", "my-context"], after)

    def test_stop_restores_the_previous_context(self):
        after = self.stop("kind-sentinel-boutique-connected")
        self.assertIn(["kubectl", "config", "use-context", "kind-sentinel-boutique-connected"], after)
        self.assertIn(["kubectl", "config", "use-context", "kind-sentinel-boutique-connected"],
                      self.stop("kind-sentinel-boutique-connected", delete_fails=True))

    def test_stop_does_not_reselect_the_deleted_lab(self):
        after = self.stop(lab.CONTEXT)
        self.assertFalse([call for call in after if call[:2] == ["kubectl", "config"]])


def lab_manifest():
    return overlay({"items": [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": NAMESPACE},
         "spec": {"template": {"spec": {"containers": [{"name": "server", "env": [
             {"name": "SHIPPING_SERVICE_ADDR", "value": "shippingservice:50051"},
             {"name": "CART_SERVICE_ADDR", "value": "cartservice:7070"}]}]}}}}
        for name in ("frontend", "checkoutservice", "shippingservice", "cartservice")]},
        envoy_image="envoy@sha256:" + "e" * 64, inspector_image="inspector")


def envoy_configmaps(manifest):
    return {item["metadata"]["name"]: item["data"] for item in manifest["items"]
            if item["kind"] == "ConfigMap" and "envoy.json" in item.get("data", {})}


def walk(value, key):
    """Every value stored under key anywhere in a JSON document."""
    if isinstance(value, dict):
        for name, item in value.items():
            if name == key:
                yield item
            yield from walk(item, key)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item, key)


class SdsConfigurationTests(unittest.TestCase):
    """P5.06: every Envoy loads its key pair and trust bundle through file-based SDS with watched_directory."""

    def setUp(self):
        self.manifest = lab_manifest()
        self.configmaps = envoy_configmaps(self.manifest)

    def test_every_envoy_uses_sds_with_watched_directories_and_no_inline_or_static_keys(self):
        self.assertEqual(len(self.configmaps), 5)  # two callers, two ingress sidecars, the gateway
        for name, data in self.configmaps.items():
            with self.subTest(configmap=name):
                config = json.loads(data["envoy.json"])
                text = data["envoy.json"]
                for forbidden in ("tls_certificates", "inline_string", "inline_bytes", "private_key", "tls.key",
                                  "\"validation_context\":", "BEGIN"):
                    self.assertNotIn(forbidden, text)
                sources = list(walk(config, "sds_config"))
                self.assertTrue(sources)
                for source in sources:
                    self.assertEqual(set(source), {"path_config_source", "resource_api_version"})
                    self.assertEqual(source["path_config_source"]["watched_directory"], {"path": "/config"})
                    self.assertIn(source["path_config_source"]["path"][len("/config/"):], data)
                self.assertEqual(set(data) - {"envoy.json"}, {f"sds-{item}.json" for item in sds_names(config)})

    def test_sds_files_name_mounted_paths_only_and_watch_their_directories(self):
        for name, data in self.configmaps.items():
            for key, text in data.items():
                if key == "envoy.json":
                    continue
                with self.subTest(configmap=name, file=key):
                    self.assertNotRegex(text, "inline|BEGIN|PRIVATE")
                    [secret] = json.loads(text)["resources"]
                    if "validation_context" in secret:
                        self.assertEqual(secret["validation_context"], {
                            "trusted_ca": {"filename": "/trust/ca.crt"}, "watched_directory": {"path": "/trust"}})
                    else:
                        identity = secret["name"][:-len("-cert")]
                        self.assertEqual(secret["tls_certificate"], {
                            "certificate_chain": {"filename": f"/pki/{identity}/tls.crt"},
                            "private_key": {"filename": f"/pki/{identity}/tls.key"},
                            "watched_directory": {"path": f"/pki/{identity}"}})

    def test_san_matchers_are_exact_and_stay_in_the_static_configuration(self):
        for name, data in self.configmaps.items():
            config = json.loads(data["envoy.json"])
            contexts = list(walk(config, "combined_validation_context"))
            self.assertTrue(contexts)
            for context in contexts:
                with self.subTest(configmap=name):
                    self.assertEqual(context["validation_context_sds_secret_config"]["name"], "trust-bundle")
                    for matcher in context["default_validation_context"].get("match_typed_subject_alt_names", []):
                        self.assertEqual(set(matcher), {"san_type", "matcher"})
                        self.assertEqual((matcher["san_type"], list(matcher["matcher"])), ("DNS", ["exact"]))
                        self.assertRegex(matcher["matcher"]["exact"], r"^[a-z0-9-]+\.internal$")
            for key, text in data.items():
                if key != "envoy.json":
                    self.assertNotIn("subject_alt_names", text)
        # Every server listener still requires and checks a client identity; every client checks its server's.
        gateway = json.loads(self.configmaps["security-gateway"]["envoy.json"])
        for listener in gateway["static_resources"]["listeners"]:
            tls = listener["filter_chains"][0]["transport_socket"]["typed_config"]
            self.assertTrue(tls["require_client_certificate"])
            self.assertTrue(tls["common_tls_context"]["combined_validation_context"]["default_validation_context"]
                            ["match_typed_subject_alt_names"])

    def test_identity_secrets_are_whole_read_only_volumes_never_subpath(self):
        for item in self.manifest["items"]:
            if item["kind"] != "Deployment":
                continue
            pod = item["spec"]["template"]["spec"]
            volumes = {volume["name"]: volume for volume in pod.get("volumes", [])}
            for container in pod["containers"]:
                mounts = container.get("volumeMounts", [])
                for mount in mounts:
                    with self.subTest(deployment=item["metadata"]["name"], mount=mount["mountPath"]):
                        self.assertNotIn("subPath", mount)
                        self.assertNotIn("subPathExpr", mount)
                        if mount["mountPath"].startswith("/pki/"):
                            self.assertIs(mount["readOnly"], True)
                            self.assertEqual(volumes[mount["name"]]["secret"]["secretName"],
                                             "security-" + mount["mountPath"][len("/pki/"):])
                if container.get("image", "").startswith("envoy@"):
                    trust = [mount for mount in mounts if mount["mountPath"] == "/trust"]
                    self.assertEqual(len(trust), 1)
                    self.assertEqual(volumes[trust[0]["name"]], {"name": TRUST_BUNDLE, "configMap": {"name": TRUST_BUNDLE}})
                    # Each SDS key pair the Envoy subscribes to is one of its mounted identities.
                    config = next(volumes[mount["name"]]["configMap"]["name"] for mount in mounts
                                  if mount["mountPath"] == "/config")
                    names = sds_names(json.loads(envoy_configmaps(self.manifest)[config]["envoy.json"]))
                    mounted = {mount["mountPath"][len("/pki/"):] for mount in mounts if mount["mountPath"].startswith("/pki/")}
                    self.assertEqual({name[:-len("-cert")] for name in names if name != "trust-bundle"}, mounted)
        self.assertNotIn("Secret", {item["kind"] for item in self.manifest["items"]})

    def test_access_logs_record_the_peer_serial_and_upstream_tls_failure(self):
        for route in ROUTES:
            log = envoy_config(route=route)["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0][
                "typed_config"]["access_log"][0]["typed_config"]["log_format"]["json_format"]
            self.assertEqual((log["peer_serial"], log["upstream_tls"]),
                             ("%DOWNSTREAM_PEER_SERIAL%", "%UPSTREAM_TRANSPORT_FAILURE_REASON%"))
        with self.assertRaises(ValueError):
            sds_resource("unknown")


def unresolved(pins):
    """pins with every value back to TODO-PIN, as before they were resolved."""
    pins = deepcopy(pins)
    pins["chart"]["sha256"] = "TODO-PIN"
    for image in pins["images"].values():
        image["digest"] = "TODO-PIN"
    return pins


def resolved(pins):
    pins = deepcopy(pins)
    pins["chart"]["sha256"] = "c" * 64
    for image in pins["images"].values():
        image["digest"] = "sha256:" + "b" * 64
    return pins


class FakeOpenSSL:
    """Records commands and creates the files openssl would, with placeholder contents (no real keys)."""

    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        args = [str(arg) for arg in args]
        self.calls.append(args)
        for flag in ("-keyout", "-out"):
            if flag in args:
                Path(args[args.index(flag) + 1]).write_text("-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----\n",
                                                            encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class CertLifecycleTests(unittest.TestCase):
    """P5.06: cert-manager issues every lab identity; pins, rollover and negatives are prepared offline."""

    def test_one_certificate_per_identity_with_an_exact_san_and_a_new_key_on_renewal(self):
        items = cert_lifecycle.certificates()
        self.assertEqual([item["metadata"]["name"] for item in items], ["security-" + name for name in lab.IDENTITIES])
        self.assertEqual(set(lab.IDENTITIES), {"gateway", "upstream", "cart-upstream", "proxy-client", "client",
                                               "frontend-client", "checkoutservice-client", VERIFIER_IDENTITY})
        for item, (identity, role) in zip(items, cert_lifecycle.IDENTITIES, strict=True):
            spec = item["spec"]
            self.assertEqual(item["metadata"]["namespace"], NAMESPACE)
            self.assertEqual((spec["secretName"], spec["dnsNames"]), ("security-" + identity, [identity + ".internal"]))
            self.assertEqual(spec["issuerRef"], {"group": "cert-manager.io", "kind": "ClusterIssuer",
                                                 "name": "sentinel-lab-ca"})
            self.assertEqual((spec["duration"], spec["renewBefore"]), ("2h", "1h"))
            self.assertEqual(spec["privateKey"]["rotationPolicy"], "Always")
            self.assertIn("server auth" if role == "server" else "client auth", spec["usages"])
        # The overlay mounts only Secrets that a Certificate issues.
        mounted = {volume["secret"]["secretName"] for item in lab_manifest()["items"] if item["kind"] == "Deployment"
                   for volume in item["spec"]["template"]["spec"].get("volumes", []) if "secret" in volume}
        self.assertLessEqual(mounted, {item["spec"]["secretName"] for item in items})

    def test_the_rotation_profile_respects_cert_managers_minimums(self):
        # cert-manager refuses a duration under 1h and a renewBefore under 5m; rotation renews every ten minutes.
        for item in cert_lifecycle.certificates("rotation"):
            self.assertEqual((item["spec"]["duration"], item["spec"]["renewBefore"]), ("1h", "50m"))
        self.assertGreater(50 * 60, cert_lifecycle.GUARD_MIN_REMAINING_SECONDS)
        with self.assertRaises(KeyError):
            cert_lifecycle.certificates("ten-minutes")

    def test_the_guard_identities_are_a_valid_policy(self):
        from services.guard.eligibility import validate_policy
        identities = cert_lifecycle.guard_identities(["upstream"])
        validate_policy({"security_constraints": {"certificate_identities": identities}})
        self.assertEqual(identities[0]["issuer"], cert_lifecycle.ISSUER_REF)

    def test_committed_pins_are_unresolved_and_every_one_has_a_resolution_command(self):
        pins = cert_lifecycle.load_pins()
        problems = cert_lifecycle.unresolved_pins(pins)
        # Every TODO-PIN is reported (today all six: the chart SHA-256 and five image digests),
        # and nothing else is, so the count stays right once the pins are resolved.
        self.assertEqual(len(problems), cert_lifecycle.PINS.read_text(encoding="utf-8").count("\"TODO-PIN\""))
        commands = cert_lifecycle.pin_commands(pins)
        for name, image in pins["images"].items():
            self.assertTrue(any(image["repository"] + ":" + pins["version"] in line and "images." + name in line
                                for line in commands), name)
        self.assertTrue(any("Get-FileHash" in line for line in commands))
        # The digest is printed bare (sha256:...), as pins.json wants it, not JSON-quoted.
        for line in commands:
            self.assertNotIn("{{json", line)
        self.assertEqual(cert_lifecycle.unresolved_pins(resolved(pins)), [])
        broken = resolved(pins)
        broken["images"]["webhook"]["digest"] = "sha256:short"
        broken["chart"]["url"] = "https://example.invalid/cert-manager.tgz"
        self.assertEqual(len(cert_lifecycle.unresolved_pins(broken)), 2)

    def test_helm_values_pin_every_image_by_digest(self):
        values = cert_lifecycle.helm_values(resolved(cert_lifecycle.load_pins()))
        images = [values["image"], *(values[name]["image"] for name in ("webhook", "cainjector", "startupapicheck",
                                                                        "acmesolver"))]
        for image in images:
            self.assertRegex(image["digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertNotIn("tag", image)
        self.assertIs(values["crds"]["enabled"], True)
        self.assertIs(values["prometheus"]["enabled"], False)

    def test_install_refuses_unresolved_pins_or_a_changed_chart_before_any_command(self):
        command = FakeOpenSSL()
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "pins are unresolved"):
                cert_lifecycle.install(Path(folder), "node-1", command, pins=unresolved(cert_lifecycle.load_pins()),
                                       fetch=lambda url: self.fail("fetched"))
            pins = resolved(cert_lifecycle.load_pins())
            with self.assertRaisesRegex(ValueError, "checksum changed"):
                cert_lifecycle.install(Path(folder), "node-1", command, pins=pins, fetch=lambda url: b"chart")
            self.assertEqual(command.calls, [])
            pins["chart"]["sha256"] = hashlib.sha256(b"chart").hexdigest()
            cert_lifecycle.install(Path(folder), "node-1", command, pins=pins, fetch=lambda url: b"chart")
            values = json.loads((Path(folder) / "cert-manager-values.json").read_text(encoding="utf-8"))
        self.assertEqual(values, cert_lifecycle.helm_values(pins))
        pulls = [call for call in command.calls if "pull" in call]
        self.assertEqual(len(pulls), 10)
        helm = command.calls[-1]
        self.assertEqual(helm[:3], ["helm", "upgrade", "--install"])
        self.assertEqual(helm[helm.index("--kube-context") + 1], "kind-sentinel-boutique-security")
        self.assertNotIn("sentinel-lab", " ".join(arg for call in command.calls for arg in call).replace(
            "sentinel-lab-ca", ""))

    def test_the_trust_bundle_holds_ca_certificates_only(self):
        pem = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
        bundle = cert_lifecycle.trust_bundle([pem, pem.replace("AAAA", "BBBB")])
        self.assertEqual(bundle["metadata"], {"name": TRUST_BUNDLE, "namespace": NAMESPACE})
        self.assertEqual(bundle["data"]["ca.crt"].count("BEGIN CERTIFICATE"), 2)
        for bad in ([], ["-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"], ["not a pem"],
                    [pem + "-----BEGIN RSA PRIVATE KEY-----\nAA\n-----END RSA PRIVATE KEY-----\n"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cert_lifecycle.trust_bundle(bad)

    def lab(self, folder):
        directory = Path(folder) / "run"
        (directory / "pki").mkdir(parents=True)
        return directory

    def test_issue_seeds_the_issuer_and_requests_every_certificate_without_keys_in_public_files(self):
        command = FakeOpenSSL()
        with tempfile.TemporaryDirectory() as folder:
            directory = self.lab(folder)
            ca = cert_lifecycle.make_ca(directory / "pki", "ca-1", command=command)
            cert_lifecycle.issue(directory, ca, command)
            public = (directory / "certificates.json").read_text(encoding="utf-8")
            secret = json.loads((directory / "pki" / "ca-secret-ca-1.json").read_text(encoding="utf-8"))
        kinds = [item["kind"] for item in json.loads(public)["items"]]
        self.assertEqual(kinds, ["ClusterIssuer", "ConfigMap"] + ["Certificate"] * len(lab.IDENTITIES))
        self.assertNotIn("PRIVATE", public)
        self.assertNotIn("tls.key", public)
        self.assertEqual((secret["metadata"], sorted(secret["data"])),
                         ({"name": "sentinel-lab-ca", "namespace": "cert-manager"}, ["tls.crt", "tls.key"]))
        kubectl = [call for call in command.calls if call[0] == "kubectl"]
        self.assertTrue(all(call[1] == "--context=kind-sentinel-boutique-security" for call in kubectl))
        self.assertIn("--for=condition=Ready", kubectl[-1])

    def test_every_key_file_is_owner_only_before_openssl_writes_a_key_into_it(self):
        events = []

        def command(args, **kwargs):
            args = [str(arg) for arg in args]
            if "-keyout" in args:
                key = Path(args[args.index("-keyout") + 1])
                # The file exists, is empty and was restricted before the key is written.
                events.append(("openssl", key.name, key.exists() and key.stat().st_size == 0))
            return FakeOpenSSL()(args)

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(cert_lifecycle, "_private_file", side_effect=lambda path: events.append(
                    ("private", Path(path).name, True))):
            directory = self.lab(folder)
            ca = cert_lifecycle.make_ca(directory / "pki", "ca-1", command=command)
            cert_lifecycle.sign(directory / "pki" / "client", ca, "client.internal", command=command)
            with self.assertRaises(FileExistsError):
                cert_lifecycle.private_key_file(ca / "ca.key")
        for name in ("ca.key", "tls.key"):
            mine = [event for event in events if event[1] == name]
            self.assertEqual(mine[:2], [("private", name, True), ("openssl", name, True)], name)

    def test_negatives_are_an_untrusted_ca_a_wrong_san_and_an_already_expired_ten_minute_certificate(self):
        command = FakeOpenSSL()
        now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as folder:
            directory = self.lab(folder)
            cert_lifecycle.make_ca(directory / "pki", "ca-1", command=command)
            made = cert_lifecycle.negatives(directory, command, now=now)
            secrets = {name: json.loads((directory / "pki" / ("secret-" + name + ".json")).read_text(encoding="utf-8"))
                       for name in made}
        self.assertEqual(made, ["negative-untrusted-ca", "negative-wrong-san", "negative-expired"])
        self.assertEqual({secret["metadata"]["name"] for secret in secrets.values()},
                         {"security-" + name for name in made})
        requests = [call for call in command.calls if call[1:3] == ["req", "-new"]]
        self.assertEqual([call[call.index("-subj") + 1] for call in requests],
                         ["/CN=client.internal", "/CN=wrong-client.internal", "/CN=client.internal"])
        signing = [call for call in command.calls if call[1] in ("x509", "ca")]
        self.assertIn("untrusted-ca", signing[0][signing[0].index("-CA") + 1])
        self.assertIn("ca-1", signing[1][signing[1].index("-CA") + 1])
        expired = signing[2]
        self.assertEqual(expired[1], "ca")
        self.assertEqual((expired[expired.index("-startdate") + 1], expired[expired.index("-enddate") + 1]),
                         ("261114114900Z", "261114115900Z"))
        self.assertIn("ca-1", expired[expired.index("-cert") + 1])
        for name in lab.PROBE_IDENTITIES:
            self.assertTrue(name in made or name in ("client", "proxy-client", VERIFIER_IDENTITY, "negative-old-ca"))

    def test_a_ca_rollover_overlaps_both_cas_then_keeps_only_the_new_one(self):
        command = FakeOpenSSL()
        with tempfile.TemporaryDirectory() as folder:
            directory = self.lab(folder)
            cert_lifecycle.make_ca(directory / "pki", "ca-1", command=command)
            self.assertEqual(cert_lifecycle.rollover(directory, "start", command), ["ca-1", "ca-2"])
            start = json.loads((directory / "trust-bundle-start.json").read_text(encoding="utf-8"))
            old_ca_trust = (directory / "pki" / cert_lifecycle.OLD_CA_IDENTITY / "ca.crt").read_text(encoding="utf-8")
            self.assertIsNone(cert_lifecycle.rollover(directory, "switch", command))
            issuer = json.loads((directory / "issuer-ca-2.json").read_text(encoding="utf-8"))
            self.assertEqual(cert_lifecycle.rollover(directory, "finish", command), ["ca-2"])
            with self.assertRaises(ValueError):
                cert_lifecycle.rollover(directory, "later", command)
        self.assertEqual(start["data"]["ca.crt"].count("BEGIN CERTIFICATE"), 2)
        # The old-CA test client trusts both CAs, so a refusal after the rollover is the gateway's.
        self.assertEqual(old_ca_trust.count("BEGIN CERTIFICATE"), 2)
        self.assertEqual(issuer, cert_lifecycle.cluster_issuer("sentinel-lab-ca", "sentinel-lab-ca-2"))
        deleted = [call[-2] for call in command.calls if "delete" in call]
        self.assertEqual(deleted, ["security-" + name for name in lab.IDENTITIES])

    def test_the_recipe_and_pin_check_run_no_cluster_command(self):
        out = io.StringIO()
        with patch("subprocess.run", side_effect=AssertionError("cluster command attempted")), \
                patch("sys.stdout", out), \
                patch.object(cert_lifecycle, "load_pins", return_value=unresolved(cert_lifecycle.load_pins())):
            self.assertEqual(cert_lifecycle.main(["pins", "--check"]), 1)
            self.assertEqual(cert_lifecycle.main(["pins"]), 0)
            self.assertEqual(cert_lifecycle.main(["plan", "--directory", "lab"]), 0)
        plan = json.loads(out.getvalue()[out.getvalue().index("{\n"):])
        self.assertEqual(list(plan), ["offline", "start", "rotation", "negatives", "escalation", "rollover"])
        self.assertIn("Unresolved: chart.sha256: TODO-PIN", out.getvalue())
        self.assertEqual(cert_lifecycle.CONTEXT, lab.CONTEXT)

    def test_the_identity_report_is_the_guards_decision_for_every_lab_certificate(self):
        now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)

        def record(identity, **changes):
            values = {"kind": "Certificate", "namespace": NAMESPACE, "name": "security-" + identity, "uid": identity,
                      "generation": 1, "secret_name": "security-" + identity, "dns_names": [identity + ".internal"],
                      "issuer": dict(cert_lifecycle.ISSUER_REF), "ready": "True", "ready_observed_generation": 1,
                      "not_before": "2026-11-14T11:30:00Z", "not_after": "2026-11-14T13:30:00Z", "revision": 1}
            values.update(changes)
            return values
        healthy = [record(identity) for identity in lab.IDENTITIES]
        self.assertEqual(cert_lifecycle.identity_report(healthy, now), {"status": "PASS", "findings": []})
        expiring = healthy[:1] + [record("upstream", not_after="2026-11-14T12:20:00Z")] + healthy[2:]
        report = cert_lifecycle.identity_report(expiring, now)
        self.assertEqual(report["status"], "ESCALATE")
        self.assertEqual([(item["code"], item["status"]) for item in report["findings"]],
                         [("SECURITY_IDENTITY_EXPIRING", "UNKNOWN")])
        self.assertEqual(cert_lifecycle.identity_report(healthy[:-1], now)["status"], "ESCALATE")
        self.assertEqual(cert_lifecycle.identity_report([dict(healthy[0], ready=True)], now)["status"], "INVALID")

    def test_live_steps_refuse_a_run_folder_that_is_not_a_cert_manager_lab(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "session.json").write_text(json.dumps({"context": lab.CONTEXT}), encoding="utf-8")
            with patch("subprocess.run", side_effect=AssertionError("cluster command attempted")), \
                    self.assertRaisesRegex(ValueError, "not a cert-manager security lab"):
                cert_lifecycle.main(["negatives", "--directory", folder])


if __name__ == "__main__":
    unittest.main()
