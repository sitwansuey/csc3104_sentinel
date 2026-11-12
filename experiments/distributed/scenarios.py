"""Fault scenarios S0-S8 for the multi-process topology (A1 design).

Each scenario starts a Topology, injects one kind of fault at a precise point,
lets the system finish, and returns notes. run_scenarios.py then checks the
invariants. Every fault point is armed through runtime/fault_points.py, so the
same code is inert in any run that is not a lab fault run.

expected_failures names the invariants a scenario is known to break on this
code base; those are the "before" evidence for later items. S1b and S1c
left the crashed Executor's ledger in doubt after its peer finished the
action until P3.04 made a restarted Executor settle its in-doubt rows from
live state before claiming. S8 is the P3.04 hazard: an approval revoked
while the lease holder is paused just before its patch, so the other
Executor seals the target. S7 showed the clock-skew wedge until P3.03 ordered results by
Coordinator sequence; it now ends FAILED with no write instead of wedging,
because a host 300 s behind refuses the approval by its own clock. S7b
checks the skew bound fails closed while the in-bound peer recovers the
action. S0, S1d and S1e are additions to the A1 list: a no-fault
baseline, a Verifier crash, and an Executor crash where only that host returns.
"""
from dataclasses import dataclass, field
import os
import time

from experiments.distributed.topology import TERMINAL


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    run: object
    deadline_seconds: float = 60
    options: dict = field(default_factory=dict)
    expected_failures: frozenset = frozenset()
    known_defect: str = ""


