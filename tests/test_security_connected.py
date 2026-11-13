from collections import Counter
from copy import deepcopy
from contextlib import closing, redirect_stdout
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
from scripts.security.security_lab import ROUTES
from services.grpc_inspection import ROUTE_METHODS
from scripts.test_boutique_live import BoutiqueExperiment
from scripts.security.scan_image_security import archive_config_id
from scripts.security.watch_security import assess_scan, inspection_alerts, load_inventory, scan_cycle
from services.image_security import summarize


IMAGE = "registry.example/app@sha256:" + "a" * 64
METHOD = "/hipstershop.ShippingService/GetQuote"


def base():
    return {"items": [{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": NAMESPACE},
                       "spec": {"template": {"spec": {"containers": [{"name": "server", "env": [
                           {"name": "SHIPPING_SERVICE_ADDR", "value": "shippingservice:50051"},
                           {"name": "CART_SERVICE_ADDR", "value": "cartservice:7070"}]}]}}}}
                      for name in (*CALLERS, "shippingservice", "cartservice")]}


def deployment(manifest, name):
    return next(item for item in manifest["items"] if item["kind"] == "Deployment" and item["metadata"]["name"] == name)


def envoy_json(manifest, name):
    item = next(item for item in manifest["items"] if item["kind"] == "ConfigMap" and item["metadata"]["name"] == name)
    return json.loads(item["data"]["envoy.json"])["static_resources"]


def endpoint(cluster):
    address = cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]["socket_address"]
    return address["address"], address["port_value"]


