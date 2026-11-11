"""Deterministic model checker for Executor writes that may still be in flight.

A seeded schedule interleaves the real execute() and reconcile() of two
Executor hosts with a simulated API server (tests/support/sim_apiserver.py):
our write landing at once, late or never; lost responses; another writer
bumping the resourceVersion just before our patch arrives; third-party writes,
evidence erasure, delete and recreate; read outages; clock jumps; and process
crashes before or after every journal (ledger) call and adapter step. Each
host keeps its ledger across a crash, like a file on disk; everything else in
the process is lost. At the end every request still in flight is delivered or
lost, then each host runs restart reconcile passes with nothing else changing.

With fencing on (the default, fence_after=30 s), a reconcile pass fences a
write still in doubt. Fences can be answered, lose their response, stay in
flight or never arrive, and the process can crash around every fence step.
Their fates come from a second generator seeded from the schedule's seed, so
the schedules are the same with fencing on and off. In the final passes the
API server answers every fence (its response may still be lost).

The checker compares what each host records with ground truth from the
simulator. Invariants (I), the fence checks (F) and the peer-adoption check
(P1) are checked on every recorded result or step; the liveness property (L1)
after the final passes. See INVARIANTS.

Assumptions, as in the Executor guide: other writers never write this action's
evidence annotation; a resourceVersion never repeats; requests are tested
against the object they commit to. The Coordinator and the Guard are not
modelled: Guard validation always allows, and no new action is executed.
"""

import argparse
from collections import Counter
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta, UTC
import json
import os
from pathlib import Path
import random
import sys

from services.executor import core
from services.executor import store
from tests.support.sim_apiserver import FENCE_ANNOTATION, PatchFailed, SimAPIServer, SimDeploymentAPI


ROOT = Path(__file__).resolve().parents[2]
START = datetime(2026, 10, 10, 12, 0, 40, tzinfo=UTC)
HOSTS = ("A", "B")
STEPS = 30
PASSES = 3
DEFAULT_SEEDS = 2000
OPEN = frozenset({"WRITE_STARTED", "UNCERTAIN"})

INVARIANTS = {
    "I1": "At most one write of the action commits, and each host sends at most one write for it.",
    "I2": "APPLIED is recorded only after a write of this action (this host's or a peer's) committed.",
    "I3": ("CONFLICT and SUPERSEDED are never recorded while a write of this action can still commit, "
           "and CONFLICT never by a host whose own write committed."),
    "I4": ("No permanent wedge: once the target is provably beyond commit (deleted, another UID or "
           "another resourceVersion), a host's next completed call that reads it ends the action there."),
    "I5": ("No final result in doubt: a host never ends the action while its own write can still commit, "
           "or, unless it is a FAILED from a host that sent no write, while any write of it can."),
    "L1": ("After every request is delivered or lost and nothing else changes, if the target is provably "
           "beyond commit, every journaled host is done within the reconcile passes and a new proposal "
           "for the same workload is admitted. With fencing on, a host still in doubt at the end fails L1 "
           "even if the target is not beyond commit."),
    "F1": ("At most one request tested at V0 commits, so a fence never commits together with a write or another "
           "fence of the action; and CONFLICT/FENCED is recorded only by a host whose own fence committed."),
    "F2": ("Write-ahead fencing: a host sends a fence only after journaling its nonce, so while its committed "
           "fence is still on the object it records CONFLICT/FENCED, never SUPERSEDED."),
    "P1": ("Peer adoption: a host never records CONFLICT while a peer's committed write of the action "
           "is still on the object (same UID, template and evidence); it reports APPLIED instead."),
    "X": "The Executor raised an unexpected exception.",
}
SAFETY = ("I1", "I2", "I3", "I5", "F1", "X")
LIVENESS = ("I4", "L1")
# Not in the design's I1-I5: a peer-unaware Executor stays safe but reports CONFLICT for an applied action.
ADOPTION = ("P1",)
# A host that forgets its own fence stays safe (SUPERSEDED) but can no longer prove the write did not commit.
FENCING = ("F2",)
FENCE_AFTER = 30.0

_FENCE_CRASHES = ("ledger.record_fence.before", "ledger.record_fence.after", "fence.send.before",
                  "fence.send.after", "fence.commit.after", "ledger.confirm_fence.after")
