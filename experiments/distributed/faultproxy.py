"""A TCP fault proxy between one worker and the Coordinator. No admin rights needed.

Modes apply to each new connection, from the moment it is accepted:

- pass: forward both ways.
- blackhole: accept and read, never forward or answer, so the client times out.
- reset: accept, then close at once with a TCP reset.
- delay: wait delay_ms before connecting upstream, then forward both ways.
- drop-response: forward the request, discard the reply, then close (EOF). The
  server sees and may commit the request; the client sees a lost response.

The proxy works at the TCP level, so TLS passes through it unchanged. With
tls=True, drop-response first lets the handshake complete (it forwards server
bytes until the client has answered them once), then drops everything else
from the server, so the encrypted request still reaches the server.
"""
import selectors
import socket
import struct
import threading
import time

MODES = frozenset({"pass", "blackhole", "reset", "delay", "drop-response"})
CHUNK = 65536


class FaultProxy:
    def __init__(self, upstream, *, bind="127.0.0.1", port=0, tls=False):
        if bind not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("The fault proxy listens on loopback only")
        self.upstream, self.tls = upstream, tls
        self.mode, self.delay_ms = "pass", 0
        self.counts = {mode: 0 for mode in MODES}
        self._listener = socket.create_server((bind, port))
        self._listener.settimeout(0.2)
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._sockets = set()
        self._thread = threading.Thread(target=self._accept, daemon=True)

    @property
    def address(self):
        return self._listener.getsockname()[:2]

    @property
    def port(self):
        return self.address[1]

    def start(self):
        self._thread.start()
        return self

    def set_mode(self, mode, *, delay_ms=0):
        if mode not in MODES:
            raise ValueError("Unknown fault proxy mode")
        if mode == "delay" and not 0 < delay_ms <= 60000:
            raise ValueError("Delay must be from 1 to 60000 ms")
        with self._lock:
            self.mode, self.delay_ms = mode, delay_ms

    def close(self):
        self._closing.set()
        if self._thread.is_alive():
            self._thread.join(2)
        self._listener.close()
        with self._lock:
            sockets = list(self._sockets)
        for sock in sockets:
            _close(sock)

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()

    def _track(self, sock):
        with self._lock:
            self._sockets.add(sock)
        return sock

    def _accept(self):
        while not self._closing.is_set():
            try:
                client, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                mode, delay = self.mode, self.delay_ms
                self.counts[mode] += 1
            self._track(client)
            threading.Thread(target=self._handle, args=(client, mode, delay), daemon=True).start()

    def _handle(self, client, mode, delay_ms):
        upstream = None
        try:
            if mode == "reset":
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                return
            if mode == "blackhole":
                client.settimeout(0.2)
                while not self._closing.is_set():
                    try:
                        if not client.recv(CHUNK):
                            return
                    except TimeoutError:
                        continue
                return
            if mode == "delay":
                deadline = time.monotonic() + delay_ms / 1000
                while time.monotonic() < deadline and not self._closing.is_set():
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            upstream = self._track(socket.create_connection(self.upstream, timeout=5))
            self._pump(client, upstream, drop_response=mode == "drop-response")
        except OSError:
            return
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    _close(sock)
                    with self._lock:
                        self._sockets.discard(sock)

    def _pump(self, client, upstream, *, drop_response):
        """Copy bytes both ways until either side closes."""
        # Plain HTTP: every server byte is the reply. TLS: the reply starts after
        # the client has answered the server's handshake flight once.
        server_spoke = client_answered = False
        selector = selectors.DefaultSelector()
        selector.register(client, selectors.EVENT_READ, upstream)
        selector.register(upstream, selectors.EVENT_READ, client)
        try:
            while not self._closing.is_set():
                for key, _ in selector.select(timeout=0.2):
                    source, destination = key.fileobj, key.data
                    data = source.recv(CHUNK)
                    if not data:
                        return
                    if source is client:
                        client_answered = client_answered or server_spoke
                        destination.sendall(data)
                        continue
                    server_spoke = True
                    if drop_response and (not self.tls or client_answered):
                        continue  # Discard the reply; the client will see EOF.
                    destination.sendall(data)
        finally:
            selector.close()


def _close(sock):
    try:
        sock.close()
    except OSError:
        return


class ProxyGroup:
    """The proxies of one worker (delivery and approvals), switched together."""

    def __init__(self, proxies):
        self.proxies = dict(proxies)

    def set_mode(self, mode, **options):
        for proxy in self.proxies.values():
            proxy.set_mode(mode, **options)

    def close(self):
        for proxy in self.proxies.values():
            proxy.close()

    def __getitem__(self, name):
        return self.proxies[name]
