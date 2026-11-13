# Replicas below the reviewed minimum

Tags: replicas, scale, scaled down, ReplicasBelowReviewedMinimum, capacity

The Deployment's desired replica count (spec.replicas) is lower than the
minimum an operator reviewed for this workload, for example after a mistaken
manual scale-down or an automation error. Zero replicas is a normal state for a
workload nobody expects to run, so this applies only where a reviewed minimum
exists.

Gather the Deployment's spec.replicas, its status, recent events and any
HorizontalPodAutoscaler that targets it. Compare the live Pod template with the
last observed healthy template. If the configuration differs, restore the
healthy template first (failed-rollout guidance); scaling up would start more
Pods from a configuration nobody reviewed.

If the configuration equals the healthy template, restoring the replica count
recorded with that healthy baseline is a reasonable repair. Propose a scale to
exactly that count: never more replicas than the reviewed count, never fewer
than the live count, and never together with a template change. If an
autoscaler manages the Deployment, escalate instead, because it would fight a
manual scale. Confirm target identity, current state and dependency
constraints, obtain approval when required, and verify that the restored count
holds and its Pods are ready and healthy afterwards.

This is general development guidance. Evaluation-case solutions and benchmark
labels must not be added to the retrieval corpus.
