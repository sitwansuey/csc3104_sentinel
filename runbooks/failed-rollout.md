# Failed rollout with repeatedly crashing containers

Tags: rollout, startup, CrashLoopBackOff, configuration

Gather Deployment conditions, current and previous container logs, rollout
history and the last observed healthy Pod template. Determine whether a recent
template change explains the startup failure. A repeated restart may retain the
same faulty configuration.

If an observed healthy baseline is retained and policy permits restoring it,
propose restoration of that explicit template. Confirm target identity, current
state and dependency constraints; obtain approval when required. Verify service
health after application. If evidence or the healthy baseline is missing,
escalate rather than inventing a recovery configuration.

This is general development guidance. Evaluation-case solutions and benchmark
labels must not be added to the retrieval corpus.
