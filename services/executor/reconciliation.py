"""Total, pure decision table for reconciling one journaled Deployment write.

classify() answers two questions from one read of the target: did this action's
write commit, and can it still commit? It relies on three Kubernetes facts:

1. Our patch always changes the object (it writes a fresh evidence annotation),
   so a commit always advances metadata.resourceVersion.
2. The patch carries test ops on metadata.uid and metadata.resourceVersion == V0,
   and the API server commits it through an etcd transaction conditioned on the
   object it tested. It can only commit while the live version is still V0.
3. A resourceVersion never returns to an earlier value for the same object.
   This assumes no etcd restore without --bump-revision.

So once the object is gone, has another UID, or has a version other than V0,
the request can never commit, even if it is still in flight. Only "same UID,
version still V0" stays in doubt. Versions are compared for equality only.

The module has no I/O. FENCED and DEFINITE_REJECTION are reserved for later
fencing and rejection-classification stages; production code supplies neither.
"""

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Any, Mapping, Optional

from shared.canonical import canonical_json, sha256_hex as fingerprint


ACTION_ANNOTATION = "sentinel.io/execution"
_EVIDENCE_FIELDS = frozenset({"action_id", "proposal_sha256", "target_uid", "template_sha256", "attempt_id"})
_NONCE = re.compile(r"[a-f0-9]{32}\Z")
_IDENTITY_FIELDS = ("cluster_id", "kind", "namespace", "name")


class Basis(str, Enum):
    """Why a reconciliation decision was reached; recorded in result details."""

    EVIDENCE_MATCH = "EVIDENCE_MATCH"
    COMMITTED_THEN_OVERWRITTEN = "COMMITTED_THEN_OVERWRITTEN"
    TARGET_DELETED = "TARGET_DELETED"
    TARGET_REPLACED = "TARGET_REPLACED"
    ADVANCED_WITHOUT_EVIDENCE = "ADVANCED_WITHOUT_EVIDENCE"
    FENCED = "FENCED"  # Reserved: nothing produces fence nonces yet.
    DEFINITE_REJECTION = "DEFINITE_REJECTION"  # Reserved: no rejection classifier yet.
    UNCHANGED_IN_DOUBT = "UNCHANGED_IN_DOUBT"
    READ_UNAVAILABLE = "READ_UNAVAILABLE"
    INCONSISTENT = "INCONSISTENT"


@dataclass(frozen=True)
class Observation:
    """The facts from one read of the named target that the table may use."""

    present: bool
    uid: Optional[str] = None
    resource_version: Optional[str] = None
    evidence_annotation: Optional[str] = None
    fence_annotation: Optional[str] = None
    template_sha256: Optional[str] = None


ABSENT = Observation(present=False)


@dataclass(frozen=True)
class Decision:
    """status is an execution_result status; writer is 'own' or 'peer:<attempt>'."""

    status: str
    basis: Basis
    version: Optional[str]
    may_fence: bool = False
    writer: Optional[str] = None


def observe(snapshot: Any, target: Mapping[str, Any]) -> Optional[Observation]:
    """Build an Observation, or None if the snapshot cannot support a decision."""
    try:
        deployment = snapshot["deployment"]
        metadata = deployment["metadata"]
        identity = {"cluster_id": snapshot["cluster_id"], "kind": deployment["kind"],
                    "namespace": metadata["namespace"], "name": metadata["name"]}
        uid, version = metadata["uid"], metadata["resourceVersion"]
        if (any(identity[field] != target[field] for field in _IDENTITY_FIELDS)
                or not isinstance(uid, str) or not uid.strip()
                or not isinstance(version, str) or not version.strip()):
            return None
        annotations = metadata.get("annotations")
        evidence = annotations.get(ACTION_ANNOTATION) if isinstance(annotations, dict) else None
        template = deployment.get("spec", {}).get("template")
        digest = fingerprint(template) if isinstance(template, dict) else None
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        return None
    return Observation(present=True, uid=uid, resource_version=version,
                       evidence_annotation=evidence if isinstance(evidence, str) else None,
                       template_sha256=digest)


def action_attempt(annotation: Optional[str], proposal: Mapping[str, Any]) -> Optional[str]:
    """Return the attempt nonce if the annotation is exact evidence for this action.

    Matching is at action level: action ID, proposal hash, target UID and
    template hash must all match, so a peer's attempt of the same action counts.
    A copied marker for another action or a changed proposal does not.
    """
    if not isinstance(annotation, str) or len(annotation) > 4096:
        return None
    try:
        evidence = json.loads(annotation)
        if (not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_FIELDS
                or canonical_json(evidence) != annotation):
            return None
        attempt = evidence["attempt_id"]
        matches = (evidence["action_id"] == proposal["action_id"]
                   and evidence["proposal_sha256"] == fingerprint(proposal)
                   and evidence["target_uid"] == proposal["target"]["uid"]
                   and evidence["template_sha256"] == proposal["parameters"]["template_sha256"]
                   and isinstance(attempt, str) and _NONCE.fullmatch(attempt) is not None)
    except (KeyError, TypeError, ValueError, RecursionError):
        return None
    return attempt if matches else None


def classify(row: Optional[Mapping[str, Any]], proposal: Mapping[str, Any],
             observation: Optional[Observation], rejection: Any = None, *,
             fence_nonces: frozenset[str] = frozenset()) -> Decision:
    """Evaluate rows R0-R6 in order; the first match wins.

    row is the local ledger entry or None. V0 is its expected_version, or the
    proposal's based_on_resource_version for a stateless or adopted row.
    """
    if observation is None:  # R0
        return Decision("NEEDS_RECONCILIATION", Basis.READ_UNAVAILABLE, None)
    if not observation.present:  # R1
        return Decision("SUPERSEDED", Basis.TARGET_DELETED, None)
    version = observation.resource_version
    if observation.uid != proposal["target"]["uid"]:  # R2
        return Decision("SUPERSEDED", Basis.TARGET_REPLACED, version)
    expected = row.get("expected_version") if row is not None else None
    base = proposal["based_on_resource_version"]
    if expected is not None and expected != base:
        # execute() only writes at the proposal's version; anything else is a corrupt journal.
        return Decision("NEEDS_RECONCILIATION", Basis.INCONSISTENT, version)
    attempt = action_attempt(observation.evidence_annotation, proposal)
    if attempt is not None:  # R3
        if version == base:
            return Decision("NEEDS_RECONCILIATION", Basis.INCONSISTENT, version)
        own = row is not None and isinstance(row.get("evidence"), dict) and row["evidence"].get("attempt_id") == attempt
        writer = "own" if own else "peer:" + attempt
        if observation.template_sha256 == proposal["parameters"]["template_sha256"]:
            return Decision("APPLIED", Basis.EVIDENCE_MATCH, version, writer=writer)
        return Decision("SUPERSEDED", Basis.COMMITTED_THEN_OVERWRITTEN, version, writer=writer)
    if observation.fence_annotation is not None and observation.fence_annotation in fence_nonces:  # R4
        return Decision("CONFLICT", Basis.FENCED, version)
    if version != base:  # R5
        if rejection is not None:
            return Decision("CONFLICT", Basis.DEFINITE_REJECTION, version)
        return Decision("SUPERSEDED", Basis.ADVANCED_WITHOUT_EVIDENCE, version)
    if rejection is not None:  # R6
        return Decision("FAILED", Basis.DEFINITE_REJECTION, version)
    return Decision("NEEDS_RECONCILIATION", Basis.UNCHANGED_IN_DOUBT, version, may_fence=True)
