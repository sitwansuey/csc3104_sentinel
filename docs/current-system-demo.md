# Current system: team update, presentation script and flowchart

Use the [demo instruction book](boutique-demo-handbook.md) for exact setup,
injection and cleanup commands. This document describes the current tested scope,
not the unfinished final platform.

## What changed for each role

| Role | Existing implementation reused | Integration added for the Boutique demo |
| --- | --- | --- |
| 1 Coordinator | Incident lifecycle, SQLite records, routing, expiring approvals and result ingestion | The Boutique harness delivers real Observer incidents, records approval through the public interface and sends actual execution results. Dashboard reads this exact run's database. Approval expiry support was added earlier with the live executor; no new lifecycle rewrite was needed. |
| 2 Observer | Kubernetes evidence collector, detector, persistent checkpoint/outbox and Coordinator sink | Full collector now reads shipping Deployment, owned Pods, warnings and bounded diagnostic logs. Durable observer state/outbox is used in the experiment. Inconsistent rollout samples receive bounded read-only retries; the failure timer resets on collection gaps. |
| 3 Planner | Shared proposal contract; implementation remains a stub | No Planner implementation added. The harness supplies an explicitly operator-authored proposal. |
| 4 Guard | Evaluator, strict policies, reports, approval checks, live reference reads and admission preview | The same Guard validates actual shipping state/baseline and current approval. Existing Coordinator Guard reports are visible in the connected dashboard. The teammate's evaluator/dashboard design is reused. |
| 5 Executor | Restricted adapter, target allowlist, conditional patch, durable journal, local locks, duplicate handling and reconciliation | Demo harness gained workload configuration hooks; Boutique supplies its own deployment, image, health and fault adapters. Real executor workers run against shippingservice, including a crash after a real patch. The production recovery algorithm was not replaced. |
| 6 Verifier | Reviewed dependency traversal and sustained health evaluator | The live adapter invokes real dependency evaluation, then a durable worker consumes verifier.request and publishes outcomes from actual gRPC, readiness, identity and exact-template evidence. |

The Guard teammate's existing read-only UI now connects through
`scripts/boutique_dashboard.py`. It serves actual records from the experiment,
collects projected live cluster data without producing a second incident stream,
and verifies the page/API. `scripts/test-boutique-live.ps1` manages setup and
optional cleanup. `scripts/test_boutique_live.py` coordinates the experiment.

## Presentation script: current system

“Sentinel is a Kubernetes incident diagnosis and guarded recovery project. Our
final goal includes LLM-assisted planning and independent outcome verification.
Today's demonstration exercises the implemented safety and execution path using
a real Online Boutique shipping service; planning is supplied by the operator.

“Docker Desktop runs a local kind Kubernetes cluster. The harness deploys the
actual upstream shippingservice image pinned to a resolved manifest digest. It
makes successful gRPC shipping-quote requests and stores the exact healthy Pod
template, its hash and the measured health evidence as the recovery baseline.

“The UI shows real Coordinator records and live Kubernetes telemetry. The
operator then injects a bad PORT configuration. The application fails startup.
Our Observer collects Deployment, owned Pod, Warning event and diagnostic log
evidence, detects sustained failure and durably delivers the incident.

“While the Planner is unfinished, the harness provides a clearly labelled
rollback proposal referring to the stored baseline. The Guard checks actual
state, policy, baseline, resources, dependency evidence and approval. Missing
approval or a hard rejection produces no executor mutation. An approval never
overrides a hard safety constraint.

“The executor uses a separate identity permitted to get and patch only the
selected Deployment. Under local target coordination it rechecks authorization,
journals intent and submits one patch with UID and resource-version tests. It
restores the specific baseline rather than undoing whichever revision happens
to be latest. Kubernetes then replaces the Pods using that restored template.

“The demo also tests duplicate requests, changed payloads under a reused ID,
competing workers and a real process crash after Kubernetes accepted a patch.
On restart, the executor reconciles recorded attempt evidence against live state
without submitting another mutation.

“Role 6 now verifies recovery independently of the execution result. APPLIED first moves the incident to VERIFYING and creates a durable verifier.request. The worker collects three actual gRPC shipping quotes over at least ten seconds. It checks positive USD quotes, full target identity, ready and available replicas, stable configuration and the exact approved template hash. Only passing evidence produces RECOVERED. Known failed health produces NOT_RECOVERED / FAILED; missing evidence produces UNKNOWN / NEEDS_REVIEW. Outcome publication and request acknowledgement commit together after rechecking the active action. Replaying a completed request cannot close another incident.

## Setup, injection, execution and cleanup flowchart

Solid paths describe the current integrated experiment. The harness supplies planning; Role 6 uses its actual evaluator and measured live evidence. The reviewed graph covers only shippingservice; no automatic graph discovery is claimed.

