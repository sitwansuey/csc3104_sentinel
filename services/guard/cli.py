"""Evaluate a Guard proposal, optionally collecting live admission evidence."""

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile

from services.guard.core import evaluate_with_report
from services.guard.adapters import AdapterError, load_json, read_deployment, read_reference_versions
from services.guard.decisions import validate_policy
from services.guard.eligibility import referenced_objects
from services.guard.preview import _private_file, _same_deployment, server_dry_run
from shared.contracts import ContractError, validate_message


EXIT_CODES = {"ALLOW": 0, "REJECT": 2, "ESCALATE": 3}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Do not echo arbitrary arguments or input contents into diagnostics.
        raise AdapterError("ARGUMENT_ERROR", "command_line", "Invalid arguments; use --help for usage")


def _utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an ISO UTC timestamp") from exc
    if parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError("expected a timezone-aware UTC timestamp")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    for name in ("proposal", "policy", "baseline", "dependencies"):
        parser.add_argument(f"--{name}", required=True, metavar="PATH", help=f"Trusted {name} JSON input")
    parser.add_argument("--approval", metavar="PATH", help="Trusted operator approval JSON; never fabricated by the CLI")
    state = parser.add_mutually_exclusive_group(required=True)
    state.add_argument("--live-state", metavar="PATH", help="Observation-shaped JSON snapshot")
    state.add_argument("--context", help="Explicit kubectl context for a read-only Deployment lookup")
    parser.add_argument("--cluster-id", help="Operator-configured cluster identity for --context; never inferred from the proposal")
    parser.add_argument("--kubeconfig", metavar="PATH", help="Optional private kubeconfig for --context; does not modify the environment")
    parser.add_argument("--now", type=_utc_timestamp, metavar="UTC", help="Fixture-only evaluation time; forbidden with --context")
    preview = parser.add_mutually_exclusive_group()
    preview.add_argument("--server-dry-run", action="store_true", help="Preview a bound rollback using server admission; requires --context and patch authorization")
    preview.add_argument("--preview", metavar="PATH", help="Trusted server-preview snapshot JSON for offline fixture evaluation only")
    parser.add_argument("--report", metavar="PATH", help="Atomically write the bounded explanation report before emitting a decision")
    return parser


def _error(code: str, source: str, message: str) -> int:
    print(json.dumps({
        "kind": "guard_error", "error": {
            "code": code, "input": source, "message": message,
        },
    }), file=sys.stderr)
    return 1


def _report_destination(args):
    """Reject aliases of any input, including symlinks and existing hard links."""
    if args.report is None:
        return None
    try:
        destination = Path(args.report).resolve()
        for name in ("proposal", "policy", "baseline", "dependencies", "approval",
                     "live_state", "preview", "kubeconfig"):
            value = getattr(args, name)
            if value is None:
                continue
            source = Path(value).resolve()
            if destination == source or (
                destination.exists() and source.exists() and os.path.samefile(destination, source)
            ):
                raise AdapterError("ARGUMENT_ERROR", "command_line",
                                   "The report destination must differ from every input and kubeconfig")
        return destination
    except (OSError, ValueError, RuntimeError) as exc:
        raise AdapterError("ARGUMENT_ERROR", "command_line", "Invalid report destination") from exc


