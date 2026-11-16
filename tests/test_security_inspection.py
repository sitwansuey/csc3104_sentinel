from contextlib import closing
from http.client import HTTPConnection
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from runtime.dashboard import snapshot
from runtime.security_inspector import make_server
from runtime.security_store import record
from scripts.security.security_lab import envoy_config
from services.grpc_inspection import MAX_BODY, METHODS, ROUTE_METHODS, inspect_request


METHOD = "/hipstershop.ShippingService/GetQuote"


def varint(value):
    output = bytearray()
    while value > 127:
        output.append((value & 127) | 128)
        value >>= 7
    return bytes(output + bytes([value]))


def field(number, value):
    return varint(number << 3 | 2) + varint(len(value)) + value


def request(quantity=1, product=b"OLJCESPC7Z", address=b""):
    item = field(1, product) + b"\x10" + varint(quantity)
    return (field(1, address) if address else b"") + field(2, item)


def frame(data):
    return b"\x00" + len(data).to_bytes(4, "big") + data


ADD, GET_CART, EMPTY_CART = ("/hipstershop.CartService/" + name for name in ("AddItem", "GetCart", "EmptyCart"))
USER = b"6f1c2a4e-9b7d-4c3e-8a2f-0d5e6b7c8a9f"  # a storefront session id (UUID)


def cart_user(user=USER):
    return field(1, user)


def cart_add(quantity=1, product=b"OLJCESPC7Z", user=USER):
    return cart_user(user) + field(2, field(1, product) + b"\x10" + varint(quantity))


