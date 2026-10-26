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
on the shipping target. The harness supplies the proposal and an explicit
isolated-service dependency attestation. The Guard checks actual state, baseline,
policy, registry/account, resource budgets, approval and optional admission
preview. Missing approval, hard rejection and stale state make zero executor
mutations. The restored template and execution marker are one conditional patch.

The test includes a competing process, exact duplicate replay, changed intent
under one ID, a server-side version race, and actual process death after a real
patch. Restart reconciles live evidence without another mutation. Quote checks
run after both recoveries. APPLIED routes to VERIFYING; no Role 6 outcome is emitted.

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

The first shipping experiment passed on 25 October 2026 in
`runs/boutique-live-20261025T131709Z-ac6908/`. The later full Observer/dashboard run
passed in `runs/boutique-live-20261025T150203Z-404085/`. See the
[recorded integrated validation](boutique-demo-handbook.md#recorded-integrated-validation)
for exact checks, browser evidence and limitations.

Remaining work is actual Planner and Verifier integration, connected-service and
second-workload evaluation, distributed transport, authenticated authority and
cross-host ownership/fencing. The [Executor core guide](executor.md)
explains the existing executor code and its limits.