EXECUTE_CRASHES = ("ledger.prepare.before", "ledger.prepare.after", "read", "ledger.record_validation.after",
                   "ledger.start_write.before", "ledger.start_write.after", "send.before", "send.after",
                   "commit.after", "ledger.finish.before", "ledger.finish.after") + _FENCE_CRASHES
RECONCILE_CRASHES = ("ledger.adopt.before", "ledger.adopt.after", "read", "ledger.finish.before",
                     "ledger.finish.after", "ledger.schedule_reconcile.before",
                     "ledger.schedule_reconcile.after") + _FENCE_CRASHES
_WEIGHTED_STEPS = ((("execute", "A"), 4), (("execute", "B"), 2), (("reconcile", "A"), 3), (("reconcile", "B"), 1),
                   (("deliver", None), 3), (("drop", None), 1), (("external", None), 2), (("outage", None), 1),
                   (("clock", None), 1))
_EXTERNAL = (("bump", 3), ("erase-evidence", 2), ("template", 2), ("restore-template", 1), ("delete", 1),
             ("recreate", 1))
_FATES = (("ok", 25), ("lost", 20), ("queued", 40), ("race", 15), ("peer-race", 10))
_FENCE_FATES = (("ok", 40), ("lost", 20), ("queued", 25), ("dropped", 15))
_FINAL_FENCE_FATES = (("ok", 1), ("lost", 1))
_KNOWN_REFUSALS = ("No journaled write exists to reconcile",)  # Pre-table executors refuse a peer reconcile.


class SimulatedCrash(BaseException):
    """Process death: skips ordinary exception handling, like the real thing."""


@dataclass(frozen=True)
class Step:
    kind: str
    host: str | None = None
    arg: object = None
    crash: str | None = None

    def describe(self):
        text = self.kind + ("@" + self.host if self.host else "")
        if self.arg is not None:
            text += " " + str(self.arg)
        return text + (" crash=" + self.crash if self.crash else "")


@dataclass(frozen=True)
class Schedule:
    seed: int
    steps: tuple
    drain: tuple  # For each request left in flight, in order: True delivers it, False loses it.


@dataclass
class Outcome:
    schedule: Schedule
    violations: list = field(default_factory=list)  # (code, step index or "end", message)
    trace: list = field(default_factory=list)
    category: str = ""
    statuses: dict = field(default_factory=dict)

    @property
    def codes(self):
        return {code for code, _, _ in self.violations}


def _pick(rng, weighted):
    total = sum(weight for _, weight in weighted)
    roll = rng.uniform(0, total)
    for value, weight in weighted:
        roll -= weight
        if roll <= 0:
            return value
    return weighted[-1][0]


def _call_step(rng, kind, host):
    crashes = EXECUTE_CRASHES if kind == "execute" else RECONCILE_CRASHES
    crash = rng.choice(crashes) if rng.random() < 0.25 else None
    return Step(kind, host, _pick(rng, _FATES) if kind == "execute" else None, crash)


def generate(seed, length=STEPS):
    """The schedule for one seed. The same seed always gives the same schedule.

    Each seed also draws how often other writers act (never, rarely or often),
    so quiet runs where a write stays in doubt to the end are covered as well.
    """
    rng = random.Random(seed)
    noise = rng.choice((0, 0.5, 2))
    weighted = tuple(((kind, host), weight * noise if kind == "external" else weight)
                     for (kind, host), weight in _WEIGHTED_STEPS)
    steps = []
    if rng.random() < 0.8:
        steps.append(_call_step(rng, "execute", "A"))
    while len(steps) < length:
        kind, host = _pick(rng, weighted)
        if kind in ("execute", "reconcile"):
            steps.append(_call_step(rng, kind, host))
        elif kind in ("deliver", "drop"):
            steps.append(Step(kind, arg=rng.choice(("oldest", "newest"))))
        elif kind == "external":
            steps.append(Step(kind, arg=_pick(rng, _EXTERNAL)))
        elif kind == "outage":
            steps.append(Step(kind, host=rng.choice(HOSTS)))
        else:
            steps.append(Step(kind, arg=rng.choice((61, 700))))
    drain = tuple(rng.random() < 0.75 for _ in range(4))
    return Schedule(seed, tuple(steps), drain)


def load_fixtures():
    def read(relative):
        return json.loads((ROOT / relative).read_text(encoding="utf-8"))
    return {"proposal": read("data/sentinel/samples/messages/proposal.json"),
            "live_state": read("data/sentinel/samples/observations.json")["failed-rollout"][-1],
            "baseline": read("data/sentinel/samples/baseline.json")}


