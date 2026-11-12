from collections import Counter
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiments.boutique.harness.connected import APPLICATIONS, UPSTREAM, UPSTREAM_SHA256, service_ports
from runtime.security_store import import_inspections, recent, update_alert
from scripts.security import network_policy
from scripts.security.boutique_security_manifest import CALLERS, NAMESPACE, caller_config, overlay, proxy_config
from scripts.test_boutique_live import BoutiqueExperiment
from scripts.security.scan_image_security import archive_config_id
from scripts.security.watch_security import assess_scan, inspection_alerts, load_inventory, scan_cycle
from services.image_security import summarize


IMAGE = "registry.example/app@sha256:" + "a" * 64
METHOD = "/hipstershop.ShippingService/GetQuote"


def base():
    return {"items": [{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": NAMESPACE},
                       "spec": {"template": {"spec": {"containers": [{"name": "server", "env": [
                           {"name": "SHIPPING_SERVICE_ADDR", "value": "shippingservice:50051"}]}]}}}} for name in (*CALLERS,"shippingservice")]}


class ConnectedSecurityTests(unittest.TestCase):
    def test_real_callers_use_private_loopback_and_mutually_authenticated_gateway(self):
        source = base()
        result = overlay(source, envoy_image="envoy@sha256:abc", inspector_image="inspector@sha256:abc")
        self.assertEqual(source, base())
        for item in result["items"]:
            if item["kind"] == "Deployment" and item["metadata"]["name"] in CALLERS:
                pod = item["spec"]["template"]["spec"]
                self.assertEqual(pod["containers"][0]["env"][0]["value"], "127.0.0.1:50052")
                self.assertEqual(pod["containers"][1]["name"], "shipping-egress")
                self.assertNotIn("volumeMounts", pod["containers"][0])
        config = caller_config("frontend-client")
        listener = config["static_resources"]["listeners"][0]
        self.assertEqual(listener["address"]["socket_address"]["address"], "127.0.0.1")
        self.assertEqual(config["static_resources"]["clusters"][0]["transport_socket"]["typed_config"]["sni"], "gateway.internal")

    def test_policies_allow_only_gateway_upstream_backend_chain(self):
        result = overlay(base(), envoy_image="envoy", inspector_image="inspector")
        policies = {i["spec"]["podSelector"]["matchLabels"]["app"]: i["spec"] for i in result["items"] if i["kind"] == "NetworkPolicy"}
        for target, caller, port in (("shippingservice", "security-gateway", 8444),):
            self.assertEqual(policies[target]["ingress"], [{"from": [{"podSelector": {"matchLabels": {"app": caller}}}],
                                                         "ports": [{"protocol": "TCP", "port": port}]}])
        self.assertEqual(len(policies), 2)
        cluster = proxy_config(True)["static_resources"]["clusters"][0]
        self.assertEqual(cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]["socket_address"]["address"],"127.0.0.1")

    def test_tls_server_checks_peer_identity_in_addition_to_ca(self):
        for upstream, names in ((False, {"frontend-client.internal", "checkoutservice-client.internal", "client.internal"}),
                                (True, {"proxy-client.internal"})):
            config = proxy_config(upstream)
            tls = config["static_resources"]["listeners"][0]["filter_chains"][0]["transport_socket"]["typed_config"]
            self.assertTrue(tls["require_client_certificate"])
            sans = tls["common_tls_context"]["validation_context"]["match_typed_subject_alt_names"]
            self.assertEqual({san["matcher"]["exact"] for san in sans}, names)

    def test_changed_upstream_caller_configuration_requires_review(self):
        source = base()
        source["items"][0]["spec"]["template"]["spec"]["containers"][0]["env"] = []
        with self.assertRaises(ValueError):
            overlay(source, envoy_image="envoy", inspector_image="inspector")