def _write_report(destination, report):
    """Publish a complete private JSON record atomically in its directory."""
    temporary = None
    try:
        descriptor, path = tempfile.mkstemp(prefix=".sentinel-report-", suffix=".json",
                                            dir=destination.parent)
        temporary = Path(path)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            _private_file(temporary)
            json.dump(report, stream, allow_nan=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise AdapterError("REPORT_UNAVAILABLE", "report",
                           "The Guard report could not be written") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                raise AdapterError("REPORT_UNAVAILABLE", "report",
                                   "The Guard report temporary file could not be removed") from exc


def main(argv: list[str] | None = None) -> int:
    """Emit exactly one JSON result, or a JSON error with no authorisation."""
    try:
        args = _parser().parse_args(argv)
        if args.context is not None and args.now is not None:
            raise AdapterError("ARGUMENT_ERROR", "command_line", "--now is only allowed with --live-state fixture inputs")
        if args.context is not None and not args.cluster_id:
            raise AdapterError("ARGUMENT_ERROR", "command_line", "--context requires an operator-configured --cluster-id")
        if args.live_state is not None and (args.cluster_id is not None or args.kubeconfig is not None):
            raise AdapterError("ARGUMENT_ERROR", "command_line", "--cluster-id and --kubeconfig require --context")
        if args.server_dry_run and args.context is None:
            raise AdapterError("ARGUMENT_ERROR", "command_line", "--server-dry-run requires --context")
        if args.preview is not None and args.context is not None:
            raise AdapterError("ARGUMENT_ERROR", "command_line", "--preview is only allowed with --live-state fixtures")
        report_destination = _report_destination(args)
        proposal = load_json(args.proposal, "proposal")
        validate_message(proposal)
        if proposal["kind"] != "proposal":
            raise ContractError("Expected a proposal")
        policy = load_json(args.policy, "policy")
        baseline = load_json(args.baseline, "baseline")
        dependencies = load_json(args.dependencies, "dependencies")
        approval = load_json(args.approval, "approval") if args.approval else None
        preview = load_json(args.preview, "preview") if args.preview else None
        if args.context is not None:
            # Refuse development/under-specified policies before any kubectl
            # request. The pure evaluator validates the remaining inputs again.
            validate_policy(policy, require_live=True)
            live_state = read_deployment(proposal["target"], context=args.context,
                                         cluster_id=args.cluster_id, kubeconfig=args.kubeconfig)
            matching_live = (
                isinstance(live_state, dict)
                and live_state.get("cluster_id") == args.cluster_id == proposal["target"]["cluster_id"]
                and _same_deployment(live_state.get("deployment"), proposal["target"],
                                     proposal["based_on_resource_version"])
            )
            if matching_live and policy.get("baseline_constraints", {}).get("require_compatible_references", False):
                template = baseline.get("pod_template") if isinstance(baseline, dict) else None
                refs, _findings = referenced_objects(template, proposal["target"]["namespace"])
                # Unsupported/malformed references are independently rediscovered
                # by the pure evaluator; an empty list never overrides findings.
                live_state["reference_versions"] = read_reference_versions(
                    proposal["target"], refs, context=args.context,
                    cluster_id=args.cluster_id, kubeconfig=args.kubeconfig,
                ) if refs else []
        else:
            live_state = load_json(args.live_state, "live_state")
        result, report = evaluate_with_report(
            proposal, live_state, policy, baseline, dependencies,
            approval=approval, now=args.now, preview=preview,
        )
        if args.server_dry_run and (
            result["verdict"] == "ALLOW" or (
                result["verdict"] == "ESCALATE"
                and set(result["reason_codes"]) <= {"SERVER_DRY_RUN_REQUIRED"}
            )
        ):
            preview = server_dry_run(proposal, live_state, baseline, context=args.context,
                                     cluster_id=args.cluster_id, kubeconfig=args.kubeconfig)
            # Live calls use a fresh real clock after admission latency; expired
            # state/approval must prevent authorization at this second check.
            result, report = evaluate_with_report(
                proposal, live_state, policy, baseline, dependencies,
                approval=approval, now=args.now, preview=preview,
            )
        if report_destination is not None:
            _write_report(report_destination, report)
    except AdapterError as exc:
        return _error(exc.code, exc.source, str(exc))
    except ContractError:
        return _error("INVALID_GUARD_INPUT", "evaluation", "Input did not satisfy the Guard contract")
    print(json.dumps(result, allow_nan=False))
    return EXIT_CODES[result["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
