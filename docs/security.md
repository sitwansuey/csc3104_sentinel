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
traffic. Only the shipping path is protected by mTLS and inspection: the other
Boutique services keep their upstream plaintext connections. From P4.06 the lab
also applies graph-derived default-deny NetworkPolicies to all 11 services (see
below); that part has been tested offline only. The smaller `scripts/security/security_lab.py`
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
  target's port. In the lab only the cartservice probe is allowed: shipping
  keeps its existing `protect-*` policies, so it still accepts only the mTLS
  gateway, and a Sentinel probe of shipping is refused there. The plain set
  for the connected namespace (`graph_policies`) also admits the redis-cart
  probe on 6379, because a scenario-pair run ([dual-fault.md](dual-fault.md))
  checks redis-cart with a Redis PING from a Pod with the same two labels
  (`connected_probe_targets`); the security lab never runs a pair, so its
  committed set is unchanged.

In the lab, frontend and checkoutservice reach shipping only through
`security-gateway:8443`, and only the gateway reaches shippingservice, on
8444. The graph policies never add an ingress rule for shippingservice or the
gateway, so they cannot reopen the plaintext bypass.

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
namespace: 16 identities by 13 Services. The tester image is the busybox
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

The live run has not been done. To do it, use a fresh run folder (the path
below is an example):

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

Two gaps remain. Only the shipping path is protected by mTLS and inspection,
and the NetworkPolicies for the other services have not run live. Workload images are
scanned and raise dashboard alerts, but nothing gates on them: the 11 November
evaluation found High or Critical findings in 11 of 13 images. See
`L-SECURITY-SHIPPING-ONLY` and `L-UNGATED-CVES` in [limitations](limitations.md).

Lab certificates are short-lived fixtures, not a rotation/revocation/workload-identity
system. Availability/capacity, current-image reconciliation and protected
Guard/Executor/Verifier integration remain tracked work. Keep credentials,
private keys and unsanitised logs out of Git and exported image contexts.