class ArchiveBindingTests(unittest.TestCase):
    def archive(self, folder, *, tamper=False):
        config = json.dumps({"os": "linux", "architecture": "amd64"}).encode()
        config_id = "sha256:" + hashlib.sha256(config).hexdigest()
        manifest = json.dumps({"config": {"digest": config_id}}).encode()
        manifest_id = "sha256:" + hashlib.sha256(manifest).hexdigest()
        index = json.dumps({"manifests": [{"digest": manifest_id, "platform": {"os": "linux", "architecture": "amd64"}}]}).encode()
        image_id = "sha256:" + hashlib.sha256(index).hexdigest()
        files = {"manifest.json": json.dumps([{"Config": "blobs/sha256/" + config_id.split(":")[1]}]).encode(),
                 "blobs/sha256/" + config_id.split(":")[1]: config,
                 "blobs/sha256/" + manifest_id.split(":")[1]: manifest,
                 "blobs/sha256/" + image_id.split(":")[1]: index + (b" " if tamper else b"")}
        path = Path(folder) / "image.tar"
        with tarfile.open(path, "w") as archive:
            for name, raw in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                archive.addfile(member, io.BytesIO(raw))
        return path, image_id, config_id

    def test_oci_index_and_platform_config_are_distinct_but_cryptographically_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            path, image, config = self.archive(folder)
            self.assertNotEqual(image, config)
            self.assertEqual(archive_config_id(path, image), config)

    def test_tampered_descriptor_chain_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path, image, _ = self.archive(folder, tamper=True)
            with self.assertRaises(ValueError):
                archive_config_id(path, image)


class SecurityMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.journal = self.root / "telemetry.sqlite3"

    def events(self):
        with closing(sqlite3.connect(self.journal)) as db:
            return recent(db)

    def test_duplicate_imports_are_suppressed_and_raw_extra_fields_refused(self):
        event = {"kind": "inspection", "at": "2026-11-04T01:00:00Z", "method": METHOD, "bytes": 20, "verdict": "DENY", "rule": "QUANTITY"}
        import_inspections(self.journal, [event, event])
        import_inspections(self.journal, [event])
        self.assertEqual(len(self.events()), 1)
        with self.assertRaises(ValueError):
            import_inspections(self.journal, [{**event, "raw_payload": "PRIVATE"}])

    def test_alerts_deduplicate_across_restarts_and_resolve_on_verified_clean_scan(self):
        report = {"image": IMAGE, "status": "FINDINGS", "counts": {"HIGH": 1, "CRITICAL": 0},
                  "findings": [{"id": "CVE-TEST", "package": "lib", "installed": "1", "severity": "HIGH"}]}
        assess_scan(self.journal, report)
        assess_scan(self.journal, report)
        self.assertEqual(len(self.events()), 1)
        assess_scan(self.journal, {"image": IMAGE, "status": "UNKNOWN"})
        self.assertEqual(len(self.events()), 2)
        self.assertTrue(all(e["status"] == "OPEN" for e in self.events()))
        assess_scan(self.journal, {"image": IMAGE, "status": "NO_KNOWN_CVES", "counts": {}, "findings": []})
        self.assertEqual(sum(e["status"] == "RESOLVED" for e in self.events()), 2)

    def test_recent_denials_alert_but_old_and_future_records_do_not(self):
        now = datetime(2026,11,4,1,tzinfo=timezone.utc)
        event = {"kind": "inspection", "at": now.isoformat(), "method": METHOD, "bytes": 20, "verdict": "DENY", "rule": "QUANTITY"}
        import_inspections(self.journal, [event])
        inspection_alerts(self.journal, now=now)
        inspection_alerts(self.journal, now=now)
        self.assertEqual(sum(e["kind"] == "alert" for e in self.events()), 1)
        inspection_alerts(self.journal, now=now+timedelta(seconds=61))
        self.assertEqual(self.events()[0]["status"], "RESOLVED")

    def test_scan_error_does_not_stop_remaining_images_or_create_clean_evidence(self):
        calls = []
        def scanner(image, directory, **kwargs):
            calls.append(image)
            raise RuntimeError("network unavailable")
        inventory = [{"image": IMAGE}, {"image": IMAGE.replace("a"*64,"b"*64)}]
        results = scan_cycle(inventory, self.journal, self.root / "scans", scanner=scanner)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(r["status"] == "UNKNOWN" for r in results))
        self.assertEqual(sum(e["kind"] == "alert" for e in self.events()), 2)

    def test_inventory_rejects_mutable_tags_extra_actions_and_duplicates(self):
        path = self.root / "inventory.json"
        for value in ([{"image": "app:latest"}], [{"image": IMAGE, "command": "patch"}], [{"image": IMAGE}]*2, []):
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                load_inventory(path)


