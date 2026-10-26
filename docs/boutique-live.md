# Single-service Online Boutique experiment: scope and design

For exact installation, UI startup, injection and cleanup commands, use the
[Boutique demo instruction book](boutique-demo-handbook.md). For a presentation,
use the [current-system script and flowchart](current-system-demo.md).
This reference describes the workload-specific design rather than duplicating
the operational instructions.

## Actual workload and configuration

The harness deploys the real upstream Online Boutique shippingservice v0.10.7
in namespace `sentinel-boutique`, on dedicated kind cluster
`sentinel-boutique-lab`. It uses the application implemented in the
[upstream source](https://github.com/GoogleCloudPlatform/microservices-demo/tree/v0.10.7/src/shippingservice)
and exercises `hipstershop.ShippingService/GetQuote` from the
[upstream protocol](https://github.com/GoogleCloudPlatform/microservices-demo/blob/v0.10.7/protos/demo.proto).
It is not a replacement application or a deployed storefront.

`shipping_manifest()` in `scripts/test_boutique_live.py` generates the lab's
ServiceAccount, Deployment and Service. One replica with `Recreate` deliberately
makes a bad rollout cause visible unavailability. It is an experiment setting,
not a production availability recommendation. Optional profiling/tracing is disabled.

The harness resolves repository manifest digests for shippingservice v0.10.7 and
`fullstorydev/grpcurl:v1.9.3`, then pulls the immutable references through the
kind node runtime. This avoids Docker Desktop multi-architecture export/import
failures. `images.json` records the actual resolved manifests; tags are resolved
at run time, so preserve those references with experiment results.

## Baseline, fault and recovery

Baseline evidence contains three successful quote samples over at least ten
seconds, one ready/available replica, actual Deployment UID and exact Pod-template
hash. The executor does not infer a healthy baseline from rollout history.

The operator sets `PORT=invalid-port`. Role 2 collects actual Deployment, owned
Pod, Warning event and bounded log evidence; its checkpoint and durable outbox
hand the incident to Coordinator. The harness runs those implementations on the
host rather than deploying the separate Observer container.

The same strict Guard and restricted executor used by the original demo operate
on the shipping target. The harness supplies the proposal. Role 6 evaluates the reviewed single-service
graph in `infra/boutique-dependencies.json` against the current Deployment identity
and version; this is scoped dependency analysis, not automatic graph discovery. The Guard checks actual state, baseline,
policy, registry/account, resource budgets, approval and optional admission
preview. Missing approval, hard rejection and stale state make zero executor
mutations. The restored template and execution marker are one conditional patch.

The test includes a competing process, exact duplicate replay, changed intent
under one ID, a server-side version race, and actual process death after a real
patch. Restart reconciles live evidence without another mutation. After each APPLIED result, `runtime/verifier_pipeline.py` consumes the durable
`verifier.request`. Role 6 evaluates three real gRPC quote samples across at least
ten seconds, full target identity, readiness/availability and the approved template.
A valid RECOVERED outcome closes the incident; failed health maps to FAILED and
missing or inconsistent evidence maps to NEEDS_REVIEW. Collection occurs outside
the Coordinator transaction, with eligibility rechecked before publication.

The HTTP evidence interface remains backward-compatible. Boutique uses explicit
`protocol: grpc`, `grpc_status: 0` and an application-level quote check; no HTTP
status is invented for gRPC. A timeout remains unavailable evidence. Runtime
verification binds the expected template hash to the persisted approved proposal.
Both `approved-rollback/outcome.json` and `crash-after-write/outcome.json` preserve
the formal outcomes.

## Current boundaries and evidence

There is no LLM call, automatic dependency discovery, Chaos Mesh experiment or
whole-Boutique/cloud deployment. The dashboard is read-only. Missing Metrics API
usage remains unavailable rather than becoming fabricated resource measurements.
On failure, an explicitly labelled administrator restoration can run as teardown;
it never counts as successful executor recovery.

Evidence lives under ignored `runs/boutique-live-*`: baseline, image references,
observations/incidents, policies/proposals/approvals, journals, execution results,
quote samples and dashboard validation. Temporary executor credentials are removed.
The wrapper optionally deletes only the named test cluster; caches/evidence remain.

Historical results below predate live Role 6 integration and therefore ended at
VERIFYING. Do not treat those old runs as evidence of the new outcome path.

The first shipping experiment passed on 25 October 2026 in
`runs/boutique-live-20261025T131709Z-ac6908/`. The later full Observer/dashboard run
passed in `runs/boutique-live-20261025T150203Z-404085/`. See the
[recorded integrated validation](boutique-demo-handbook.md#recorded-integrated-validation)
for exact checks, browser evidence and limitations.

Remaining work is actual Planner integration, connected-service and
second-workload evaluation, distributed transport, authenticated authority and
cross-host ownership/fencing. The [Executor core guide](executor.md)
explains the existing executor code and its limits.

## Integrated Role 6 validation: 26 October 2026

Source run `boutique-live-20261026T072233Z-88a8c2` passed in the separate sentinel-boutique-lab cluster. All 12 checkpoints passed. Both incidents finished RECOVERED with two APPLIED execution results, two CLEAR dependency results and two passing outcomes. Three actual post-action gRPC/readiness/template samples were evaluated in each recovery round; the crash round made zero additional writes after restart. Dashboard frontend and snapshot returned HTTP 200, with 56 cluster samples and six Guard reports. Temporary executor credentials were removed. The existing packaged demo was left running separately.

Regression validation: 564 Python tests and 54 dashboard tests passed; frontend type-check and production build passed. Thirteen new tests cover gRPC evidence, fail-closed missing evidence, exact template/identity, lifecycle transitions, stale requests, replay, concurrent changes and atomic outcome/request persistence. Tests include simulated unhealthy and missing evidence; only healthy recovery outcomes were exercised in this live run. These results validate this source working tree, not an older exported runner image.
