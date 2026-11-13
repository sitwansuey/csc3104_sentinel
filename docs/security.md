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

The live run was done once, on 13 Nov 2026 at 02d91e5, on a fresh
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

Two gaps remain. Only the shipping path is protected by mTLS and inspection:
the other services now have L4 default-deny NetworkPolicies in the security
lab only, shown by one live run on one kind node, and their traffic is still
plaintext; the connected recovery cluster applies none of these policies.
Workload images are
scanned and raise dashboard alerts, and a CVE gate now exists, but no live run
has used it: the 11 November evaluation found High or Critical findings in 11
of 13 images. See `L-SECURITY-SHIPPING-ONLY` and `L-UNGATED-CVES` in
[limitations](limitations.md).

Lab certificates are short-lived fixtures, not a rotation/revocation/workload-identity
system. Availability/capacity, current-image reconciliation and protected
Guard/Executor/Verifier integration remain tracked work. Keep credentials,
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
  from the current template, so it is not gated.
- **Admission.** `deploy/security/approved-images-vap.yaml` is a
  ValidatingAdmissionPolicy that admits a Deployment CREATE or UPDATE, or a
  Pod CREATE, in `sentinel-boutique-connected` only when every container image
  is listed in the ConfigMap `sentinel-admission/sentinel-approved-images`.
  `gate_images.py vap-params` writes that ConfigMap from passing gate results:
  `repository@sha256` references only, never a tag. Pod UPDATEs are not
  matched, so Chaos Mesh pod-failure is not blocked. With Deny on, the Guard's
  server dry-run reports a denial as `SERVER_DRY_RUN_REJECTED`.
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
| `IMAGE_CURRENT_SCAN_UNAVAILABLE` | UNKNOWN | Inheritance cannot be shown: the current image has no usable scan |
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
escalates, caller-supplied scans are ignored, a worse digest is refused) and
`tests/test_cve_gate_admission.py` (the admission policy's shape, the
parameters hold digests only, unscanned digests are not approved, the
inventory never guesses a digest, and the Guard image holds every module
the Guard imports, the gate's included).

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

   Stop if the server is older than 1.30 or any container is UNKNOWN.

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

4. Apply in Audit and Warn mode and check the policy compiled:

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
   must be unchanged afterwards (0 writes).

7. Rerun with the gate on:
   `python scripts/test_boutique_live.py --connected --target cartservice --approval auto-lab --image-scans $run/scans --cve-exceptions deploy/security/cve-exceptions.json`
   (P3.09), and the same with `--scenario N2`, `N3` and `T1` (P4.03). R2 and
   C2 inject an image digest that names no published image, so with Deny on
   their injection is refused by admission: run them only in Audit and Warn
   mode, or report them as blocked by the gate.

8. Clean up: delete the binding, the policy and the `sentinel-admission`
   namespace, then collect the run with `scripts/evidence/collect.py`.

### Owner sign-offs

These are safety-critical and wait for an owner:

- the Guard change: the gate block, its codes and the argument above, before
  any live policy sets it;
- the admission policy, before step 5 switches it to Deny;
- non-regression as the default mode, and which targets move to strict mode
  (P5.03);
- every CVE exception: a human signs each one, with a role, rationale and at
  most 30 days (P5.03). The agent prepares none.
