"""Run one guarded live rollback or inspect a possibly committed write.

The caller supplies protected, authoritative policy/baseline/dependency/approval
files and an independent execution-scope allowlist. JSON files do not authenticate
operators or evidence producers. Use the restricted executor kubeconfig, never an
administrator's credentials. APPLIED is execution evidence, not verified health.

build_adapters returns the same injected interface used by the local team worker.
Its ledger and lock directory must be shared by every worker on one host. There
are no distributed leases. Reconciliation needs only retained scope,
credentials and proposal (the local journal is optional); it never reauthorizes
or starts a spec mutation. --baselines DIR replaces --baseline with one
measured baseline per target, DIR/<target name>.json; a file whose target is
not the proposal's target refuses execution. With --fence-after SECONDS it may send one
metadata-only fence for a write still in doubt after that long (0 fences at
once). --transport rest (P5.08) reads and writes the Deployment over HTTPS
instead of kubectl; kubectl stays the default. Exit codes: 0 APPLIED/NOOP, 2 FAILED, 3 CONFLICT,
4 NEEDS_RECONCILIATION, 5 SUPERSEDED, 1 no result.
"""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

from services.executor.core import ExecutionAdapters, GuardCheck, execute, reconcile
from services.executor.store import (
    ActionConflict, LocalTargetCoordinator, SQLiteExecutionLedger, TargetBusy, target_key,
)
from services.guard.core import GuardInputError, assert_decision_usable, evaluate
from services.guard.adapters import AdapterError, load_json, read_reference_versions
from services.guard.decisions import validate_policy
from services.guard.eligibility import referenced_objects
from services.guard.preview import server_dry_run as collect_server_preview
from shared import log
from shared.contracts import ContractError, validate_message


LOG = log.get_logger("executor")
EXIT_CODES = {"APPLIED": 0, "NOOP": 0, "FAILED": 2, "CONFLICT": 3, "NEEDS_RECONCILIATION": 4, "SUPERSEDED": 5}
# The Executor's Deployment transport: kubectl (default) or the structured REST client (P5.08).
TRANSPORTS = ("kubectl", "rest")
# A baseline file name is the target's Kubernetes name, so it can never leave the directory.
_TARGET_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")


def _utc_now():
    return datetime.now(timezone.utc)


def target_baseline(directory, target):
    """The measured baseline for target from directory/<name>.json; AdapterError if it is for another target."""
    name = target.get("name") if isinstance(target, dict) else None
    if not isinstance(name, str) or not _TARGET_NAME.fullmatch(name):
        raise AdapterError("INVALID_BASELINE", "baseline", "The proposal target has no valid baseline name")
    baseline = load_json(Path(directory) / (name + ".json"), "baseline")
    if not isinstance(baseline, dict) or baseline.get("target") != target:
        raise AdapterError("BASELINE_TARGET_MISMATCH", "baseline", "The baseline belongs to another target")
    return baseline


class LiveGuardAuthorizer:
    """Collect live evidence for the exact snapshot already read under the lock.

    Paths are reread for each validation. Reference metadata augments a copy of
    that same Deployment snapshot; there is no second independent state lookup.
    The standard Guard owns every policy, approval and evidence decision.
    Each check reads clock (UTC) itself, after any slow evidence collection,
    rather than the caller's earlier now; it is this host's clock, the same
    one the Executor stamps its records with.
    """

    def __init__(self, *, policy, baseline, dependencies, approval, context,
                 cluster_id, kubeconfig, server_dry_run=False, approval_reader=None, clock=None,
                 baselines=None):
        self.paths = dict(policy=policy, baseline=baseline,
                          dependencies=dependencies, approval=approval)
        self.baselines = baselines
        self.clock = clock or (lambda: _utc_now())
        self.context, self.cluster_id, self.kubeconfig = context, cluster_id, kubeconfig
        self.dependency_reader = lambda proposal: load_json(self.paths["dependencies"], "dependencies")
        self.server_dry_run = server_dry_run
        self.approval_reader = approval_reader or (lambda proposal: load_json(self.paths["approval"], "approval"))

    def validate(self, proposal, live_state, *, now):
        policy = load_json(self.paths["policy"], "policy")
        validate_policy(policy, require_live=True)
        inputs = {
            "proposal": deepcopy(proposal), "live_state": deepcopy(live_state),
            "policy": policy,
            "baseline": (target_baseline(self.baselines, proposal["target"]) if self.baselines is not None
                         else load_json(self.paths["baseline"], "baseline")),
            "dependencies": self.dependency_reader(deepcopy(proposal)),
            "approval": deepcopy(self.approval_reader(deepcopy(proposal))), "preview": None,
        }
        baseline = inputs["baseline"]
        snapshot = inputs["live_state"]
        if policy.get("baseline_constraints", {}).get("require_compatible_references"):
            references = []
            templates = [baseline.get("pod_template", {}) if isinstance(baseline, dict) else {},
                         snapshot["deployment"].get("spec", {}).get("template", {})]
            for template in templates:
                selected, _findings = referenced_objects(template, proposal["target"]["namespace"])
                references.extend(selected)
            snapshot["reference_versions"] = read_reference_versions(
                proposal["target"], references, context=self.context,
                cluster_id=self.cluster_id, kubeconfig=self.kubeconfig,
            )
        decision = evaluate(**inputs, now=self.clock())
        preview_only = (decision["verdict"] == "ESCALATE"
                        and set(decision["reason_codes"]) <= {"SERVER_DRY_RUN_REQUIRED"})
        if self.server_dry_run and (decision["verdict"] == "ALLOW" or preview_only):
            inputs["preview"] = collect_server_preview(
                proposal, snapshot, baseline, context=self.context,
                cluster_id=self.cluster_id, kubeconfig=self.kubeconfig,
            )
            # Remote admission latency consumes the evidence/approval validity window.
            decision = evaluate(**inputs, now=self.clock())
        return GuardCheck(decision=decision, baseline=baseline, inputs=inputs)

    def assert_usable(self, check, *, now):
        current = deepcopy(self.approval_reader(deepcopy(check.inputs["proposal"])))
        if current != check.inputs["approval"]:
            raise GuardInputError("Authoritative approval changed; fresh validation is required")
        return assert_decision_usable(check.decision, **check.inputs, now=self.clock())


