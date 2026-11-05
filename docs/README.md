# Documentation map

Start with the [demo handbook](boutique-demo-handbook.md): prerequisites,
connected storefront, ordered browser injections, evidence and cleanup.
[System flow](current-system-demo.md) explains the current implementation;
[repository review](repository-review-2026-11-05.md) records accepted changes,
validation and remaining work.

## Setup and experiments

| Purpose | Guide |
| --- | --- |
| Install with scripts or manually | [Dependencies](dependencies.md) and the repository README |
| Current full storefront, shipping repair | [Connected Boutique](boutique-connected.md) |
| Smaller shipping-only lab | [Boutique live](boutique-live.md) |
| Browser injection gates and evidence | [Experiment controls](experiment-controls.md) |
| Dashboard/API | [Dashboard](dashboard.md), [response contract](dashboard-api.md) |
| Future image build and laptop transfer | [Packaged demo](packaged-demo.md), [historical image scan](image-security.md) |

## Role implementation

| Role | Design and operation |
| --- | --- |
| 1 Coordinator | [Persistence, approvals and lifecycle](coordinator.md) |
| 2 Observer | [Runtime](observer-runtime.md), [telemetry](observer-telemetry.md), [live checks](observer-live-test.md) |
| 3 Planner | [Offline flow](offline-flow.md), [provider boundary](provider-recovery.md); real model integration remains pending |
| 4 Guard | [Rules and ownership](recovery-guard.md), [v2 gates](guard-v2.md), [runtime](guard-runtime.md), [handoffs](guard-integration-contract.md), [evaluation](guard-evaluation.md) |
| 5 Executor | [Core](executor.md), [live adapter/RBAC](executor-live.md) |
| 6 Verifier | [Evaluator](verifier.md), [durable worker](verifier-worker.md), [live readers](verifier-live.md), [connected acceptance](verifier-connected-validation.md) |

## Optional integration work

[Dependency revalidation](dependency-revalidation.md) explains revocation.
[Worker transport](worker-transport.md) is a delivery foundation, not a deployed
cross-host ownership system. [Protected Boutique](security-connected.md) and
[security components](security.md) describe a separate lab; protected guarded
recovery remains unfinished. [Project automation](project-automation.md) covers
repository workflow configuration.

Offline demonstrations, test fixtures and specialised acceptance scripts remain
because they test different failure boundaries. They are not alternative claims
of live recovery. Generated journals, scans, screenshots and export archives
belong under ignored `runs/`, not in the source tree.
