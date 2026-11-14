# Optional protected-workload experiments

Security code is retained as a separate, opt-in experiment. From P5.05 the code
also runs the Sentinel guarded recovery chain on the protected template (see
[Protected recovery](#protected-recovery-p505)); that has been checked offline
only, not in a live run. The current baseline
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
gateway Pod leaves both gateway Services, and callers are refused. The two
routes share fate: one gateway Pod serves both, so a cart flood that keeps
all 16 slots busy at two probe checks in a row also takes shipping (and so
checkout) out of service for at least one probe period. That fails closed, as
E10 asks, but it widens an outage; whether a busy inspector should mark the
Pod unready at all, or only an unwritable journal should, is an owner
decision. The defaults are not measured against storefront latency yet. One
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
# 4. Stop the inspector process (SIGINT ends serve_forever; the container restarts). Until /readyz
#    answers again, cart and shipping calls through the gateway are refused.
kubectl $ctx $ns exec deployment/security-gateway -c inspector -- python -c "import os, signal; os.kill(1, signal.SIGINT)"
python scripts/security/boutique_security.py probe --route cart --directory $run
python scripts/security/boutique_security.py probe --route shipping --directory $run
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

Step 3: the invalid probe exits 1, the valid one 0. Step 4: after the SIGINT
the first two probes exit 1 (the inspector is restarting) and the last exits
0. These probes race the container restart: a probe that starts after the
inspector is back can succeed, so repeat step 4 if both succeeded and note
it.

Record the outputs and timings with the run, then collect evidence as in the
NetworkPolicy recipe above and stop the lab. What this can show, on one kind
node on one host: step 1, how Envoy answered cart calls at the load the
storefront reached, and that no request reached cartservice without an
inspector ALLOW in that window; step 2, the inspector's own bound; step 4,
that calls are refused while the inspector restarts. It is not a capacity
figure for real machines.

Image scan/candidate workflows remain explicit. A candidate needs reviewed
metadata and adjacent passing scan evidence; a scan does not grant repair
authorization. The [CVE gate](#cve-gate-p502) below is what turns scans into
Guard and admission decisions, and `image_inventory.py` feeds the monitor the
digests that are running; both are tested offline only. `--help` on
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

Two gaps remain. Only the shipping path has been shown live with mTLS and
inspection; the cart route exists in code with offline tests only, and the
cart-to-redis-cart edge stays plaintext and uninspected. The other services
have L4 default-deny NetworkPolicies in the security lab only, shown by one
live run on one kind node, and their traffic is still plaintext; the
connected recovery cluster applies none of these policies.
Workload images are
scanned and raise dashboard alerts, and a CVE gate now exists, but no live run
has used it: the 11 November evaluation found High or Critical findings in 11
of 13 images. See `L-SECURITY-SHIPPING-ONLY` and `L-UNGATED-CVES` in
[limitations](limitations.md).

Lab certificates are short-lived fixtures, not a rotation/revocation/workload-identity
system. The inspector's capacity is bounded in code (P5.04) but not measured
live, and one gateway replica stays a single point of failure.
Current-image reconciliation remains tracked work, and the protected
Guard/Executor/Verifier integration (P5.05) has not run live. Keep credentials,
private keys and unsanitised logs out of Git and exported image contexts.

## CVE gate (P5.02)

Status: built and tested offline only. No committed live policy turns the gate
on, the admission policy has never been evaluated by an API server, and the
live recipe below has not run. Offline tests are not a live result.

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

### Live recipe (not run)

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

- the Guard change: the gate block, its codes and the argument above, before
  any live policy sets it;
- the Guard image change: `deploy/docker/guard.Dockerfile` now copies
  `services/image_security.py` and `runtime/image_scans.py`;
- the admission policy, before step 5 switches it to Deny;
- non-regression as the default mode, and which targets move to strict mode
  (P5.03);
- every CVE exception: a human signs each one, with a role, rationale and at
  most 30 days (P5.03). The agent prepares none.

## Protected recovery (P5.05)

Until P5.05 the security lab and guarded recovery never met (#69): the
Executor read Secret and ConfigMap versions with its own kubeconfig
(`runtime/live_executor.py::LiveGuardAuthorizer`), the Verifier's plaintext
probe is refused by the lab's NetworkPolicies, and the Guard knew nothing of
the TLS sidecar in the protected template. P5.05 adds the pieces offline.
None of it has run against a cluster yet (`L-PROTECTED-RECOVERY-OFFLINE` in
[limitations](limitations.md)).

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
  The reader file must not be the Executor's kubeconfig. The reader's Role,
  `deploy/sentinel/guard-reader-rbac.yaml`, is generated from the protected
  shippingservice template and grants `get` on `configmap/security-upstream`
  and `secret/security-upstream` only
  (`experiments/boutique/harness/protected.py::guard_reader_rbac`).
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

Residual risks, also in the threat model: the reader can read the named
Secret's key bytes (RBAC has no metadata-only `get`, and kubectl fetches the
whole object before selecting the UID and resourceVersion; the bytes never
reach Python, and the one-hour token is deleted when the run ends). P5.06
replaces these Secret reads with cert-manager Certificate status reads. The
Verifier's client key is a Secret that any Pod in the namespace could mount.
The sidecar rule pins the image, command, arguments and mounts and refuses
lifecycle hooks, `exec` probes and unreviewed fields, but it does not pin
ports, network-probe timing or resources; the resource and privilege rules
still apply, with the raised memory-limit ceiling above. Secret and
ConfigMap volumes are compared by kind and name, not by `items`,
`optional` or `defaultMode`.

### Live recipe (staged; not run)

Run on the security lab cluster only, from a new run folder, and never while
another heavy run is going: P4.01, P4.03, P4.05, P5.07 and this run each take
most of Docker's 15 GB, and the harness refuses while the heavy-lab lock is
held. The lab's two-day certificates are made at `start`, so start the lab
for each session. `python scripts/security/protected_recovery.py plan` prints
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
by digest, no reviewed file has a `TODO-PIN`, and the heavy-lab lock is free.
The harness runs the same preflight before its first cluster command.

Expected, stage (b): `PASS: protected baseline captured; ...`. The baseline's
template holds `shipping-ingress` with the reviewed Envoy digest, and its
`reference_versions` hold `ConfigMap/security-upstream` and
`Secret/security-upstream`, read by the Guard reader.
`protected-rbac-checks.json` has every row as expected: the Executor gets and
patches `deployment/shippingservice` and gets no Secret or ConfigMap
(`can-i get secrets`, `get secret/security-upstream` and `get
configmap/security-upstream` all `no`); the reader gets
`secret/security-upstream` and `configmap/security-upstream` and nothing
else (`secret/security-gateway`, `secrets`, `list secrets` and the
Deployment all `no`). `bypass-before.json` shows the plaintext Service, the
Pod IP and the gateway outage all refused.

Expected, stage (c): `PASS: protected template recovered with one approved
write; ...`. The policy carries `allowed_sidecars` for shipping-ingress; the
three negative cases make zero writes; the Coordinator Guard and the
Executor's own check both ALLOW with the reader's reference versions;
`transport-stats.json` shows one write; `outcome.json` is RECOVERED from
samples taken by `sentinel-verifier-probe` Pods through the gateway;
`bypass-before.json`, `bypass-during.json` (fault in place) and
`bypass-after.json` all show every bypass refused; the run folder's
`telemetry.sqlite3` holds inspector events in the action window; and
`summary.json` records both private kubeconfigs removed. A Guard REJECT with
`SECURITY_SIDECAR_*`, or an ESCALATE with
`BASELINE_REFERENCE_EVIDENCE_MISSING`, is the protected rules failing
closed: record it as such, not as a recovery. The `--dashboard` view and the
crash-reconciliation case are not part of this run yet.

### Owner sign-offs

- the Guard rule `allowed_sidecars`, its codes and the argument above;
- the reference reads moving to the Guard-reader identity, and the reader's
  residual Secret read until P5.06;
- the Verifier client identity at the gateway and its probe policy;
- the protected run's 192Mi container memory-limit ceiling: it lets the
  shipping container be restored with up to 192Mi as well, because the Guard
  has no per-container ceilings;
- the `protected` catalog scope (R1's labels are unchanged; only its scopes
  gain `protected`).