# ----- P4.06: graph-derived NetworkPolicies and the enforcement matrix -----

LAB_IDENTITIES = ("sentinel-intruder", "sentinel-traffic", "security-test", "security-gateway", *sorted(APPLICATIONS))


def upstream_manifest():
    """A minimal manifest read straight from the pinned upstream YAML text (no YAML library)."""
    items = []
    for document in UPSTREAM.read_text(encoding="utf-8").split("\n---\n"):
        kind = re.search(r"^kind: (\w+)$", document, re.MULTILINE)
        name = re.search(r"^  name: (\S+)$", document, re.MULTILINE)
        if not kind or not name or name.group(1) in {"loadgenerator", "frontend-external"}:
            continue
        name = name.group(1)
        if kind.group(1) == "Deployment":
            env = [{"name": key, "value": host + ":" + port} for key, host, port in re.findall(
                r'^\s+- name: (\w+)\n\s+value: "([a-z0-9-]+):(\d+)"$', document, re.MULTILINE)]
            items.append({"kind": "Deployment", "metadata": {"name": name},
                          "spec": {"template": {"spec": {"containers": [{"name": "server", "env": env}]}}}})
        elif kind.group(1) == "Service":
            ports = re.findall(r"^    port: (\d+)$", document, re.MULTILINE)
            target = re.findall(r"^    targetPort: (\d+)$", document, re.MULTILINE)
            items.append({"kind": "Service", "metadata": {"name": name},
                          "spec": {"selector": {"app": name},
                                   "ports": [{"port": int(p), "targetPort": int(t)} for p, t in zip(ports, target, strict=True)]}})
    return {"items": items}


def pod(labels):
    return {"namespace": NAMESPACE, "labels": labels}


def identity(name):
    if name.startswith("probe:"):
        return {"app": network_policy.PROBE_APP, network_policy.PROBE_TARGET: name.split(":", 1)[1]}
    return {"app": name}


def reachable(items, namespace=NAMESPACE, identities=LAB_IDENTITIES, probes=("cartservice", "shippingservice")):
    """Every (source, destination, Pod port) the policies allow, over all identities and Service ports."""
    graph = network_policy.load_graph()
    ports = {name: {value["target"]} for name, value in graph["ports"].items()}
    ports["shippingservice"] |= {8444}
    ports["security-gateway"] = {8443}
    sources = [*identities, *("probe:" + target for target in probes)]
    found = set()
    for source in sources:
        for destination, numbers in ports.items():
            for port in sorted(numbers):
                if network_policy.allowed(items, {"namespace": namespace, "labels": identity(source)},
                                          {"namespace": namespace, "labels": {"app": destination}}, port):
                    found.add((source, destination, port))
    return found


def graph_edges():
    graph = network_policy.load_graph()
    return {(edge["from"], edge["to"], graph["ports"][edge["to"]]["target"]) for edge in graph["edges"]}


def lab_items():
    protect = [item for item in overlay(base(), envoy_image="envoy", inspector_image="inspector")["items"]
               if item["kind"] == "NetworkPolicy"]
    return protect + network_policy.security_lab_policies(network_policy.load_graph())


class ReviewedGraphTests(unittest.TestCase):
    def test_reviewed_graph_is_the_pinned_upstream_wiring(self):
        graph = network_policy.load_graph()
        self.assertEqual(graph["source"]["sha256"], UPSTREAM_SHA256)
        self.assertEqual(hashlib.sha256(UPSTREAM.read_bytes()).hexdigest(), UPSTREAM_SHA256)
        self.assertEqual(set(graph["nodes"]), APPLICATIONS)
        self.assertTrue(network_policy.same_graph(graph, network_policy.graph_from_manifest(upstream_manifest())))
        self.assertEqual(len(graph["edges"]), 15)
        self.assertEqual(graph["ports"]["emailservice"], {"service": 5000, "target": 8080})

    def test_service_ports_refuse_missing_named_or_duplicate_ports(self):
        for change in ("drop", "named", "two"):
            manifest = upstream_manifest()
            service = next(item for item in manifest["items"] if item["kind"] == "Service"
                           and item["metadata"]["name"] == "redis-cart")
            if change == "drop":
                manifest["items"].remove(service)
            elif change == "named":
                service["spec"]["ports"][0]["targetPort"] = "redis"
            else:
                service["spec"]["ports"].append({"port": 1, "targetPort": 1})
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                service_ports(manifest)

    def test_matrix_image_is_the_busybox_release_pinned_upstream(self):
        pinned = re.search(r"image: busybox:[0-9.]+@(sha256:[a-f0-9]{64})", UPSTREAM.read_text(encoding="utf-8"))
        self.assertEqual(network_policy.MATRIX_IMAGE, "docker.io/library/busybox@" + pinned.group(1))