def _same_file(first, second):
    return first == second or (first.exists() and second.exists() and os.path.samefile(first, second))


def _protect_outputs(ledger, locks, inputs, allowed_targets, directories=()):
    """Reject writable aliases of inputs/credentials before opening SQLite/locks.

    directories are input directories (a baselines directory): the ledger and
    the lock directory must not be inside one, nor contain one.
    """
    try:
        database, lock_directory = Path(ledger).resolve(), Path(locks).resolve()
        for folder in (Path(path).resolve() for path in directories if path is not None):
            if (folder in database.parents or folder == lock_directory or folder in lock_directory.parents
                    or lock_directory in folder.parents):
                raise ValueError("Ledger or locks overlap an input directory")
        protected = [Path(path).resolve() for path in inputs if path is not None]
        writable = [database, *(Path(str(database) + suffix) for suffix in ("-journal", "-wal", "-shm"))]
        writable.extend(lock_directory / (target_key(target) + ".lock") for target in allowed_targets)
        for source in protected:
            if source == lock_directory or lock_directory in source.parents:
                raise ValueError("Lock directory contains a protected input")
            if any(_same_file(destination, source) for destination in writable):
                raise ValueError("Output aliases a protected input")
        if database == lock_directory or lock_directory in database.parents:
            raise ValueError("Ledger must be outside the lock directory")
    except (OSError, TypeError, ValueError, RuntimeError, KeyError) as exc:
        raise AdapterError("ARGUMENT_ERROR", "command_line",
                           "Ledger and locks must be separate from inputs and credentials") from exc


def build_adapters(*, context, cluster_id, kubeconfig, allowed_targets, ledger, locks,
                   policy=None, baseline=None, dependencies=None, approval=None,
                   server_dry_run=False, reconcile_only=False, runner=None, approval_reader=None,
                   fence_after_seconds=None, clock=None, baselines=None, transport="kubectl"):
    """Build the standard team ExecutionAdapters using an explicit live client.

    allowed_targets is a trusted list of complete target identities or its JSON
    path, independent of the proposal. New execution requires all four trusted
    input paths (or an injected authoritative approval_reader) and a strict live
    policy before any API read. baselines, a directory of per-target baselines,
    replaces baseline; at most one of them is given. reconcile_only
    defers those files entirely because the core reconciliation path never
    writes the spec. fence_after_seconds comes from trusted configuration only;
    None (the default) disables fencing. clock (UTC) is this host's one clock:
    the snapshot, the Guard re-check and the ledger stamps all read it.
    transport, also trusted configuration only, picks the Executor's
    Deployment client: "kubectl" (the default) or "rest", which runs one
    read-only kubectl config view for the credentials and then speaks HTTPS
    itself. The Guard's optional reference reads and server dry run still
    use kubectl either way. The caller owns and must close adapters.ledger.
    """
    from services.executor.kubernetes_adapter import KubectlDeploymentAPI

    if transport not in TRANSPORTS:
        raise AdapterError("ARGUMENT_ERROR", "transport", "The Executor transport must be kubectl or rest")
    clock = clock or _utc_now
    scope_path = allowed_targets if isinstance(allowed_targets, (str, Path)) else None
    targets = load_json(scope_path, "allowed_targets") if scope_path is not None else deepcopy(allowed_targets)
    if not isinstance(targets, list) or not 1 <= len(targets) <= 128:
        raise AdapterError("INVALID_EXECUTION_SCOPE", "allowed_targets", "A nonempty trusted target allowlist is required")
    if not kubeconfig:
        raise AdapterError("ARGUMENT_ERROR", "kubeconfig", "An explicit restricted kubeconfig is required")
    paths = dict(policy=policy, baseline=baseline, dependencies=dependencies, approval=approval)
    if baseline is not None and baselines is not None:
        raise AdapterError("ARGUMENT_ERROR", "command_line", "Give either one baseline or a baselines directory")
    _protect_outputs(ledger, locks, [*paths.values(), kubeconfig, scope_path], targets, [baselines])
    if not reconcile_only:
        if (any(paths[key] is None for key in ("policy", "dependencies")) or (baseline is None and baselines is None)
                or (approval is None and approval_reader is None)):
            raise AdapterError("ARGUMENT_ERROR", "command_line", "Execution requires trusted policy, baseline, dependencies and approval paths")
        validate_policy(load_json(policy, "policy"), require_live=True)
    options = dict(context=context, cluster_id=cluster_id, kubeconfig=kubeconfig,
                   allowed_targets=targets, clock=clock)
    if runner is not None:
        options["runner"] = runner
    if transport == "rest":
        from services.executor.rest_adapter import KubernetesRESTDeploymentAPI
        cluster = KubernetesRESTDeploymentAPI.from_kubeconfig(**options)
    else:
        cluster = KubectlDeploymentAPI(**options)
    authorizer = LiveGuardAuthorizer(**paths, context=context, cluster_id=cluster_id,
                                     kubeconfig=kubeconfig, server_dry_run=server_dry_run,
                                     approval_reader=approval_reader, clock=clock, baselines=baselines)
    coordination = LocalTargetCoordinator(locks)
    store = SQLiteExecutionLedger(ledger)
    return ExecutionAdapters(ledger=store, coordination=coordination, cluster=cluster,
                             authorizer=authorizer, clock=clock, fence_after_seconds=fence_after_seconds)