class ConnectedSecurityTests(unittest.TestCase):
    def test_real_callers_use_private_loopback_and_mutually_authenticated_gateway(self):
        source = base()
        result = overlay(source, envoy_image="envoy@sha256:abc", inspector_image="inspector@sha256:abc")
        self.assertEqual(source, base())
        for item in result["items"]:
            if item["kind"] == "Deployment" and item["metadata"]["name"] in CALLERS:
                pod = item["spec"]["template"]["spec"]
                self.assertEqual({entry["name"]: entry["value"] for entry in pod["containers"][0]["env"]},
                                 {"SHIPPING_SERVICE_ADDR": "127.0.0.1:50052", "CART_SERVICE_ADDR": "127.0.0.1:50053"})
                self.assertEqual(pod["containers"][1]["name"], "protected-egress")
                self.assertEqual(pod["containers"][1]["ports"], [{"containerPort": 50052}, {"containerPort": 50053}])
                self.assertNotIn("volumeMounts", pod["containers"][0])
        config = caller_config("frontend-client")
        listener = config["static_resources"]["listeners"][0]
        self.assertEqual(listener["address"]["socket_address"]["address"], "127.0.0.1")
        self.assertEqual(config["static_resources"]["clusters"][0]["transport_socket"]["typed_config"]["sni"], "gateway.internal")

    def test_cart_wiring_is_rewritten_for_both_callers_only(self):
        result = overlay(base(), envoy_image="envoy", inspector_image="inspector")
        for name in ("shippingservice", "cartservice"):
            # The protected services keep their own (unused) env entries; only callers are rewired.
            env = {entry["name"]: entry["value"] for entry in deployment(result, name)["spec"]["template"]["spec"]["containers"][0]["env"]}
            self.assertEqual(env, {"SHIPPING_SERVICE_ADDR": "shippingservice:50051", "CART_SERVICE_ADDR": "cartservice:7070"})
        for caller in CALLERS:
            with self.subTest(caller=caller):
                config = envoy_json(result, "security-" + caller + "-egress")
                listeners = {listener["name"]: listener["address"]["socket_address"] for listener in config["listeners"]}
                self.assertEqual(listeners, {"upstream": {"address": "127.0.0.1", "port_value": 50052},
                                             "upstream-cart": {"address": "127.0.0.1", "port_value": 50053}})
                self.assertTrue(all("transport_socket" not in listener["filter_chains"][0] for listener in config["listeners"]))
                self.assertEqual({cluster["name"]: endpoint(cluster) for cluster in config["clusters"]},
                                 {"shipping": ("security-gateway", 8443), "cart": ("security-cart-gateway", 8445)})
                for cluster in config["clusters"]:
                    tls = cluster["transport_socket"]["typed_config"]
                    self.assertEqual(tls["sni"], "gateway.internal")
                    self.assertEqual(tls["common_tls_context"]["tls_certificates"][0]["certificate_chain"]["filename"],
                                     "/pki/" + caller + "-client/tls.crt")
                # A route's egress listener forwards only that route's methods.
                paths = {listener["name"]: {route["match"].get("path") for route in listener["filter_chains"][0]["filters"][0]
                                            ["typed_config"]["route_config"]["virtual_hosts"][0]["routes"]}
                         for listener in config["listeners"]}
                self.assertEqual(paths["upstream-cart"], set(ROUTE_METHODS["cart"]) | {None})
                self.assertEqual(paths["upstream"], set(ROUTE_METHODS["shipping"]) | {None})

    def test_each_route_has_distinct_ports_and_its_own_tls_sidecar(self):
        result = overlay(base(), envoy_image="envoy", inspector_image="inspector")
        ports = [(spec["port"], spec["egress_port"], spec["gateway_port"], spec["upstream_port"]) for spec in ROUTES.values()]
        flat = [port for group in ports for port in group]
        self.assertEqual(len(flat), len(set(flat)))
        self.assertEqual(ports, [(50051, 50052, 8443, 8444), (7070, 50053, 8445, 8446)])
        cart = deployment(result, "cartservice")["spec"]["template"]["spec"]
        self.assertEqual([container["name"] for container in cart["containers"]], ["server", "cart-ingress"])
        ingress = cart["containers"][1]
        self.assertEqual(ingress["ports"], [{"containerPort": 8446}])
        self.assertEqual(ingress["readinessProbe"], {"tcpSocket": {"port": 8446}, "periodSeconds": 3})
        self.assertEqual({mount["mountPath"] for mount in ingress["volumeMounts"]}, {"/config", "/pki/cart-upstream"})
        upstream = envoy_json(result, "security-cart-upstream")
        self.assertEqual(endpoint(upstream["clusters"][0]), ("127.0.0.1", 7070))
        tls = upstream["listeners"][0]["filter_chains"][0]["transport_socket"]["typed_config"]
        self.assertTrue(tls["require_client_certificate"])
        self.assertEqual(tls["common_tls_context"]["validation_context"]["match_typed_subject_alt_names"],
                         [{"san_type": "DNS", "matcher": {"exact": "proxy-client.internal"}}])
        services = {item["metadata"]["name"]: item["spec"] for item in result["items"] if item["kind"] == "Service"}
        self.assertEqual(services["security-cart-upstream"]["selector"], {"app": "cartservice"})
        self.assertEqual(services["security-cart-upstream"]["ports"][0]["port"], 8446)
        self.assertEqual(services["security-cart-gateway"]["selector"], {"app": "security-gateway"})
        self.assertEqual(services["security-cart-gateway"]["ports"][0]["port"], 8445)
        self.assertEqual(services["security-gateway"]["ports"][0]["port"], 8443)
        # The gateway dials each service's TLS sidecar with the proxy identity and checks the sidecar's name.
        gateway = envoy_json(result, "security-gateway")
        clusters = {cluster["name"]: cluster for cluster in gateway["clusters"]}
        self.assertEqual(set(clusters), {"shipping", "cart", "inspector"})
        self.assertEqual(endpoint(clusters["cart"]), ("security-cart-upstream", 8446))
        self.assertEqual(clusters["cart"]["transport_socket"]["typed_config"]["sni"], "cart-upstream.internal")
        self.assertEqual(endpoint(clusters["shipping"]), ("security-upstream", 8444))
        self.assertEqual([listener["address"]["socket_address"]["port_value"] for listener in gateway["listeners"]],
                         [8443, 8445])

    def test_policies_allow_only_gateway_upstream_backend_chain(self):
        result = overlay(base(), envoy_image="envoy", inspector_image="inspector")
        policies = {i["spec"]["podSelector"]["matchLabels"]["app"]: i["spec"] for i in result["items"] if i["kind"] == "NetworkPolicy"}
        for target, caller, port in (("shippingservice", "security-gateway", 8444), ("cartservice", "security-gateway", 8446)):
            self.assertEqual(policies[target]["ingress"], [{"from": [{"podSelector": {"matchLabels": {"app": caller}}}],
                                                         "ports": [{"protocol": "TCP", "port": port}]}])
        self.assertEqual(policies["security-gateway"]["ingress"], [{
            "from": [{"podSelector": {"matchLabels": {"app": name}}} for name in (*CALLERS, "security-test")],
            "ports": [{"protocol": "TCP", "port": 8443}, {"protocol": "TCP", "port": 8445}]}])
        self.assertEqual(len(policies), 3)
        cluster = proxy_config(True)["static_resources"]["clusters"][0]
        self.assertEqual(cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]["socket_address"]["address"],"127.0.0.1")

    def test_tls_server_checks_peer_identity_in_addition_to_ca(self):
        for route in ROUTES:
            for upstream, names in ((False, {"frontend-client.internal", "checkoutservice-client.internal", "client.internal"}),
                                    (True, {"proxy-client.internal"})):
                config = proxy_config(upstream, route=route)
                tls = config["static_resources"]["listeners"][0]["filter_chains"][0]["transport_socket"]["typed_config"]
                with self.subTest(route=route, upstream=upstream):
                    self.assertTrue(tls["require_client_certificate"])
                    sans = tls["common_tls_context"]["validation_context"]["match_typed_subject_alt_names"]
                    self.assertEqual({san["matcher"]["exact"] for san in sans}, names)

    def test_changed_upstream_caller_configuration_requires_review(self):
        for index, change in ((0, []), (1, [{"name": "SHIPPING_SERVICE_ADDR", "value": "shippingservice:50051"}]),
                              (0, [{"name": "SHIPPING_SERVICE_ADDR", "value": "shippingservice:50051"},
                                   {"name": "CART_SERVICE_ADDR", "value": "redis-cart:6379"}]),
                              (1, [{"name": "SHIPPING_SERVICE_ADDR", "value": "shippingservice:50051"},
                                   {"name": "CART_SERVICE_ADDR", "value": "cartservice:7070"},
                                   {"name": "CART_SERVICE_ADDR", "value": "cartservice:7070"}])):
            source = base()
            source["items"][index]["spec"]["template"]["spec"]["containers"][0]["env"] = change
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "caller wiring changed"):
                overlay(source, envoy_image="envoy", inspector_image="inspector")
        source = base()
        source["items"] = [item for item in source["items"] if item["metadata"]["name"] != "cartservice"]
        with self.assertRaisesRegex(ValueError, "cart service is missing"):
            overlay(source, envoy_image="envoy", inspector_image="inspector")

    def test_gateway_inspector_is_bounded_and_its_readiness_is_the_real_readyz(self):
        result = overlay(base(), envoy_image="envoy", inspector_image="inspector")
        pod = deployment(result, "security-gateway")["spec"]["template"]["spec"]
        envoy, inspector = pod["containers"]
        self.assertEqual(envoy["ports"], [{"containerPort": 8443}, {"containerPort": 8445}])
        self.assertEqual(inspector["args"], ["--journal", "/data/telemetry.sqlite3", "--stdout-events",
                                             "--max-inflight", "16", "--max-rps", "200"])
        command = inspector["readinessProbe"]["exec"]["command"]
        self.assertEqual(command[:2], ["python", "-c"])
        self.assertIn("http://127.0.0.1:9001/readyz", command[2])
        self.assertNotIn("tcpSocket", inspector["readinessProbe"])
        # The inspector never listens beyond loopback, so no httpGet probe or container port exposes it.
        self.assertNotIn("ports", inspector)
        gateway = envoy_json(result, "security-gateway")
        breaker = next(cluster for cluster in gateway["clusters"] if cluster["name"] == "inspector")["circuit_breakers"]
        self.assertEqual(breaker["thresholds"][0]["max_connections"], 32)
        self.assertEqual(breaker["thresholds"][0]["max_pending_requests"], 32)
        for listener in gateway["listeners"]:
            authz = listener["filter_chains"][0]["filters"][0]["typed_config"]["http_filters"][0]["typed_config"]
            with self.subTest(listener=listener["name"]):
                self.assertFalse(authz["failure_mode_allow"])
                self.assertEqual(authz["status_on_error"], {"code": "ServiceUnavailable"})


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
    ports["cartservice"] |= {8446}
    ports["security-gateway"] = {8443, 8445}
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