class _NoLock:
    """Steps run one at a time, so the per-host target lock never contends."""

    class _Held:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def hold(self, target):
        return self._Held()


class _AllowAuthorizer:
    """Guard is not under test: every fresh validation allows."""

    def __init__(self, run):
        self.run = run

    def validate(self, proposal, snapshot, *, now):
        decision = {"schema_version": "1.0", "kind": "guard_result", "incident_id": proposal["incident_id"],
                    "action_id": proposal["action_id"], "target": proposal["target"], "verdict": "ALLOW",
                    "reason_codes": ["VALIDATION_PASSED"], "policy_version": "model-check",
                    "validated_resource_version": snapshot["deployment"]["metadata"]["resourceVersion"],
                    "checked_at": now.isoformat().replace("+00:00", "Z")}
        return core.GuardCheck(decision, json.loads(json.dumps(self.run.baseline)), {})

    def assert_usable(self, check, *, now):
        return None


class _Journal:
    """Ledger proxy: crash points around every journal write, and result checks."""

    _WRITES = frozenset({"prepare", "record_validation", "start_write", "adopt", "schedule_reconcile", "finish",
                         "record_fence", "confirm_fence"})

    def __init__(self, ledger, run, host):
        self._ledger, self._run, self._host = ledger, run, host

    def __getattr__(self, name):
        attribute = getattr(self._ledger, name)
        if name not in self._WRITES:
            return attribute

        def call(*args, **kwargs):
            self._run.point("ledger." + name + ".before")
            if name == "finish":
                self._run.check_result(self._host, args[1])
            value = attribute(*args, **kwargs)
            self._run.point("ledger." + name + ".after")
            return value
        return call


class _Host:
    def __init__(self, run, name):
        self.name = name
        self.ledger = store.SQLiteExecutionLedger(":memory:")
        self.cluster = SimDeploymentAPI(run.server, name, action_id=run.action_id, clock=run.clock, point=run.point)
        options = {"fence_after_seconds": run.fence_after} if run.fencing else {}
        self.adapters = core.ExecutionAdapters(ledger=_Journal(self.ledger, run, name), coordination=_NoLock(),
                                               cluster=self.cluster, authorizer=_AllowAuthorizer(run),
                                               clock=run.clock, **options)

    def phase(self, action_id):
        row = self.ledger.get(action_id)
        return row["phase"] if row is not None else None

    def fence_nonces(self, action_id):
        """Nonces this host journaled; none for an Executor that cannot fence."""
        fences = getattr(self.ledger, "fences", None)
        return {fence["nonce"] for fence in fences(action_id)} if fences is not None else set()


def _fence_nonce(request):
    """The nonce in a fence request's marker annotation."""
    value = request.operations[-1]["value"]
    annotation = value.get(FENCE_ANNOTATION) if isinstance(value, dict) else value
    return json.loads(annotation)["nonce"]


def _default_execute(proposal, adapters):
    return core.execute(proposal, adapters)  # Looked up per call, so tests can patch core.


def _default_reconcile(proposal, adapters):
    return core.reconcile(proposal, adapters)


def can_fence():
    """True if the Executor under test has the fencing option."""
    return any(item.name == "fence_after_seconds" for item in fields(core.ExecutionAdapters))


