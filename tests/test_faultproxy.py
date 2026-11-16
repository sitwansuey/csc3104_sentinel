"""Every fault proxy mode against a loopback echo server, plain and TLS."""
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest

from experiments.distributed.faultproxy import FaultProxy, ProxyGroup
from experiments.distributed.topology import find_openssl, make_certificates


class EchoServer:
    """Reads one request chunk, records it, echoes it back and closes."""

    def __init__(self, tls_context=None):
        self.tls_context = tls_context
        self.received = []
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(0.2)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def address(self):
        return self.listener.getsockname()[:2]

    def _serve(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except OSError:
                continue
            threading.Thread(target=self._echo, args=(connection,), daemon=True).start()

    def _echo(self, connection):
        try:
            connection.settimeout(5)
            if self.tls_context is not None:
                connection = self.tls_context.wrap_socket(connection, server_side=True)
            data = connection.recv(65536)
            if data:
                self.received.append(data)
                connection.sendall(data)
        except OSError:
            pass
        finally:
            connection.close()

    def close(self):
        self.stopping.set()
        self.thread.join(2)
        self.listener.close()


class FaultProxyTests(unittest.TestCase):
    def setUp(self):
        self.server = EchoServer()
        self.addCleanup(self.server.close)
        self.proxy = FaultProxy(self.server.address).start()
        self.addCleanup(self.proxy.close)

    def exchange(self, payload=b"ping", timeout=2):
        with socket.create_connection(self.proxy.address, timeout=timeout) as client:
            client.sendall(payload)
            return client.recv(65536)

    def wait_received(self, count=1):
        deadline = time.monotonic() + 5
        while len(self.server.received) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.server.received

    def test_pass_forwards_both_ways(self):
        self.assertEqual(self.exchange(b"hello"), b"hello")
        self.assertEqual(self.wait_received(), [b"hello"])

    def test_blackhole_never_forwards_so_the_client_times_out(self):
        self.proxy.set_mode("blackhole")
        with self.assertRaises((TimeoutError, socket.timeout)):
            self.exchange(timeout=0.5)
        time.sleep(0.2)
        self.assertEqual(self.server.received, [])

    def test_reset_closes_with_a_reset_before_forwarding(self):
        self.proxy.set_mode("reset")
        with socket.create_connection(self.proxy.address, timeout=2) as client:
            time.sleep(0.2)
            with self.assertRaises((ConnectionResetError, ConnectionAbortedError)):
                client.sendall(b"ping")
                client.recv(65536)
        self.assertEqual(self.server.received, [])

    def test_delay_adds_at_least_the_configured_latency(self):
        self.proxy.set_mode("delay", delay_ms=300)
        started = time.monotonic()
        self.assertEqual(self.exchange(), b"ping")
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_drop_response_delivers_the_request_and_the_client_sees_eof(self):
        self.proxy.set_mode("drop-response")
        self.assertEqual(self.exchange(b"commit-me"), b"")
        self.assertEqual(self.wait_received(), [b"commit-me"])

    def test_modes_switch_back_to_pass_for_new_connections(self):
        self.proxy.set_mode("drop-response")
        self.assertEqual(self.exchange(), b"")
        self.proxy.set_mode("pass")
        self.assertEqual(self.exchange(b"again"), b"again")
        self.assertEqual(self.proxy.counts["drop-response"], 1)

    def test_invalid_mode_delay_and_bind_are_refused(self):
        for mode, options in (("partition", {}), ("delay", {}), ("delay", {"delay_ms": 60001})):
            with self.subTest(mode=mode, options=options), self.assertRaises(ValueError):
                self.proxy.set_mode(mode, **options)
        with self.assertRaises(ValueError):
            FaultProxy(self.server.address, bind="0.0.0.0")

    def test_a_group_switches_every_route_together(self):
        other = FaultProxy(self.server.address).start()
        self.addCleanup(other.close)
        group = ProxyGroup({"delivery": self.proxy, "approvals": other})
        group.set_mode("blackhole")
        self.assertEqual({proxy.mode for proxy in group.proxies.values()}, {"blackhole"})
        self.assertIs(group["approvals"], other)


@unittest.skipUnless(find_openssl(), "openssl is not installed")
class FaultProxyTLSTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.certificates = make_certificates(Path(directory.name))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(self.certificates["cert"], self.certificates["key"])
        self.server = EchoServer(server_context)
        self.addCleanup(self.server.close)
        self.proxy = FaultProxy(self.server.address, tls=True).start()
        self.addCleanup(self.proxy.close)
        self.client_context = ssl.create_default_context(cafile=str(self.certificates["ca"]))

    def exchange(self, payload):
        with socket.create_connection(self.proxy.address, timeout=3) as raw:
            with self.client_context.wrap_socket(raw, server_hostname="127.0.0.1") as client:
                client.sendall(payload)
                return client.recv(65536)

    def test_the_throwaway_ca_verifies_the_server_through_the_proxy(self):
        self.assertEqual(self.exchange(b"over-tls"), b"over-tls")
        self.assertFalse((Path(self.certificates["ca"]).parent / "ca.key").exists())

    def test_drop_response_completes_the_handshake_then_drops_the_reply(self):
        self.proxy.set_mode("drop-response")
        try:
            reply = self.exchange(b"commit-me")
        except (ssl.SSLError, ConnectionError):
            reply = b""
        self.assertEqual(reply, b"")
        deadline = time.monotonic() + 5
        while not self.server.received and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.server.received, [b"commit-me"])

    def test_without_tls_awareness_drop_response_would_break_the_handshake(self):
        blind = FaultProxy(self.server.address, tls=False).start()
        self.addCleanup(blind.close)
        blind.set_mode("drop-response")
        self.proxy = blind
        with self.assertRaises((ssl.SSLError, ConnectionError, TimeoutError, socket.timeout)):
            self.exchange(b"never-arrives")
        time.sleep(0.2)
        self.assertEqual(self.server.received, [])


if __name__ == "__main__":
    unittest.main()