class GraphPolicyTests(unittest.TestCase):
    def setUp(self):
        self.graph = network_policy.load_graph()
        self.items = network_policy.graph_policies(self.graph, "sentinel-boutique-connected")

    def test_every_service_is_isolated_both_ways_with_dns_and_covered(self):
        for name in APPLICATIONS:
            labels = {"namespace": "sentinel-boutique-connected", "labels": {"app": name}}
            selecting = {(item["metadata"]["name"], kind) for item in self.items
                         for kind in item["spec"]["policyTypes"]
                         if network_policy._matches(item["spec"]["podSelector"], labels["labels"])}
            with self.subTest(name=name):
                self.assertIn(("default-deny-ingress", "Ingress"), selecting)
                self.assertIn(("default-deny-egress", "Egress"), selecting)
                dns = {"namespace": "kube-system", "labels": {"k8s-app": "kube-dns"}}
                self.assertTrue(network_policy.allowed(self.items, labels, dns, 53, "UDP"))
                self.assertTrue(network_policy.allowed(self.items, labels, dns, 53, "TCP"))
                self.assertFalse(network_policy.allowed(self.items, labels, {**dns, "labels": {"k8s-app": "other"}}, 53, "UDP"))
        callees = {edge["to"] for edge in self.graph["edges"]} | {"frontend"}
        self.assertEqual({item["metadata"]["name"] for item in self.items if item["metadata"]["name"].startswith("ingress-")},
                         {"ingress-" + name for name in callees})

    def test_reachability_is_exactly_the_graph_plus_probes_and_storefront_traffic(self):
        expected = graph_edges() | {("sentinel-traffic", "frontend", 8080), ("probe:cartservice", "cartservice", 7070),
                                    ("probe:shippingservice", "shippingservice", 50051)}
        found = reachable(self.items, "sentinel-boutique-connected",
                          identities=("sentinel-intruder", "sentinel-traffic", *sorted(APPLICATIONS)))
        self.assertEqual(found, expected)

    def test_each_graph_edge_is_allowed_exactly_once_in_each_direction(self):
        for key, peers, side in (("ingress", "from", "callee"), ("egress", "to", "caller")):
            pairs = Counter()
            for item in self.items:
                owner = item["spec"]["podSelector"].get("matchLabels", {}).get("app")
                for rule in item["spec"].get(key, []):
                    for peer in rule[peers]:
                        other = peer["podSelector"]["matchLabels"].get("app")
                        if "namespaceSelector" in peer or other in {"sentinel-probe", "sentinel-traffic"}:
                            continue
                        pairs[(other, owner) if side == "callee" else (owner, other)] += 1
            with self.subTest(direction=key):
                self.assertEqual(pairs, Counter({(edge["from"], edge["to"]): 1 for edge in self.graph["edges"]}))

    def test_no_rule_admits_any_pod_or_address(self):
        for item in self.items + lab_items():
            for key, peers in (("ingress", "from"), ("egress", "to")):
                for rule in item["spec"].get(key, []):
                    with self.subTest(policy=item["metadata"]["name"]):
                        self.assertTrue(rule.get(peers))
                        self.assertTrue(rule.get("ports"))
                        for peer in rule[peers]:
                            self.assertNotIn("ipBlock", peer)
                            self.assertTrue(peer.get("podSelector", {}).get("matchLabels"))
                            if key == "ingress":
                                self.assertNotIn("namespaceSelector", peer)
                            elif "namespaceSelector" in peer:
                                self.assertEqual(peer, network_policy.DNS_RULE["to"][0])

    def test_redis_cart_is_reachable_only_from_cartservice(self):
        for items, namespace in ((self.items, "sentinel-boutique-connected"), (lab_items(), NAMESPACE)):
            sources = {source for source, destination, _ in reachable(items, namespace) if destination == "redis-cart"}
            self.assertEqual(sources, {"cartservice"})
        policy = next(item for item in self.items if item["metadata"]["name"] == "ingress-redis-cart")
        self.assertEqual(policy["spec"]["ingress"], [{"from": [{"podSelector": {"matchLabels": {"app": "cartservice"}}}],
                                                      "ports": [{"protocol": "TCP", "port": 6379}]}])

    def test_regeneration_is_byte_stable_and_the_committed_file_is_current(self):
        shuffled = dict(self.graph, edges=list(reversed(self.graph["edges"])), nodes=list(reversed(self.graph["nodes"])))
        first = network_policy.render(network_policy.security_lab_policies(self.graph))
        self.assertEqual(first, network_policy.render(network_policy.security_lab_policies(shuffled)))
        self.assertEqual(first, network_policy.SECURITY_POLICIES.read_text(encoding="utf-8"))
        self.assertEqual(network_policy.render(network_policy.graph_policies(self.graph, "ns")),
                         network_policy.render(network_policy.graph_policies(shuffled, "ns")))

    def test_a_new_env_edge_changes_exactly_the_two_affected_policies(self):
        manifest = upstream_manifest()
        adservice = next(item for item in manifest["items"] if item["kind"] == "Deployment"
                         and item["metadata"]["name"] == "adservice")
        adservice["spec"]["template"]["spec"]["containers"][0]["env"].append(
            {"name": "PRODUCT_CATALOG_SERVICE_ADDR", "value": "productcatalogservice:3550"})
        changed = dict(self.graph, **network_policy.graph_from_manifest(manifest))
        before = {item["metadata"]["name"]: item for item in self.items}
        after = {item["metadata"]["name"]: item for item in network_policy.graph_policies(changed, "sentinel-boutique-connected")}
        self.assertEqual({name for name in before.keys() | after.keys() if before.get(name) != after.get(name)},
                         {"ingress-productcatalogservice", "egress-adservice"})
        again = network_policy.graph_policies(dict(self.graph, **network_policy.graph_from_manifest(manifest)),
                                              "sentinel-boutique-connected")
        self.assertEqual(network_policy.render(list(after.values())), network_policy.render(again))
        with self.assertRaisesRegex(ValueError, "differs from the reviewed graph"):
            network_policy.reviewed_security_policies(changed)

    def test_unknown_callee_or_unreviewed_shipping_caller_raises(self):
        for edges in ([{"from": "frontend", "to": "shoppingassistantservice"}],
                      [{"from": "frontend", "to": "frontend"}]):
            with self.subTest(edges=edges), self.assertRaises(ValueError):
                network_policy.edges(dict(self.graph, edges=edges))
        with self.assertRaisesRegex(ValueError, "shipping caller"):
            network_policy.security_lab_policies(dict(self.graph, edges=[{"from": "cartservice", "to": "shippingservice"}]))
        with self.assertRaises(ValueError):
            network_policy.policies([("frontend", "intruder", 80)], "ns", workloads=["frontend"], ports={"frontend": 8080})

    def test_stale_reviewed_policy_file_is_refused(self):
        derived = network_policy.graph_from_manifest(upstream_manifest())
        self.assertEqual(network_policy.reviewed_security_policies(derived),
                         network_policy.security_lab_policies(self.graph))
        with tempfile.TemporaryDirectory() as folder:
            stale = Path(folder) / "policies.json"
            stale.write_text(network_policy.SECURITY_POLICIES.read_text(encoding="utf-8").replace("6379", "6380"),
                             encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "differ from the reviewed file"):
                network_policy.reviewed_security_policies(derived, policies_path=stale)

    def test_connected_probe_pod_reaches_only_its_target(self):
        with tempfile.TemporaryDirectory() as folder:
            experiment = BoutiqueExperiment(Path(folder) / "run")
            try:
                experiment.client_image = "grpcurl@sha256:" + "c" * 64
                experiment.select_target("cartservice")
                labels = experiment.probe_pod("cart-probe-x", "hipstershop.CartService/GetCart", {})["metadata"]["labels"]
            finally:
                experiment.store.close()
        source = {"namespace": "sentinel-boutique-connected", "labels": labels}
        reached = {name for name in APPLICATIONS if network_policy.allowed(
            self.items, source, {"namespace": "sentinel-boutique-connected", "labels": {"app": name}},
            self.graph["ports"][name]["target"])}
        self.assertEqual(reached, {"cartservice"})