class Run:
    def __init__(self, schedule, *, execute=None, reconcile=None, passes=PASSES, fixtures=None,
                 fence_after=FENCE_AFTER):
        fixtures = fixtures or load_fixtures()
        self.schedule = schedule
        self.execute = execute or _default_execute
        self.reconcile = reconcile or _default_reconcile
        self.passes = passes
        self.fence_after = fence_after
        self.fencing = fence_after is not None and can_fence()
        # A separate generator, so fence fates never change the schedule itself.
        self.fence_rng = random.Random(f"fence-{schedule.seed}")
        self.final = False
        self.proposal = fixtures["proposal"]
        self.baseline = fixtures["baseline"]
        self.action_id = self.proposal["action_id"]
        self.uid0 = self.proposal["target"]["uid"]
        self.v0 = self.proposal["based_on_resource_version"]
        self.server = SimAPIServer.from_snapshot(fixtures["live_state"])
        self.now = START
        self.outcome = Outcome(schedule)
        self.step = None
        self.call = None
        self.wedged = set()
        self.hosts = {name: _Host(self, name) for name in HOSTS}

    def clock(self):
        return self.now

    def close(self):
        for host in self.hosts.values():
            host.ledger.close()

    # Ground truth -------------------------------------------------------

    def beyond_commit(self):
        """True when no write tested at V0 can ever commit again, provably from a read."""
        return not self.server.present or self.server.uid != self.uid0 or self.server.resource_version != self.v0

    def settled(self):
        """DONE rows only replay their result, so once all hosts are done nothing can change."""
        return (not self.server.in_flight(self.action_id, kind=None)
                and all(host.phase(self.action_id) == "DONE" for host in self.hosts.values()))

    def violate(self, code, message):
        self.outcome.violations.append((code, self.step, message))
        self.outcome.trace.append(f"   !! {code}: {message}")

    def point(self, name):
        if name == "send.before" and self.call is not None:
            if any(r.host == self.call["host"] and r.action_id == self.action_id and r.kind == "write"
                   for r in self.server.requests):
                self.violate("I1", f"host {self.call['host']} sends a second write for the action")
        if name == "fence.send.after" and self.call is not None:
            host = self.call["host"]
            sent = [r for r in self.server.requests if r.host == host and r.kind == "fence"][-1]
            if _fence_nonce(sent) not in self.hosts[host].fence_nonces(self.action_id):
                self.violate("F2", f"host {host} sent fence #{sent.number} before journaling its nonce")
        if self.call is not None and self.call["crash"] == name and not self.call["crashed"]:
            self.call["crashed"] = True
            raise SimulatedCrash(name)

    def check_result(self, host, result):
        status = result["status"]
        live = [r for r in self.server.in_flight(self.action_id) if self.server.can_commit(r)]
        own_live = [r for r in live if r.host == host]
        sent = any(r.host == host for r in self.server.requests if r.action_id == self.action_id)
        if status == "APPLIED" and not self.server.committed(self.action_id):
            self.violate("I2", f"host {host} records APPLIED but no write of the action committed")
        if status in ("CONFLICT", "SUPERSEDED") and live:
            self.violate("I3", f"host {host} records {status} while request #{live[0].number} can still commit")
        if status == "CONFLICT" and self.server.committed(self.action_id, host):
            self.violate("I3", f"host {host} records CONFLICT although its own write committed")
        elif status == "CONFLICT" and self.committed_write_visible():
            self.violate("P1", f"host {host} records CONFLICT although a peer's write of the action committed "
                               "and is still on the object")
        if status != "NEEDS_RECONCILIATION":
            if own_live:
                self.violate("I5", f"host {host} records {status} while its own request can still commit")
            elif live and not (status == "FAILED" and not sent):
                self.violate("I5", f"host {host} records {status} while request #{live[0].number} can still commit")
        basis = _basis(result)
        if basis == "/FENCED" and not self.server.committed(self.action_id, host, kind="fence"):
            self.violate("F1", f"host {host} records {status}/FENCED but none of its fences committed")
        if status == "SUPERSEDED" and basis == "/ADVANCED_WITHOUT_EVIDENCE" and self.own_fence_visible(host):
            self.violate("F2", f"host {host} records SUPERSEDED although its own committed fence is on the object")
        peer = " (peer write)" if "adopted peer write" in str(result.get("details")) else ""
        self.outcome.statuses.setdefault(host, []).append(status + basis + peer)

    def own_fence_visible(self, host):
        """True if the object still carries the marker of a fence from host that committed."""
        if not self.server.present or self.server.uid != self.uid0:
            return False
        annotation = (self.server.deployment["metadata"].get("annotations") or {}).get(FENCE_ANNOTATION)
        nonce = json.loads(annotation)["nonce"] if annotation else None
        return any(_fence_nonce(r) == nonce for r in self.server.committed(self.action_id, host, kind="fence"))

    def committed_write_visible(self):
        """True if the live object still is exactly what a committed write of the action left:
        the same UID, its template and its evidence annotation."""
        if not self.server.present:
            return False
        metadata = self.server.deployment["metadata"]
        annotation = (metadata.get("annotations") or {}).get(core.ACTION_ANNOTATION)
        for request in self.server.committed(self.action_id):
            uid, _version, template, evidence = (operation["value"] for operation in request.operations)
            written = evidence[core.ACTION_ANNOTATION] if isinstance(evidence, dict) else evidence
            if (metadata["uid"], self.server.deployment["spec"]["template"], annotation) == (uid, template, written):
                return True
        return False

    def check_commits(self):
        if len(self.server.committed(self.action_id)) > 1 and not any(
                code == "I1" and "commit" in message for code, _, message in self.outcome.violations):
            self.violate("I1", "more than one write of the action committed")
        fences = self.server.committed(self.action_id, kind="fence")
        if fences and (self.server.committed(self.action_id) or len(fences) > 1) and not any(
                code == "F1" and "together" in message for code, _, message in self.outcome.violations):
            self.violate("F1", "a fence committed together with another fence or a write of the action")

    # Steps ----------------------------------------------------------------

    def run_call(self, label, kind, host_name, *, fate=None, crash=None):
        host = self.hosts[host_name]
        position = len(self.outcome.trace)
        self.call = {"host": host_name, "crash": crash, "crashed": False}
        host.cluster.last_read_ok = False
        if fate is not None:
            host.cluster.fate = fate
        host.cluster.fence_fate = _pick(self.fence_rng, _FINAL_FENCE_FATES if self.final else _FENCE_FATES)
        function = self.execute if kind == "execute" else self.reconcile
        text = label
        try:
            result = function(json.loads(json.dumps(self.proposal)), host.adapters)
            text += f" -> {result['status']}{_basis(result)}"
            completed = True
        except SimulatedCrash as crash_point:
            text += f" -> crashed at {crash_point}"
            completed = False
        except Exception as exc:  # noqa: BLE001 - the model reports, rather than hides, any executor error
            completed = False
            if isinstance(exc, ValueError) and str(exc) in _KNOWN_REFUSALS:
                text += " -> refused (no journaled write)"
            else:
                text += f" -> raised {type(exc).__name__}"
                self.violate("X", f"{kind}@{host_name} raised {type(exc).__name__}: {exc}")
        finally:
            host.cluster.fate = "ok"
            host.cluster.fence_fate = "ok"
            host.cluster.outage = False
            self.call = None
        self.outcome.trace.insert(position, text)
        if (completed and host.cluster.last_read_ok and self.beyond_commit()
                and host.phase(self.action_id) in OPEN and host_name not in self.wedged):
            self.wedged.add(host_name)  # Reported once per host; L1 covers the rest.
            self.violate("I4", f"host {host_name} still holds the workload ({host.phase(self.action_id)}) "
                               "although the target is beyond commit")

    def _request(self, which):
        requests = self.server.in_flight(self.action_id, kind=None)
        if not requests:
            return None
        return requests[0] if which == "oldest" else requests[-1]

    def external(self, kind, index):
        server = self.server
        value = str(index)

        def bump(deployment):
            deployment["metadata"].setdefault("annotations", {})["example.org/bump"] = value

        def erase(deployment):
            (deployment["metadata"].get("annotations") or {}).pop(core.ACTION_ANNOTATION, None)

        def template(deployment):
            deployment["spec"]["template"].setdefault("metadata", {}).setdefault(
                "annotations", {})["example.org/edit"] = value

        def restore(deployment):
            deployment["spec"]["template"] = json.loads(json.dumps(self.baseline["pod_template"]))

        try:
            if kind == "delete":
                changed = server.delete()
            elif kind == "recreate":
                changed = server.recreate("recreated-uid-" + value)
            else:
                changed = server.external_write({"bump": bump, "erase-evidence": erase, "template": template,
                                                 "restore-template": restore}[kind])
        except PatchFailed:
            changed = False
        return changed

    def apply(self, index, step):
        self.step = index
        self.now += timedelta(seconds=1)
        prefix = f"{index:02d} {step.describe()}"
        if step.kind in ("execute", "reconcile"):
            self.run_call(prefix, step.kind, step.host, fate=step.arg, crash=step.crash)
        elif step.kind in ("deliver", "drop"):
            request = self._request(step.arg)
            if request is None:
                self.outcome.trace.append(prefix + " -> nothing in flight")
            elif step.kind == "deliver":
                committed = self.server.deliver(request)
                self.outcome.trace.append(prefix + f" -> #{request.number} from {request.host} "
                                          + ("committed rv=" + str(request.resource_version) if committed
                                             else "rejected"))
            else:
                self.server.drop(request)
                self.outcome.trace.append(prefix + f" -> #{request.number} from {request.host} lost")
        elif step.kind == "external":
            changed = self.external(step.arg, index)
            self.outcome.trace.append(prefix + (f" -> rv={self.server.resource_version} uid={self.server.uid}"
                                                if changed else " -> no change"))
        elif step.kind == "outage":
            self.hosts[step.host].cluster.outage = True
            self.outcome.trace.append(prefix)
        elif step.kind == "clock":
            self.now += timedelta(seconds=int(step.arg))
            self.outcome.trace.append(prefix)
        self.check_commits()

    def finish(self):
        self.step = "end"
        for index, request in enumerate(self.server.in_flight(self.action_id, kind=None)):
            label = "fence " if request.kind == "fence" else ""
            if self.schedule.drain[index % len(self.schedule.drain)]:
                committed = self.server.deliver(request)
                self.outcome.trace.append(f"drain {label}#{request.number} from {request.host} -> "
                                          + ("committed" if committed else "rejected"))
            else:
                self.server.drop(request)
                self.outcome.trace.append(f"drain {label}#{request.number} from {request.host} -> lost")
        self.check_commits()
        self.final = True
        for number in range(self.passes):
            self.now += timedelta(seconds=61)
            for name, host in self.hosts.items():
                for proposal in host.ledger.pending_proposals():
                    if proposal["action_id"] == self.action_id:
                        self.run_call(f"pass {number + 1} reconcile@{name}", "reconcile", name)
        journaled = [name for name, host in self.hosts.items() if host.phase(self.action_id) not in (None, "PREPARED")]
        if not journaled:
            self.outcome.category = "never-journaled"
            return
        self.check_commits()
        if not self.beyond_commit():
            # Every request was lost and nothing else wrote: no read can tell lost from late.
            self.outcome.category = "unprovable"
            if self.fencing:
                # A fence turns this case into a proof, so nothing may stay in doubt.
                for name in journaled:
                    if self.hosts[name].phase(self.action_id) in OPEN:
                        self.violate("L1", f"host {name} is still in doubt after {self.passes} reconcile "
                                           "passes although fencing is on")
            return
        self.outcome.category = "provable"
        for name in journaled:
            phase = self.hosts[name].phase(self.action_id)
            if phase in OPEN:
                self.violate("L1", f"host {name} is still {phase} after {self.passes} reconcile passes")
        fresh = json.loads(json.dumps(self.proposal))
        fresh["action_id"] += "-NEXT"
        fresh["rationale"] = "A later proposal for the same workload."
        for name, host in self.hosts.items():
            try:
                host.ledger.prepare(fresh)
            except store.TargetBusy:
                self.violate("L1", f"host {name} refuses a new proposal for the workload (TargetBusy)")