def _wait(condition, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    raise TimeoutError(what)


def _writes(topology):
    if topology.mode == "kind":
        return topology.kind_writes()
    from tests.support.file_cluster import FileCluster
    with FileCluster(topology.run_dir / "cluster.db") as cluster:
        return cluster.committed_writes()


def _crash_and_restart(topology, point, *, qualifier=None, down_until_terminal=False):
    """Crash the process that reaches point, then restart it on its own state directory.

    With down_until_terminal the crashed host stays down until the incident
    ends, so the peer Executor (not a race between the two) takes the command.
    """
    stem = topology.arm(point, "exit", qualifier=qualifier)
    topology.start_observer()
    topology.wait_reached(stem)
    name = topology.reached_by(stem)
    _wait(lambda: not topology.processes[name].running, 15, name + " did not exit at the fault point")
    exit_code = topology.processes[name].popen.returncode
    if down_until_terminal:
        topology.wait_terminal(45)
    topology.restart(name)
    return {"fault_point": point, "crashed": name, "exit_code": exit_code,
            "restarted": "after the incident ended" if down_until_terminal else "at once"}


def s0(topology):
    """No fault: the whole flow across separate processes, two Executors polling."""
    topology.start_observer()
    return {}


def s1a(topology):
    """Kill the Planner mid-inference, then restart it on the same state directory."""
    return _crash_and_restart(topology, "planner.mid_inference")


def s1b(topology):
    """Kill an Executor after it journalled the write intent, before the patch; the peer takes over."""
    return _crash_and_restart(topology, "executor.after_start_write", down_until_terminal=True)


def s1c(topology):
    """Kill an Executor after the patch committed, before it saved the result; the peer takes over."""
    return _crash_and_restart(topology, "executor.after_patch_before_save", down_until_terminal=True)


def s1e(topology):
    """Kill an Executor after its write intent, stop its peer, and restart it at once.

    Only the crashed host can take the command again. Its ledger says the write
    may be in flight, so it reconciles and, after the settle delay, fences:
    the action ends FAILED with no template write (P2.06 across processes).
    """
    stem = topology.arm("executor.after_start_write", "exit")
    topology.start_observer()
    topology.wait_reached(stem)
    name = topology.reached_by(stem)
    peer = "executor-b" if name == "executor-a" else "executor-a"
    topology.kill(peer)
    _wait(lambda: not topology.processes[name].running, 15, name + " did not exit at the fault point")
    topology.restart(name)
    return {"fault_point": "executor.after_start_write", "crashed": name, "stopped_peer": peer}


def s1d(topology):
    """Kill the Verifier in the middle of collecting health samples."""
    return _crash_and_restart(topology, "verifier.mid_collection")


def s2(topology):
    """Stall the lease-holding Executor past its lease; the other one takes over; the stale one must not write.

    POSIX: SIGSTOP (all threads stop, like a long GC pause). Windows has no
    suspend without psutil, so the stall is the blocked fault point plus a
    blackholed link, which also stops lease renewal.
    """
    stem = topology.arm("executor.before_patch", "block")
    topology.start_observer()
    topology.wait_reached(stem)
    stale = topology.reached_by(stem)
    method = "suspend" if os.name != "nt" else "block+blackhole"
    if method == "suspend":
        topology.suspend(stale)
    else:
        topology.links[stale].set_mode("blackhole")
    if topology.mode == "kind":  # Kind counts writes without the writer's name: wait for any write.
        _wait(lambda: any(row["marker"] != "unobserved" for row in topology.kind_writes()), 60,
              "the other Executor did not write")
    else:
        _wait(lambda: any(row["host"] != stale for row in _writes(topology)), 60, "the other Executor did not write")
    if method == "suspend":
        topology.resume(stale)
    else:
        topology.links[stale].set_mode("pass")
    topology.release(stem)
    return {"stalled": stale, "stall": method}


def s3(topology):
    """Blackhole the lease holder's link to the Coordinator after its write intent."""
    stem = topology.arm("executor.after_start_write", "block")
    topology.start_observer()
    topology.wait_reached(stem)
    cut = topology.reached_by(stem)
    topology.links[cut].set_mode("blackhole")
    topology.release(stem)
    try:
        topology.wait_state({"VERIFYING", *TERMINAL}, 45)
    finally:
        topology.links[cut].set_mode("pass")
    return {"partitioned": cut}


def s4(topology):
    """Drop the response to the Executor's result POST; the saved body is replayed."""
    stem = topology.arm("executor.after_patch_before_save", "block")
    topology.start_observer()
    topology.wait_reached(stem)
    holder = topology.reached_by(stem)
    topology.links[holder].set_mode("drop-response")
    topology.release(stem)
    try:
        topology.wait_state({"VERIFYING", *TERMINAL}, 45)
    finally:
        topology.links[holder].set_mode("pass")
    _wait(lambda: any(record.get("replay") is True for record in topology.processes[holder].stdout_records()),
          30, "the Executor did not replay its saved result")
    return {"dropped_for": holder, "replay_receipt": True}


def s5(topology):
    """Kill the Coordinator after it committed a result, before the reply; restart it; restart it again later."""
    stem = topology.arm("coordinator.after_commit_before_reply", "exit", qualifier="execution_result")
    topology.start_observer()
    topology.wait_reached(stem)
    _wait(lambda: not topology.processes["coordinator"].running, 15, "the Coordinator did not exit")
    exit_code = topology.processes["coordinator"].popen.returncode
    topology.restart_coordinator()
    topology.wait_state({"VERIFYING", *TERMINAL}, 45)
    topology.restart_coordinator()  # A second restart while workers keep claiming.
    return {"fault_point": "coordinator.after_commit_before_reply", "exit_code": exit_code, "restarts": 2}


def s6(topology):
    """Blackhole every worker link while an approval is pending: no progress and no write, then recovery."""
    topology.start_observer()
    topology.wait_state({"AWAITING_APPROVAL"}, 45)
    (row,) = [item for item in topology.incidents() if item["state"] == "AWAITING_APPROVAL"]
    for link in topology.links.values():
        link.set_mode("blackhole")
    topology.approve(row["active_action_id"])  # The operator reaches the Coordinator directly.
    time.sleep(3 * topology.lease_seconds)
    during = {"writes": len(_writes(topology)), "states": topology.states()}
    for link in topology.links.values():
        link.set_mode("pass")
    if during["writes"]:
        raise AssertionError("A write happened while every worker was partitioned")
    return {"partition_seconds": 3 * topology.lease_seconds, "during_partition": during}


def _result_refusals(topology):
    from experiments.distributed.invariants import log_lines
    return sum(1 for line in log_lines(topology.processes["coordinator"].stderr_path)
               if line.get("operation") == "result" and line.get("http_status") == 400)


def s7(topology):
    """Both Executors' clocks 300 s behind the others (gated test clock offset), no skew bound.

    The offset moves the Executor's whole clock: its snapshot, its ledger
    stamps and its local Guard re-check. Before P3.03 the Coordinator refused
    the Executor's result (stamped before the Guard ALLOW) with HTTP 400 for
    ever and the incident stayed READY. Now results are ordered by the request
    they answer, so the result is accepted. The re-check judges the
    Coordinator-stamped operator approval by the Executor's own clock, sees it
    300 s in the future and refuses: the action ends FAILED with no write. It
    does not recover, since both Executors are out. Each worker's measured
    offset is in the summary.
    """
    topology.start_observer()
    terminal = topology.wait_terminal(45)
    return {"clock_offsets": dict(topology.clock_offsets), "terminal": terminal,
            "refusals_seen": _result_refusals(topology)}


def s7b(topology):
    """executor-a's clock 5 s behind with a 2 s skew bound: it fails closed and executor-b does the work.

    executor-a measures its offset, reports it, stays unready and never claims;
    the Coordinator would refuse its claims too.
    """
    from experiments.distributed.invariants import log_lines
    skewed = "executor-a"
    _wait(lambda: topology.worker_probe(skewed)[0] == 503, 15, skewed + " did not turn unready")
    status, body = topology.worker_probe(skewed)
    topology.start_observer()
    topology.wait_terminal(45)
    claims = sum(1 for line in log_lines(topology.processes["coordinator"].stderr_path)
                 if line.get("identity") == skewed and line.get("operation") == "claim"
                 and line.get("outcome") == "claimed")
    if claims:
        raise AssertionError(f"{skewed} claimed work outside the skew bound")
    return {"skewed": skewed, "readyz": {"status": status, **body}, "claims_by_skewed": claims,
            "statuses": sorted({record.get("status") for record in topology.processes[skewed].stdout_records()})}


def s8(topology):
    """Revoke the approval while the lease holder is paused just before its patch; the other Executor seals.

    The paused Executor passed its last lease and approval checks before the
    pause, so only the seal stops its patch: the Coordinator queues
    executor.seal when it withdraws the claimed command, the other Executor
    sends a metadata-only patch with the same resourceVersion test, and the
    paused patch is refused when it resumes. The action ends FAILED with no
    template write.
    """
    stem = topology.arm("executor.before_patch", "block")
    topology.start_observer()
    topology.wait_reached(stem)
    paused = topology.reached_by(stem)
    (row,) = [item for item in topology.incidents() if item["state"] == "READY"]
    topology.revoke(row["active_action_id"])
    _wait(lambda: _sealed(topology), 30, "the other Executor did not seal the target")
    topology.release(stem)
    topology.wait_terminal(45)
    from experiments.distributed.invariants import _rows
    ledger = topology.paths["state"] / paused / "executor.db"
    # The paused Executor resumes, its patch is refused, and it closes its own ledger row.
    _wait(lambda: [row["phase"] for row in _rows(ledger, "SELECT phase FROM executor_actions")] == ["DONE"], 30,
          paused + " did not finish its write attempt after the release")
    (owner,) = _sealed(topology)
    (status,) = _rows(ledger, "SELECT json_extract(result, '$.status') AS status FROM executor_actions")
    return {"paused": paused, "paused_result": status["status"], "sealed_epoch": owner["sealed_epoch"],
            "epoch": owner["epoch"], "writes_after_release": len(_writes(topology))}


def _sealed(topology):
    return topology.query("SELECT epoch, sealed_epoch FROM target_ownership WHERE sealed_epoch > 0 "
                          "AND seal_required = 0")


SCENARIOS = {scenario.name: scenario for scenario in (
    Scenario("S0", "No fault (baseline)", s0),
    Scenario("S1a", "Kill the Planner mid-inference", s1a),
    Scenario("S1b", "Kill an Executor after the write intent, before the patch", s1b),
    Scenario("S1c", "Kill an Executor after the patch, before the result is saved", s1c),
    Scenario("S1d", "Kill the Verifier mid-collection", s1d),
    Scenario("S1e", "Kill an Executor after the write intent; only it comes back", s1e),
    Scenario("S2", "Stall the lease-holding Executor past its lease", s2),
    Scenario("S3", "Partition the lease-holding Executor from the Coordinator", s3),
    Scenario("S4", "Drop the response to the Executor's result", s4),
    Scenario("S5", "Restart the Coordinator between commit and reply, and again later", s5),
    Scenario("S6", "Partition every worker from the Coordinator", s6, options={"auto_approve": False}),
    Scenario("S7", "Executor clocks 300 s behind (clock skew)", s7, deadline_seconds=45,
             options={"clock_offsets": {"executor-a": -300, "executor-b": -300}}),
    Scenario("S7b", "One Executor's clock 5 s behind with a 2 s skew bound", s7b, deadline_seconds=45,
             options={"clock_offsets": {"executor-a": -5}, "max_clock_skew": 2}),
    Scenario("S8", "Revoke the approval while the lease holder is paused before its patch", s8),
)}
DEFAULT = ("S1b", "S1c", "S2", "S4", "S5")  # The plan's CI subset; S7 checks ordering under skew.