class SecurityLabPolicyTests(unittest.TestCase):
    def test_lab_routes_shipping_only_through_the_gateway_and_keeps_every_other_edge(self):
        expected = {edge for edge in graph_edges() if edge[1] != "shippingservice"}
        expected |= {("frontend", "security-gateway", 8443), ("checkoutservice", "security-gateway", 8443),
                     ("security-test", "security-gateway", 8443), ("security-gateway", "shippingservice", 8444),
                     ("sentinel-traffic", "frontend", 8080), ("probe:cartservice", "cartservice", 7070)}
        self.assertEqual(reachable(lab_items()), expected)

    def test_graph_policies_never_own_the_protected_shipping_chain(self):
        names = {item["metadata"]["name"] for item in network_policy.security_lab_policies(network_policy.load_graph())}
        self.assertFalse(names & {"ingress-shippingservice", "ingress-security-gateway"})
        self.assertIn("egress-security-gateway", names)

    def test_lab_manifest_destinations_and_sources_cover_the_gateway_chain(self):
        manifest = overlay(upstream_manifest(), envoy_image="envoy", inspector_image="inspector")
        destinations = {entry["id"]: entry for entry in network_policy.matrix_destinations(manifest)}
        self.assertEqual(set(destinations), APPLICATIONS | {"security-gateway", "security-upstream"})
        self.assertEqual((destinations["frontend"]["service_port"], destinations["frontend"]["target_port"]), (80, 8080))
        sources = {entry["id"] for entry in network_policy.matrix_sources(APPLICATIONS | {"security-gateway"},
                                                                          ("cartservice",), extra=("security-test",))}
        self.assertEqual(len(sources), 2 + len(APPLICATIONS) + 2 + 1)
        self.assertTrue({"intruder", "traffic", "app-security-test", "probe-cartservice"} <= sources)