def _fence_after(value):
    seconds = float(value)
    if not 0 <= seconds < float("inf"):
        raise ValueError("Fence delay must be a non-negative finite number")
    return seconds


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise AdapterError("ARGUMENT_ERROR", "command_line", "Invalid arguments; use --help for usage")


def _parser():
    parser = _Parser(description=__doc__)
    for name in ("proposal", "allowed-targets", "kubeconfig", "ledger", "locks"):
        parser.add_argument(f"--{name}", required=True, metavar="PATH")
    for name in ("policy", "baseline", "dependencies", "approval"):
        parser.add_argument(f"--{name}", metavar="PATH", help="Trusted JSON input; required for new execution")
    parser.add_argument("--baselines", metavar="DIR",
                        help="Trusted per-target baselines (DIR/<target name>.json), instead of --baseline")
    parser.add_argument("--context", required=True, help="Explicit restricted kubectl context")
    parser.add_argument("--cluster-id", required=True, help="Trusted Sentinel cluster identity")
    parser.add_argument("--server-dry-run", action="store_true", help="Obtain optional server admission evidence before execution")
    parser.add_argument("--reconcile", action="store_true", help="Inspect a possibly committed write; no spec write")
    parser.add_argument("--fence-after", type=_fence_after, metavar="SECONDS",
                        help="Fence a write still in doubt (same UID and version) after this long; 0 fences now")
    parser.add_argument("--transport", choices=TRANSPORTS, default="kubectl",
                        help="Deployment client: kubectl (default) or rest (HTTPS; definite rejections decided at once)")
    return parser


def main(argv=None):
    """Emit one execution result, or a bounded error without raw API/file data."""
    adapters = None
    try:
        args = _parser().parse_args(argv)
        proposal = load_json(args.proposal, "proposal")
        validate_message(proposal)
        if proposal["kind"] != "proposal":
            raise ContractError("Expected a proposal")
        _protect_outputs(args.ledger, args.locks,
                         [args.proposal, args.policy, args.baseline, args.dependencies,
                          args.approval, args.kubeconfig, args.allowed_targets],
                         [proposal["target"]], [args.baselines])
        adapters = build_adapters(
            context=args.context, cluster_id=args.cluster_id, kubeconfig=args.kubeconfig,
            allowed_targets=args.allowed_targets, ledger=args.ledger, locks=args.locks,
            policy=args.policy, baseline=args.baseline, dependencies=args.dependencies,
            approval=args.approval, server_dry_run=args.server_dry_run,
            reconcile_only=args.reconcile, fence_after_seconds=args.fence_after, baselines=args.baselines,
            transport=args.transport,
        )
        result = (reconcile if args.reconcile else execute)(proposal, adapters)
    except Exception as exc:  # noqa: BLE001 - top-level boundary: print one error code and exit 1
        code = (exc.code if isinstance(exc, AdapterError) else
                "TARGET_BUSY" if isinstance(exc, TargetBusy) else
                "ACTION_CONFLICT" if isinstance(exc, ActionConflict) else
                "INVALID_EXECUTOR_INPUT" if isinstance(exc, ContractError) else "EXECUTOR_ERROR")
        log.failure(LOG, "executor.run.failed", exc, level=log.ERROR, reason=code)
        print(json.dumps({"kind": "executor_error", "error": {"code": code,
                          "message": "Execution did not produce a result; inspect trusted inputs and local records."}}), file=sys.stderr)
        return 1
    finally:
        if adapters is not None:
            adapters.ledger.close()
    print(json.dumps(result, allow_nan=False))
    return EXIT_CODES[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