class InspectionTests(unittest.TestCase):
    def inspect(self, data=None, **kwargs):
        return inspect_request(kwargs.pop("path", METHOD), kwargs.pop("content_type", "application/grpc"),
                               kwargs.pop("encoding", ""), frame(request()) if data is None else data, **kwargs)

    def test_real_schema_allows_good_request_without_disclosing_personal_data(self):
        result = self.inspect(frame(request(address=field(1, b"PRIVATE-STREET-ADDRESS"))))
        self.assertEqual(result["verdict"], "ALLOW")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("OLJCESPC7Z", json.dumps(result))

    def test_quantity_product_and_item_limits_are_enforced(self):
        for payload, rule in ((request(0), "QUANTITY"), (request(101), "QUANTITY"),
                              (request(2**64-1), "QUANTITY"), (request(product=b"bad\n"), "TEXT_CONSTRAINT"),
                              (request(product=b"<script>"), "PRODUCT_ID"),
                              (request() * 21, "ITEM_COUNT")):
            with self.subTest(rule=rule):
                self.assertEqual(self.inspect(frame(payload))["rule"], rule)

    def test_empty_cart_quote_is_a_legitimate_upstream_request(self):
        self.assertEqual(self.inspect(frame(b""))["verdict"], "ALLOW")
        self.assertEqual(self.inspect(frame(field(1,field(1,b"Street"))),
                         path="/hipstershop.ShippingService/ShipOrder")["rule"], "ITEM_COUNT")

    def test_compressed_partial_oversized_multiframe_and_truncated_bodies_denied(self):
        for data, options in ((b"\x01" + frame(request())[1:], {}), (frame(request()), {"encoding": "gzip"}),
                              (frame(request()), {"partial": True}), (b"x" * (MAX_BODY+1), {}),
                              (frame(request()) * 2, {}), (frame(request())[:-1], {}), (b"", {})):
            with self.subTest(options=options):
                self.assertEqual(self.inspect(data, **options)["verdict"], "DENY")

    def test_parser_ambiguity_unknown_fields_wrong_wire_and_bad_varints_denied(self):
        for data in (request() + b"\x18\x01", request()+b"\x00", b"\x0a\xff\xff",
                     b"\x0a\x02\x0a\x00\x0a\x00", b"\x0a"+b"\xff"*10,
                     field(2, field(1,b"OK")+b"\x10\x01\x10\x02"),
                     field(2, field(1,b"\xff")+b"\x10\x01")):
            with self.subTest(data=data):
                self.assertEqual(self.inspect(frame(data))["verdict"], "DENY")

    def test_unknown_methods_and_content_types_are_not_passed_through(self):
        for options in ({"path": "/grpc.reflection.v1.ServerReflection/ServerReflectionInfo"},
                        {"path": METHOD + "?bypass=1"}, {"content_type": "application/grpc-web"}):
            self.assertEqual(self.inspect(**options)["verdict"], "DENY")

    def test_ship_order_requires_an_address_without_recording_it(self):
        path = "/hipstershop.ShippingService/ShipOrder"
        self.assertEqual(self.inspect(path=path)["rule"], "ADDRESS_REQUIRED")
        self.assertEqual(self.inspect(frame(request(address=field(1,b"PRIVATE"))), path=path)["verdict"], "ALLOW")

    def test_valid_cart_calls_pass_with_only_projected_metadata(self):
        for path, data, items in ((ADD, cart_add(), 1), (ADD, cart_add(10), 1), (GET_CART, cart_user(), None),
                                  (EMPTY_CART, cart_user(), None),
                                  # Field order is free in protobuf; item before user_id is the same request.
                                  (ADD, field(2, field(1, b"OLJCESPC7Z") + b"\x10\x01") + cart_user(), 1)):
            with self.subTest(path=path, data=data):
                result = self.inspect(frame(data), path=path, route="cart")
                self.assertEqual((result["verdict"], result["rule"], result["method"]), ("ALLOW", "CART_SCHEMA", path))
                self.assertEqual(result.get("items"), items)
                self.assertEqual(set(result), {"method", "bytes", "verdict", "rule"} | ({"items"} if items else set()))
                self.assertNotIn(USER.decode(), json.dumps(result))
                self.assertNotIn("OLJCESPC7Z", json.dumps(result))

    def test_cart_quantity_user_id_and_item_constraints_are_enforced(self):
        for path, data, rule in ((ADD, cart_add(0), "QUANTITY"), (ADD, cart_add(11), "QUANTITY"),
                                 (ADD, cart_add(2**64 - 1), "QUANTITY"),
                                 (ADD, cart_user() + field(2, field(1, b"OLJCESPC7Z")), "QUANTITY"),
                                 (ADD, cart_add(product=b"<script>"), "PRODUCT_ID"),
                                 (ADD, cart_user() + field(2, b"\x10\x01"), "PRODUCT_ID"),
                                 (ADD, cart_user(), "ITEM_REQUIRED"),
                                 (ADD, cart_add(user=b"user id"), "USER_ID"),
                                 (ADD, cart_add(user=b"a" * 65), "TEXT_CONSTRAINT"),
                                 (GET_CART, b"", "USER_ID"), (GET_CART, cart_user(b""), "USER_ID"),
                                 (GET_CART, cart_user(b"../../etc"), "USER_ID"),
                                 (EMPTY_CART, cart_user(b"a" * 65), "TEXT_CONSTRAINT"),
                                 (EMPTY_CART, cart_user(b"bad\n"), "TEXT_CONSTRAINT"),
                                 (GET_CART, cart_user(b"\xff"), "PROTOBUF_UTF8")):
            with self.subTest(path=path, rule=rule, data=data):
                result = self.inspect(frame(data), path=path, route="cart")
                self.assertEqual((result["verdict"], result["rule"]), ("DENY", rule))
                self.assertNotIn("items", result)

    def test_cart_unknown_duplicate_and_truncated_fields_are_denied_without_echo(self):
        secret = b"SECRET-VALUE-42"
        for path, data, rule in ((GET_CART, cart_user() + field(2, secret), "PROTOBUF_SCHEMA"),
                                 (GET_CART, cart_user() + cart_user(secret), "PROTOBUF_DUPLICATE"),
                                 (ADD, cart_add() + cart_user(secret), "PROTOBUF_DUPLICATE"),
                                 (ADD, cart_user() + field(2, field(1, b"OLJCESPC7Z") + b"\x10\x01\x10\x02"),
                                  "PROTOBUF_DUPLICATE"),
                                 (ADD, cart_user() + field(2, field(1, b"OLJCESPC7Z") + b"\x10\x01" + field(3, secret)),
                                  "PROTOBUF_SCHEMA"),
                                 (ADD, cart_user() + cart_add()[len(cart_user()):] * 2, "PROTOBUF_DUPLICATE"),
                                 (GET_CART, b"\x08\x01", "PROTOBUF_SCHEMA"),
                                 (GET_CART, cart_user()[:-1], "PROTOBUF_TRUNCATED"),
                                 (ADD, cart_user() + field(2, field(1, b"OLJCESPC7Z") + b"\x10\xff"), "PROTOBUF_TRUNCATED"),
                                 (ADD, cart_user() + b"\x12\xff\xff", "PROTOBUF_TRUNCATED"),
                                 (GET_CART, b"\x0a" + b"\xff" * 10, "PROTOBUF_VARINT")):
            with self.subTest(rule=rule, data=data):
                result = self.inspect(frame(data), path=path, route="cart")
                self.assertEqual((result["verdict"], result["rule"]), ("DENY", rule))
                self.assertNotIn(secret.decode(), json.dumps(result))
        self.assertEqual(self.inspect(frame(cart_user())[:-1], path=GET_CART, route="cart")["rule"], "UNARY_FRAME_LENGTH")

    def test_unknown_cart_methods_and_cross_route_calls_are_denied(self):
        for path, route, rule in (("/hipstershop.CartService/DeleteCart", "cart", "METHOD_NOT_ALLOWED"),
                                  ("/hipstershop.CartService/GetCart/", "cart", "METHOD_NOT_ALLOWED"),
                                  (GET_CART, "shipping", "ROUTE_MISMATCH"), (METHOD, "cart", "ROUTE_MISMATCH"),
                                  (GET_CART, "redis", "ROUTE_MISMATCH")):
            with self.subTest(path=path, route=route):
                result = self.inspect(frame(cart_user()), path=path, route=route)
                self.assertEqual((result["verdict"], result["rule"]), ("DENY", rule))
        self.assertEqual(self.inspect(frame(cart_user()), path="/hipstershop.CartService/DeleteCart")["method"], "unrecognized")
        # Without a route (direct callers of inspect_request), any reviewed method keeps its schema check.
        self.assertEqual(self.inspect(frame(cart_user()), path=GET_CART)["verdict"], "ALLOW")
        self.assertEqual(METHODS, ROUTE_METHODS["shipping"] | ROUTE_METHODS["cart"])
        self.assertFalse(ROUTE_METHODS["shipping"] & ROUTE_METHODS["cart"])

    def test_cart_gateway_routes_only_cart_methods_to_cart_and_never_fails_open(self):
        for route in ("shipping", "cart"):
            config = envoy_config(route=route)["static_resources"]
            chain = config["listeners"][0]["filter_chains"][0]
            manager = chain["filters"][0]["typed_config"]
            routes = manager["route_config"]["virtual_hosts"][0]["routes"]
            with self.subTest(route=route):
                self.assertTrue(chain["transport_socket"]["typed_config"]["require_client_certificate"])
                self.assertEqual({entry["match"]["path"] for entry in routes[:-1]}, set(ROUTE_METHODS[route]))
                self.assertEqual({entry["route"]["cluster"] for entry in routes[:-1]}, {route})
                self.assertEqual(routes[-1], {"name": "denied", "match": {"prefix": "/"}, "direct_response": {"status": 403}})
                authz = manager["http_filters"][0]["typed_config"]
                self.assertFalse(authz["failure_mode_allow"])
                self.assertFalse(authz["with_request_body"]["allow_partial_message"])
                self.assertIn({"key": "x-sentinel-route", "value": route},
                              authz["http_service"]["authorization_request"]["headers_to_add"])
                # Only grpc-encoding is copied from the client, so a client cannot choose its route.
                self.assertEqual(authz["allowed_headers"], {"patterns": [{"exact": "grpc-encoding"}]})
                inspector = next(cluster for cluster in config["clusters"] if cluster["name"] == "inspector")
                self.assertEqual(inspector["circuit_breakers"]["thresholds"][0]["max_requests"], 32)
        cart = envoy_config(route="cart")["static_resources"]
        self.assertEqual(cart["listeners"][0]["address"]["socket_address"]["port_value"], 8445)
        self.assertEqual(cart["clusters"][0]["transport_socket"]["typed_config"]["sni"], "cart-upstream.internal")
        upstream = envoy_config(upstream=True, route="cart")["static_resources"]
        self.assertEqual(upstream["listeners"][0]["address"]["socket_address"]["port_value"], 8446)
        self.assertEqual(upstream["listeners"][0]["filter_chains"][0]["transport_socket"]["typed_config"]
                         ["common_tls_context"]["tls_certificate_sds_secret_configs"][0]["name"],
                         "cart-upstream-cert")
        self.assertEqual(len(upstream["clusters"]), 1)  # no inspector on the ingress sidecar

    def test_tls_and_authz_configuration_does_not_allow_partial_or_fail_open(self):
        config = envoy_config()["static_resources"]
        chain = config["listeners"][0]["filter_chains"][0]
        self.assertTrue(chain["transport_socket"]["typed_config"]["require_client_certificate"])
        manager = chain["filters"][0]["typed_config"]
        authz = manager["http_filters"][0]["typed_config"]
        self.assertFalse(authz["failure_mode_allow"])
        self.assertFalse(authz["with_request_body"]["allow_partial_message"])
        self.assertIn({"key": "content-type", "value": "application/octet-stream"},
                      authz["http_service"]["authorization_request"]["headers_to_add"])
        self.assertTrue(manager["route_config"]["virtual_hosts"][0]["routes"][0]["match"]["headers"])
        self.assertNotIn("admin", envoy_config())
        transport = config["clusters"][0]["transport_socket"]["typed_config"]
        self.assertEqual(transport["sni"], "upstream.internal")
        self.assertTrue(transport["common_tls_context"]["combined_validation_context"]["default_validation_context"]
                        ["match_typed_subject_alt_names"])


class InspectorHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "events.sqlite3"
        self.server = make_server(self.path, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def post(self, data, headers=None, path=METHOD):
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)) as client:
            client.request("POST", path, body=data, headers={"Content-Type": "application/octet-stream",
                "x-sentinel-body-format": "grpc", "x-sentinel-route": "cart" if "CartService" in path else "shipping",
                **(headers or {})})
            response = client.getresponse()
            response.read()
            return response.status

    def test_cart_calls_are_inspected_over_http_and_bound_to_their_route(self):
        self.assertEqual(self.post(frame(cart_add()), path=ADD), 200)
        self.assertEqual(self.post(frame(cart_add(quantity=11)), path=ADD), 403)
        # A cart call sent on the shipping route (and the reverse) is denied and recorded.
        self.assertEqual(self.post(frame(cart_user()), {"x-sentinel-route": "shipping"}, path=GET_CART), 403)
        self.assertEqual(self.post(frame(request()), {"x-sentinel-route": "cart"}), 403)
        with closing(sqlite3.connect(self.path)) as db:
            rows = [json.loads(row[0]) for row in db.execute("SELECT payload FROM security_events")]
        self.assertEqual([(row["verdict"], row["rule"]) for row in rows], [
            ("ALLOW", "CART_SCHEMA"), ("DENY", "QUANTITY"), ("DENY", "ROUTE_MISMATCH"), ("DENY", "ROUTE_MISMATCH")])
        self.assertNotIn(USER.decode(), json.dumps(rows))

    def test_a_missing_unknown_or_repeated_route_is_refused_before_inspection(self):
        for headers in ({"x-sentinel-route": ""}, {"x-sentinel-route": "redis"}, {"x-sentinel-route": "Cart"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.post(frame(request()), headers), 400)
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)) as client:
            client.putrequest("POST", METHOD)
            body = frame(request())
            for key, value in (("Content-Type", "application/octet-stream"), ("x-sentinel-body-format", "grpc"),
                               ("x-sentinel-route", "shipping"), ("x-sentinel-route", "cart"),
                               ("Content-Length", str(len(body)))):
                client.putheader(key, value)
            client.endheaders(body)
            response = client.getresponse()
            response.read()
            self.assertEqual(response.status, 400)
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)) as client:
            client.request("POST", METHOD, body=frame(request()), headers={
                "Content-Type": "application/octet-stream", "x-sentinel-body-format": "grpc"})
            response = client.getresponse()
            response.read()
            self.assertEqual(response.status, 400)
        self.assertFalse(self.path.exists())  # Nothing reached inspection or the journal.

    def test_get_paths_other_than_readyz_are_refused(self):
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)) as client:
            client.request("GET", "/readyz/../healthz")
            response = client.getresponse()
            response.read()
            self.assertEqual(response.status, 405)

    def test_real_http_enforces_payload_and_persists_only_projected_metadata(self):
        self.assertEqual(self.post(frame(request(address=field(1, b"PRIVATE-STREET")))), 200)
        self.assertEqual(self.post(frame(request(101))), 403)
        with closing(sqlite3.connect(self.path)) as db:
            rows = list(db.execute("SELECT payload FROM security_events"))
        self.assertEqual(len(rows), 2)
        self.assertNotIn("PRIVATE-STREET", str(rows))
        self.assertNotIn("OLJCESPC7Z", str(rows))

    def test_audit_failure_never_allows_forwarding(self):
        with patch("runtime.security_inspector.record", side_effect=OSError("disk full")):
            self.assertEqual(self.post(frame(request())), 503)

    def test_transfer_encoding_and_partial_body_are_refused(self):
        self.assertEqual(self.post(b"", {"Transfer-Encoding": "chunked"}), 400)
        self.assertEqual(self.post(frame(request()), {"x-envoy-auth-partial-body": "true"}), 403)

    def test_duplicate_lengths_and_oversized_body_are_refused(self):
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)) as client:
            client.putrequest("POST", METHOD)
            client.putheader("Content-Length", "0")
            client.putheader("Content-Length", "0")
            client.endheaders()
            response = client.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        self.assertEqual(self.post(b"", {"Content-Length": str(MAX_BODY+1)}), 400)

    def test_security_evidence_is_readable_by_dashboard_without_other_producers(self):
        record(self.path, "inspection", {"method": METHOD, "bytes": 10, "verdict": "DENY", "rule": "QUANTITY"})
        result = snapshot(Path(self.directory.name) / "missing.sqlite3", self.path)
        self.assertEqual(result["security_events"][0]["rule"], "QUANTITY")
        self.assertFalse(result["sources"]["coordinator"]["connected"])


if __name__ == "__main__":
    unittest.main()