def _basis(result):
    details = result.get("details", "") if isinstance(result, dict) else ""
    if isinstance(details, str) and details.startswith("[basis=") and "]" in details:
        return "/" + details[len("[basis="):details.index("]")]
    return ""


def run_schedule(schedule, **options):
    run = Run(schedule, **options)
    try:
        for index, step in enumerate(schedule.steps):
            run.apply(index, step)
            if run.settled():
                run.outcome.trace.append("(every host is done and nothing is in flight; later steps cannot matter)")
                break
        run.finish()
    finally:
        run.close()
    return run.outcome


def check_seed(seed, *, steps=STEPS, **options):
    return run_schedule(generate(seed, steps), **options)


def shrink(schedule, code, **options):
    """Greedily drop steps, crashes and odd fates while the same invariant still fails."""
    def fails(steps):
        return code in run_schedule(Schedule(schedule.seed, tuple(steps), schedule.drain), **options).codes

    steps = list(schedule.steps)
    progress = True
    while progress:
        progress = False
        for index in range(len(steps)):
            candidate = steps[:index] + steps[index + 1:]
            if fails(candidate):
                steps, progress = candidate, True
                break
        if progress:
            continue
        for index, step in enumerate(steps):
            simpler = [replace(step, crash=None)] if step.crash else []
            if step.kind == "execute" and step.arg != "ok":
                simpler.append(replace(step, arg="ok"))
            for option in simpler:
                candidate = steps[:index] + [option] + steps[index + 1:]
                if fails(candidate):
                    steps, progress = candidate, True
                    break
            if progress:
                break
    return Schedule(schedule.seed, tuple(steps), schedule.drain)


