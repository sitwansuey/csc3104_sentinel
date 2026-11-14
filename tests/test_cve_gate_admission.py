"""The CVE gate outside the Guard (P5.02): admission parameters, CI checks, the scan reader and the live inventory.

Everything here is offline. The ValidatingAdmissionPolicy is checked
structurally; no API server has evaluated it, and kubectl is never called.
"""

from contextlib import closing, redirect_stdout
from datetime import UTC, datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from runtime.image_scans import load_cve_exceptions, newest_scans, read_image_scans
from scripts.security import gate_images, image_inventory
from scripts.security.scan_image_security import TRIVY_VERSION
from scripts.security.watch_security import load_inventory
from tests.support.manifest_yaml import load_all

ROOT = Path(__file__).resolve().parents[1]
VAP = ROOT / "deploy/security/approved-images-vap.yaml"
NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)
CART = "us-central1-docker.pkg.dev/online-boutique-ci/microservices-demo/cartservice@sha256:" + "c" * 64
CART_WORSE = "us-central1-docker.pkg.dev/online-boutique-ci/microservices-demo/cartservice@sha256:" + "d" * 64
REDIS = "redis@sha256:" + "e" * 64
UNSCANNED = "us-central1-docker.pkg.dev/online-boutique-ci/microservices-demo/emailservice@sha256:" + "a" * 64
PINNED = re.compile(r"(?:[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/)?[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}")
RULES = {"block_severities": ["CRITICAL", "HIGH"], "mode": "non_regression", "max_scan_age_seconds": 86400,
         "trusted_scanner_images": [TRIVY_VERSION]}