def matrix_rows(*cells):
    return [{"source": source, "destination": destination, "expected": expected, "observed": observed}
            for source, destination, expected, observed in cells]


class MatrixVerdictTests(unittest.TestCase):
    GOOD = (("app-cartservice", "redis-cart", "allow", "open"), ("intruder", "redis-cart", "deny", "closed"))

    def test_pass_needs_every_allowed_open_every_forbidden_refused_and_the_journey(self):
        self.assertEqual(network_policy.evaluate(matrix_rows(*self.GOOD), journey_passed=True)["status"], "PASS")

    def test_a_forbidden_edge_that_connects_is_unknown_never_pass(self):
        verdict = network_policy.evaluate(matrix_rows(*self.GOOD, ("intruder", "cartservice", "deny", "open")),
                                          journey_passed=True)
        self.assertEqual(verdict["status"], "UNKNOWN")
        self.assertIn("intruder->cartservice", verdict["reasons"][0])

    def test_untested_cells_missing_journey_or_one_sided_matrix_are_unknown(self):
        for rows, journey in ((matrix_rows(*self.GOOD, ("intruder", "adservice", "deny", "untested")), True),
                              (matrix_rows(*self.GOOD), None),
                              (matrix_rows(self.GOOD[1]), True), (matrix_rows(self.GOOD[0]), True), ([], True)):
            with self.subTest(rows=rows, journey=journey):
                self.assertEqual(network_policy.evaluate(rows, journey_passed=journey)["status"], "UNKNOWN")

    def test_a_refused_allowed_edge_or_failed_journey_is_fail(self):
        self.assertEqual(network_policy.evaluate(matrix_rows(*self.GOOD, ("app-frontend", "adservice", "allow", "closed")),
                                                 journey_passed=True)["status"], "FAIL")
        self.assertEqual(network_policy.evaluate(matrix_rows(*self.GOOD), journey_passed=False)["status"], "FAIL")

    def test_output_parsing_ignores_duplicates_and_unknown_ids(self):
        text = "MATRIX a open 0\nMATRIX b closed 3\nMATRIX b open 0\nMATRIX z open 0\nnoise\n"
        self.assertEqual(network_policy.parse_output(text, {"a", "b"}), {"a": ("open", 0)})