```mermaid
flowchart TD
    A[Install/check Docker, kind, kubectl, Python and Node] --> B[Build dashboard frontend]
    B --> C[Wrapper creates dedicated kind cluster]
    C --> D[Start read-only UI and live telemetry collector]
    D --> E[Deploy actual shippingservice with immutable image]
    E --> F[Measure healthy gRPC quotes and save exact template/hash]
    F --> G[Provision separate restricted executor identity]
    G --> H[Operator presses Enter: inject invalid PORT]
    H --> I[Role 2 collects Deployment, Pod, event and log evidence]
    I --> J[Role 2 detects failure and persists incident/outbox]
    J --> K[Role 1 persists incident and queues planning]
    K --> L[Operator-authored proposal; Role 3 pending]
    L --> LD[Role 6 evaluates reviewed graph and live deployment state]
    LD --> M[Role 4 validates policy, state, baseline and dependencies]
    M -->|Hard failure| N[Reject: zero executor mutations]
    M -->|Approval required| O[Role 1 records expiring operator approval]
    O --> P[Fresh Role 4 validation]
    P -->|ALLOW| Q[Role 5 locks target and binds immutable action ID]
    Q --> R[Fresh state/Guard checks; journal WRITE_STARTED]
    R --> S[Restricted UID/version conditional patch]
    S --> T[Kubernetes replaces Pods from healthy template]
    S -->|Timeout or worker crash| U[Read-only reconciliation of live action/template evidence]
    U -->|Proven applied| V[Persist APPLIED and deliver to Role 1]
    U -->|Insufficient evidence| W[NEEDS_RECONCILIATION; block competing actions]
    T --> V
    V --> X[Role 1 records VERIFYING]
    X --> VR[Durable verifier.request]
    VR --> VH[Role 6 collects 3 real gRPC samples over at least 10 seconds]
    VH --> VC[Check quote, identity, replicas, stable approved template and timing]
    VC --> VP[Atomically publish outcome and acknowledge request]
    VP -->|RECOVERED| OK[Role 1 records RECOVERED]
    VP -->|NOT_RECOVERED| BAD[Role 1 records FAILED]
    VP -->|UNKNOWN| UNK[Role 1 records NEEDS_REVIEW]
    OK --> Z[Inspect UI and saved evidence]
    BAD --> Z
    UNK --> Z
    N --> Z
    W --> Z
    Z --> AA[Stop UI/collector; remove temporary credentials]
    AA --> AB[Delete named kind cluster; retain evidence/cache]
    I -. actual observations .-> D
    K -. records .-> D
    M -. Guard reports .-> D
    V -. execution results .-> D
```

The actual harness performs refusal/concurrency tests and two recoveries: normal
execution and the post-write crash case. The chart condenses their common path.
Fresh Guard validation also occurs inside the executor's lock immediately before
execution; a previously stored ALLOW alone does not authorize the mutation.

## What is deployed where

| Location | Current contents |
| --- | --- |
| Desktop | Python harness, Coordinator/Observer/Guard/Executor/Verifier runtime calls, SQLite files, UI server and telemetry collector |
| Docker Desktop | kind node container and image storage |
| Kubernetes inside kind | shippingservice, its ServiceAccount/Service, restricted executor RBAC and short-lived gRPC client Pods |
| Browser | Built React UI reading the loopback dashboard API |

The six responsibility areas are not six independently deployed services in this
experiment. The dashboard is read-only and grants no execution permission.

## Packaged laptop demo

The packaged runner is now implemented. Follow the [packaged demo guide](packaged-demo.md)
for `scripts/demo-start.ps1` and `scripts/demo-stop.ps1`. The image bundles Python,
kubectl and the built dashboard; host bootstrap manages Docker/kind and imports
cached images. The runner joins kind's private network with a TLS-verified
internal kubeconfig, has no Docker socket and writes evidence to a persistent
host mount. This moves the local runtime calls described above into one container;
it does not create six independently deployed microservices.

The host-run diagram and workflow above remain valid for the development route.
The packaged route uses a separate `sentinel-boutique-package` cluster and the
same recovery logic. Administrator setup/injection credentials remain distinct
from the executor's restricted credentials. Planner remains pending. Existing packaged images must be rebuilt to include live Verifier integration.

Use the recorded checks in the packaging guide to distinguish validated image,
cluster and browser behaviour from still-unverified second-laptop portability.

## Verification scope and artifacts

The two successful recovery rounds now each create post-health.json and outcome.json. One full run expects two APPLIED execution results, two CLEAR dependency results, two RECOVERED outcomes and twelve PASS checkpoints across nine behavioral test categories. Nine quote probes cover three baseline samples and three after each rollback. The reviewed graph is infra/boutique-dependencies.json, scoped to shippingservice. The generic demo-api experiment retains its original health hook; these changes integrate the Boutique path. Always-on Verifier scheduling beyond this experiment and automatic dependency discovery remain future work.