def explain(outcome, code, **options):
    """A readable report: the seed, the invariant and a minimal failing trace."""
    minimal = run_schedule(outcome.schedule, **options)
    if code not in minimal.codes:
        # Shrinking needs the same Executor variant (patches, hooks) that produced the failure.
        lines = [f"{code} failed for seed {outcome.schedule.seed}: {INVARIANTS[code]}",
                 "It did not fail again with these options; the original trace:"]
        return "\n".join(lines + ["  " + line for line in outcome.trace])
    minimal = run_schedule(shrink(outcome.schedule, code, **options), **options)
    lines = [f"{code} failed for seed {outcome.schedule.seed}: {INVARIANTS[code]}", "Minimal trace:"]
    lines += ["  " + line for line in minimal.trace]
    return "\n".join(lines)


@dataclass
class Report:
    seeds: int = 0
    failures: Counter = field(default_factory=Counter)  # code -> seeds that violate it
    first: dict = field(default_factory=dict)  # code -> first failing Outcome
    categories: Counter = field(default_factory=Counter)
    observed: Counter = field(default_factory=Counter)  # recorded status/basis -> seeds that saw it

    @property
    def safety_holds(self):
        return not any(self.failures[code] for code in SAFETY)

    @property
    def liveness_holds(self):
        return not any(self.failures[code] for code in LIVENESS)

    @property
    def adoption_holds(self):
        return not any(self.failures[code] for code in ADOPTION)

    @property
    def fencing_holds(self):
        return not any(self.failures[code] for code in FENCING)

    def summary(self):
        return {"seeds": self.seeds, "steps_per_seed": STEPS, "reconcile_passes": PASSES,
                "safety_holds": self.safety_holds, "liveness_holds": self.liveness_holds,
                "adoption_holds": self.adoption_holds, "fencing_holds": self.fencing_holds,
                "failing_seeds": {code: self.failures[code] for code in INVARIANTS
                                  if code != "X" or self.failures[code]},
                "first_failing_seed": {code: outcome.schedule.seed for code, outcome in sorted(self.first.items())},
                "end_states": dict(sorted(self.categories.items())),
                "observed_in_seeds": dict(sorted(self.observed.items()))}