class TesterPodTests(unittest.TestCase):
    def test_tester_is_unprivileged_bounded_never_ready_and_carries_the_source_labels(self):
        source = {"id": "app-cartservice", "labels": {"app": "cartservice"}}
        pod = network_policy.tester_pod("netmatrix-x", NAMESPACE, "run1", source, [("redis-cart", "10.96.0.5", 6379)])
        spec, container = pod["spec"], pod["spec"]["containers"][0]
        self.assertEqual(pod["metadata"]["labels"], {"app": "cartservice", network_policy.MATRIX_LABEL: "run1"})
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertTrue(spec["securityContext"]["runAsNonRoot"])
        self.assertEqual(container["readinessProbe"]["exec"]["command"], ["false"])
        self.assertEqual(container["image"], network_policy.MATRIX_IMAGE)
        self.assertEqual(container["resources"]["limits"], {"cpu": "50m", "memory": "32Mi"})
        self.assertIn("check redis-cart 10.96.0.5 6379\n", container["command"][2])

    def test_script_arguments_are_validated(self):
        for check in (("a;rm", "10.0.0.1", 1), ("a", "10.0.0.1;id", 1), ("a", "10.0.0.1", 0), ("a", "host", 1)):
            with self.subTest(check=check), self.assertRaises(ValueError):
                network_policy.tester_script([check])


class FakeCluster:
    """kubectl for run_matrix: testers connect according to the policies, or to everything."""

    def __init__(self, items, *, enforce=True, control=True, create_fails=()):
        self.items, self.enforce, self.control, self.create_fails = items, enforce, control, set(create_fails)
        self.pods, self.calls, self.manifest = {}, [], overlay(upstream_manifest(), envoy_image="e", inspector_image="i")

    def __call__(self, *args):
        self.calls.append(args)
        done = lambda stdout="", code=0: SimpleNamespace(stdout=stdout, stderr="", returncode=code)
        if args[1:3] == ("get", "services"):
            services = [{"metadata": {"name": entry["id"]}, "spec": {"clusterIP": f"10.96.1.{index}"}}
                        for index, entry in enumerate(network_policy.matrix_destinations(self.manifest), 1)]
            return done(json.dumps({"items": services}))
        if args[1:3] == ("get", "service"):
            return done(json.dumps({"spec": {"clusterIP": "10.96.0.10"}}))
        if args[1] == "create":
            data = json.loads(Path(args[3]).read_text(encoding="utf-8"))
            if any(data["metadata"]["name"].startswith("netmatrix-" + source) for source in self.create_fails):
                return done(code=1)
            self.pods[data["metadata"]["name"]] = data
            return done()
        if args[1:3] == ("get", "pods"):
            return done(json.dumps({"items": [{"metadata": {"name": name}, "status": {"phase": "Succeeded"}}
                                              for name in self.pods]}))
        if args[1] == "logs":
            return done(self.output(self.pods[args[2]]))
        return done()

    def output(self, data):
        labels = {key: value for key, value in data["metadata"]["labels"].items() if key != network_policy.MATRIX_LABEL}
        destinations = {entry["id"]: entry for entry in network_policy.matrix_destinations(self.manifest)}
        lines = []
        for line in data["spec"]["containers"][0]["command"][2].splitlines():
            if not line.startswith("check "):
                continue
            _, name, _, _ = line.split()
            if name == "control":
                is_open = self.control
            else:
                is_open = not self.enforce or network_policy.allowed(
                    self.items, {"namespace": NAMESPACE, "labels": labels},
                    {"namespace": NAMESPACE, "labels": destinations[name]["labels"]}, destinations[name]["target_port"])
            lines.append(f"MATRIX {name} {'open' if is_open else 'closed'} {0 if is_open else 3}")
        return "\n".join(lines) + "\n"


