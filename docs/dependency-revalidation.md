# Dependency revalidation

Run `python -m unittest tests.test_dependency_revalidation -v` for a cluster-free
real-component demonstration. The tests use the Verifier graph evaluator,
Coordinator persistence and Guard with explicit synthetic infrastructure inputs.

A new finding for an authorized but unexecuted action moves READY back to
VALIDATING. Pending execution commands are cancelled. Guard must evaluate the
newest finding before another command can be issued. BLOCKED rejects; UNKNOWN
follows policy; CLEAR still requires state, baseline, resource and approval gates.
Older findings and mismatched identities cannot replace accepted evidence.

Once execution has started or completed, this API refuses dependency updates.
Execution retains its existing fresh Guard validation and conditional-write
checks. This local transaction does not provide a distributed fencing guarantee.