def check(seeds, *, steps=STEPS, **options):
    report = Report()
    fixtures = options.pop("fixtures", None) or load_fixtures()
    for seed in seeds:
        outcome = check_seed(seed, steps=steps, fixtures=fixtures, **options)
        report.seeds += 1
        report.categories[outcome.category] += 1
        seen = {status for statuses in outcome.statuses.values() for status in statuses}
        if any("crashed at" in line for line in outcome.trace):
            seen.add("process crash")
        report.observed.update(seen)
        for code in outcome.codes:
            report.failures[code] += 1
            report.first.setdefault(code, outcome)
    return report


def seeds_from_environment(default=DEFAULT_SEEDS):
    """SENTINEL_MODEL_SEEDS: a count ('5000') or a half-open range ('2000:10000')."""
    value = os.environ.get("SENTINEL_MODEL_SEEDS", "").strip()
    if not value:
        return range(default)
    start, _, stop = value.partition(":")
    return range(int(start), int(stop)) if stop else range(int(start))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the Executor in-flight write model checker offline.")
    parser.add_argument("--seeds", type=int, default=None,
                        help="number of seeds (default 2000 or SENTINEL_MODEL_SEEDS)")
    parser.add_argument("--start", type=int, default=0, help="first seed")
    parser.add_argument("--explain", action="store_true", help="print a minimal trace for each failing invariant")
    parser.add_argument("--no-fence", action="store_true", help="run the Executor with fencing disabled")
    args = parser.parse_args(argv)
    seeds = range(args.start, args.start + args.seeds) if args.seeds is not None else seeds_from_environment()
    options = {"fence_after": None if args.no_fence else FENCE_AFTER}
    report = check(seeds, **options)
    summary = report.summary()
    summary["executor"] = "decision-table" if hasattr(core, "classify") else "pre-table"
    summary["fencing"] = options["fence_after"] is not None and can_fence()
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    if args.explain:
        for code, outcome in sorted(report.first.items()):
            print("\n" + explain(outcome, code, **options), file=sys.stderr)
    if not report.safety_holds:
        return 1
    return 0 if report.liveness_holds and report.adoption_holds and report.fencing_holds else 2


if __name__ == "__main__":
    raise SystemExit(main())
