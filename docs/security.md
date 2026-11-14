# Optional protected-workload experiments

Security code is retained as a separate, opt-in experiment. From P5.05 the code
also runs the Sentinel guarded recovery chain on the protected template (see
[Protected recovery](#protected-recovery-p505)); one smoke round passed live on
14 Nov 2026. Phase 5 results below are lab observations on one kind node on one
Docker host, with automated auto-lab approvals for recovery, unscored until
owner sign-off. Security repeats r1-r3 are pending. The current baseline
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
From P5.06 `start` also installs cert-manager with Helm (so `helm` must be on
PATH) and refuses before any cluster command while a cert-manager pin in
`deploy/cert-manager/pins.json` is unresolved. All six were lab-resolved on
14 Nov 2026 at 7ac2752; owner review remains pending
(see [Certificate lifecycle](#certificate-lifecycle-p506)).
The script owns a dedicated Calico/kind lab with mTLS and inspected shipping
traffic. Shipping and cart both passed live mTLS and inspection probes in the
Phase 5 smoke round (see
[Cart route and inspector capacity](#cart-route-and-inspector-capacity-p504)).
The other Boutique services keep their upstream plaintext connections. From P4.06 the lab
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
existing `security-test` probe identity and the cartservice Sentinel probe
(still a tester identity, so the matrix shows the lab refuses it since P5.04).
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

### Live results (14 Nov 2026)

Attempt 5, `p5-security-20261114T171037Z`, smoke round only:

- `smoke-lab/validation.json`: PASS; shipping, cart and Verifier mTLS controls
  succeeded, all 16 negative probes were refused, and storefront journeys
  before and after proxy restore passed. The matrix opened 22/22 allowed cells
  and timed out 248/248 forbidden cells, with no untested cell.
- 200 workers for 30 s: 9,343 storefront requests, 14 HTTP 200, 9,329 HTTP 500,
  zero connection errors; p50/p95 506.9/1,315.7 ms. The inspector shed 2,769
  inflight requests; its sampled readiness was 200. Availability was poor:
  storefront requests mostly failed while inspection shed load, fail-closed.
- Log comparison passed: 195 upstream 200s versus 258 inspector GetCart
  ALLOWs, zero AddItem ALLOWs. Three invalid cart probes and one invalid
  shipping probe were refused; the valid cart probe under load was also refused.
- Direct burst: 200/200 completed, 166 overload denials, zero invalid requests
  allowed, p50/p95 1,319.7/2,878.6 ms. Invalid/valid cart probes after the burst
  were refused/accepted. SIGSTOP froze the inspector; cart and shipping were
  refused, and cart recovered after SIGCONT (`inspector-outage.json`: PASS).
- Resource snapshot covers the whole node: 136.88% CPU, 2.68 GiB/15.23 GiB;
  it does not measure the inspector container's CPU or memory.

Earlier failures: attempt 1's Jetstack HTTP 403 with urllib's default
User-Agent was fixed by ac8ac91 (named User-Agent, pinned hash still checked;
also UTF-8 tool output). Attempt 2's Envoy SDS crash loops lacked node id and
cluster; 02d40be supplies them. Attempt 3's valid cart mTLS call hit Kestrel's
HTTP/2 protocol error because `:scheme https` reached plaintext loopback;
e8d6fed overwrites it to http in upstream sidecars. Attempt 4 validated, but
SIGINT restarted the inspector before the probe arrived, so cart succeeded.
The scratch runner changed to node-side SIGSTOP/SIGCONT; no fix commit was
recorded. That earlier load recorded 9,843 requests, 2,633 shed and 171 burst
overload denials, with zero invalid requests allowed.

Repeats r1-r3 (each on a freshly built cluster) passed the same checks: 4/4
rounds in all, bundles `p5sec-p504-smoke` and `p5sec-p504-r1`..`r3`. Owner
sign-off remains pending.

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
gateway Pod leaves both gateway Services, and callers are refused. The two
routes share fate: one gateway Pod serves both, so a cart flood that keeps
all 16 slots busy at two probe checks in a row also takes shipping (and so
checkout) out of service for at least one probe period. That fails closed, as
E10 asks, but it widens an outage; whether a busy inspector should mark the
Pod unready at all, or only an unwritable journal should, is an owner
decision. Smoke measured storefront latency and mostly HTTP 500s under load;
the defaults are not a production capacity target. One
gateway replica is a single point of failure; nothing here makes it highly
available.

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
- The live recipe departs from E10 in how it loads the gateway. E10 asks for
  200 concurrent grpcurl `GetCart` calls through the gateway. The reviewed
  grpcurl image has no shell, so one test Pod makes one call, and 200 test
  Pods at once would not fit on the one kind node. Step 1 below therefore
  loads the cart route through the storefront instead (every cart page load
  is a `GetCart` through the gateway), runs the invalid probes through the
  gateway while that load runs, and compares the inspector's ALLOW events
  with the requests that reached cartservice
  (`tests/test_security_lab_serve.py::GatewayLoadRecipeTests.test_bypass_check_fails_on_an_uninspected_request_or_an_allowed_add_item`).
  Step 2's 200 concurrent calls go straight to the inspector and skip Envoy.
  The owner should accept this or ask for a load client image with a shell.

### Live recipe (P5.04)

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
checkout through both routes) passed; the matrix is PASS with 22 allowed
cells and 18 sources by 15 destinations (from P5.05 the Verifier's probe
identity is a source too, and reaches the two gateway Services only); a
lab started before P5.05 has 20 allowed cells and 17 sources. The run folder's `telemetry.sqlite3` holds
CartService inspection events.

Load, burst and inspector outage, with the lab still running:

```powershell
$ctx = "--context=kind-sentinel-boutique-security"; $ns = "--namespace=sentinel-boutique-security"
$run = "data/experimentation/runs/protected-boutique-cart"
# 1. Load through the whole cart route for 90 s: 200 workers load the storefront's cart page over
#    and over, so each load sends GetCart through frontend's egress sidecar, the gateway's Envoy
#    (32-connection circuit breaker, ext_authz with failure_mode_allow false) and the inspector.
#    While the load runs, invalid cart and shipping calls go through the gateway from test Pods.
$t0 = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$front = Start-Process kubectl -ArgumentList $ctx,$ns,"port-forward","service/frontend","18081:80" -PassThru -NoNewWindow
Start-Sleep 3
$load = Start-Process python -ArgumentList "scripts/security/inspector_burst.py","--storefront-port","18081","--workers","200","--seconds","90" -RedirectStandardOutput "$run/gateway-load.json" -PassThru -NoNewWindow
Start-Sleep 10
foreach ($i in 1..3) { python scripts/security/boutique_security.py probe --route cart --quantity 11 --directory $run; "cart quantity 11: exit $LASTEXITCODE" }
python scripts/security/boutique_security.py probe --route shipping --quantity 101 --directory $run; "shipping quantity 101: exit $LASTEXITCODE"
python scripts/security/boutique_security.py probe --route cart --directory $run; "valid cart call under load: exit $LASTEXITCODE"
kubectl $ctx $ns exec deployment/security-gateway -c inspector -- python -c "import http.client as h; c = h.HTTPConnection('127.0.0.1', 9001, timeout=2); c.request('GET', '/readyz'); r = c.getresponse(); print(r.status, r.read().decode())"
$load.WaitForExit(); Stop-Process -Id $front.Id
Get-Content "$run/gateway-load.json"
kubectl $ctx $ns logs deployment/security-gateway -c inspector --since-time=$t0 | Out-File -Encoding utf8 "$run/inspector-window.log"
kubectl $ctx $ns logs deployment/cartservice -c cart-ingress --since-time=$t0 | Out-File -Encoding utf8 "$run/cart-ingress-window.log"
kubectl $ctx $ns logs deployment/security-gateway -c envoy --since-time=$t0 | Out-File -Encoding utf8 "$run/gateway-window.log"
python scripts/security/inspector_burst.py --compare "$run/inspector-window.log" "$run/cart-ingress-window.log"
Get-Content "$run/gateway-window.log" | Where-Object { $_ -like '{*' } | ConvertFrom-Json | Group-Object route, status, flags | Select-Object Count, Name
# 2. 200 concurrent cart checks straight to the inspector, to see its own bound. This skips
#    Envoy. port-forward enters the gateway Pod's own network namespace, where the inspector
#    listens on 127.0.0.1:9001.
$forward = Start-Process kubectl -ArgumentList $ctx,$ns,"port-forward","deployment/security-gateway","19001:9001" -PassThru -NoNewWindow
Start-Sleep 3
python scripts/security/inspector_burst.py --port 19001 --count 200
kubectl $ctx $ns top pod -l app=security-gateway --containers
Stop-Process -Id $forward.Id
# 3. After the load and the burst, an invalid cart call through the gateway is still refused
#    (exit 1) and a valid one succeeds again (exit 0).
python scripts/security/boutique_security.py probe --route cart --quantity 11 --directory $run
python scripts/security/boutique_security.py probe --route cart --directory $run
# 4. Freeze the inspector from its kind node, keeping it stopped while probes start.
$gatewayPod = (kubectl $ctx $ns get pod -l app=security-gateway -o json | ConvertFrom-Json).items[0]
$node = $gatewayPod.spec.nodeName
$containerId = ($gatewayPod.status.containerStatuses | Where-Object name -eq inspector).containerID -replace '^containerd://', ''
$inspectorPid = (docker exec $node crictl inspect $containerId | ConvertFrom-Json).info.pid
docker exec $node kill -STOP $inspectorPid
try {
  python scripts/security/boutique_security.py probe --route cart --directory $run
  python scripts/security/boutique_security.py probe --route shipping --directory $run
} finally {
  docker exec $node kill -CONT $inspectorPid
}
kubectl $ctx $ns rollout status deployment/security-gateway --timeout=120s
kubectl $ctx $ns wait pod -l app=security-gateway --for=condition=Ready --timeout=120s
python scripts/security/boutique_security.py probe --route cart --directory $run
```

Expected, step 1: `gateway-load.json` has `completed: true` and a mix of
200 and 500 answers (the frontend answers 500 when its `GetCart` is denied);
it measures and does not judge. Connection errors through `kubectl
port-forward` are possible at 200 workers; record their count, they are
neither a pass nor a fail. All four invalid probes exit 1. The valid probe
under load may exit 0 or 1 (overload may deny it); record which. The
`/readyz` line shows the inspector's `shed` counts. `--compare` prints
`passed: true` with `allowed_add_item: 0`: no more cart requests reached
cartservice (status 200 in the cart-ingress log) than the inspector allowed,
so none passed uninspected. A request just before `$t0` can, rarely, show in
one log and not the other; if `--compare` fails by one, repeat the step and
look at both logs before calling it a bypass. The gateway log grouping shows
how Envoy answered: 403 and 503 entries (flag `UAEX` for an ext_authz denial
or error) next to the 200s. If both `shed` counts are 0 and there are no
503s, the load never reached the bound through the gateway (the frontend or
port-forward limited it first): say so, and step 1 then shows only that
nothing bypassed the inspector at the load reached.

Step 2: `inspector_burst.py` prints `passed: true`, `allowed_invalid: 0`,
some 503s under `denied_overload`, and p50 and p95 latencies. 200
connections at once through port-forward against a listen backlog of 64 can
produce connection errors, which fail this step without saying anything
about the bound; repeat once and record both runs. `kubectl top` shows the
inspector inside its 200m CPU and 96Mi memory limits (the readiness probe
starts a Python process every 5 s inside the same limit). `kubectl top`
needs metrics-server, which the lab does not install; without it, record
`docker stats --no-stream sentinel-boutique-security-control-plane` (the
whole node) and the inspector container's restart count instead, and say so.

Step 3: the invalid probe exits 1, the valid one 0. Step 4: both probes
while SIGSTOP holds the inspector exit 1; after SIGCONT and readiness, the
last cart probe exits 0. Thaw in `finally` even if a probe fails.

Record the outputs and timings with the run, then collect evidence as in the
NetworkPolicy recipe above and stop the lab. What this can show, on one kind
node on one host: step 1, how Envoy answered cart calls at the load the
storefront reached, and that no request reached cartservice without an
inspector ALLOW in that window; step 2, the inspector's own bound; step 4,
that calls are refused while the inspector is frozen. It is not a capacity
figure for real machines.

Image scan/candidate workflows remain explicit. A candidate needs reviewed
metadata and adjacent passing scan evidence; a scan does not grant repair
authorization. The [CVE gate](#cve-gate-p502) below is what turns scans into
Guard and admission decisions, and `image_inventory.py` feeds the monitor the
digests that are running; the gate passed four connected-lab rounds below.
`--help` on
`scan_image_security.py`, `build_security_candidate.py`, `watch_security.py`,
`gate_images.py` and `image_inventory.py` documents the exact arguments.

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

Shipping and cart now have live lab mTLS and inspection results; the
cart-to-redis-cart edge stays plaintext and uninspected. Other services have
L4 default-deny only in the security lab and still use plaintext; connected
recovery installs none of these policies. The CVE gate passed four lab
rounds, but non-regression still permits inherited findings; the 11 November
evaluation found High or Critical findings in 11 of 13 images. See
`L-SECURITY-SHIPPING-ONLY` and `L-UNGATED-CVES` in [limitations](limitations.md).

Certificate rotation and protected recovery have smoke results below. The
single gateway and inspector shedding still cost availability. Current-image
reconciliation and owner sign-offs remain pending. Keep credentials, private
keys and unsanitised logs out of Git and exported image contexts.

## CVE gate (P5.02)

### Live results (14 Nov 2026)

Connected attempt 7 completed smoke plus r1-r3: four rounds, 24 P5.02 bundles
among the 52 `p5conn-*` bundles in [the dated evidence](../evidence/2026-11-14).
Each round recorded Audit warnings for an unscanned digest, Deny refusal,
C1 recovery including crash-after-write reconciliation, shipping negative
checks, IMAGE_CVE_INHERITED PASS checks (four per round), and a worse shipping
image REJECT with IMAGE_CVE_REGRESSION. The regression check used the Guard
CLI: UID/resourceVersion stayed unchanged and Executor writes were zero;
other UNKNOWN checks were present, so this is not an isolated regression test.
N2 RESTART/RECOVERED, N3 ESCALATE/NEEDS_REVIEW and T1 NO_INCIDENT each passed
4/4 under Deny. These are single-node lab runs on one Docker host, with code
rules and automated auto-lab approval, unscored until owner sign-off.
Non-regression permits inherited vulnerabilities; P5.03 remains pending.

Earlier connected attempts failed as follows:

| Attempt | Cause | Fix |
| --- | --- | --- |
| 1 | `rollout restart --dry-run` is unsupported | Scratch runner corrected; no commit recorded |
| 2 | Exact-string admission approved the helper's short name, but the Pod used its docker.io name | b3f18be approves both reviewed forms |
| 3 | Audit enforcement raced the probe about 1 s after apply | Scratch runner waits for Audit; no commit recorded |
| 4 | API server v1.37.0 retained the old 21-entry params ConfigMap after same-name delete/recreate | 3ccbf49 uses a fresh ConfigMap name and runner patches paramRef |
| 5 | Crash worker omitted image_scans/cve_exceptions; Guard re-check failed closed before writing | 7b35066 passes the gate inputs |
| 6 | Uppercase N2 in the bundle id was refused after the scenario passed | Scratch runner lowercases ids; no commit recorded |

Attempts 2 and 3 had passing P5.07/P5.08 smoke checks; their partial runs are
not added to the four collected rounds.

One pure function, `services/image_security.py::gate`, decides whether images
may run. It reads scan summaries (the output of `scan_image_security.py` or
`watch_security.py`), a reviewed rule block and reviewed exceptions, and
returns PASS, FAIL or UNKNOWN per container. It never scans, so a Trivy
database update cannot change a decision that is already bound to its inputs.
Three places use it:

- **The Guard.** When a policy sets
  `security_constraints.image_vulnerability_gate`, every ROLLBACK and RESTART
  is gated over each container and initContainer image of the baseline
  template (a RESTART writes that template plus a token). The scans arrive only
  as `live_state.image_scans`, added by the trusted reader
  (`runtime/image_scans.py`) in `LiveGuardAuthorizer`, the Guard worker and
  `services/guard/cli.py --context --image-scans`. They are therefore part of
  `inputs_sha256`: a changed scan voids an earlier ALLOW. SCALE creates Pods
  from the current template, so it is not gated. In `non_regression` mode this
  changes nothing, since the current image's findings count as inherited; in
  strict mode (P5.03) SCALE of a vulnerable current image would still be
  allowed, so P5.03 must decide whether to gate SCALE too.
- **Admission.** `deploy/security/approved-images-vap.yaml` is a
  ValidatingAdmissionPolicy that admits a Deployment CREATE or UPDATE, or a
  Pod CREATE, in `sentinel-boutique-connected` only when every container image
  is listed in the ConfigMap `sentinel-admission/sentinel-approved-images`.
  `gate_images.py vap-params` writes that ConfigMap from passing gate results:
  `repository@sha256` references only, never a tag. Pod UPDATEs are not
  matched, so Chaos Mesh pod-failure is not blocked, and neither is an
  ephemeral container added later through the `pods/ephemeralcontainers`
  subresource (`kubectl debug`): ephemeral containers are checked only when a
  Pod is created with them. With Deny on, a server dry-run of a template that
  names an unapproved digest fails, but kubectl does not tell an admission
  denial apart from other failures, so the Guard's preview
  (`services/guard/preview.py`) reports it as UNKNOWN with detail
  `SERVER_DRY_RUN_FAILED`, and the Guard escalates on
  `SERVER_DRY_RUN_UNKNOWN` rather than rejecting. The preview rejects only
  when admission changes the template (`ADMISSION_TEMPLATE_MUTATED`). A
  REJECT for a worse image comes from the Guard's own gate
  (`IMAGE_CVE_REGRESSION`), not from admission.
- **CI.** The `test` job runs `gate_images.py check-exceptions` on
  `deploy/security/cve-exceptions.json` and fails on a malformed or expired
  exception. Sentinel's own images are scanned and gated in the release
  workflow (P2.09), not by this gate. Upstream Boutique images are not scanned
  in CI.

### Rules and results

The reviewed rule block is `deploy/security/cve-gate.json`: block CRITICAL
and HIGH, mode `non_regression`, scans at most 86400 s old, the pinned Trivy
digest (`TRIVY_VERSION` in `scan_image_security.py`) as the only trusted
scanner, and UNKNOWN results escalate. Unknown fields, a severity list without
CRITICAL, an age over 30 days or a tagged scanner are refused, so the block
cannot be weakened by a typo.

| Code | Status | Meaning |
| --- | --- | --- |
| `IMAGE_SCANS_MISSING` | UNKNOWN | The Guard received no `image_scans` at all |
| `IMAGE_SCAN_MISSING` | UNKNOWN | No completed scan for this digest; a failed scan or a tag counts as missing |
| `IMAGE_SCAN_STALE` | UNKNOWN | The scan is older than the limit, or dated in the future |
| `IMAGE_SCANNER_UNTRUSTED` | FAIL | The scan does not name the pinned scanner digest |
| `IMAGE_CVE_REGRESSION` | FAIL | A blocked finding that the same container's current image does not have |
| `IMAGE_CVE_INHERITED` | PASS (non_regression), FAIL (strict) | A blocked finding the current image already has |
| `IMAGE_CURRENT_SCAN_UNAVAILABLE` | UNKNOWN | Inheritance cannot be shown: the current image has no usable scan. An image-fault recovery (R2, C2: the current template runs an unscanned or nonexistent digest) to a baseline with High or Critical findings therefore escalates |
| `IMAGE_CVE_BLOCKED` | FAIL | Strict mode, blocked findings, current image unknown |
| `IMAGE_CVE_EXCEPTED` | PASS | Covered by a reviewed, unexpired exception for this digest and id |
| `IMAGE_EXCEPTION_EXPIRED` | FAIL | The exception that covered a needed finding has expired |
| `IMAGE_EXCEPTIONS_UNREVIEWED` | FAIL | The exceptions do not match the policy's `exceptions_sha256`; none apply |
| `IMAGE_CVE_CLEAR` | PASS | A usable scan with no blocked finding |

Non-regression is the default because a config-only rollback keeps the same
digest: blocking it for upstream findings it does not introduce would block
every recovery to the upstream Boutique images. An image rollback to a worse
digest is still refused. Containers are compared by name, so a High in a new
initContainer image is a regression even if the main container already has it.
Strict mode, which also refuses inherited findings, is for targets whose images
have been rebuilt and triaged (P5.03).

Exceptions live in `deploy/security/cve-exceptions.json` (schema
`sentinel-cve-exceptions-v1`, empty today). Each names one image digest and one
vulnerability id, a reviewer role (not a person), a rationale, and an approval
and expiry at most 30 days apart. The policy pins the canonical SHA-256 of the
list in `exceptions_sha256`, so an exception added without a policy review
fails the gate.

### Why this cannot weaken the Guard

The Guard's verdict is REJECT if any check FAILs, ESCALATE if any is UNKNOWN,
and ALLOW only otherwise (`services/guard/decisions.py::evaluate_with_report`).
The gate only adds checks, so with the gate on a decision can only stay the
same or become stricter; no existing check is removed or changed. Without the
policy block nothing changes at all. Absent, unreadable or malformed scans
give UNKNOWN or `BASELINE_ELIGIBILITY_INVALID`, never PASS: a reader error in
the Executor leaves `image_scans` out, and scans already present in the live
state handed to the authorizer are discarded before the trusted reader runs,
so a caller cannot supply its own. The Guard image now also copies
`services/image_security.py` and `runtime/image_scans.py`, which the gate
imports.

The tests that show this, each failing before this change:
`tests/test_image_security.py::CveGateTests` (inherited, regression, missing,
stale, untrusted, strict, expired and foreign exceptions, rule validation),
`tests/test_guard_eligibility.py::ImageGateEligibilityTests` and
`ImageGateDecisionTests` (absent scans ESCALATE, a worse baseline is a
regression, malformed scans are invalid, duplicate container names cannot hide
an image), `tests/test_guard.py` (a changed `image_scans` changes
`inputs_sha256` and voids the decision), `tests/test_live_executor.py` (the
authorizer asks for every image of both templates, a reader failure
escalates, caller-supplied scans are ignored, a worse digest is refused and
`execute()` makes no write) and
`tests/test_cve_gate_admission.py` (the admission policy's shape, the
parameters hold digests only, unscanned digests are not approved, the
inventory never guesses a digest, and the Guard image holds every module
the Guard imports, the gate's included). Two tests pin what the Guard
already did with a failed server dry-run, so the admission text above stays
true: `tests/test_guard_preview.py` (a policy denial is UNKNOWN,
`SERVER_DRY_RUN_FAILED`) and `tests/test_guard_decisions.py` (that preview
escalates).

What the gate does not show. Non-regression is measured against the image
the same container runs now, not against an approved set. If the current
template is itself the faulty or attacker-written one, a vulnerable but
scanned current digest makes the same findings in a baseline look inherited;
and an image swapped in out of band while admission is still Audit and Warn
counts the same way. Admission in Deny mode is what limits who can make the
current template worse. The trust roots are write access, not signatures:
`scanner_image` is a field each scan file declares about itself, so whoever
can write the `--image-scans` directory decides what the Guard sees, and
whoever can write the ConfigMap `sentinel-admission/sentinel-approved-images`
can approve an image for admission. Both must stay with the operator.

### Live inventory

`scripts/security/image_inventory.py` reads Deployments and Pods in one
namespace (only `kind-sentinel-boutique-connected`; anything naming
sentinel-lab is refused) and records each container's digest: the spec image
when pinned, otherwise the single repository digest its Pods report in
`containerStatuses[].imageID`. A bare config ID, Pods that disagree or no Pod
stays UNKNOWN. Each digest gets the age of its newest scan (FRESH, STALE,
FAILED or MISSING). `--scan-queue` writes the running digests, those needing a
scan first, in the format `watch_security.py --inventory` reads; `--journal`
raises the dashboard's `SCAN_STALE` alert for each digest that is not FRESH.

An index mismatch stays UNKNOWN unless `--platform-indexes <file>` supplies
recorded registry proof. The JSON format is
`{"schema":"sentinel-platform-indexes-v1","indexes":[{"image":"repository@sha256:<index>","index_file":"relative/index.json"}]}`.
Save each index's exact bytes with `docker buildx imagetools inspect <image> --raw`
(without adding a newline). The inventory verifies the bytes against the image
digest and reads Nodes with the same read-only GET. Both spec and Pod references
must select the same manifest in the same repository for that Pod's node OS and
architecture. Missing proof, unknown nodes and ambiguous platform variants stay
UNKNOWN. The inventory retains the spec reference for scanning and admission and
records the platform manifest evidence in `platform_resolution`. Saved
`--from-json` snapshots must include the Nodes when using this option.

### Live recipe (P5.02)

Run on `kind-sentinel-boutique-connected` only, with the connected lab up and
no other heavy-lab run active (`experiments/lab_lock.py`). Never on
sentinel-lab. Every step after the preflight changes the lab, so a person runs
it. Use a new folder; `$run` below is that folder.

1. Preflight and inventory:

   ```powershell
   $run = "data/experimentation/runs/cve-gate-<date>"
   python scripts/boutique_chaos_recovery.py preflight
   kubectl --context kind-sentinel-boutique-connected version
   python scripts/security/image_inventory.py --context kind-sentinel-boutique-connected --scans $run/scans --output $run/inventory.json --scan-queue $run/queue.json
   ```

   Stop if the server is older than 1.30 or any container is UNKNOWN. Stop
   too if any container's `image` in `inventory.json` is not a plain
   `repository@sha256:` reference (a tag, or `repository:tag@sha256:`):
   admission compares the spec string exactly, while the inventory resolved
   it to a digest, so under Deny that workload's next update would be denied.
   One way to list them:
   `(Get-Content $run/inventory.json | ConvertFrom-Json).workloads | Where-Object { $_.image -notmatch '^([^/@]+/)*[^/:@]+@sha256:[a-f0-9]{64}$' }`
   must print nothing.

2. Scan every running digest with the pinned Trivy (about 2-5 minutes each):
   `python scripts/security/watch_security.py --inventory $run/queue.json --journal $run/security.db --directory $run/scans --once`.
   Rerun the inventory; every digest must be FRESH.

3. Generate the admission parameters. Add the harness's own digest-pinned
   images (the grpcurl probe, the traffic and Redis probe images, which a
   `docker image inspect --format "{{index .RepoDigests 0}}"` shows) with
   `--extra-image`:

   ```powershell
   python scripts/security/gate_images.py vap-params --scans $run/scans --rules deploy/security/cve-gate.json --exceptions deploy/security/cve-exceptions.json --inventory $run/inventory.json --extra-image <probe repository@sha256> --output $run/approved-images.json --report $run/vap-report.json
   ```

   On a lab where the policy was installed before, give the ConfigMap a
   fresh name (`--configmap-name sentinel-approved-images-<run>`) and, after
   applying the policy, point the binding at it
   (`kubectl patch validatingadmissionpolicybinding sentinel-approved-images --type=merge -p '{"spec":{"paramRef":{"name":"sentinel-approved-images-<run>"}}}'`).
   On 14 Nov 2026 the connected lab's API server (Kubernetes v1.37.0, kind)
   kept evaluating an earlier list after the ConfigMap was deleted and
   recreated under the same name, with or without its namespace; a fresh
   name was read at once. A helper given by its short Docker Hub name is
   approved under its `docker.io/...` name too, since admission compares
   the exact string a Pod spec carries.

4. Steps 4 to 8 change the connected lab and do not take the heavy-lab lock
   themselves. Before step 4, check that no other heavy-lab run holds it
   (`Test-Path (Join-Path ([IO.Path]::GetTempPath()) sentinel-heavy-lab.lock)`
   must print False) and that any P4.01 run on `kind-sentinel-dist` has
   finished, and start no other heavy-lab run until step 8 is done.
   Apply in Audit and Warn mode and check the policy compiled:

   ```powershell
   kubectl --context kind-sentinel-boutique-connected create namespace sentinel-admission
   kubectl --context kind-sentinel-boutique-connected apply -f $run/approved-images.json
   kubectl --context kind-sentinel-boutique-connected apply -f deploy/security/approved-images-vap.yaml
   kubectl --context kind-sentinel-boutique-connected get validatingadmissionpolicy sentinel-approved-images -o jsonpath="{.status}"
   ```

   The status must show no type-checking errors. Then
   `kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected rollout restart deployment/cartservice --dry-run=server`
   must print no warning, and
   `kubectl ... set image deployment/cartservice server=<an unscanned repository@sha256> --dry-run=server`
   must print the policy's warning.

5. Switch to Deny:
   `kubectl --context kind-sentinel-boutique-connected patch validatingadmissionpolicybinding sentinel-approved-images --type merge -p '{"spec":{"validationActions":["Deny","Audit"]}}'`.
   Checks: the unscanned `set image` (without `--dry-run`) is denied; the C1
   environment fault (`kubectl set env`) is admitted.

6. Guard checks, each read-only (the Guard CLI never writes). For the
   config rollback (C1), `python -m services.guard.cli` with the run's
   proposal, policy (with the gate block), baseline, dependencies and
   approval, `--context`, `--cluster-id`, `--image-scans $run/scans` and
   `--report` must ALLOW with `IMAGE_CVE_INHERITED` PASS in the report. With a
   crafted baseline naming a scanned, more vulnerable digest, the same command
   must exit 2 with `IMAGE_CVE_REGRESSION`; the Deployment's resourceVersion
   must be unchanged afterwards (0 writes). Last, the admission denial as the
   Guard sees it: a crafted baseline naming a digest that has a fresh,
   passing scan in `$run/scans` but is not in the approved ConfigMap (for
   example one scanned after step 3), with the policy's
   `require_server_dry_run` on and `--server-dry-run` added, must exit 3 with
   ESCALATE and `SERVER_DRY_RUN_UNKNOWN` in the report (detail
   `SERVER_DRY_RUN_FAILED`), never ALLOW. The CLI runs the dry-run only when
   every other check passes, so a baseline the gate already refuses never
   reaches admission here.

7. Rerun with the gate on:
   `python scripts/test_boutique_live.py --connected --target cartservice --approval auto-lab --image-scans $run/scans --cve-exceptions deploy/security/cve-exceptions.json`
   (P3.09), and the same with `--scenario N2`, `N3` and `T1` (P4.03). R2 and
   C2 inject an image digest that names no published image, so with Deny on
   their injection is refused by admission: run them only in Audit and Warn
   mode, or report them as blocked by the gate. Even in Audit and Warn mode
   the Guard will not ALLOW their recovery when the upstream baseline image
   has High or Critical findings: the faulty current digest has no scan, so
   inheritance cannot be shown and the Guard escalates with
   `IMAGE_CURRENT_SCAN_UNAVAILABLE`. That is the gate failing closed; report
   it as such, not as a recovery.

8. Clean up: delete the binding, the policy and the `sentinel-admission`
   namespace, then collect the run with `scripts/evidence/collect.py`.

### Owner sign-offs

These are safety-critical and wait for an owner:

- the Guard change: the gate block, its codes and the argument above;
  live use so far is lab-only;
- the Guard image change: `deploy/docker/guard.Dockerfile` now copies
  `services/image_security.py` and `runtime/image_scans.py`;
- the admission policy; Deny has run in the lab, with owner sign-off pending;
- non-regression as the default mode, and which targets move to strict mode
  (P5.03);
- every CVE exception: a human signs each one, with a role, rationale and at
  most 30 days (P5.03). The agent prepares none.

## Protected recovery (P5.05)

Until P5.05 the security lab and guarded recovery never met (#69): the
Executor read Secret and ConfigMap versions with its own kubeconfig
(`runtime/live_executor.py::LiveGuardAuthorizer`), the Verifier's plaintext
probe is refused by the lab's NetworkPolicies, and the Guard knew nothing of
the TLS sidecar in the protected template. P5.05 adds the pieces below
(`L-PROTECTED-RECOVERY-OFFLINE` in [limitations](limitations.md)).

### Live results (14 Nov 2026)

In `p5-security-20261114T171037Z`, `smoke-protected-baseline` passed 4/4 checks
and `smoke-protected-full` passed 14/14: one approved rollback restored the
protected shipping template, the authenticated Verifier reported RECOVERED,
and bypass checks passed before, during and after repair beside working
controls. Missing approval, hard-policy rejection and stale state each made
zero writes; duplicate replay and changed-ID rejection passed. The RBAC
matrix passed with no Secret read by Executor or Guard reader; reader RBAC
and private credentials were removed. Approval was automated auto-lab.

The shared lab startup/route failures and fixes are recorded under
[P5.04](#cart-route-and-inspector-capacity-p504). A separate expiry run
(`smoke-protected-expiring`) was FAILED by the harness despite safe Guard
escalation with zero write attempts: it expected an approval response
(`Guard must require approval before live execution`). Emergency operator
restore ran; no harness fix commit is recorded. See [P5.06](#certificate-lifecycle-p506).
Baseline and full stages passed in all four rounds (smoke and r1-r3,
`p5sec-p505-*`). The expiry drill's protected run (`p5sec-p506-smoke-expiry`)
is recorded FAILED by the generic recovery harness because the Guard
escalated instead of asking for approval, the intended fail-closed result;
its proof (Guard ESCALATE, zero Executor writes) is `identity-expiring.json`
in `p5sec-p506-smoke`. One Docker host, unscored; owner sign-off and the
documented coverage gaps remain.

### What changed

- **Sidecar-aware Guard policy.** `security_constraints.allowed_sidecars`
  lists each target's reviewed sidecars: name, image digest, optionally
  command and arguments, and the exact read-only mounts with the ConfigMap or
  Secret behind each one (`services/guard/eligibility.py::_sidecars`). With
  the rule configured, a restored template may hold one application container
  and the target's reviewed sidecars only. Another container or any init
  container is `SECURITY_SIDECAR_NOT_ALLOWED`; a different Envoy digest is
  `SECURITY_SIDECAR_IMAGE`; another command or arguments, a lifecycle hook
  (`postStart` or `preStop`) or an `exec` probe is `SECURITY_SIDECAR_COMMAND`,
  because each runs a process beside the mounted key; any container field
  outside the reviewed set (name, image, pull policy, command, args, ports,
  resources, securityContext, volumeMounts, env, envFrom, the termination
  message fields and network probes without a `host`) is
  `SECURITY_SIDECAR_FIELDS`; another Secret, a writable mount, a `subPath`
  or a host path behind a mount, or another container that mounts or
  references the sidecar's Secret, is `SECURITY_SIDECAR_MOUNTS`; environment
  references are `SECURITY_SIDECAR_ENV`; a template without the reviewed
  sidecar is `SECURITY_SIDECAR_MISSING`. Each is a FAIL, so the decision is
  REJECT. The reviewed entries come from the overlay itself
  (`scripts/security/boutique_security_manifest.py::reviewed_sidecars`), and a
  test fails when the overlay's sidecar and the entry differ. The protected
  policy allows the registries `us-central1-docker.pkg.dev` (Boutique) and
  `docker.io` (the digest-pinned Envoy) only; the inspector's `sentinel.local`
  image runs in the gateway, never in a recovery target, so it is not allowed.
  The Guard's resource ceilings apply to every container alike, so the
  protected run raises shipping's container memory-limit ceiling from 128Mi to
  192Mi, the reviewed sidecar's own limit, and nothing else
  (`experiments/boutique/harness/protected.py::protected_ceilings`).
- **Split Guard-reader identity.** `LiveGuardAuthorizer` and `build_adapters`
  take `reference_kubeconfig` (and `reference_context`); the CLI has
  `--reference-kubeconfig` and `--reference-context`, and a worker
  configuration has `reference_kubeconfig` and `reference_context`. The
  reference reads use only that identity. Without it, a template with
  ConfigMap or Secret references gets no versions, the Guard reports
  `BASELINE_REFERENCE_EVIDENCE_MISSING` (UNKNOWN, so ESCALATE) and the
  Executor writes nothing; a template without references needs no reader.
  The reader file must not be the Executor's kubeconfig, nor a link to it;
  `LiveGuardAuthorizer` itself refuses that, because the worker's Guard role
  builds it without `build_adapters`
  (`tests/test_live_executor.py::LiveExecutorTests.test_the_authorizer_itself_refuses_the_executor_kubeconfig_as_reader`).
  The reader's Role,
  `deploy/sentinel/guard-reader-rbac.yaml`, is generated from the protected
  shippingservice template and granted `get` on `configmap/security-upstream`
  and `secret/security-upstream` only
  (`experiments/boutique/harness/protected.py::guard_reader_rbac`). Since
  P5.06 it grants `get` on `configmap/security-trust-bundle`,
  `configmap/security-upstream` and
  `certificates.cert-manager.io/security-upstream`, and on no Secret (see
  [Certificate lifecycle](#certificate-lifecycle-p506)).
- **Authenticated Verifier probe.** A new client identity,
  `sentinel-verifier-client`, gets its certificate and Secret like the lab's
  other identities. The gateway's SAN allowlist admits it; the services' own
  TLS sidecars do not. The probe Pod mounts that Secret and the probe schema
  read-only, presents the certificate, checks `gateway.internal` and calls
  `security-gateway:8443`
  (`scripts/security/boutique_security_manifest.py::verifier_probe_pod`). Its
  label, `app=sentinel-verifier-probe`, is admitted at the gateway, and a new
  egress policy lets those Pods send only DNS and the gateway's two route
  ports. The lab's own validation adds `verifier_identity_quote`, an
  authenticated quote with the Verifier's identity, and its matrix gains that
  identity as a source (22 allowed cells, 18 sources by 15 destinations).
- **Protected harness.** `scripts/test_boutique_live.py --protected`
  (`experiments/boutique/harness/protected.py::ProtectedBoutiqueExperiment`)
  recovers the running lab's shippingservice from R1 with the connected
  harness's Guard, Executor and Verifier. It deploys nothing; it needs the
  lab's run folder (`--security-run`). The catalog has a `protected` scope for
  the security lab with shippingservice only. The run takes the heavy-lab
  lock, so it refuses while P4.01, P4.03, P4.05 or a P5.07 run holds it.
  The bypass probes run before the fault (at the end of the RBAC matrix),
  during it (after the fault, before the approved write) and after the
  repair. Each time point counts only beside an authenticated control call
  through the gateway that succeeds both before the gateway outage and after
  the gateway is restored, as the lab's own validation pairs its denials
  with `valid_mtls_quote`: the Verifier's shipping quote before and after
  the repair, and during the fault, which breaks shipping, the lab client's
  cart call (GetCart). A shipping Pod without an IP fails the check as not
  attempted. The run is not a PASS unless all three time points passed
  (`experiments/boutique/harness/protected.py::ProtectedBoutiqueExperiment.bypass_checks`,
  `tests/test_protected_recovery.py::ProtectedExperimentTests.test_refused_bypasses_count_only_beside_a_working_authenticated_call`,
  `tests/test_protected_recovery.py::ProtectedExperimentTests.test_the_full_run_checks_bypasses_before_during_and_after_the_repair`).
  During the fault R1 stops shipping's own server, so a refused direct call
  at that time point does not by itself show that the policy refused it; the
  before and after time points are the ones that show the policy.

### Why no existing check is weaker (owner sign-off)

These touch the Guard, the Executor's credentials and reads, a Verifier probe
and certificates, so they wait for an owner's review. The argument, with the
tests that hold it:

- Every earlier eligibility rule is unchanged and still runs on the sidecar.
  `ProtectedSidecarEligibilityTests` reruns the whole eligibility suite with
  the reviewed sidecar in the template, so a privileged sidecar is still
  `SECURITY_PRIVILEGED` and a host path still `SECURITY_HOST_ACCESS`
  (`tests/test_guard_eligibility.py::ProtectedSidecarEligibilityTests.test_a_privileged_sidecar_is_rejected`,
  `tests/test_guard_eligibility.py::ProtectedSidecarEligibilityTests.test_a_host_path_is_rejected`).
  A policy without `allowed_sidecars` behaves as before.
- The sidecar rule only adds FAILs. It never turns another finding into a
  PASS, and a malformed rule refuses the policy
  (`tests/test_guard_eligibility.py::ProtectedSidecarEligibilityTests.test_malformed_sidecar_rules_are_refused`).
- Pinning the command and arguments is not enough on its own: a lifecycle
  hook or an `exec` probe would run any shell inside the sidecar, beside the
  TLS key, so both are refused, as is every container field outside the
  reviewed set; no other container may mount or reference the sidecar's
  Secret
  (`tests/test_guard_eligibility.py::ProtectedSidecarEligibilityTests.test_a_lifecycle_hook_or_exec_probe_cannot_run_a_command_in_the_sidecar`,
  `tests/test_guard_eligibility.py::ProtectedSidecarEligibilityTests.test_unreviewed_sidecar_fields_are_rejected`,
  `tests/test_guard_eligibility.py::ProtectedSidecarEligibilityTests.test_no_other_container_may_read_the_sidecars_secret`,
  `tests/test_guard_eligibility.py::ProtectedSidecarDecisionTests.test_a_lifecycle_hook_or_exec_probe_is_rejected`).
- The reference reads moved from the Executor's identity to the reader's; no
  read became optional. When the reader is missing the evidence is absent,
  which the existing rule already makes UNKNOWN, and a `reference_versions`
  already in the snapshot is dropped rather than trusted
  (`tests/test_live_executor.py::LiveExecutorTests.test_reference_reads_never_use_the_executor_kubeconfig`,
  `tests/test_live_executor.py::LiveExecutorTests.test_reference_versions_already_in_the_snapshot_are_never_trusted`).
  One behaviour changed: a template with references that was ALLOWed by
  reading with the Executor's kubeconfig now ESCALATEs unless a reader is
  configured. No current live target has references, so no live run is
  affected; the Executor Role never granted Secret or ConfigMap reads, so
  such a run could not have passed live anyway.
- The Executor's Role is unchanged: `get` and `patch` on named Deployments.
  With the real reference adapter, every `kubectl get` of a ConfigMap or
  Secret names the reader's kubeconfig and context
  (`tests/test_live_executor.py::LiveExecutorTests.test_the_executor_credential_never_reads_a_secret`).
- The gateway accepts one more client identity. That identity reaches the
  same inspected listeners as the other callers, under the same route and
  inspection rules; no service's TLS sidecar accepts it
  (`tests/test_security_connected.py::ConnectedSecurityTests.test_the_verifier_identity_is_accepted_at_the_gateway_only`),
  and its Pods cannot reach any other port
  (`tests/test_protected_recovery.py::AuthenticatedProbeTests.test_the_probe_pod_reaches_only_the_gateways_route_ports`).
- A rollback restores the whole protected template, sidecar included, with
  one write; an extra container, another Envoy digest or a host path writes
  nothing
  (`tests/test_protected_recovery.py::ProtectedRollbackTests.test_one_write_restores_the_protected_template_byte_for_byte`,
  `tests/test_protected_recovery.py::ProtectedRollbackTests.test_an_extra_container_or_another_envoy_digest_writes_nothing`).

Residual risks, also in the threat model: until P5.06 the reader could
read the named Secret's key bytes (RBAC has no metadata-only `get`, and
kubectl fetches the whole object before selecting the UID and
resourceVersion; the bytes never reached Python). P5.06 removed that read:
the reader now gets the cert-manager Certificate that owns the Secret, which
holds no key material. The token lasts one hour. The run removes the reader's
kubeconfig file and deletes its ServiceAccount, Role and RoleBinding at the
end, which revokes the token at once; `summary.json` records
`guard_reader_rbac_deleted`, and when that is not `true` delete them by
hand (below). The
Verifier's client key is a Secret that any Pod in the namespace could mount.
The sidecar rule pins the image, command, arguments and mounts and refuses
lifecycle hooks, `exec` probes and unreviewed fields, but it does not pin
ports, network-probe timing or resources; the resource and privilege rules
still apply, with the raised memory-limit ceiling above. Secret and
ConfigMap volumes are compared by kind and name, not by `items`,
`optional` or `defaultMode`.

### Live recipe (P5.05; staged)

Run on the security lab cluster only, from a new run folder, and never while
another heavy run is going: P4.01, P4.03, P4.05, P5.07 and this run each take
most of Docker's 15 GB, and the harness refuses while the heavy-lab lock is
held. Since P5.06 the lab's identities are issued by cert-manager at `start`
(two-hour certificates renewed an hour before expiry), and `start` refuses
until the cert-manager pins are resolved (Certificate lifecycle, below).
`python scripts/security/protected_recovery.py plan` prints
these commands.

```powershell
# Stage (a), offline: the changes and the reviewed Guard-reader RBAC.
python -m unittest tests.test_guard_eligibility tests.test_live_executor tests.test_security_connected tests.test_protected_recovery
python scripts/security/protected_recovery.py rbac --check

# Start the lab with this revision (it creates the Verifier identity), then check the preconditions.
$lab = "data/experimentation/runs/protected-recovery-lab"
python scripts/security/boutique_security.py start --directory $lab --exit-after-test
python scripts/security/protected_recovery.py preflight --security-run $lab --stage baseline

# Stage (b), baseline only: protected baseline, RBAC can-i matrix and the bypass probes; no fault.
python scripts/test_boutique_live.py --protected --security-run $lab --protected-stage baseline --run-dir data/experimentation/runs/protected-recovery-baseline

# Stage (c), full recovery: R1 on the protected shippingservice, one approved write, authenticated verification.
python scripts/test_boutique_live.py --protected --security-run $lab --protected-stage full --run-dir data/experimentation/runs/protected-recovery

# Afterwards: the security-only launcher still passes on its own, then stop the lab.
python scripts/security/boutique_security.py test --directory $lab --exit-after-test
python scripts/security/boutique_security.py stop --directory $lab
```

The preflight refuses, with one reason per line, unless the lab's
`validation.json` passed and includes `verifier_identity_quote`, the run
folder's `session.json` names `sentinel-boutique-security`, the catalog's
`protected` scope is that lab with shippingservice only, the committed
Guard-reader RBAC matches the reviewed template, Envoy and grpcurl are pinned
by digest, no reviewed file has a `TODO-PIN`, the cert-manager pins are
resolved and the lab's `session.json` says its identities come from
cert-manager (P5.06), and the heavy-lab lock is free.
The harness runs the same preflight before its first cluster command.

Expected, stage (b): `PASS: protected baseline captured; ...`. The baseline's
template holds `shipping-ingress` with the reviewed Envoy digest; its
`reference_versions` hold `ConfigMap/security-trust-bundle` and
`ConfigMap/security-upstream`, and its `certificates` hold the
`security-upstream` Certificate's status (P5.06), all read by the Guard
reader. `protected-rbac-checks.json` has every row as expected: the Executor
gets and patches `deployment/shippingservice` and gets no Secret, ConfigMap
or Certificate (`can-i get secrets`, `get secret/security-upstream`, `get
configmap/security-upstream` and `get
certificates.cert-manager.io/security-upstream` all `no`); the reader gets
the two ConfigMaps and `certificates.cert-manager.io/security-upstream` and
nothing else (`secret/security-upstream`, `secret/security-gateway`,
`secrets`, `list secrets`, `list certificates.cert-manager.io` and the
Deployment all `no`). Neither identity may mint a lab identity (P5.06):
for both, `create certificates.cert-manager.io` and `create
certificaterequests.cert-manager.io` in the lab namespace and with
`--all-namespaces`, `get secret/sentinel-lab-ca` and `get
secret/sentinel-lab-ca-2` in the `cert-manager` namespace, and `patch
clusterissuers.cert-manager.io/sentinel-lab-ca` are all `no`; those rows
carry their `namespace` in the file. `bypass-before.json` shows the plaintext Service, the
Pod IP and the gateway outage all refused, and its `positive_control`
shows the Verifier's authenticated quote succeeding before the outage and
after the gateway is restored.

Expected, stage (c): `PASS: protected template recovered with one approved
write; ...`. The policy carries `allowed_sidecars` for shipping-ingress; the
three negative cases make zero writes; the Coordinator Guard and the
Executor's own check both ALLOW with the reader's reference versions;
`transport-stats.json` shows one write; `outcome.json` is RECOVERED from
samples taken by `sentinel-verifier-probe` Pods through the gateway;
`bypass-before.json`, `bypass-during.json` (fault in place) and
`bypass-after.json` all show every bypass attempted and refused and both
positive controls succeeded (the lab client's cart call in
`bypass-during.json`); the run folder's `telemetry.sqlite3` holds inspector
events in the action window; and `summary.json` records both private
kubeconfigs removed, `bypass_checks` true for before, during and after, and
`guard_reader_rbac_deleted` true. If it is not true, delete the reader by
hand: `kubectl --context kind-sentinel-boutique-security delete -f
deploy/sentinel/guard-reader-rbac.yaml --ignore-not-found`. A Guard REJECT with
`SECURITY_SIDECAR_*`, or an ESCALATE with
`BASELINE_REFERENCE_EVIDENCE_MISSING`, is the protected rules failing
closed: record it as such, not as a recovery. The `--dashboard` view, the
crash-reconciliation case and the base harness's conditional-race case are
not part of this run yet: the protected run's acceptance is one write.

### Owner sign-offs

- the Guard rule `allowed_sidecars`, its codes and the argument above;
- the reference reads moving to the Guard-reader identity (its residual
  Secret read is removed by P5.06, signed off separately below);
- the Verifier client identity at the gateway and its probe policy. Its
  SAN is admitted on both gateway listeners (shipping 8443 and cart 8445)
  and its egress allows both route ports, so its key could also call the
  cart route, although the probe only calls the shipping quote. Narrowing it
  to the shipping listener and port 8443 is left to the owner;
- the protected run's 192Mi container memory-limit ceiling: it lets the
  shipping container be restored with up to 192Mi as well, because the Guard
  has no per-container ceilings;
- the `protected` catalog scope (R1's labels are unchanged; only its scopes
  gain `protected`). The runtime LabControl scopes now include the security
  cluster and shippingservice through it; `--protected` refuses
  `--dashboard` and `--ui-controls`, so no UI path reaches it today.

## Certificate lifecycle (P5.06)

Until P5.06 the lab's certificates were two-day OpenSSL fixtures made at
`start`, never rotated, and Envoy read them from static file paths. Issue
#70 asks for one certificate framework; P5.06 picks cert-manager for the
Kubernetes security lab, loads every key pair through Envoy's file-based SDS
so a renewal needs no restart, and makes expired, untrusted or misnamed
identities escalate (`L-LAB-CERTS` in [limitations](limitations.md)).
The standalone compose lab (`scripts/security/security_lab.py`) uses the same SDS configuration but keeps
its two-day OpenSSL certificates.

### Live results (14 Nov 2026)

Smoke only, `smoke-certs` in `p5-security-20261114T171037Z`:

- Rotation passed: gateway serial changed; gateway and frontend Certificate
  revisions advanced 2 to 3; Pod identities/restart counts stayed unchanged;
  checkout passed 79/79 samples. The record does not show all eight revisions
  advancing in that comparison.
- Untrusted-CA, wrong-SAN and expired clients were refused, 3/3. These are
  gateway refusals only; they create no incident or live Planner escalation.
- CA rollover passed: old CA accepted during overlap, new CA accepted after
  switch, old CA refused after finish; Pod identities/restart counts unchanged,
  checkout 323/323 samples passed.
- Expiry escalation ran once in smoke, not in every repeat. With the controller
  stopped and about 1,742 s left, identity inspection escalated; the Guard
  report had SECURITY_IDENTITY_EXPIRING and ESCALATE, with zero Executor write
  attempts (`identity-expiring.json`: passed). The enclosing recovery harness
  returned FAILED because it expected approval before execution; operator
  emergency restore ran. No fix commit is recorded for this harness mismatch.
  Restored identities subsequently passed.

Startup failures: Jetstack's default-User-Agent 403 was fixed by ac8ac91;
missing Envoy SDS node id/cluster by 02d40be. The valid-cart Kestrel `:scheme`
error was fixed by e8d6fed; the SIGINT outage race was replaced in the scratch
runner by SIGSTOP/SIGCONT, without a recorded commit (details under [P5.04](#cart-route-and-inspector-capacity-p504)).
Pins were lab-resolved at 7ac2752; owner review remains pending. Rotation, the three
client refusals and the CA rollover passed in all four rounds (smoke and
r1-r3, `p5sec-p506-*`); the expiry escalation ran in the smoke round only. One
Docker host, unscored;
wrong upstream SAN/issuer Guard escalation remains offline-only.

### What changed

- **One issuer.** `boutique_security.py start` makes a lab CA in the run
  folder's private `pki` directory (the key is owner-only, through
  `services/guard/preview._private_file`), installs the pinned cert-manager
  chart with Helm, seeds a CA ClusterIssuer `sentinel-lab-ca` with that CA
  and requests one Certificate per lab identity
  (`scripts/security/cert_lifecycle.py::certificates`): Secret
  `security-<identity>`, the exact DNS SAN `<identity>.internal`, server or
  client usage, a two-hour lifetime renewed an hour before expiry, and a new
  private key on every renewal (`rotationPolicy: Always`).
  `--profile rotation` renews every ten minutes instead (one-hour lifetime,
  renewed 50 minutes before expiry): cert-manager refuses a lifetime under
  one hour, so the design's 10-minute certificate cannot come from
  cert-manager, and the expired test identity below is made locally. No key
  enters `manifest.json`, `certificates.json` or the repository; the CA
  Secret and test-identity Secrets are written to owner-only files in the
  run folder and applied from there.
- **File-based SDS.** Every Envoy (callers' egress sidecars, the services'
  ingress sidecars, the gateway) names its key pair in
  `tls_certificate_sds_secret_configs` and its trusted CAs in
  `combined_validation_context.validation_context_sds_secret_config`, each an
  SDS subscription to a file in its own configuration ConfigMap with
  `watched_directory` on `/config`
  (`scripts/security/security_lab.py::tls_context`,
  `scripts/security/security_lab.py::sds_resource`). The SDS files name
  paths only: `/pki/<identity>/tls.crt` and `tls.key`, watched on
  `/pki/<identity>`, and `/trust/ca.crt`, watched on `/trust`. A directory
  is watched, not a file, because the kubelet updates a Secret or ConfigMap
  volume by swapping a symlinked directory. Identity Secrets are mounted as
  whole read-only volumes, never with `subPath`, which the kubelet never
  updates. The SAN matchers stay exact and in the static configuration, so
  a renewed file cannot change which peers are accepted. Each Envoy's
  access log adds `peer_serial` (the caller certificate's serial) and
  `upstream_tls` (the reason a TLS connection to its upstream failed).
- **Trust bundle and CA rollover.** Every Envoy also mounts the public
  ConfigMap `security-trust-bundle` at `/trust`. A rollover has three steps
  (`scripts/security/cert_lifecycle.py::rollover`): `start` makes a second
  CA and puts both CAs in the bundle; `switch` points the ClusterIssuer at
  the new CA and deletes each identity Secret so cert-manager reissues it;
  `finish` leaves only the new CA in the bundle. The overlap lasts from
  `start` until every certificate is reissued and every Envoy has reloaded;
  after `finish`, a certificate from the old CA is refused.
- **The Guard reads Certificates, not Secrets.** A new rule,
  `security_constraints.certificate_identities`, names each reviewed
  identity: its Secret, its Certificate, the issuer, the DNS names and
  `min_remaining_seconds` (1800 in the protected run)
  (`services/guard/eligibility.py::_certificate_identities`). For every
  such Secret the template mounts, the Guard needs the Certificate's status
  from the trusted reader (`live_state.certificates`,
  `services/guard/adapters.py::read_certificate_status`): Ready for its
  current generation, notBefore in the past, at least the margin left
  before notAfter, and exactly the reviewed Secret, issuer and DNS names.
  Each check that cannot pass is UNKNOWN, so the decision is ESCALATE and
  nothing is written: `SECURITY_IDENTITY_EVIDENCE_MISSING`,
  `SECURITY_IDENTITY_NOT_READY`, `SECURITY_IDENTITY_EXPIRING` (expired or
  inside the margin), `SECURITY_IDENTITY_NOT_YET_VALID` and
  `SECURITY_IDENTITY_SPEC`. Malformed status is
  `BASELINE_ELIGIBILITY_INVALID` (FAIL, REJECT). A rollback restores the
  template, not the identity, so none of these is something a rollback
  could repair. For a Secret that a reviewed Certificate owns, the
  reference check compares the Certificate's UID and generation with the
  baseline's instead of the Secret's resourceVersion (see the argument
  below). The Executor's check, the Coordinator's Guard state and the Guard
  CLI read the Certificate with the reader identity and never read the
  Secret (`runtime/live_executor.py::LiveGuardAuthorizer._attach_reference_versions`).
  The reader's Role grants `get` on the two ConfigMaps and
  `certificates.cert-manager.io/security-upstream` only
  (`deploy/sentinel/guard-reader-rbac.yaml`).
- **Observer and Planner.** The Observer labels a collected log line that
  carries a TLS certificate refusal (verify failed, expired, unknown CA,
  bad certificate, SAN mismatch) `CertificateRejected`
  (`services/observer/core.py::_log_reason`). The Planner escalates any
  incident with that signal by a code rule, before any provider or model
  call, with a fixed rationale and `runbooks/identity-failure.md`; ROLLBACK,
  RESTART and SCALE are never proposed for it
  (`services/planner/core.py::certificate_rejected`). The evaluation counts
  that ESCALATE as a rule decision.
- **Pins.** `deploy/cert-manager/pins.json` pins cert-manager v1.18.2 by its
  chart URL and SHA-256 and its five images by digest. Each unresolved value
  is `TODO-PIN`; `python scripts/security/cert_lifecycle.py pins` prints the
  commands that resolve them on a networked machine (`curl` and
  `Get-FileHash` for the chart, `docker buildx imagetools inspect` for each
  image). `boutique_security.py start` and the protected-recovery preflight
  refuse until none is left, before any cluster command. Only digests reach
  the Helm values (`scripts/security/cert_lifecycle.py::helm_values`), and
  the chart is checked against its SHA-256 before Helm sees it. The version
  and pins were lab-resolved at 7ac2752 on 14 Nov 2026; owner review is pending.

### Why no existing check is weaker (owner sign-off)

The rules for this are in the roadmap: a safety-critical change needs a
written argument, tests that fail before and pass after, and an owner's
sign-off. The new tests are in `tests/test_security_lab_context.py`,
`tests/test_guard_eligibility.py` (`CertificateIdentityEligibilityTests`,
`CertificateIdentityDecisionTests`), `tests/test_guard_references.py`,
`tests/test_observer.py`, `tests/test_planner.py`,
`tests/test_live_executor.py` and `tests/test_protected_recovery.py`.

- A policy without `certificate_identities` behaves exactly as before: a
  Secret is still compared by its own UID and resourceVersion
  (`tests/test_guard_eligibility.py::CertificateIdentityEligibilityTests.test_without_the_rule_the_secret_is_still_compared_by_its_own_version`).
  `CertificateIdentityEligibilityTests` reruns the whole eligibility and
  sidecar suites with the rule configured.
- One comparison changes, on purpose. For a Secret that a reviewed
  Certificate owns, the reference check now compares the Certificate's UID
  and generation instead of the Secret's resourceVersion. A renewal writes
  a new key and certificate into the Secret, which moves its
  resourceVersion; under the old check every renewal between the baseline
  and the repair would have refused the repair, and renewals happen every
  hour. The Certificate's UID changes only when it is replaced, and its
  generation only when its spec (issuer, names, Secret, lifetime) changes;
  both still FAIL the check
  (`tests/test_guard_eligibility.py::CertificateIdentityEligibilityTests.test_a_replaced_or_respecified_certificate_is_a_changed_reference`,
  `tests/test_protected_recovery.py::ProtectedRollbackTests.test_a_replaced_certificate_is_a_changed_reference`).
  What the Guard no longer sees is a change to the Secret's content that
  leaves the Certificate alone. That is covered in two ways: the new rule
  requires the certificate to be Ready, unexpired and the reviewed identity,
  and cert-manager's readiness checks are meant to set Ready to False when
  the issued Secret no longer matches its Certificate (not yet observed
  live here). The residual risk is trusting cert-manager's readiness check
  for the Secret's content; the threat model records it.
- The trusted CAs are a ConfigMap reference compared exactly as before, so
  a CA rollover is a changed reference and refuses a repair until a new
  baseline is captured.
- The new rule adds UNKNOWN findings and, for malformed status, a FAIL. It
  never turns another finding into a PASS. An identity failure ESCALATEs
  even with a valid approval, because an approval cannot clear an
  eligibility finding
  (`tests/test_guard_eligibility.py::CertificateIdentityDecisionTests.test_an_expiring_or_not_ready_identity_escalates_even_when_approved`).
- The reader lost its only Secret permission. Neither the Executor nor the
  reader can read any Secret, and with the real adapters no `kubectl`
  call names one
  (`tests/test_live_executor.py::LiveExecutorTests.test_with_the_real_adapters_no_kubectl_call_names_a_secret`,
  `tests/test_protected_recovery.py::GuardReaderTests.test_the_can_i_matrix_denies_every_secret_read_once_certificates_own_them`).
  Status already in a snapshot is dropped, never trusted
  (`tests/test_live_executor.py::LiveExecutorTests.test_certificate_status_already_in_the_snapshot_is_never_trusted`).
- Envoy keeps every TLS rule it had: client certificates are required, the
  SAN matchers are the same exact names in the static configuration, and
  the minimum version is unchanged. Trust now comes from the bundle instead
  of each identity's own `ca.crt`; outside a rollover the bundle is the
  same single lab CA. During a rollover, two CAs are trusted on purpose.
- The Planner rule only adds an ESCALATE before any provider call; without
  the signal, the same incident still reaches the provider
  (`tests/test_planner.py::IdentityFailureTests.test_without_the_signal_the_same_incident_is_a_rollback`).

Residual risks, also in the threat model: the lab CA key sits in the run
folder (owner-only) for the lab's life, so anyone who can read that folder
can mint a trusted identity. The same key is also inside the cluster.
`issue` applies it as Secret `sentinel-lab-ca` (`tls.crt` and `tls.key`) in
the `cert-manager` namespace, and `rollover switch` applies the second CA as
`sentinel-lab-ca-2` there (`scripts/security/cert_lifecycle.py::issue`,
`scripts/security/cert_lifecycle.py::rollover`). The issuer is a
cluster-scoped ClusterIssuer, and cert-manager's default approver approves
every request. So any principal that may create a Certificate or a
CertificateRequest in any namespace, get either CA Secret, or edit the
ClusterIssuer can mint, for example, `client.internal` or the Verifier's
SAN, and the gateway's exact SAN allowlist accepts it. Before P5.06 the CA
key never entered the cluster. Why a ClusterIssuer is still the lab's
choice (owner sign-off below):

- In the lab, those rights belong to the cluster admin and to cert-manager's
  own controllers. Sentinel's identities hold none of them: the protected
  run's can-i matrix asserts `no` for the Executor and the Guard reader on
  `create` Certificates and CertificateRequests, in the lab namespace and
  in all namespaces, on `get` of both CA Secrets in `cert-manager`, and on
  `patch` of the ClusterIssuer
  (`experiments/boutique/harness/protected.py::rbac_matrix`,
  `tests/test_protected_recovery.py::GuardReaderTests.test_neither_identity_may_mint_a_lab_identity_or_read_a_ca_key`).
  No other principal in the cluster has been checked live.
- A namespaced CA Issuer would need the CA Secret in the lab namespace
  itself. There, anyone who may create a Pod in the namespace could mount
  it, which is a wider group than those who may read Secrets in
  `cert-manager`. Every workload identity Secret is already in that
  namespace.
- cert-manager's approver-policy, limited to the lab namespace and the
  reviewed DNS names, is the stronger control. It adds another component to
  pin and review, and nothing in Sentinel creates a CertificateRequest. It
  is the owner's call for anything beyond the lab.

While the cert-manager controller is down,
a Certificate's Ready condition is stale, but the Guard checks notAfter
itself. A `CertificateRejected` line labels evidence; it does not make an
incident on its own. The gateway's log, where an upstream refusal appears,
is not collected for a recovery target. The test identities' keys are
Secrets that any Pod in the namespace could mount, like the Verifier's.
Trust now lives in the ConfigMap `security-trust-bundle` rather than in
each identity's Secret, so anyone who may write ConfigMaps in the
namespace can add a CA that every Envoy trusts after its next reload; in
the lab only the cluster admin can. The Guard compares the bundle as a
reference, so a changed bundle refuses a repair, but nothing stops Envoy
from loading it. The same holds for each Envoy's own configuration
ConfigMap, which carries its SDS files and is hot-reloaded. For repeated
fields, `combined_validation_context` merges the SDS validation context
into the static one, so a principal that may write that ConfigMap can add
a trusted CA or a SAN matcher without a restart. The Observer's
`CertificateRejected` rule matches any collected log line with a TLS
refusal. A workload that logs `certificate verify failed` therefore makes
the Planner escalate, even when a rollback could have repaired a bad
rollout that broke a TLS client. That fails safe (no write) but suppresses
the repair.

### Live recipe (P5.06)

Run on the security lab only, from a new run folder, never alongside another
heavy run. `python scripts/security/cert_lifecycle.py plan` prints these
commands.

```powershell
# 0. On a networked machine: resolve the pins, edit deploy/cert-manager/pins.json, then check.
python scripts/security/cert_lifecycle.py pins
python scripts/security/cert_lifecycle.py pins --check

# 1. Start the lab (the rotation profile renews every ten minutes; the default renews every hour).
$lab = "data/experimentation/runs/cert-lifecycle-lab"
python scripts/security/boutique_security.py start --directory $lab --profile rotation --exit-after-test
python scripts/security/cert_lifecycle.py identity

# 2. Rotation without a restart. Record restart counts, Certificate revisions and the gateway's served serial.
kubectl --context kind-sentinel-boutique-security -n sentinel-boutique-security get pods -o jsonpath="{range .items[*]}{.metadata.name}{' '}{.status.containerStatuses[*].restartCount}{'\n'}{end}"
kubectl --context kind-sentinel-boutique-security -n sentinel-boutique-security get certificates.cert-manager.io -o jsonpath="{range .items[*]}{.metadata.name}{' '}{.status.revision}{' '}{.status.notAfter}{'\n'}{end}"
# In a second terminal, keep a port-forward open; the gateway sends its certificate before it asks for the client's.
kubectl --context kind-sentinel-boutique-security -n sentinel-boutique-security port-forward service/security-gateway 18443:8443
$null | openssl s_client -connect 127.0.0.1:18443 -servername gateway.internal 2>$null | openssl x509 -noout -serial
kubectl --context kind-sentinel-boutique-security -n sentinel-boutique-security delete secret security-gateway security-frontend-client --wait=false
kubectl --context kind-sentinel-boutique-security -n sentinel-boutique-security wait --for=condition=Ready certificates.cert-manager.io --all --timeout=300s
# Wait about 90 s for the kubelet to update the volumes, then repeat the three reads and check out in the storefront.
python scripts/security/boutique_security.py probe --directory $lab --route shipping

# 3. Refused client identities: an untrusted CA, a SAN the gateway's allowlist does not name (signed by the lab CA)
#    and a ten-minute certificate that has already expired. The gateway refuses them; no incident and no escalation follow.
python scripts/security/cert_lifecycle.py negatives --directory $lab
python scripts/security/boutique_security.py probe --directory $lab --identity negative-untrusted-ca
python scripts/security/boutique_security.py probe --directory $lab --identity negative-wrong-san
python scripts/security/boutique_security.py probe --directory $lab --identity negative-expired

# 4. An expiring identity escalates with zero writes. Stop the controller, wait until the
#    upstream certificate has under 30 minutes left (rotation profile: 30 to 60 minutes after
#    its last renewal), then run the protected recovery.
kubectl --context kind-sentinel-boutique-security -n cert-manager scale deployment/cert-manager --replicas=0
python scripts/security/cert_lifecycle.py identity
python scripts/test_boutique_live.py --protected --security-run $lab --protected-stage full --run-dir data/experimentation/runs/identity-expiring
kubectl --context kind-sentinel-boutique-security -n cert-manager scale deployment/cert-manager --replicas=1

# 5. CA rollover with an overlap (run after step 3: the test identities trust only the first CA).
python scripts/security/cert_lifecycle.py rollover start --directory $lab
python scripts/security/boutique_security.py probe --directory $lab --identity negative-old-ca
python scripts/security/cert_lifecycle.py rollover switch --directory $lab
# Wait about 90 s for the kubelet to update every Secret volume before probing: a probe Pod trusts only the
# ca.crt in its own Secret, so it can briefly refuse a gateway still serving an old-CA certificate.
python scripts/security/boutique_security.py probe --directory $lab --route shipping
python scripts/security/cert_lifecycle.py rollover finish --directory $lab
python scripts/security/boutique_security.py probe --directory $lab --identity negative-old-ca
```

Expected. Step 1: `identity` exits 0 and every Certificate is Ready.
Step 2: every container's restart count is unchanged; the two Certificates'
revisions went up by one; the gateway serves a new serial; the gateway's
access log shows a new `peer_serial` for frontend's calls; checkout and the
probe succeed throughout (a short gap while the kubelet syncs is a finding
to record, not to retry away). Step 3: each probe exits 1, and that is
all the step shows. The plan asks that these three certificates be
"refused and escalate with 0 writes". For a client identity, the recipe
narrows that to refusal by the gateway (owner sign-off below), for two
reasons. First, a refused client makes no incident: no Deployment is
unhealthy, so there is nothing for the Observer to open, the Planner to
escalate or the Executor to write. Second, the refusal is not visible as
`CertificateRejected` anywhere live. The gateway's HTTP access log records
requests, and a client whose handshake fails never makes one. Envoy's own
handshake-failure messages are expected at debug level, below the lab's
`--log-level warn` (not observed here). The gateway's log is not collected
for a recovery target either. The Observer's `CertificateRejected` tests
use synthetic log lines, so the Observer-to-Planner identity rule has no
live path until a listener-level access log with
`%DOWNSTREAM_TRANSPORT_FAILURE_REASON%` is added and the gateway's log is
collected (deferred). `negative-wrong-san` is signed by the lab CA with a
SAN that the gateway's allowlist does not name, so it also stands for an
identity outside the allowlist. Removing a name from a running gateway's
allowlist is not part of the recipe. Step 4: once the
margin is crossed, `identity` exits 1 with `SECURITY_IDENTITY_EXPIRING` for
`security-upstream`; the protected run's Guard decision is ESCALATE with
that code, `transport-stats.json` shows zero writes, and the run is recorded
as the identity rule failing closed, not as a recovery. Left longer, the
certificate expires and the gateway's access log shows `upstream_tls` with a
verify failure, which the Observer would label `CertificateRejected` if that
log were collected (it is not, for a recovery target). Step 5: the old-CA
probe succeeds after `start`, every probe and checkout succeed after
`switch` with every Certificate reissued from the new CA, and the old-CA
probe exits 1 after `finish`; restart counts are unchanged throughout.
A wrong SAN or an untrusted issuer on the upstream Certificate itself (patch
its `dnsNames` or `issuerRef`) is refused by the gateway and gives
`SECURITY_IDENTITY_SPEC` in `identity`; a protected run cannot reach the
Guard in that state, because its baseline capture already fails through the
gateway, so the Guard's decision for those two cases is shown offline only
(`tests/test_protected_recovery.py::ProtectedRollbackTests.test_an_identity_that_cannot_serve_escalates_and_writes_nothing`:
ESCALATE with `SECURITY_IDENTITY_SPEC` and no write). So, live, the
plan's "escalate with 0 writes" is shown for the upstream's own identity
and for expiry only (step 4, reached by stopping the controller, on the
rotation profile). Injecting a wrong SAN or issuer after the baseline is
captured would need a harness step that does not exist, and is deferred.
The design's 10-minute certificate renewed 5 minutes early cannot come from
cert-manager, which refuses a lifetime under one hour. The rotation profile
(one hour, renewed 50 minutes early, so a renewal every ten minutes)
replaces it for rotation, and a locally signed certificate that expired a
minute ago, valid for ten minutes, replaces it for the expired client.

### Owner sign-offs

- the Guard rule `certificate_identities`, its codes, ESCALATE (not REJECT)
  for an identity that cannot serve, and the 30-minute margin;
- comparing an owned Secret through its Certificate's UID and generation, so
  a renewal is not a changed reference, and trusting cert-manager's
  readiness for the Secret's content (the argument above);
- the trust bundle as a reviewed ConfigMap, and the rollover policy: both CAs
  until every certificate is reissued, then the new one only, with a new
  baseline after a rollover;
- adding cert-manager v1.18.2 to the security lab and reviewing the six
  lab-resolved pins (7ac2752); smoke used them, owner review remains pending;
- the Planner's identity rule and `runbooks/identity-failure.md`;
- the lab CA key and the test keys living in the run folder for the lab's
  life;
- the lab CA key inside the cluster, as Secrets `sentinel-lab-ca` and
  `sentinel-lab-ca-2` in the `cert-manager` namespace, behind a
  cluster-wide ClusterIssuer that cert-manager's default approver signs for
  any namespace. Anyone who may create Certificates or CertificateRequests
  anywhere, read those Secrets or edit the issuer can mint an identity the
  gateway accepts. The argument above is for keeping a ClusterIssuer in the
  lab rather than a namespaced Issuer or approver-policy, and the can-i
  rows say that neither Sentinel identity can mint one;
- the narrowed live verification. For the untrusted-CA, wrong-SAN and
  expired client identities, the plan's "refused and escalate with 0
  writes" becomes refusal by the gateway only: a refused client makes no
  incident, and its handshake failure reaches no collected log. The
  Planner's `CertificateRejected` rule has no live path until the gateway's
  log, with a listener-level access log, is collected. For the upstream's
  own identity, ESCALATE with 0 writes is shown live for expiry only
  (step 4). For a wrong SAN or an untrusted issuer it is shown offline only;
- the rotation profile (one-hour certificates renewed 50 minutes early) and
  the locally signed, already-expired ten-minute certificate replacing the
  design's 10-minute cert-manager profile, which cert-manager refuses.
