# Your role: Recovery Guard owner (Member 4)

You own the decision between the Planner's suggested recovery and the Executor's
cluster write. Your responsibility is to explain, demonstrate and enforce when
a proposed action is safe enough to permit, when it must be rejected, and when
the system needs more evidence or operator involvement.

For example: OpenRCA suspects a faulty `currencyservice` rollout. Member 3 turns
that diagnosis into a typed rollback proposal. Your Guard independently checks
the actual Deployment identity/version, policy, baseline health and integrity,
configuration compatibility, security, resource budgets, dependencies, approval
and any required server preview. It returns a bound, expiring decision and an
audit explanation. Member 5 may execute only after fresh validation; Member 6
checks whether the service actually recovers.

## Your concrete deliverables

1. **Maintain the validation rules and policy.** Set appropriate limits for each
   Online Boutique service. Decide allowed targets/actions, baseline age,
   stability duration, image registries, service accounts, CPU/memory budgets
   and evidence freshness with the team. Keep policy versioned and reviewable.
2. **Maintain the Guard interface.** Accept the agreed typed inputs and emit
   version-2 decisions plus useful reports. Coordinate changes with Member 5 so
   old, expired or mismatched decisions cannot authorize writes.
3. **Test the failure cases.** Show that wrong targets, stale state, changed
   ConfigMaps/Secrets, unsafe templates, excessive resources and revoked or
   expired approvals prevent execution. Missing evidence should escalate or
   reject according to policy. Confirm hard rejections beat approval.
4. **Prove the execution boundary.** Work with Member 5 to demonstrate zero
   writes after rejection, escalation or input errors; conditional writes must
   stop if state changes after validation. Use fake integration tests first,
   then repeat the important cases against the shared cluster.
5. **Provide evaluation evidence.** Record the scenario, expected decision,
   actual decision, failed rule, input fingerprints and validation latency.
   Include a valid recovery case so the Guard does not merely block everything.
   Explain conservative/unsupported cases and the limits of each check.

The standalone starting point is:

```bash
python scripts/demo_guard_v2.py
python scripts/demo_guard_handoff.py
python -m unittest discover -s tests
```

The first command demonstrates the new policy gates with fabricated inputs.
The handoff demonstration exercises fake execution behavior. Neither substitutes
for a later live integration experiment.

## Inputs and handoffs to agree with teammates

| Member | What you need / provide |
| --- | --- |
| **1: Coordinator + environment** | Receive versioned trusted policy, incident identity, authoritative approval/revocation records and baseline-store access. Provide decisions/reports for persistence and routing. Member 1 leads shared hosting and team access. |
| **2: Monitoring + incident context** | Receive fresh observations with timestamps and exact cluster/namespace/UID/resourceVersion. Agree how stale, missing and unavailable state is represented. |
| **3: Retrieval + Planner / RCA integration** | Receive typed proposals bound to the observed version and a trusted baseline ID/hash. Provide actionable reasons when proposals fail. Treat OpenRCA diagnoses as advisory. |
| **5: Restricted Executor** | Provide expiring decisions bound to exact inputs. Agree mandatory fresh Guard evaluation, current approval checks, conditional writes and duplicate handling. Obtain required server preview through the trusted adapter. |
| **6: Dependencies + Verifier** | Receive fresh dependency findings and trusted healthy-baseline evidence bound to target/template and stability window. Agree evidence sources, compatibility metadata and behavior when evidence is missing. |

These are proposed integration responsibilities until the team adopts the
contracts. Your code cannot establish agreement or authenticated provenance on
its own. The system owners must implement the trusted transport/store and
Executor checks at their boundaries.

You do not need to wait for hosting to develop the Guard. Your immediate work is
policy review, fixture-based demonstrations and integration contracts. Once
Member 1 provides the shared Online Boutique environment, replace synthetic
inputs with trusted adapters and collect the live evaluation evidence.