def stamp(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def scan(image, *findings, age=60, **changes):
    record = {"image": image, "status": "FINDINGS" if findings else "NO_KNOWN_CVES", "scanner": "Trivy",
              "scanner_image": TRIVY_VERSION, "scanned_at": stamp(age), "automatic_patching": False,
              "findings": [{"id": cve, "package": "p", "installed": "1", "fixed": None, "severity": level,
                            "recommendation": "r"} for cve, level in findings]}
    record.update(changes)
    return record


def deployment(name, *containers, labels=None):
    return {"kind": "Deployment", "metadata": {"name": name},
            "spec": {"selector": {"matchLabels": labels or {"app": name}},
                     "template": {"spec": {"containers": [{"name": c, "image": i} for c, i in containers]}}}}


def pod(name, labels, *statuses, containers=()):
    return {"kind": "Pod", "metadata": {"name": name, "labels": labels},
            "spec": {"containers": [{"name": c, "image": i} for c, i in containers]},
            "status": {"containerStatuses": [{"name": c, "imageID": i} for c, i in statuses]}}


class Workspace(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.scans = self.directory / "scans"
        self.scans.mkdir()

    def write_scan(self, name, record):
        (self.scans / name).write_text(json.dumps(record), encoding="utf-8")

    def write(self, name, value):
        path = self.directory / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path


class ApprovedImagesPolicyTests(unittest.TestCase):
    def setUp(self):
        self.documents = {document["kind"]: document for document in load_all(VAP.read_text(encoding="utf-8"))}
        self.policy = self.documents["ValidatingAdmissionPolicy"]
        self.binding = self.documents["ValidatingAdmissionPolicyBinding"]

    def test_policy_and_binding_reference_each_other_and_start_without_deny(self):
        self.assertEqual(set(self.documents), {"ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"})
        self.assertEqual(self.binding["spec"]["policyName"], self.policy["metadata"]["name"])
        self.assertEqual(self.binding["spec"]["validationActions"], ["Audit", "Warn"])  # nothing denied yet
        self.assertEqual(self.policy["spec"]["failurePolicy"], "Fail")

    def test_params_are_the_generated_configmap_and_a_missing_one_denies(self):
        self.assertEqual(self.policy["spec"]["paramKind"], {"apiVersion": "v1", "kind": "ConfigMap"})
        self.assertEqual(self.binding["spec"]["paramRef"], {
            "name": gate_images.CONFIGMAP_NAME, "namespace": gate_images.CONFIGMAP_NAMESPACE,
            "parameterNotFoundAction": "Deny"})

    def test_deployments_are_checked_on_create_and_update_and_pods_only_on_create(self):
        rules = {tuple(rule["resources"]): rule["operations"] for rule in
                 self.policy["spec"]["matchConstraints"]["resourceRules"]}
        self.assertEqual(rules, {("deployments",): ["CREATE", "UPDATE"], ("pods",): ["CREATE"]})

    def test_every_container_kind_is_checked_against_the_digest_list(self):
        variables = {item["name"]: item["expression"] for item in self.policy["spec"]["variables"]}
        self.assertIn("params.data['digests'].split('\\n')", variables["approved"])
        for group in ("containers", "initContainers", "ephemeralContainers"):
            self.assertIn(f"variables.podSpec.{group}.map(c, c.image)", variables["images"])
        [validation] = self.policy["spec"]["validations"]
        self.assertEqual(validation["expression"], "variables.images.all(image, image != '' && image in variables.approved)")

    def test_binding_is_limited_to_the_connected_lab_namespace(self):
        [selector] = self.binding["spec"]["matchResources"]["namespaceSelector"]["matchExpressions"]
        self.assertEqual((selector["key"], selector["operator"], selector["values"]),
                         ("kubernetes.io/metadata.name", "In", ["sentinel-boutique-connected"]))

    def test_docs_state_what_the_guard_preview_does_with_a_denial(self):
        # services/guard/preview.py maps any non-zero kubectl exit, a denial
        # included, to UNKNOWN (SERVER_DRY_RUN_FAILED), and the Guard then
        # escalates (tests/test_guard_preview.py, tests/test_guard_decisions.py).
        # Neither the policy comment nor the security doc may say it rejects.
        security = " ".join((ROOT / "docs/security.md").read_text(encoding="utf-8").split())
        comment = " ".join(line.lstrip("# ") for line in VAP.read_text(encoding="utf-8").splitlines()
                           if line.startswith("#"))
        for name, text in (("docs/security.md", security), ("approved-images-vap.yaml", comment)):
            with self.subTest(name=name):
                for claim in ("SERVER_DRY_RUN_REJECTED", "two layers agree"):
                    self.assertFalse(claim in text, f"{name} still says {claim}")
                for code in ("SERVER_DRY_RUN_UNKNOWN", "SERVER_DRY_RUN_FAILED"):
                    self.assertTrue(code in text, f"{name} does not name {code}")


class ScanReaderTests(Workspace):
    def test_newest_completed_scan_wins_and_failures_never_hide_it(self):
        self.write_scan("old.json", scan(CART, ("CVE-1", "HIGH"), age=7200))
        self.write_scan("new.json", scan(CART, ("CVE-1", "HIGH"), ("CVE-2", "LOW"), age=60))
        self.write_scan("failed.json", {"image": CART, "status": "UNKNOWN", "findings": []})
        self.write_scan("broken.json", "{not json")
        (self.scans / "garbage.json").write_text("{not json", encoding="utf-8")
        self.write_scan("undated.json", scan(CART, ("CVE-3", "CRITICAL"), scanned_at="soon"))
        self.write_scan("process.json", {"pid": 1})
        result = read_image_scans(self.scans, [CART])
        [record] = result["scans"]
        self.assertEqual(record["scanned_at"], stamp(60))
        self.assertEqual({item["id"] for item in record["findings"]}, {"CVE-1", "CVE-2"})
        self.assertEqual(set(record), {"image", "status", "findings", "scanner_image", "scanned_at"})
        self.assertEqual(result["exceptions"], [])

    def test_a_failed_scan_is_kept_only_when_nothing_completed(self):
        self.write_scan("failed.json", {"image": CART, "status": "UNKNOWN", "findings": []})
        self.assertEqual(read_image_scans(self.scans, [CART])["scans"][0]["status"], "UNKNOWN")

    def test_only_requested_digests_are_returned_and_aliases_match(self):
        self.write_scan("redis.json", scan("docker.io/library/redis@sha256:" + "e" * 64))
        self.write_scan("cart.json", scan(CART))
        self.assertEqual([item["image"] for item in read_image_scans(self.scans, [REDIS])["scans"]],
                         ["docker.io/library/redis@sha256:" + "e" * 64])
        self.assertEqual(set(newest_scans(self.scans)), {"docker.io/library/redis@sha256:" + "e" * 64, CART})

    def test_exceptions_file_must_name_its_schema(self):
        good = self.write("exceptions.json", {"schema": "sentinel-cve-exceptions-v1", "exceptions": []})
        self.assertEqual(load_cve_exceptions(good), [])
        for value in ({"exceptions": []}, {"schema": "other", "exceptions": []}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_cve_exceptions(self.write("bad.json", value))


class GateCommandTests(Workspace):
    def run_main(self, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            status = gate_images.main(["--now", NOW.isoformat(), *map(str, arguments)])
        return status, json.loads(output.getvalue())

    def test_gate_exit_codes_follow_the_verdict(self):
        rules = self.write("rules.json", RULES)
        self.write_scan("cart.json", scan(CART, ("CVE-1", "HIGH")))
        self.write_scan("worse.json", scan(CART_WORSE, ("CVE-1", "HIGH"), ("CVE-9", "CRITICAL")))
        cases = ((CART, CART, 0, "PASS"), (CART_WORSE, CART, 1, "FAIL"), (UNSCANNED, CART, 2, "UNKNOWN"))
        for candidate, current, code, verdict in cases:
            with self.subTest(candidate=candidate):
                status, report = self.run_main("gate", "--scans", self.scans, "--rules", rules,
                                               "--candidate", "server=" + candidate, "--current", "server=" + current)
                self.assertEqual((status, report["verdict"]), (code, verdict))

    def test_rules_can_come_from_a_guard_policy_and_are_validated(self):
        policy = self.write("policy.json", {"security_constraints": {"image_vulnerability_gate": RULES}})
        self.assertEqual(gate_images.load_rules(policy), RULES)
        for value in ({"security_constraints": {}}, dict(RULES, mode="off"), {"security_constraints": None}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gate_images.load_rules(self.write("bad.json", value))

    def test_check_exceptions_fails_on_an_expired_or_malformed_entry(self):
        def entry(approved, expires):
            return {"image": CART, "id": "CVE-1", "approved_at": stamp(approved), "expires_at": stamp(expires),
                    "rationale": "Reviewed.", "reviewer": "security-owner"}
        valid = self.write("valid.json", {"schema": "sentinel-cve-exceptions-v1", "exceptions": [entry(86400, -86400)]})
        expired = self.write("expired.json", {"schema": "sentinel-cve-exceptions-v1", "exceptions": [entry(864000, 60)]})
        malformed = self.write("malformed.json", {"schema": "sentinel-cve-exceptions-v1", "exceptions": [{"id": "x"}]})
        self.assertEqual(self.run_main("check-exceptions", valid), (0, {"status": "OK", "count": 1, "expired": []}))
        status, report = self.run_main("check-exceptions", expired)
        self.assertEqual((status, report["status"], report["expired"]), (1, "EXPIRED", [{"image": CART, "id": "CVE-1"}]))
        self.assertEqual(self.run_main("check-exceptions", malformed)[0], 1)
        self.assertEqual(self.run_main("check-exceptions", self.directory / "missing.json")[0], 1)

    def test_the_committed_exceptions_file_passes_the_ci_check(self):
        self.assertEqual(self.run_main("check-exceptions", ROOT / "deploy/security/cve-exceptions.json")[0], 0)

    def test_pairs_must_be_distinct_container_assignments(self):
        for values in (["server"], ["=x"], ["a=x", "a=y"]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                gate_images.pairs(values, "--candidate")


class VapParamsTests(Workspace):
    def inventory(self, *workloads):
        return {"schema": "sentinel-image-inventory-v1", "workloads": [
            {"kind": kind, "name": name, "container": container, "image": digest, "digest": digest}
            for kind, name, container, digest in workloads]}

    def params(self, inventory, **options):
        return gate_images.vap_params(scans=self.scans, rules=RULES, exceptions=None, inventory=inventory, now=NOW,
                                      **options)

    def test_vap_params_contain_digests_only(self):
        self.write_scan("cart.json", scan(CART, ("CVE-1", "HIGH")))
        self.write_scan("redis.json", scan(REDIS))
        inventory = self.inventory(("Deployment", "cartservice", "server", CART),
                                   ("Deployment", "redis-cart", "redis", "docker.io/library/redis@sha256:" + "e" * 64))
        configmap, report = self.params(inventory, extra_images=["fullstorydev/grpcurl@sha256:" + "f" * 64])
        lines = configmap["data"]["digests"].split("\n")
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, PINNED)
            self.assertNotIn(":", line.split("@")[0].rsplit("/", 1)[-1], "a tag before the digest")
        self.assertIn(CART, lines)
        self.assertIn(REDIS, lines)  # the short spelling a Pod spec may use
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual((configmap["metadata"]["name"], configmap["metadata"]["namespace"]),
                         (gate_images.CONFIGMAP_NAME, gate_images.CONFIGMAP_NAMESPACE))

    def test_tags_are_refused(self):
        for extra in ("fullstorydev/grpcurl:v1.9.3", "fullstorydev/grpcurl:v1.9.3@sha256:" + "f" * 64):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.params(self.inventory(), extra_images=[extra])

    def test_an_unscanned_or_unresolved_digest_is_not_approved(self):
        self.write_scan("cart.json", scan(CART))
        inventory = self.inventory(("Deployment", "cartservice", "server", CART),
                                   ("Deployment", "emailservice", "server", UNSCANNED),
                                   ("Deployment", "redis-cart", "redis", None))
        configmap, report = self.params(inventory)
        self.assertEqual(configmap["data"]["digests"], CART)
        self.assertEqual(report["verdict"], "UNKNOWN")
        self.assertEqual({item["name"]: item["verdict"] for item in report["workloads"]},
                         {"cartservice": "PASS", "emailservice": "UNKNOWN", "redis-cart": "UNKNOWN"})

    def test_a_baseline_is_approved_only_if_it_passes_against_the_running_image(self):
        self.write_scan("cart.json", scan(CART, ("CVE-1", "HIGH")))
        self.write_scan("worse.json", scan(CART_WORSE, ("CVE-1", "HIGH"), ("CVE-9", "CRITICAL")))
        baselines = self.directory / "baselines"
        baselines.mkdir()
        for name, image in (("cartservice", CART_WORSE),):
            (baselines / (name + ".json")).write_text(json.dumps({
                "target": {"name": name}, "pod_template": {"spec": {"containers": [{"name": "server", "image": image}]}}}),
                encoding="utf-8")
        configmap, report = self.params(self.inventory(("Deployment", "cartservice", "server", CART)), baselines=baselines)
        self.assertEqual(configmap["data"]["digests"], CART)
        self.assertEqual(report["baselines"][0]["verdict"], "FAIL")

    def test_cli_writes_the_configmap_and_report(self):
        self.write_scan("cart.json", scan(CART))
        rules = self.write("rules.json", RULES)
        inventory = self.write("inventory.json", self.inventory(("Deployment", "cartservice", "server", CART)))
        output, report = self.directory / "out/params.json", self.directory / "out/report.json"
        with redirect_stdout(io.StringIO()):
            status = gate_images.main(["--now", NOW.isoformat(), "vap-params", "--scans", str(self.scans), "--rules",
                                       str(rules), "--inventory", str(inventory), "--output", str(output),
                                       "--report", str(report)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.read_text())["data"]["digests"], CART)
        self.assertEqual(json.loads(report.read_text())["approved"], [CART])


class InventoryTests(Workspace):
    def objects(self):
        return {"items": [
            deployment("cartservice", ("server", CART)),
            deployment("emailservice", ("server", "registry.example/email:v1")),
            deployment("redis-cart", ("redis", "redis:alpine")),
            deployment("adservice", ("server", "registry.example/ad:v1")),
            pod("cartservice-1", {"app": "cartservice"}, ("server", "docker-pullable://" + CART)),
            pod("emailservice-1", {"app": "emailservice"}, ("server", UNSCANNED)),
            pod("redis-cart-1", {"app": "redis-cart"}, ("redis", "sha256:" + "9" * 64)),
            pod("adservice-1", {"app": "adservice"}, ("server", "registry.example/ad@sha256:" + "1" * 64)),
            pod("adservice-2", {"app": "adservice"}, ("server", "registry.example/ad@sha256:" + "2" * 64)),
            pod("probe", {"run": "probe"}, ("probe", CART_WORSE), containers=[("probe", "probe:v1")])]}

    def test_digests_resolve_from_pinned_specs_or_one_repo_digest_and_never_by_guessing(self):
        entries = {(item["name"], item["container"]): item for item in image_inventory.workloads(self.objects())}
        self.assertEqual(entries[("cartservice", "server")]["digest"], CART)
        self.assertEqual(entries[("emailservice", "server")]["digest"], UNSCANNED)
        self.assertEqual(entries[("redis-cart", "redis")]["status"], "UNKNOWN")  # a config ID is not a repo digest
        self.assertEqual(entries[("adservice", "server")]["status"], "UNKNOWN")  # Pods disagree
        self.assertEqual(entries[("probe", "probe")]["kind"], "Pod")
        self.assertEqual(entries[("probe", "probe")]["digest"], CART_WORSE)

    def test_a_pinned_spec_that_the_pods_do_not_run_is_unknown(self):
        objects = {"items": [deployment("cartservice", ("server", CART)),
                             pod("cartservice-1", {"app": "cartservice"}, ("server", CART_WORSE))]}
        [entry] = image_inventory.workloads(objects)
        self.assertEqual(entry["status"], "UNKNOWN")

    def test_index_mismatch_requires_verified_manifests_on_every_pods_node_platform(self):
        objects = {"items": [deployment("cartservice", ("server", CART)),
                             pod("cartservice-1", {"app": "cartservice"}, ("server", CART_WORSE)),
                             {"kind": "Node", "metadata": {"name": "node"}, "status": {"nodeInfo": {
                                 "operatingSystem": "linux", "architecture": "amd64"}}}]}
        objects["items"][1]["spec"]["nodeName"] = "node"
        manifest = CART.split("@")[0] + "@sha256:" + "1" * 64
        mapping = {image: {("linux", "amd64"): manifest} for image in (CART, CART_WORSE)}
        [entry] = image_inventory.workloads(objects, platform_manifests=mapping)
        self.assertEqual(entry["digest"], CART)  # keep the exact pinned spec for admission
        self.assertEqual(entry["platform_resolution"][0]["manifest"], manifest)
        for change in ("missing", "different", "platform", "node", "unknown", "repository"):
            with self.subTest(change=change):
                data = json.loads(json.dumps(objects))
                proof = {image: dict(platforms) for image, platforms in mapping.items()}
                if change == "missing":
                    proof.pop(CART_WORSE)
                elif change == "different":
                    proof[CART_WORSE][("linux", "amd64")] = CART_WORSE
                elif change == "platform":
                    data["items"][2]["status"]["nodeInfo"]["architecture"] = "arm64"
                elif change == "node":
                    data["items"][1]["spec"].pop("nodeName")
                elif change == "unknown":
                    data["items"][1]["status"]["containerStatuses"].append({"name": "server", "imageID": "sha256:123"})
                else:
                    other = "other.example/cart@" + CART_WORSE.split("@")[1]
                    data["items"][1]["status"]["containerStatuses"][0]["imageID"] = other
                    proof[other] = proof.pop(CART_WORSE)
                [entry] = image_inventory.workloads(data, platform_manifests=proof)
                self.assertEqual(entry["status"], "UNKNOWN")

    def test_recorded_indexes_require_exact_digest_bytes_and_unique_platform_selection(self):
        child = {"digest": "sha256:" + "1" * 64, "platform": {"os": "linux", "architecture": "amd64"}}
        index = {"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [child]}
        for ambiguous in (False, True):
            index["manifests"] = [child] * (2 if ambiguous else 1)
            raw = json.dumps(index).encode()
            image = "docker.io/library/redis@sha256:" + hashlib.sha256(raw).hexdigest()
            recorded = self.directory / "index.json"
            recorded.write_bytes(raw)
            mapping = self.write("mapping.json", {"schema": "sentinel-platform-indexes-v1", "indexes": [
                {"image": image, "index_file": "index.json"}]})
            manifests = image_inventory.load_platform_manifests(mapping)
            self.assertEqual(manifests[image][("linux", "amd64")], None if ambiguous else
                             "docker.io/library/redis@" + child["digest"])
            recorded.write_bytes(raw + b"\n")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                image_inventory.load_platform_manifests(mapping)

    def test_scan_ages_new_digests_and_the_queue(self):
        self.write_scan("cart.json", scan(CART, age=60))
        self.write_scan("probe.json", scan(CART_WORSE, age=90000))
        self.write_scan("email.json", {"image": UNSCANNED, "status": "UNKNOWN", "findings": []})
        previous = {"digests": {CART: {}}}
        inventory = image_inventory.build(self.objects(), namespace="ns", scans=self.scans, previous=previous, now=NOW)
        self.assertEqual({digest: record["scan"] for digest, record in inventory["digests"].items()},
                         {CART: "FRESH", CART_WORSE: "STALE", UNSCANNED: "FAILED"})
        self.assertEqual(inventory["digests"][CART], {"scan": "FRESH", "scan_age_seconds": 60, "new": False})
        self.assertEqual(inventory["unresolved"], 2)
        self.assertEqual(inventory["scan_queue"][-1], CART)  # fresh and known: queued last, to stay fresh
        self.assertEqual(load_inventory(self.write("queue.json", [{"image": d} for d in inventory["scan_queue"]])),
                         [{"image": d} for d in inventory["scan_queue"]])

    def test_cli_writes_inventory_queue_and_stale_alerts(self):
        source = self.write("objects.json", self.objects())
        output, queue, journal = self.directory / "inventory.json", self.directory / "queue.json", self.directory / "j.db"
        with redirect_stdout(io.StringIO()):
            status = image_inventory.main(["--from-json", str(source), "--scans", str(self.scans), "--output",
                                           str(output), "--scan-queue", str(queue), "--journal", str(journal)])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.read_text())["schema"], "sentinel-image-inventory-v1")
        self.assertEqual(len(load_inventory(queue)), 3)
        with closing(sqlite3.connect(journal)) as db:
            alerts = [json.loads(row[0]) for row in db.execute("SELECT payload FROM security_events WHERE kind='alert'")]
        self.assertEqual({alert["code"] for alert in alerts}, {"SCAN_STALE"})
        self.assertEqual(len(alerts), 4)  # three digests with no scan, and the unresolved containers

    def test_only_the_connected_lab_context_is_read_and_sentinel_lab_never(self):
        for context in ("kind-sentinel-lab", "sentinel-lab", "kind-sentinel-dist", "", None):
            with self.subTest(context=context), self.assertRaises(image_inventory.Refused):
                image_inventory.read_live(context, "ns", runner=lambda *a, **k: self.fail("kubectl must not run"))
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"items": []}))
        self.assertEqual(image_inventory.read_live("kind-sentinel-boutique-connected", "ns", runner=runner), {"items": []})
        self.assertEqual(calls[0][:6], ["kubectl", "--context", "kind-sentinel-boutique-connected", "--namespace", "ns", "get"])
        self.assertEqual(calls[0][6], "deployments,pods")


class GuardImageTests(unittest.TestCase):
    """The Guard image must hold every first-party module the Guard imports, the gate's included."""

    def copied(self):
        sources = []
        for line in (ROOT / "deploy/docker/guard.Dockerfile").read_text(encoding="utf-8").splitlines():
            if line.startswith("COPY "):
                sources.extend(line.split()[1:-1])
        return sources

    def imported_files(self):
        import ast

        pending = sorted((ROOT / "services/guard").glob("*.py"))
        seen = set()
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import) else
                         [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else [])
                if isinstance(node, ast.ImportFrom) and node.module in ("services", "runtime", "shared"):
                    names += [node.module + "." + alias.name for alias in node.names]
                for name in names:
                    if name.split(".")[0] not in ("services", "runtime", "shared"):
                        continue
                    parts = name.split(".")
                    for depth in range(1, len(parts) + 1):
                        base = ROOT.joinpath(*parts[:depth])
                        for candidate in (base / "__init__.py", base.with_suffix(".py")):
                            if candidate.is_file():
                                pending.append(candidate)
        return {path.relative_to(ROOT).as_posix() for path in seen}

    def test_every_imported_module_is_copied_into_the_guard_image(self):
        sources = self.copied()
        needed = self.imported_files()
        self.assertIn("services/image_security.py", needed)
        self.assertIn("runtime/image_scans.py", needed)
        missing = sorted(path for path in needed
                         if path not in sources and not any(source.endswith("/") and path.startswith(source)
                                                            for source in sources))
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