def protect_items():
    return [item for item in lab_items() if item["metadata"]["name"].startswith("protect-")]


def stored(items):
    """Policies as the API server returns them: server metadata added, empty rule lists omitted."""
    result = []
    for item in deepcopy(items):
        item["metadata"].update({"uid": "u-" + item["metadata"]["name"], "resourceVersion": "7", "generation": 1})
        for key in ("ingress", "egress"):
            if item["spec"].get(key) == []:
                del item["spec"][key]
        result.append(item)
    return result


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
        # redis-cart is probed only in its scenario pair (P4.05), with a Redis PING on its own port.
        expected = graph_edges() | {("sentinel-traffic", "frontend", 8080), ("probe:cartservice", "cartservice", 7070),
                                    ("probe:shippingservice", "shippingservice", 50051),
                                    ("probe:redis-cart", "redis-cart", 6379)}
        found = reachable(self.items, "sentinel-boutique-connected",
                          identities=("sentinel-intruder", "sentinel-traffic", *sorted(APPLICATIONS)),
                          probes=("cartservice", "redis-cart", "shippingservice"))
        self.assertEqual(found, expected)

    def test_the_redis_probe_pod_reaches_only_redis_cart_on_its_port(self):
        from experiments.boutique.harness import targets
        lab = SimpleNamespace(name="redis-cart", namespace="sentinel-boutique-connected",
                              profile=targets.profile("redis-cart"), baseline={"pod_template": {"spec": {
                                  "containers": [{"name": "redis", "image": "docker.io/library/redis@sha256:" + "a" * 64}]}}})
        pod = BoutiqueExperiment.redis_probe_pod(lab, "redis-probe-test")
        source = {"namespace": "sentinel-boutique-connected", "labels": pod["metadata"]["labels"]}
        self.assertEqual(source["labels"], {"app": "sentinel-probe", "sentinel.io/probe-target": "redis-cart"})
        graph = network_policy.load_graph()
        for destination, value in graph["ports"].items():
            with self.subTest(destination=destination):
                self.assertIs(network_policy.allowed(self.items, source, {
                    "namespace": "sentinel-boutique-connected", "labels": {"app": destination}}, value["target"]),
                    destination == "redis-cart")
        # Without its labels the same Pod is refused, as every Redis PING was before.
        self.assertFalse(network_policy.allowed(self.items, {**source, "labels": {}}, {
            "namespace": "sentinel-boutique-connected", "labels": {"app": "redis-cart"}}, 6379))
        self.assertIn("redis-cart", network_policy.connected_probe_targets())
        self.assertNotIn("redis-cart", network_policy.probe_targets())  # The security lab never runs a pair.

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
        # Besides cartservice, only the redis-cart probe Pod of a scenario-pair run, on the same port.
        policy = next(item for item in self.items if item["metadata"]["name"] == "ingress-redis-cart")
        self.assertEqual(policy["spec"]["ingress"], [
            {"from": [{"podSelector": {"matchLabels": {"app": "cartservice"}}}],
             "ports": [{"protocol": "TCP", "port": 6379}]},
            {"from": [{"podSelector": {"matchLabels": {"app": "sentinel-probe", "sentinel.io/probe-target": "redis-cart"}}}],
             "ports": [{"protocol": "TCP", "port": 6379}]}])
        lab_policy = next(item for item in lab_items() if item["metadata"]["name"] == "ingress-redis-cart")
        self.assertEqual(len(lab_policy["spec"]["ingress"]), 1)

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
        with self.assertRaisesRegex(ValueError, "Unreviewed cart caller: recommendationservice"):
            network_policy.security_lab_policies(dict(self.graph, edges=[{"from": "recommendationservice", "to": "cartservice"}]))
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
    def test_lab_routes_shipping_and_cart_only_through_the_gateway_and_keeps_every_other_edge(self):
        expected = {edge for edge in graph_edges() if edge[1] not in {"shippingservice", "cartservice"}}
        expected |= {(source, "security-gateway", port) for source in ("frontend", "checkoutservice", "security-test")
                     for port in (8443, 8445)}
        expected |= {("security-gateway", "shippingservice", 8444), ("security-gateway", "cartservice", 8446),
                     ("sentinel-traffic", "frontend", 8080)}
        self.assertEqual(reachable(lab_items()), expected)

    def test_cart_plaintext_port_is_closed_to_every_pod_including_its_callers_and_probe(self):
        # P5.04: frontend and checkoutservice used to reach cartservice:7070 directly, and the
        # cart probe too; now only the gateway reaches cart, on its TLS sidecar's port.
        for source in (*LAB_IDENTITIES, "probe:cartservice"):
            with self.subTest(source=source):
                self.assertFalse(network_policy.allowed(lab_items(), pod(identity(source)), pod({"app": "cartservice"}), 7070))
        reached = {source for source, destination, _ in reachable(lab_items()) if destination == "cartservice"}
        self.assertEqual(reached, {"security-gateway"})
        # cartservice itself still reaches redis-cart (the uninspected RESP edge) and nothing else.
        self.assertEqual({(destination, port) for source, destination, port in reachable(lab_items()) if source == "cartservice"},
                         {("redis-cart", 6379)})

    def test_graph_policies_never_own_the_protected_chains(self):
        names = {item["metadata"]["name"] for item in network_policy.security_lab_policies(network_policy.load_graph())}
        self.assertFalse(names & {"ingress-shippingservice", "ingress-cartservice", "ingress-security-gateway"})
        self.assertIn("egress-security-gateway", names)

    def test_a_caller_may_reach_one_callee_on_two_ports_but_never_twice_on_one(self):
        items = network_policy.policies([("frontend", "security-gateway", 8443), ("frontend", "security-gateway", 8445)],
                                        "ns", workloads=["frontend", "security-gateway"], ports={"frontend": 8080})
        egress = next(item for item in items if item["metadata"]["name"] == "egress-frontend")
        self.assertEqual([rule["ports"][0]["port"] for rule in egress["spec"]["egress"]], [8443, 8445])
        with self.assertRaisesRegex(ValueError, "Duplicate edge"):
            network_policy.policies([("frontend", "security-gateway", 8443), ("frontend", "security-gateway", 8443)],
                                    "ns", workloads=["frontend", "security-gateway"], ports={"frontend": 8080})

    def test_lab_manifest_destinations_and_sources_cover_the_gateway_chain(self):
        manifest = overlay(upstream_manifest(), envoy_image="envoy", inspector_image="inspector")
        destinations = {entry["id"]: entry for entry in network_policy.matrix_destinations(manifest)}
        self.assertEqual(set(destinations), APPLICATIONS | {"security-gateway", "security-upstream",
                                                            "security-cart-gateway", "security-cart-upstream"})
        self.assertEqual((destinations["frontend"]["service_port"], destinations["frontend"]["target_port"]), (80, 8080))
        sources = {entry["id"] for entry in network_policy.matrix_sources(APPLICATIONS | {"security-gateway"},
                                                                          ("cartservice",), extra=("security-test",))}
        self.assertEqual(len(sources), 2 + len(APPLICATIONS) + 2 + 1)
        self.assertTrue({"intruder", "traffic", "app-security-test", "probe-cartservice"} <= sources)

    def test_policy_differences_name_missing_extra_and_changed_policies(self):
        reviewed = lab_items()
        self.assertEqual(network_policy.policy_differences(reviewed, stored(reviewed), "the cluster"), [])
        changed = deepcopy(reviewed)
        policy = next(item for item in changed if item["metadata"]["name"] == "ingress-redis-cart")
        policy["spec"]["ingress"][0]["from"].append({"podSelector": {"matchLabels": {"app": "frontend"}}})
        extra = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                 "metadata": {"name": "allow-all", "namespace": NAMESPACE},
                 "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{}]}}
        found = [item for item in changed if not item["metadata"]["name"].startswith("egress-")] + [extra, changed[0]]
        reasons = network_policy.policy_differences(reviewed, found, "the cluster")
        self.assertEqual(reasons[0], "the cluster lists the NetworkPolicy 'protect-shippingservice' twice")
        self.assertTrue(reasons[1].startswith("the cluster lacks reviewed NetworkPolicies: egress-cartservice, "))
        self.assertEqual(reasons[2:], ["the cluster has unreviewed NetworkPolicies: allow-all",
                                       "the cluster has changed NetworkPolicies: ingress-redis-cart"])

    def test_an_empty_peer_list_admits_every_peer_as_in_kubernetes(self):
        for rule in ({"from": []}, {"from": [], "ports": []}, {}):
            items = [{"metadata": {"name": "p", "namespace": NAMESPACE},
                      "spec": {"podSelector": {"matchLabels": {"app": "redis-cart"}}, "policyTypes": ["Ingress"],
                               "ingress": [rule]}}]
            with self.subTest(rule=rule):
                self.assertTrue(network_policy.allowed(items, pod({"app": "sentinel-intruder"}),
                                                       pod({"app": "redis-cart"}), 6379))


