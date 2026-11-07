# Complete offline recovery

Run `python scripts/demos/demo_offline_flow.py` without a cluster, key or account.
CI runs the same command on Linux and Windows. All six components are real code.
Kubernetes, health readings and the Planner's provider are explicitly fake.

Observer detects a failed rollout from recorded observations. Coordinator
persists the incident and Planner request. The Planner retrieves the local
runbook, validates a saved healthy template and submits the proposal. Verifier
assesses the sample dependency graph; Guard evaluates the exact proposal.
An explicit offline operator approval triggers fresh validation. Executor applies
one conditional change to the fake API and Coordinator remains VERIFYING.
One healthy reading is insufficient; recovery is recorded only after the full
healthy sample window. Restarting consumers retains queued work and readings.

`python -m unittest tests.recovery.test_offline_flow -v` covers rejection, missing approval,
stale state, unhealthy outcomes, duplicate incident/planning delivery, restart
before execution and failed delivery after a committed Executor write. The
same saved action is replayed without a second write.

This demonstrates the offline milestone, not live end-to-end AI
recovery or distributed transport. Local SQLite/lock limits still apply.
