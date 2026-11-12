"""Serve worker delivery and authenticated approvals from one Coordinator process."""
import argparse
from contextlib import ExitStack, contextmanager
import ipaddress
import math
from pathlib import Path
import signal
import ssl
import threading

from runtime.approval_http import handler as approval_handler
from runtime.service_identity import load_identities
from runtime.worker_http import WorkerHTTPServer, handler as worker_handler, identities, serve_until_stopped
from runtime.worker_transport import Delivery
from services.coordinator.approvals import ROLES, load_permissions
from services.coordinator.store import SQLiteStore
from shared import log

LOG = log.get_logger("coordinator_server")


@contextmanager
def servers(database, services, approval_services, permissions, *, bind="127.0.0.1",
            port=8766, approval_port=8767, tls_context=None, request_timeout=15,
            lease_seconds=30, retention_seconds=86400, max_lifetime_seconds=300, clock=None,
            max_clock_skew=None):
    stopping = threading.Event()
    with SQLiteStore(database, clock=clock) as store:
        Delivery(store, lease_seconds=lease_seconds, retention_seconds=retention_seconds, dependency_requests=True,
                 max_clock_skew=max_clock_skew)
    with ExitStack() as stack:
        workers = stack.enter_context(WorkerHTTPServer((bind, port), worker_handler(
            database, services, lease_seconds, retention_seconds, request_timeout, stopping, clock, True,
            max_clock_skew),
            tls_context=tls_context, request_timeout=request_timeout))
        approvals = stack.enter_context(WorkerHTTPServer((bind, approval_port), approval_handler(
            database, approval_services, permissions, clock=clock, max_lifetime_seconds=max_lifetime_seconds,
            stopping=stopping, request_timeout=request_timeout), tls_context=tls_context,
            request_timeout=request_timeout))
        threads = [threading.Thread(target=serve_until_stopped, args=(server, stopping))
                   for server in (workers, approvals)]
        for thread in threads:
            thread.start()
        log.emit(LOG, "coordinator.serving", port=workers.server_port)
        log.emit(LOG, "coordinator.approvals.serving", port=approvals.server_port)
        try:
            yield workers, approvals, stopping, threads
        finally:
            stopping.set()
            for thread in threads:
                thread.join(request_timeout + 1)
            log.emit(LOG, "coordinator.stopped")
            # ExitStack closes and drains both sets of bounded HTTP handlers.


@log.entrypoint("coordinator")
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--identities", required=True)
    parser.add_argument("--approval-identities", required=True)
    parser.add_argument("--approval-permissions", required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--approval-port", type=int, default=8767)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--lease-seconds", type=int, default=30)
    parser.add_argument("--retention-seconds", type=int, default=86400)
    parser.add_argument("--request-timeout", type=float, default=15)
    parser.add_argument("--max-lifetime-seconds", type=int, default=300)
    parser.add_argument("--max-clock-skew-seconds", type=float,
                        help="Refuse claims from a worker whose reported clock offset plus uncertainty exceeds this")
    args = parser.parse_args(argv)
    if (bool(args.cert) != bool(args.key) or not args.cert and not ipaddress.ip_address(args.bind).is_loopback):
        parser.error("Non-loopback serving requires --cert and --key")
    if (args.lease_seconds < 1 or args.retention_seconds < 1 or not 1 <= args.max_lifetime_seconds <= 86400
            or not math.isfinite(args.request_timeout) or args.request_timeout <= 0):
        parser.error("Lease, retention, request timeout and approval lifetime must be positive and bounded")
    if args.max_clock_skew_seconds is not None and not 0 < args.max_clock_skew_seconds <= 3600:
        parser.error("The clock skew bound must be positive and at most one hour")
    services = identities(args.identities)
    approval_services = load_identities(args.approval_identities, ROLES)
    if set(secret for _, secret in services.values()) & set(secret for _, secret in approval_services.values()):
        parser.error("Delivery and approval boundaries require distinct credentials")
    permissions = load_permissions(args.approval_permissions, approval_services)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    tls = None
    if args.cert:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(args.cert, args.key)
    previous = {}
    try:
        with servers(args.db, services, approval_services, permissions, bind=args.bind, port=args.port,
                approval_port=args.approval_port, tls_context=tls, request_timeout=args.request_timeout,
                lease_seconds=args.lease_seconds, retention_seconds=args.retention_seconds,
                max_lifetime_seconds=args.max_lifetime_seconds,
                max_clock_skew=args.max_clock_skew_seconds) as (workers, approvals, stopping, threads):
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda *_: stopping.set())
            print(f"Coordinator delivery :{workers.server_port}, approvals :{approvals.server_port}", flush=True)
            while not stopping.wait(0.25):
                if any(not thread.is_alive() for thread in threads):
                    log.emit(LOG, "coordinator.server_thread_stopped", level=log.ERROR)
                    return 1
    finally:
        for sig, value in previous.items():
            signal.signal(sig, value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