def matrix_rows(*cells, seconds=3):
    """Cells as run_matrix records them; a closed cell waited out the connect timeout unless seconds says otherwise."""
    return [{"source": source, "destination": destination, "expected": expected, "observed": observed,
             "seconds": seconds if observed == "closed" else 0}
            for source, destination, expected, observed in cells]


class MatrixVerdictTests(unittest.TestCase):
    GOOD = (("app-cartservice", "redis-cart", "allow", "open"), ("intruder", "redis-cart", "deny", "closed"),
            ("app-productcatalogservice", "cartservice", "deny", "closed"),
            ("app-paymentservice", "checkoutservice", "deny", "closed"))

    def test_pass_needs_every_allowed_open_every_forbidden_refused_and_the_journey(self):
        verdict = network_policy.evaluate(matrix_rows(*self.GOOD), journey_passed=True)
        self.assertEqual(verdict["status"], "PASS", verdict["reasons"])
        self.assertEqual(verdict["forbidden"], {"cells": 3, "closed": 3, "timed_out": 3})

    def test_a_missing_or_allowed_sampled_forbidden_edge_is_unknown(self):
        # Judged against a set without the graph policies, intruder -> redis-cart is expected to connect.
        for rows in (matrix_rows(*self.GOOD[:3]),
                     matrix_rows(*self.GOOD[:1], ("intruder", "redis-cart", "allow", "open"), *self.GOOD[2:])):
            with self.subTest(rows=rows):
                verdict = network_policy.evaluate(rows, journey_passed=True)
                self.assertEqual(verdict["status"], "UNKNOWN")
                self.assertTrue(any("sampled forbidden edges" in reason for reason in verdict["reasons"]))
        self.assertEqual(network_policy.evaluate(matrix_rows(*self.GOOD[:3]), journey_passed=True,
                                                 required_denials=())["status"], "PASS")

    def test_a_forbidden_edge_refused_at_once_is_unknown_not_a_policy_drop(self):
        rows = matrix_rows(*self.GOOD[:1]) + matrix_rows(*self.GOOD[1:], seconds=0)
        verdict = network_policy.evaluate(rows, journey_passed=True)
        self.assertEqual(verdict["status"], "UNKNOWN")
        self.assertIn("refused at once", verdict["reasons"][0])
        self.assertEqual(verdict["forbidden"]["timed_out"], 0)

    def test_policy_drift_is_unknown_and_never_hides_a_fail(self):
        verdict = network_policy.evaluate(matrix_rows(*self.GOOD), journey_passed=True,
                                          policy_drift=["the cluster lacks reviewed NetworkPolicies: x"])
        self.assertEqual((verdict["status"], verdict["reasons"]), ("UNKNOWN", ["the cluster lacks reviewed NetworkPolicies: x"]))
        self.assertEqual(network_policy.evaluate(matrix_rows(*self.GOOD), journey_passed=False,
                                                 policy_drift=["drift"])["status"], "FAIL")

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

    def __init__(self, items, *, enforce=True, control=True, create_fails=(), live=None, live_fails=False):
        self.items, self.enforce, self.control, self.create_fails = items, enforce, control, set(create_fails)
        # live: the policies the API server reports; by default exactly the enforced ones.
        self.live, self.live_fails = items if live is None else live, live_fails
        self.pods, self.calls, self.manifest = {}, [], overlay(upstream_manifest(), envoy_image="e", inspector_image="i")

    def __call__(self, *args):
        self.calls.append(args)
        done = lambda stdout="", code=0: SimpleNamespace(stdout=stdout, stderr="", returncode=code)
        if args[1:3] == ("get", "networkpolicies"):
            return done(code=1) if self.live_fails else done(json.dumps({"items": stored(self.live)}))
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
        self.assertEqual(result["policy_drift"], [])
        self.assertEqual(result["verdict"]["untested"], 0)
        # 11 graph edges not to shipping or cart, 3 identities x 2 gateway route Services,
        # the gateway to 2 TLS sidecars, and storefront traffic to frontend.
        self.assertEqual(result["verdict"]["allowed"]["cells"], 20)
        self.assertEqual(result["verdict"]["forbidden"]["timed_out"], result["verdict"]["forbidden"]["cells"])
        self.assertTrue(all(tester["trusted"] and tester["control"] == "open" for tester in result["testers"]))
        # 16 identities (intruder, traffic, 11 workloads, gateway, security-test, cart probe) x 15 Services.
        self.assertEqual(len(result["rows"]), 16 * 15)
        allowed = {(row["source"], row["destination"]) for row in result["rows"] if row["expected"] == "allow"}
        self.assertFalse({cell for cell in allowed if cell[1] == "cartservice"})
        self.assertTrue({("app-frontend", "security-cart-gateway"), ("app-checkoutservice", "security-cart-gateway"),
                         ("app-security-gateway", "security-cart-upstream")} <= allowed)

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

    def test_judged_without_the_graph_policies_is_never_pass(self):
        # An older run folder applied only the protect-* chain; judged against that, every edge "works as expected".
        cluster = FakeCluster(protect_items())
        with tempfile.TemporaryDirectory() as folder:
            result = network_policy.run_matrix(
                cluster, folder, namespace=NAMESPACE, items=protect_items(), journey_passed=True,
                sources=network_policy.matrix_sources(APPLICATIONS | {"security-gateway"}, ("cartservice",),
                                                      extra=("security-test",)),
                destinations=network_policy.matrix_destinations(cluster.manifest), run_id="run1234567",
                clock=iter(range(10_000)).__next__, sleep=lambda _: None)
        self.assertEqual(result["verdict"]["status"], "UNKNOWN")
        self.assertEqual(result["policy_drift"], [])
        # protect-cartservice (P5.04) alone already denies productcatalogservice -> cartservice.
        self.assertIn("sampled forbidden edges missing or judged allowed: intruder->redis-cart, "
                      "app-paymentservice->checkoutservice",
                      result["verdict"]["reasons"])

    def test_cluster_whose_live_policies_differ_from_the_reviewed_set_is_unknown(self):
        permissive = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                      "metadata": {"name": "allow-intruder", "namespace": NAMESPACE},
                      "spec": {"podSelector": {"matchLabels": {"app": "paymentservice"}}, "policyTypes": ["Ingress"],
                               "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": "sentinel-intruder"}}}]}]}}
        cases = {"only protect-*": (FakeCluster(protect_items()), "lacks reviewed NetworkPolicies: default-deny-egress"),
                 "extra policy": (FakeCluster(lab_items(), live=lab_items() + [permissive]),
                                  "has unreviewed NetworkPolicies: allow-intruder"),
                 "unreadable": (FakeCluster(lab_items(), live_fails=True), "could not be read")}
        for case, (cluster, reason) in cases.items():
            with self.subTest(case=case):
                result = self.run_matrix(cluster)
                self.assertEqual(result["verdict"]["status"], "UNKNOWN")
                self.assertTrue(any(reason in entry for entry in result["policy_drift"]), result["policy_drift"])
                self.assertTrue(any(reason in entry for entry in result["verdict"]["reasons"]))

    def test_over_tight_policy_fails(self):
        items = [item for item in lab_items() if item["metadata"]["name"] != "egress-cartservice"]
        cluster = FakeCluster(items, live=lab_items())
        with tempfile.TemporaryDirectory() as folder:
            # The run judges against the reviewed set, while the cluster enforces a set without cart -> redis egress.
            result = network_policy.run_matrix(
                cluster, folder, namespace=NAMESPACE, items=lab_items(), journey_passed=True,
                sources=network_policy.matrix_sources(APPLICATIONS, ()), run_id="run1234567",
                destinations=network_policy.matrix_destinations(cluster.manifest),
                clock=iter(range(10_000)).__next__, sleep=lambda _: None)
        self.assertEqual(result["verdict"]["status"], "FAIL")
        self.assertIn("app-cartservice->redis-cart", result["verdict"]["reasons"][0])


