# Optional protected-workload experiments

Security code is retained as a separate, opt-in experiment. It does not complete
the Sentinel guarded recovery chain on protected templates. The current baseline
still uses restricted repair RBAC, strict Guard checks, non-root demo workloads,
scoped contexts, bounded reads and private test credentials.

`scripts/security/` contains the inspection labs, scan/candidate tools and scan
monitor. `services/grpc_inspection.py` and `services/image_security.py` own their
decision/validation logic; the dashboard projects their journaled events.

For the full protected Boutique lab, inspect help and use a fresh evidence path:

```powershell
python scripts/security/boutique_security.py --help
python scripts/security/boutique_security.py start --directory data/experimentation/runs/protected-boutique --exit-after-test
python scripts/security/boutique_security.py test --directory data/experimentation/runs/protected-boutique
python scripts/security/boutique_security.py stop --directory data/experimentation/runs/protected-boutique
```

Run these steps separately. Without `--exit-after-test`, `start` keeps serving
the storefront and dashboard until stopped, and `test` would then fail because
the default ports are taken; use `serve` to reopen the dashboard later, or give
`test` other ports with `--storefront-port` and `--dashboard-port`. OpenSSL
must be on PATH for certificate generation; if it fails to open an
`openssl.cfg`, a stale `OPENSSL_CONF` environment variable is the usual cause.
The script owns a dedicated Calico/kind lab with mTLS and inspected shipping
traffic. Only the shipping path is protected by mTLS and inspection in a live
run so far. From P5.04 the code also protects the cart path the same way (see
[Cart route and inspector capacity](#cart-route-and-inspector-capacity-p504));
that has been checked offline only, not in a live run. The other Boutique
services keep their upstream plaintext connections. From P4.06 the lab
also applies graph-derived default-deny NetworkPolicies to all 11 services (see
below); their enforcement matrix passed in one live run on 13 Nov 2026
(`C-SECURITY-NETPOL-LIVE`). The smaller `scripts/security/security_lab.py`
supports `start`, `test` and `stop` too. Normal connected recovery does not
install either overlay. Read each module's setup/ownership checks before use.

## Graph-derived NetworkPolicies

`scripts/security/network_policy.py` turns the reviewed caller-to-callee graph
into Kubernetes NetworkPolicies. The graph is
`deploy/boutique/connected-graph.json`: 11 services, their Service and Pod
ports, and 15 edges read from the pinned `deploy/boutique/upstream.yaml`. A
test reads that YAML directly and fails if the two disagree. The generated set
for the security lab is committed as
`deploy/boutique/security-network-policies.json`. Check it, or regenerate it
after a reviewed graph change, with:

```powershell
python scripts/security/network_policy.py --check
python scripts/security/network_policy.py --write
```

The set contains:

- `default-deny-ingress`: every Pod in the namespace accepts nothing unless
  another policy allows it. No rule anywhere allows any Pod (`from: {}`) or
  an IP range.
- `default-deny-egress`: each of the 11 services and the gateway may send
  only DNS to kube-dns (UDP and TCP 53), apart from the edges below.
- `ingress-<service>`: exactly the service's callers, on its Pod port. Each
  graph edge appears once here and once in the caller's `egress-<service>`.
  redis-cart accepts only cartservice on 6379 (and, in the connected
  namespace's set, the redis-cart probe; see below).
- frontend accepts only in-cluster storefront traffic Pods labelled
  `app=sentinel-traffic`. The lab reaches the storefront through
  `kubectl port-forward`, which enters the Pod through the container runtime
  rather than the Pod network, so NetworkPolicy does not apply to it.
- Sentinel's probe Pods now carry `app=sentinel-probe` and
  `sentinel.io/probe-target=<target>`, and each may reach only its own
  target's port. In the lab no probe is allowed since P5.04: shipping and
  cart keep their `protect-*` policies, so each accepts only the mTLS
  gateway, and a Sentinel probe of either is refused there (it needs the
  authenticated Verifier probe of P5.05). Before P5.04 the lab admitted the
  cartservice probe. The plain set
  for the connected namespace (`graph_policies`) also admits the redis-cart
  probe on 6379, because a scenario-pair run ([dual-fault.md](dual-fault.md))
  checks redis-cart with a Redis PING from a Pod with the same two labels
  (`connected_probe_targets`); the security lab never runs a pair, so its
  committed set is unchanged.

In the lab, frontend and checkoutservice reach shipping only through
`security-gateway:8443`, and only the gateway reaches shippingservice, on
8444. From P5.04 they reach cart only through `security-cart-gateway:8445`
(the same gateway Pod), and only the gateway reaches cartservice, on its TLS
sidecar's port 8446; no Pod reaches cartservice's plaintext port 7070. The
graph policies never add an ingress rule for shippingservice, cartservice or
the gateway, so they cannot reopen a plaintext bypass. cartservice still
reaches redis-cart over plaintext Redis on 6379; that edge is limited to
cartservice by the L4 policy and is not inspected.

`start` refuses to continue, before pulling any image, when the graph derived
from the manifest differs from the reviewed graph or the regenerated policies
differ from the committed file. That check needs `kubectl create
--dry-run=client`, so it runs after kind has created the lab cluster and
installed Calico; `network_policy.py --check` in the preflight below covers the
committed file before anything starts. `start` applies the policies with the
rest of the manifest and copies them to `network-policies.json` in the run
folder.

These are L4 rules keyed on the Pod label `app`. They are not
authentication: anyone allowed to create a Pod in the namespace can give it a
workload's label and be treated as that workload. Normal connected recovery
installs none of them.

### Enforcement matrix

`test` (and `start`) runs the matrix right after the first storefront journey.
One short-lived tester Pod runs for each identity: an intruder with no
reviewed role, storefront traffic, each of the 11 services, the gateway, the
existing `security-test` probe identity and the cartservice Sentinel probe.
Each tries a TCP connection, with a 3 s timeout, to every Service in the
namespace: 16 identities by 15 Services since P5.04 (13 before: the cart
route adds `security-cart-gateway` and `security-cart-upstream`), with 20
allowed cells (19 before). The tester image is the busybox
release that the upstream manifest pins by digest. Testers run as non-root
without a token, and their readiness check never passes, so a tester carrying a
workload's label never receives that workload's traffic.

Each cell's expected result comes from evaluating the reviewed policies: the
`protect-*` chain plus the committed graph-derived set, never the run folder's
copy. The run folder's `manifest.json` and the policies the cluster itself
reports (`kubectl get networkpolicies`) must both be exactly that set; any
missing, extra or changed policy is listed under `policy_drift` in
`network-matrix.json`. So an older run folder that applied only the
`protect-*` chain cannot pass. Each tester first connects to kube-dns on
TCP 53, which no policy blocks; if that control fails, the tester's results
are not counted. The verdict in `network-matrix.json`, also copied into
`validation.json`, is:

- PASS: every allowed connection worked, every forbidden one timed out (a
  dropped connection), the design's sampled forbidden edges (intruder to
  redis-cart, productcatalogservice to cartservice, paymentservice to
  checkoutservice) are among the forbidden cells, the policies match the
  reviewed set, and the storefront journey passed;
- FAIL: an allowed connection was refused, or the journey failed (the
  policies are too tight);
- UNKNOWN, never PASS: a forbidden connection worked, or was refused at once
  rather than timing out (which shows a missing listener, not a policy
  drop), a cell could not be tested, a sampled forbidden edge is missing or
  judged allowed, the policies differ from the reviewed set or could not be
  read, or the journey result is missing.

`validation.json` passes only when the matrix is PASS, and it records
`passed: false` until the last check finishes. The later shipping probes look
up workload Pods with a selector that excludes matrix testers, so a tester
that has not yet been removed is never mistaken for the workload.

### What a live run needs

The live run needs a network plugin that enforces NetworkPolicy. The security
lab's kind configuration (`deploy/kind/kind-security.yaml`) turns off kind's
default CNI, and `boutique_security.py` installs Calico 3.33.0 from a
checksum-pinned manifest. The connected recovery cluster keeps kind's default
CNI, kindnet. Older kindnet releases ignore NetworkPolicy, and whether the
installed kind enforces it has not been checked here, so the policies are
not applied there. On a cluster that does not enforce them, every forbidden
connection succeeds and the matrix reports UNKNOWN.

The live run was done once, on 13 Nov 2026 at 02d91e5, before the cart
route existed, on a fresh
`sentinel-boutique-security` cluster with Calico 3.33.0 (bundle
`protected-boutique-netpol`): PASS, 19 of 19 allowed edges open, 202 of 202
forbidden edges closed by timeout, an empty `policy_drift`, no untested cell,
and the storefront journey and the existing shipping bypass, identity and
outage probes passed. The first attempt stopped before any probe because an
inherited `OPENSSL_CONF` named a missing file; it was rerun with that variable
unset and is listed in [evidence/exclusions.json](../evidence/README.md). One
Windows host, one kind node.

<!-- BEGIN GENERATED:claim-c-security-netpol-live -->
> **C-SECURITY-NETPOL-LIVE:** The P4.06 graph-derived NetworkPolicy matrix in the protected Boutique security lab passed in 1/1 runs.
> Scope: One run on a fresh sentinel-boutique-security cluster with Calico 3.33.0 at 02d91e5: 19 of 19 allowed edges open, 202 of 202 forbidden edges closed by timeout, no policy drift, no untested cell; the storefront journey and the existing shipping bypass, identity and outage probes passed. L4 rules keyed on the Pod label app, not authentication; mTLS and inspection still cover the shipping path only, and the connected recovery cluster applies none of these policies. The first attempt stopped before any probe on an operator environment fault (evidence/exclusions.json). One Windows/amd64 host with Docker Desktop and one kind node. Provenance recorded: 02d91e5, 2026-11-13 UTC.
<!-- END GENERATED:claim-c-security-netpol-live -->

To repeat it, use a fresh run folder (the path below is an example):

```powershell
# Preflight: Docker answers, the folder is new, the reviewed policies are current.
docker info --format "{{.ServerVersion}}"
Test-Path data/experimentation/runs/protected-boutique-netpol   # must print False
python scripts/security/network_policy.py --check
kind get clusters   # sentinel-boutique-security is created if missing; the script touches no other cluster
python scripts/security/boutique_security.py start --directory data/experimentation/runs/protected-boutique-netpol --exit-after-test
```

Expected: the command prints PASS. In the run folder, `network-matrix.json`
has `verdict.status` PASS, an empty `policy_drift`, `forbidden.closed` and
`forbidden.timed_out` equal to `forbidden.cells`, and `validation.json` has
`passed: true`, including the existing shipping bypass and outage probes.
Then commit the evidence:

```powershell
python scripts/evidence/collect.py --run data/experimentation/runs/protected-boutique-netpol --class security-lab --bundle-id protected-boutique-netpol
python scripts/security/boutique_security.py stop --directory data/experimentation/runs/protected-boutique-netpol
```

The `security-lab` profile collects `network-policies.json` and
`network-matrix.json` with the earlier files. A run is evidence for this one
Windows host with one kind node, not for a multi-node cluster.

## Cart route and inspector capacity (P5.04)

This section describes code and offline tests. None of it has run on a live
cluster yet; the recipe at the end is how to run it.

### The routes

`scripts/security/security_lab.py::ROUTES` lists the protected routes:

| Route | Service port | Caller loopback port | Gateway port and Service | Service's TLS sidecar |
| --- | --- | --- | --- | --- |
| shipping | shippingservice 50051 | 50052 | 8443, `security-gateway` | 8444, `security-upstream`, container `shipping-ingress` |
| cart | cartservice 7070 | 50053 | 8445, `security-cart-gateway` | 8446, `security-cart-upstream`, container `cart-ingress` |

`boutique_security_manifest.py::overlay` rewrites `SHIPPING_SERVICE_ADDR` and
`CART_SERVICE_ADDR` in frontend and checkoutservice, and only there, to their
loopback ports. It refuses to build the lab when either variable is missing,
repeated or not the reviewed `service:port` value. Each caller gets one egress
sidecar, `protected-egress`, with one plaintext loopback listener per route;
each listener forwards only its own route's methods, over mTLS, to the
gateway. The gateway Pod has one listener per route; each sends only its
route's methods to that route's upstream cluster, and everything else gets
403. The gateway dials each service's own TLS sidecar with the
`proxy-client` identity and checks the sidecar's name (`upstream.internal`,
`cart-upstream.internal`). The sidecar forwards over loopback to the service.
`protect-cartservice` admits only the gateway, on 8446. The lab creates a
`cart-upstream` certificate and Secret for the sidecar; like the others it
never enters the manifest or the source tree.

### Cart inspection

`services/grpc_inspection.py` checks the three CartService methods against
the v0.10.7 schema (`experiments/workloads/security/cart.proto`):

- `AddItem`: `user_id` and one `item`; `product_id` matches the same rule as
  shipping, and `quantity` is 1 to 10 (the storefront offers 1 to 10).
- `GetCart` and `EmptyCart`: `user_id` only.
- `user_id` is 1 to 64 characters of letters, digits and `-` (the storefront
  uses its session id, a UUID).

The shipping rules still apply: unknown fields, a field repeated where the
schema has one value, the wrong wire type, a truncated value and a bad varint
are denied, and the rule code never contains the submitted value. A
CartService method other than the three is `METHOD_NOT_ALLOWED`. Envoy adds a
header naming the route to each inspector request (clients cannot set it:
only `grpc-encoding` is copied from the client), and the inspector denies a
method from another route as `ROUTE_MISMATCH`, so a route table that drifted
still could not pass a cart call through the shipping route or the reverse.
The journal records the method, size, verdict, rule and, for `AddItem`, an
item count of 1; never the user id or product.

### Bounded inspector

`runtime/security_inspector.py` runs at most `--max-inflight` inspections at
once (default 16; a non-blocking semaphore) and starts at most `--max-rps`
per second (default 200; a token bucket holding one second of requests). A
request over either bound gets 503 at once and is neither inspected nor
journaled; the counts are in `/readyz`. Envoy's ext_authz keeps
`failure_mode_allow` false, so a 503 from the inspector, or an inspector that
does not answer, denies the call: overload never lets a call pass
uninspected. Envoy holds at most 32 connections to the inspector (circuit
breakers on its cluster), and a call over that bound also fails as a 503, so
the inspector's threads stay bounded too. The inspector's listen backlog is 64
so that a burst of those 32 connections is answered with 503 rather than
refused by the operating system.

`GET /readyz` answers 200 only when the journal takes a write (a test insert
in a transaction that is rolled back) and a slot is free; otherwise 503 with
the reasons `journal_unwritable` or `inflight_exhausted`. The inspector serves
127.0.0.1 only, so the kubelet cannot reach it with an `httpGet` probe; the
inspector container's readiness probe runs Python inside the container to
fetch `/readyz` (every 5 s, unready after 2 failures). While it fails, the
gateway Pod leaves both gateway Services, and callers are refused. The
defaults are not measured against storefront latency yet. One gateway replica
is a single point of failure; nothing here makes it highly available.

The smaller `security_lab.py` compose lab still protects shipping only.

### Why no existing check is weaker (owner sign-off)

The inspector decides which calls reach shipping and cart, so these changes
need an owner's review before merge. The argument, with the tests that hold it:

- Overload is refused before inspection and the refusal is 503, never 200.
  Envoy treats any answer but 200 as a denial (`failure_mode_allow` false,
  `status_on_error` 503), so a full inspector can only deny
  (`tests/test_security_lab_serve.py::InspectorCapacityTests.test_overload_never_passes_a_request_that_inspection_would_deny`).
- The shipping rules are unchanged: every earlier shipping test passes as it
  was, and shipping calls now also need their own route.
- The route header is set by Envoy, and the inspector answers 400, before
  inspection, when it is missing, unknown or repeated. Before P5.04 it had no
  such check. Only `grpc-encoding` is copied from the client, so a client
  cannot pick a route
  (`tests/test_security_inspection.py::InspectorHTTPTests.test_a_missing_unknown_or_repeated_route_is_refused_before_inspection`).
- The overlay now also refuses to build when a caller's
  `SHIPPING_SERVICE_ADDR` or `CART_SERVICE_ADDR` is not the reviewed value;
  before, only the variable's presence was checked.
- One check changed shape. `network_policy.py::policies` used to refuse a
  caller that names the same callee twice; it now refuses the same callee on
  the same port twice, because the gateway serves one port per route. Graph
  edges cannot use this: `edges()` gives each callee one reviewed port, so a
  repeated graph edge is still the same port and is still refused
  (`tests/test_security_connected.py::SecurityLabPolicyTests.test_a_caller_may_reach_one_callee_on_two_ports_but_never_twice_on_one`).
- In the lab the Sentinel cartservice probe can no longer reach cartservice,
  because only the gateway can. Nothing in the security lab runs recovery, so
  nothing used that probe there; P5.05 adds the authenticated probe.

### Live recipe (not run yet)

Run on the security lab cluster only, with a new run folder. The P4.01
distributed acceptance run and the security lab each need most of Docker's
memory, so do not run them at the same time.

```powershell
# Preflight: Docker answers, nothing else is using the memory, the folder is new, the reviewed policies are current.
docker info --format "{{.ServerVersion}}"
kind get clusters   # sentinel-boutique-security is created if missing; the script touches no other cluster
Test-Path data/experimentation/runs/protected-boutique-cart   # must print False
python scripts/security/network_policy.py --check
python -m unittest tests.test_security_inspection tests.test_security_lab_serve tests.test_security_connected
python scripts/security/boutique_security.py start --directory data/experimentation/runs/protected-boutique-cart --exit-after-test
```

Expected: PASS. In `validation.json`, `valid_mtls_quote` and `valid_mtls_cart`
succeeded; every other probe failed, including `cart_invalid_quantity`,
`cart_missing_client_certificate`, `cart_wrong_client_identity`,
`cart_direct_service_bypass` (plaintext `cartservice:7070`),
`cart_direct_pod_ip_bypass`, `cart_direct_egress_bypass` (frontend's
loopback port 50053 from another Pod), `cart_proxy_outage` and
`cart_outage_direct_bypass`; both storefront journeys (add to cart and
checkout through both routes) passed; the matrix is PASS with 20 allowed
cells and 16 by 15 rows. The run folder's `telemetry.sqlite3` holds
CartService inspection events.

Burst and inspector outage, with the lab still running:

```powershell
$ctx = "--context=kind-sentinel-boutique-security"; $ns = "--namespace=sentinel-boutique-security"
# 1. 200 concurrent cart checks against the inspector itself. port-forward enters the gateway
#    Pod's own network namespace, where the inspector listens on 127.0.0.1:9001.
$forward = Start-Process kubectl -ArgumentList $ctx,$ns,"port-forward","deployment/security-gateway","19001:9001" -PassThru -NoNewWindow
python scripts/security/inspector_burst.py --port 19001 --count 200
kubectl $ctx $ns top pod -l app=security-gateway --containers
Stop-Process -Id $forward.Id
# 2. During and after the burst, an invalid cart call through the gateway is still refused (exit 1)
#    and a valid one succeeds again (exit 0).
python scripts/security/boutique_security.py probe --route cart --quantity 11 --directory data/experimentation/runs/protected-boutique-cart
python scripts/security/boutique_security.py probe --route cart --directory data/experimentation/runs/protected-boutique-cart
# 3. Stop the inspector process (SIGINT ends serve_forever; the container restarts). Until /readyz
#    answers again, cart and shipping calls through the gateway are refused.
kubectl $ctx $ns exec deployment/security-gateway -c inspector -- python -c "import os, signal; os.kill(1, signal.SIGINT)"
python scripts/security/boutique_security.py probe --route cart --directory data/experimentation/runs/protected-boutique-cart
python scripts/security/boutique_security.py probe --route shipping --directory data/experimentation/runs/protected-boutique-cart
kubectl $ctx $ns rollout status deployment/security-gateway --timeout=120s
kubectl $ctx $ns wait pod -l app=security-gateway --for=condition=Ready --timeout=120s
python scripts/security/boutique_security.py probe --route cart --directory data/experimentation/runs/protected-boutique-cart
```

Expected: `inspector_burst.py` prints `passed: true`, `allowed_invalid: 0`,
some 503s under `denied_overload`, and p50 and p95 latencies; `kubectl top`
shows the inspector inside its 200m CPU and 96Mi memory limits. `kubectl top`
needs metrics-server, which the lab does not install; without it, record
`docker stats --no-stream sentinel-boutique-security-control-plane` (the
whole node) and the inspector container's restart count instead, and say so. The invalid
probe exits 1, the valid one 0. After the SIGINT, the first two probes exit
1 (the inspector is restarting) and the last exits 0. Record the timings and
`kubectl top` output with the run, then collect evidence as in the NetworkPolicy
recipe above and stop the lab. One kind node on one host, so this shows the
bound and the fail-closed behaviour on that host, not capacity on real
machines. The step-3 probes race the container restart: a probe that starts
after the inspector is back can succeed, so repeat step 3 if both probes
succeeded and note it.

Image scan/candidate workflows remain explicit. A candidate needs reviewed
metadata and adjacent passing scan evidence; a scan does not grant repair
authorization. The monitor's inventory/freshness and candidate rollback handling
remain incomplete for a production release. `--help` on
`scan_image_security.py`, `build_security_candidate.py` and `watch_security.py`
documents the exact arguments.

Results so far:

<!-- BEGIN GENERATED:claim-c-security-lab -->
> **C-SECURITY-LAB:** The protected Boutique security lab passed in 3/3 runs, blocking every bypass, identity and outage probe.
> Scope: Shipping path only, behind one mTLS gateway in a dedicated kind cluster; scripted probes, not an adversarial test. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded.
<!-- END GENERATED:claim-c-security-lab -->

For PR #98 (merged 11 November 2026) the packaged runner and the shipping
security candidate (`0.10.7-security.2`) were rebuilt and scanned with the
project's pinned Trivy. The PR reports 0 High and 0 Medium findings in both,
with unrated Go findings left that need a newer toolchain. That scan output is
not a committed evidence bundle, and no release image is tagged.

Two gaps remain. Only the shipping path has been shown live with mTLS and
inspection; the cart route exists in code with offline tests only, and the
cart-to-redis-cart edge stays plaintext and uninspected. The other services
have L4 default-deny NetworkPolicies in the security lab only, shown by one
live run on one kind node, and their traffic is still plaintext; the
connected recovery cluster applies none of these policies.
Workload images are
scanned and raise dashboard alerts, but nothing gates on them: the 11 November
evaluation found High or Critical findings in 11 of 13 images. See
`L-SECURITY-SHIPPING-ONLY` and `L-UNGATED-CVES` in [limitations](limitations.md).

Lab certificates are short-lived fixtures, not a rotation/revocation/workload-identity
system. The inspector's capacity is bounded in code (P5.04) but not measured
live, and one gateway replica stays a single point of failure.
Current-image reconciliation and protected Guard/Executor/Verifier
integration remain tracked work. Keep credentials,
private keys and unsanitised logs out of Git and exported image contexts.
