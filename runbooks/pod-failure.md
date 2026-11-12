# Pods failing while the configuration is unchanged

Tags: pod, readiness, liveness, NotReady, Unhealthy, OOMKilled, restart

Gather Deployment conditions, Pod readiness, recent events and container logs.
Compare the live Pod template with the last observed healthy template. If the
configuration differs, this is a configuration fault: follow the failed-rollout
guidance and restore the healthy template instead.

If the configuration equals the observed healthy template and Pods are not
ready or not available, new Pods from that same template are a reasonable
repair, for example when a process is stuck or has leaked resources. Propose a
restart of that exact template; never change images, environment or resources
as part of a restart. Restart a workload at most a few times per hour; if Pods
keep failing after a restart, escalate rather than restarting again. Confirm
target identity, current state and dependency constraints, obtain approval when
required, and verify that the new Pods are ready and healthy afterwards.

This is general development guidance. Evaluation-case solutions and benchmark
labels must not be added to the retrieval corpus.
