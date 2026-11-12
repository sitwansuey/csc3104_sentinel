# Resource usage high while every Pod is ready

Tags: cpu, memory, saturation, metric, threshold, alert

A metric alert says CPU or memory usage is above a threshold, but the
Deployment reports every desired Pod ready and available. The workload is
serving. Gather the usage values, the threshold, the measurement window, the
Pod's requests and limits, and what else runs on the node.

Usage alone does not show which change, if any, caused the load. Restoring an
older template changes nothing when the template already equals the healthy
one, and new Pods from the same template do not remove load that comes from
outside the application, such as another process on the node or a burst of
traffic. Do not roll back or restart on a usage alert alone. Report it for
operator review with the measured values, and repair only when Pods actually
fail or a configuration change is shown to be the cause.

This is general development guidance. Evaluation-case solutions and benchmark
labels must not be added to the retrieval corpus.