class MatrixRunTests(unittest.TestCase):
    def run_matrix(self, cluster, journey=True):
        with tempfile.TemporaryDirectory() as folder:
            result = network_policy.run_matrix(
                cluster, folder, namespace=NAMESPACE, items=lab_items(), journey_passed=journey,
                sources=network_policy.matrix_sources(APPLICATIONS | {"security-gateway"}, ("cartservice",),
                                                      extra=("security-test",)),
                destinations=network_policy.matrix_destinations(cluster.manifest), run_id="run1234567",
                clock=iter(range(10_000)).__next__, sleep=lambda _: None)
            self.assertEqual(json.loads((Path(folder) / "network-matrix.json").read_text(encoding="utf-8")), result)
        self.assertIn(("--namespace=" + NAMESPACE, "delete", "pods", "-l", network_policy.MATRIX_LABEL + "=run1234567",
                       "--ignore-not-found", "--wait=false"), cluster.calls)
        return result

    def test_enforcing_cluster_passes_with_every_cell_tested(self):
        result = self.run_matrix(FakeCluster(lab_items()))
        self.assertEqual(result["verdict"]["status"], "PASS", result["verdict"]["reasons"])
        self.assertEqual(result["verdict"]["untested"], 0)
        self.assertEqual(result["verdict"]["allowed"]["cells"], 19)
        self.assertEqual(result["verdict"]["forbidden"]["timed_out"], result["verdict"]["forbidden"]["cells"])
        self.assertTrue(all(tester["trusted"] and tester["control"] == "open" for tester in result["testers"]))
        # 16 identities (intruder, traffic, 11 workloads, gateway, security-test, cart probe) x 13 Services.
        self.assertEqual(len(result["rows"]), 16 * 13)

    def test_cluster_that_ignores_networkpolicy_is_unknown_not_pass(self):
        result = self.run_matrix(FakeCluster(lab_items(), enforce=False))
        self.assertEqual(result["verdict"]["status"], "UNKNOWN")
        self.assertEqual(result["verdict"]["forbidden"]["closed"], 0)

    def test_failed_control_or_missing_tester_leaves_cells_untested_and_unknown(self):
        for cluster in (FakeCluster(lab_items(), control=False), FakeCluster(lab_items(), create_fails=("intruder",))):
            with self.subTest(control=cluster.control):
                result = self.run_matrix(cluster)
                self.assertEqual(result["verdict"]["status"], "UNKNOWN")
                self.assertGreater(result["verdict"]["untested"], 0)

    def test_over_tight_policy_fails(self):
        items = [item for item in lab_items() if item["metadata"]["name"] != "egress-cartservice"]
        cluster = FakeCluster(items)
        with tempfile.TemporaryDirectory() as folder:
            # The run judges against the reviewed set, while the cluster enforces a set without cart -> redis egress.
            result = network_policy.run_matrix(
                cluster, folder, namespace=NAMESPACE, items=lab_items(), journey_passed=True,
                sources=network_policy.matrix_sources(APPLICATIONS, ()), run_id="run1234567",
                destinations=network_policy.matrix_destinations(cluster.manifest),
                clock=iter(range(10_000)).__next__, sleep=lambda _: None)
        self.assertEqual(result["verdict"]["status"], "FAIL")
        self.assertIn("app-cartservice->redis-cart", result["verdict"]["reasons"][0])


class SecurityLabMatrixWiringTests(unittest.TestCase):
    def test_lab_judges_the_applied_policies_with_every_identity(self):
        from scripts.security import boutique_security as lab
        manifest = overlay(upstream_manifest(), envoy_image="e", inspector_image="i")
        manifest["items"].extend(network_policy.security_lab_policies(network_policy.load_graph()))
        seen = {}

        def run_matrix(kubectl, directory, **kwargs):
            seen.update(kwargs)
            return {"verdict": {"status": "PASS"}}
        with tempfile.TemporaryDirectory() as folder, patch.object(lab, "run_matrix", side_effect=run_matrix):
            (Path(folder) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            lab.network_matrix(Path(folder), True)
        self.assertEqual(seen["items"], [item for item in manifest["items"] if item["kind"] == "NetworkPolicy"])
        self.assertEqual(len(seen["items"]), 2 + 17)
        self.assertIn("app-security-test", {source["id"] for source in seen["sources"]})
        self.assertIn("probe-cartservice", {source["id"] for source in seen["sources"]})
        self.assertTrue(seen["journey_passed"])


if __name__ == "__main__":
    unittest.main()