def lab_manifest(policies=None):
    """The applied manifest prepare writes: the overlay plus the reviewed graph policies."""
    manifest = overlay(upstream_manifest(), envoy_image="e", inspector_image="i")
    manifest["items"].extend(network_policy.security_lab_policies(network_policy.load_graph()) if policies is None else policies)
    return manifest


class SecurityLabMatrixWiringTests(unittest.TestCase):
    def test_lab_judges_the_reviewed_policies_with_every_identity(self):
        from scripts.security import boutique_security as lab
        manifest = lab_manifest()
        seen = {}

        def run_matrix(kubectl, directory, **kwargs):
            seen.update(kwargs)
            return {"verdict": {"status": "PASS"}}
        with tempfile.TemporaryDirectory() as folder, patch.object(lab, "run_matrix", side_effect=run_matrix):
            (Path(folder) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            lab.network_matrix(Path(folder), True)
        self.assertEqual(seen["items"], lab_items())
        self.assertEqual(seen["items"], [item for item in manifest["items"] if item["kind"] == "NetworkPolicy"])
        # protect-shippingservice, -cartservice and -security-gateway, plus 16 graph-derived policies
        # (no ingress-cartservice since P5.04: protect-cartservice owns cart's ingress).
        self.assertEqual(len(seen["items"]), 3 + 16)
        self.assertEqual(seen["policy_drift"], [])
        self.assertIn("app-security-test", {source["id"] for source in seen["sources"]})
        self.assertIn("probe-cartservice", {source["id"] for source in seen["sources"]})
        self.assertTrue(seen["journey_passed"])

    def matrix(self, manifest, cluster):
        from scripts.security import boutique_security as lab
        context = []

        def command(args, **kwargs):
            context.append(tuple(args[:2]))
            return cluster(*args[2:])
        with tempfile.TemporaryDirectory() as folder, patch.object(lab, "command", side_effect=command):
            (Path(folder) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = lab.network_matrix(Path(folder), True)
        self.assertEqual(set(context), {("kubectl", "--context=kind-sentinel-boutique-security")})
        return result

    def test_reviewed_manifest_on_an_enforcing_cluster_passes(self):
        result = self.matrix(lab_manifest(), FakeCluster(lab_items()))
        self.assertEqual(result["verdict"]["status"], "PASS", result["verdict"]["reasons"])

    def test_run_folder_without_the_graph_policies_is_never_pass(self):
        # An older run folder: manifest.json and the cluster hold only the protect-* chain.
        result = self.matrix(lab_manifest(policies=[]), FakeCluster(protect_items()))
        self.assertEqual(result["verdict"]["status"], "UNKNOWN")
        self.assertTrue(result["policy_drift"][0].startswith("manifest.json lacks reviewed NetworkPolicies: default-deny-egress"))
        cell = next(row for row in result["rows"] if (row["source"], row["destination"]) == ("intruder", "redis-cart"))
        self.assertEqual((cell["expected"], cell["observed"]), ("deny", "open"))

    def test_cluster_whose_live_policies_differ_is_never_pass(self):
        tampered = deepcopy(lab_items())
        policy = next(item for item in tampered if item["metadata"]["name"] == "ingress-redis-cart")
        policy["spec"]["ingress"][0]["from"].append({"podSelector": {"matchLabels": {"app": "sentinel-intruder"}}})
        result = self.matrix(lab_manifest(), FakeCluster(lab_items(), live=tampered))
        self.assertEqual(result["verdict"]["status"], "UNKNOWN")
        self.assertEqual(result["policy_drift"], ["the cluster has changed NetworkPolicies: ingress-redis-cart"])

    def test_stale_committed_policy_file_refuses_the_matrix(self):
        from scripts.security import boutique_security as lab
        with tempfile.TemporaryDirectory() as folder:
            stale = Path(folder) / "policies.json"
            stale.write_text(network_policy.SECURITY_POLICIES.read_text(encoding="utf-8").replace("6379", "6380"),
                             encoding="utf-8")
            (Path(folder) / "manifest.json").write_text(json.dumps(lab_manifest()), encoding="utf-8")
            with patch.object(network_policy, "SECURITY_POLICIES", stale), patch.object(lab, "run_matrix") as run:
                with self.assertRaisesRegex(ValueError, "differ from the reviewed file"):
                    lab.network_matrix(Path(folder), True)
            run.assert_not_called()


class FakeLab:
    """kube and probe for validate(): every shipping check gives its expected result."""

    def __init__(self):
        self.gateway_up, self.selectors, self.probes = True, [], []

    def kube(self, *args, **kwargs):
        if args[:3] == ("get", "pods", "-l"):
            self.selectors.append(args[3])
            if args[3].startswith("app=security-gateway"):
                items = [{"status": {"podIP": "10.0.0.9"}}] if self.gateway_up else []
            else:
                items = [{"status": {"podIP": "10.0.0.5"}}]
            return SimpleNamespace(stdout=json.dumps({"items": items}), returncode=0)
        if args[:2] == ("scale", "deployment/security-gateway"):
            self.gateway_up = args[2] == "--replicas=1"
        return SimpleNamespace(stdout="", returncode=0)

    def probe(self, directory, **kwargs):
        self.probes.append(kwargs)
        # Only the authenticated valid call of each route through a running gateway succeeds.
        return {"succeeded": set(kwargs) <= {"route"} and self.gateway_up, "output": ""}


class SecurityLabValidationTests(unittest.TestCase):
    def validate(self, matrix_status, *, probe=None):
        from scripts.security import boutique_security as lab
        fake = FakeLab()
        storefront = SimpleNamespace(journey=lambda: {"passed": True}, _request=lambda opener, path: (200, "", None))
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(lab.provenance, "collect", return_value={"captured_by": "test"}), \
                patch.object(lab, "kube", side_effect=fake.kube), \
                patch.object(lab, "probe", side_effect=probe or fake.probe), \
                patch.object(lab, "collect_inspections"), patch.object(lab, "emit_observation"), \
                patch.object(lab, "network_matrix", return_value={"verdict": {"status": matrix_status}}) as matrix:
            try:
                error = None
                lab.validate(Path(folder), storefront)
            except RuntimeError as raised:
                error = raised
            saved = json.loads((Path(folder) / "validation.json").read_text(encoding="utf-8"))
        matrix.assert_called_once()
        self.assertIs(matrix.call_args.args[1], True)
        return fake, saved, error

    def test_every_check_and_a_matrix_pass_gives_a_passing_validation(self):
        fake, saved, error = self.validate("PASS")
        self.assertIsNone(error)
        self.assertIs(saved["passed"], True)
        self.assertEqual(saved["network_matrix"], {"status": "PASS"})
        self.assertEqual(len(fake.probes), 9 + 9)
        cart = [probe for probe in fake.probes if probe.get("route") == "cart"]
        self.assertEqual(len(cart), 9)
        self.assertIn({"route": "cart", "quantity": 11}, cart)
        self.assertIn({"route": "cart", "target": "cartservice:7070", "secure": False}, cart)
        self.assertIn({"route": "cart", "target": "10.0.0.5:7070", "secure": False}, cart)
        self.assertIn({"route": "cart", "target": "10.0.0.5:50053", "secure": False}, cart)
        self.assertTrue(saved["valid_mtls_cart"]["succeeded"])
        self.assertFalse(saved["cart_proxy_outage"]["succeeded"])

    def test_a_cart_bypass_that_succeeds_fails_the_validation(self):
        fake = FakeLab()

        def probe(directory, **kwargs):
            result = fake.probe(directory, **kwargs)
            if kwargs == {"route": "cart", "target": "cartservice:7070", "secure": False}:
                return {"succeeded": True, "output": ""}  # plaintext cart reachable: the route is not protected
            return result
        fake_lab, saved, error = self.validate("PASS", probe=probe)
        self.assertIsInstance(error, RuntimeError)
        self.assertIs(saved["passed"], False)
        self.assertTrue(saved["cart_direct_service_bypass"]["succeeded"])

    def test_a_cart_route_that_refuses_valid_calls_fails_the_validation(self):
        fake = FakeLab()

        def probe(directory, **kwargs):
            result = fake.probe(directory, **kwargs)
            return {"succeeded": False, "output": ""} if kwargs == {"route": "cart"} else result
        fake_lab, saved, error = self.validate("PASS", probe=probe)
        self.assertIsInstance(error, RuntimeError)
        self.assertIs(saved["passed"], False)
        self.assertTrue(saved["valid_mtls_quote"]["succeeded"])

    def test_each_cart_probe_sends_the_cart_schema_to_the_cart_gateway(self):
        from scripts.security import boutique_security as lab
        self.assertEqual(lab.probe_call("cart"), ("cart.proto", "hipstershop.CartService/GetCart", {"userId": lab.PROBE_USER}))
        self.assertEqual(lab.probe_call("cart", 11)[1:], ("hipstershop.CartService/AddItem", {
            "userId": lab.PROBE_USER, "item": {"productId": "OLJCESPC7Z", "quantity": 11}}))
        self.assertEqual(lab.probe_call("shipping", 101)[2], {"items": [{"productId": "OLJCESPC7Z", "quantity": 101}]})
        self.assertEqual(lab.probe_call("shipping")[2], {"items": [{"productId": "OLJCESPC7Z", "quantity": 1}]})
        created = []

        def kube(*args, **kwargs):
            if args[:2] == ("create", "-f"):
                created.append(json.loads(Path(args[2]).read_text(encoding="utf-8")))
            if args[:2] == ("get", "pod"):
                return SimpleNamespace(stdout=json.dumps({"status": {"phase": "Failed"}}), returncode=0)
            return SimpleNamespace(stdout="", returncode=0)
        with tempfile.TemporaryDirectory() as folder, patch.object(lab, "kube", side_effect=kube):
            self.assertEqual(lab.probe(Path(folder), route="cart", certificate=False), {"succeeded": False, "output": ""})
        args = created[0]["spec"]["containers"][0]["args"]
        self.assertEqual(args[args.index("-proto") + 1], "cart.proto")
        self.assertEqual(args[-2:], ["security-cart-gateway:8445", "hipstershop.CartService/GetCart"])
        self.assertNotIn("-cert", args)
        self.assertEqual(created[0]["metadata"]["labels"], {"app": "security-test"})

    def test_probe_action_runs_one_call_and_exits_by_its_result(self):
        from scripts.security import boutique_security as lab
        calls = []

        def probe(directory, **kwargs):
            calls.append(kwargs)
            return {"succeeded": kwargs["quantity"] is None, "output": ""}
        with tempfile.TemporaryDirectory() as folder, patch.object(lab, "probe", side_effect=probe), \
                patch.object(lab, "command") as command, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(lab.main(["probe", "--directory", folder, "--route", "cart"]), 0)
            self.assertEqual(lab.main(["probe", "--directory", folder, "--route", "cart", "--quantity", "11"]), 1)
        command.assert_not_called()  # no port-forward, no kind, no cluster change
        self.assertEqual(calls, [{"route": "cart", "quantity": None}, {"route": "cart", "quantity": 11}])
        self.assertEqual(json.loads(out.getvalue().splitlines()[1]), {"route": "cart", "quantity": 11, "succeeded": False})
        with redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            lab.main(["probe", "--route", "redis"])

    def test_a_matrix_that_is_not_pass_fails_the_validation_even_when_every_probe_passes(self):
        for status in ("UNKNOWN", "FAIL"):
            with self.subTest(status=status):
                fake, saved, error = self.validate(status)
                self.assertIsInstance(error, RuntimeError)
                self.assertIs(saved["passed"], False)
                self.assertEqual(saved["network_matrix"], {"status": status})
                # Every other check gave its expected result, so the matrix alone failed the run.
                self.assertTrue(saved["valid_mtls_quote"]["succeeded"])
                self.assertFalse(saved["proxy_outage"]["succeeded"])
                self.assertIs(saved["shopping_after_proxy_restore"]["passed"], True)
                self.assertEqual(saved["empty_cart"]["status"], 200)

    def test_a_crash_after_the_matrix_leaves_a_record_that_has_not_passed(self):
        def probe(directory, **kwargs):
            raise RuntimeError("probe Pod did not finish")
        fake, saved, error = self.validate("PASS", probe=probe)
        self.assertEqual(str(error), "probe Pod did not finish")
        self.assertIs(saved["passed"], False)
        self.assertEqual(saved["network_matrix"], {"status": "PASS"})

    def test_workload_pod_lookups_exclude_matrix_testers(self):
        fake, saved, error = self.validate("PASS")
        excluded = ",!" + network_policy.MATRIX_LABEL
        self.assertEqual(sorted(set(fake.selectors)), ["app=cartservice" + excluded, "app=frontend" + excluded,
                                                       "app=security-gateway" + excluded, "app=shippingservice" + excluded])


def dry_run_resources(extra_env=None):
    """kubectl create --dry-run output for the pinned upstream, with images; extra_env adds one caller env entry."""
    resources = []
    for item in upstream_manifest()["items"]:
        item = {"apiVersion": "apps/v1" if item["kind"] == "Deployment" else "v1", **item}
        if item["kind"] == "Deployment":
            for container in item["spec"]["template"]["spec"]["containers"]:
                container["image"] = "registry.example/" + item["metadata"]["name"] + ":v1"
            if extra_env and item["metadata"]["name"] == extra_env[0]:
                item["spec"]["template"]["spec"]["containers"][0]["env"].append(extra_env[1])
        resources.append(item)
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": resources})


class SecurityLabPrepareTests(unittest.TestCase):
    def prepare(self, resources, folder):
        from scripts.security import boutique_security as lab
        commands, applied = [], []

        def command(args, **kwargs):
            args = [str(arg) for arg in args]
            commands.append(args)
            if args[:3] == ["docker", "image", "inspect"]:
                repository = args[3].rsplit(":", 1)[0]
                return SimpleNamespace(stdout=json.dumps([{"Id": "sha256:" + "e" * 64,
                                                           "RepoDigests": [repository + "@sha256:" + "a" * 64]}]),
                                       returncode=0)
            return SimpleNamespace(stdout="", returncode=0)

        def kube(*args, **kwargs):
            if "--dry-run=client" in args:
                return SimpleNamespace(stdout=resources, returncode=0)
            if args[:2] == ("apply", "-f"):
                applied.append(Path(args[2]).name)
            return SimpleNamespace(stdout="", returncode=0)

        def certificates(pki, **kwargs):
            for identity in ("gateway", "upstream", "cart-upstream", "proxy-client", "client", "frontend-client",
                             "checkoutservice-client"):
                (pki / identity).mkdir()
                for name in ("ca.crt", "tls.crt", "tls.key"):
                    (pki / identity / name).write_bytes(b"test")
        directory = Path(folder) / "run"
        inspector = ("sentinel.local/security-inspector@sha256:" + "d" * 64, "sha256:" + "f" * 64)
        with patch.object(lab, "setup_cluster", return_value="node-1"), \
                patch.object(lab, "certificates", side_effect=certificates), \
                patch.object(lab, "command", side_effect=command), patch.object(lab, "kube", side_effect=kube), \
                patch.object(lab, "local_manifest", return_value=inspector):
            try:
                error = None
                with redirect_stdout(io.StringIO()):
                    lab.prepare(directory, None)
            except ValueError as raised:
                error = raised
        return directory, commands, applied, error

    def test_graph_drift_is_refused_before_any_image_pull(self):
        edge = ("adservice", {"name": "PRODUCT_CATALOG_SERVICE_ADDR", "value": "productcatalogservice:3550"})
        with tempfile.TemporaryDirectory() as folder:
            directory, commands, applied, error = self.prepare(dry_run_resources(edge), folder)
            self.assertFalse((directory / "manifest.json").exists())
        self.assertRegex(str(error), "differs from the reviewed graph")
        self.assertEqual([args for args in commands if "pull" in args], [])
        self.assertEqual(applied, [])

    def test_stale_reviewed_policy_file_is_refused_before_any_image_pull(self):
        with tempfile.TemporaryDirectory() as folder:
            stale = Path(folder) / "policies.json"
            stale.write_text(network_policy.SECURITY_POLICIES.read_text(encoding="utf-8").replace("6379", "6380"),
                             encoding="utf-8")
            with patch.object(network_policy, "SECURITY_POLICIES", stale):
                directory, commands, applied, error = self.prepare(dry_run_resources(), folder)
        self.assertRegex(str(error), "differ from the reviewed file")
        self.assertEqual([args for args in commands if "pull" in args], [])
        self.assertEqual(applied, [])

    def test_the_applied_manifest_carries_exactly_the_reviewed_policies(self):
        with tempfile.TemporaryDirectory() as folder:
            directory, commands, applied, error = self.prepare(dry_run_resources(), folder)
            self.assertIsNone(error)
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            listed = json.loads((directory / "network-policies.json").read_text(encoding="utf-8"))
            proto = json.loads((directory / "proto.json").read_text(encoding="utf-8"))
        self.assertEqual(set(proto["data"]), {"cart.proto", "shipping.proto"})
        self.assertIn("service CartService", proto["data"]["cart.proto"])
        # The cart sidecar's identity Secret is applied like the others; it never enters manifest.json.
        self.assertIn("cart-upstream.json", applied)
        self.assertNotIn("Secret", {item["kind"] for item in manifest["items"]})
        cart = next(item for item in manifest["items"] if item["kind"] == "Deployment"
                    and item["metadata"]["name"] == "cartservice")["spec"]["template"]["spec"]
        self.assertEqual([container["name"] for container in cart["containers"]][-1], "cart-ingress")
        committed = json.loads(network_policy.SECURITY_POLICIES.read_text(encoding="utf-8"))["items"]
        self.assertEqual(listed["items"], committed)
        self.assertEqual([item for item in manifest["items"] if item["kind"] == "NetworkPolicy"], lab_items())
        self.assertEqual(applied[0], "manifest.json")
        self.assertIn(["docker", "exec", "node-1", "crictl", "pull", network_policy.MATRIX_IMAGE], commands)


if __name__ == "__main__":
    unittest.main()
