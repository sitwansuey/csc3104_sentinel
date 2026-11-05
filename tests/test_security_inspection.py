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
from scripts.security_lab import envoy_config
from services.grpc_inspection import MAX_BODY, inspect_request


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
        self.assertTrue(transport["common_tls_context"]["validation_context"]["match_typed_subject_alt_names"])


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

    def post(self, data, headers=None):
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)) as client:
            client.request("POST", METHOD, body=data, headers={"Content-Type": "application/octet-stream",
                "x-sentinel-body-format": "grpc", **(headers or {})})
            response = client.getresponse()
            response.read()
            return response.status

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
