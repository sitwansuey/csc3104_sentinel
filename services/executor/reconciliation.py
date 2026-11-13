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

A fence resolves that last case. It is a metadata-only patch with the same
UID and resourceVersion == V0 tests that adds this action's fence marker. The
API server linearizes the two requests, so at most one commits. A fence marker
on the object, with a nonce this host journaled before sending it, proves that
our write had not committed when the fence did, and it can never commit after.

A SCALE (P5.07) is the same kind of write: its patch replaces spec.replicas
instead of the Pod template, under the same tests and with the same fresh
evidence annotation, so facts 1-3 and the fence hold for it unchanged. Its
evidence also records the replica count, and its write is APPLIED only while
the object still has that count and the template it named; a later change to
either is COMMITTED_THEN_OVERWRITTEN.

The module has no I/O. DEFINITE_REJECTION is reserved for a later
rejection-classification stage; production code does not supply it.
"""

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Any, Mapping, Optional

from shared.canonical import canonical_json, sha256_hex as fingerprint


ACTION_ANNOTATION = "sentinel.io/execution"
# One annotation family for fences. The marker carries action_id, a nonce and an
# optional ownership epoch, which later Coordinator-commanded fences may set.
FENCE_ANNOTATION = "sentinel.io/fence"
# A Coordinator-commanded seal (P3.04) also writes its ownership epoch here, as decimal text, so an
# admission policy can require it to grow (P3.05). An Executor's own fence carries no epoch.
FENCE_EPOCH_ANNOTATION = "sentinel.io/fence-epoch"
_EVIDENCE_FIELDS = frozenset({"action_id", "proposal_sha256", "target_uid", "template_sha256", "attempt_id"})
# Actions whose evidence names the action. A ROLLBACK keeps the five-field form
# written before other actions existed, so the absence of "action" means ROLLBACK.
# A SCALE's evidence also carries "replicas", the count it wrote.
_NAMED_ACTIONS = frozenset({"RESTART", "SCALE"})
_MAX_REPLICAS = 10  # shared/actions.py SCALE_MAX_REPLICAS; this module imports nothing from shared.actions.
_FENCE_FIELDS = frozenset({"action_id", "nonce"})
_NONCE = re.compile(r"[a-f0-9]{32}\Z")
_IDENTITY_FIELDS = ("cluster_id", "kind", "namespace", "name")


class Basis(str, Enum):
    """Why a reconciliation decision was reached; recorded in result details."""

    EVIDENCE_MATCH = "EVIDENCE_MATCH"
    COMMITTED_THEN_OVERWRITTEN = "COMMITTED_THEN_OVERWRITTEN"
    TARGET_DELETED = "TARGET_DELETED"
    TARGET_REPLACED = "TARGET_REPLACED"
    ADVANCED_WITHOUT_EVIDENCE = "ADVANCED_WITHOUT_EVIDENCE"
    FENCED = "FENCED"
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
    replicas: Optional[int] = None


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
        fence = annotations.get(FENCE_ANNOTATION) if isinstance(annotations, dict) else None
        template = deployment.get("spec", {}).get("template")
        digest = fingerprint(template) if isinstance(template, dict) else None
        replicas = deployment.get("spec", {}).get("replicas")
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        return None
    return Observation(present=True, uid=uid, resource_version=version,
                       evidence_annotation=evidence if isinstance(evidence, str) else None,
                       fence_annotation=fence if isinstance(fence, str) else None,
                       template_sha256=digest,
                       replicas=replicas if type(replicas) is int else None)


def fence_marker(action_id: str, nonce: str, epoch: Optional[int] = None) -> dict[str, Any]:
    """The marker a fence writes under FENCE_ANNOTATION, as canonical JSON."""
    marker: dict[str, Any] = {"action_id": action_id, "nonce": nonce}
    if epoch is not None:
        marker["epoch"] = epoch
    return marker


def valid_fence_marker(marker: Any) -> bool:
    """True for {action_id, nonce} or {action_id, nonce, epoch} with a well-formed nonce."""
    if not isinstance(marker, dict) or not _FENCE_FIELDS <= set(marker) <= _FENCE_FIELDS | {"epoch"}:
        return False
    action_id, nonce, epoch = marker["action_id"], marker["nonce"], marker.get("epoch", 0)
    return (isinstance(action_id, str) and bool(action_id.strip()) and len(action_id) <= 1024
            and isinstance(nonce, str) and _NONCE.fullmatch(nonce) is not None
            and type(epoch) is int and epoch >= 0)


def fence_nonce(annotation: Optional[str], action_id: str) -> Optional[str]:
    """Return the nonce if the annotation is a canonical fence marker for this action."""
    if not isinstance(annotation, str) or len(annotation) > 2048:
        return None
    try:
        marker = json.loads(annotation)
        if not valid_fence_marker(marker) or canonical_json(marker) != annotation:
            return None
    except (TypeError, ValueError, RecursionError):
        return None
    nonce = marker["nonce"]
    return nonce if marker["action_id"] == action_id and isinstance(nonce, str) else None


def evidence_fields_valid(evidence: Any) -> bool:
    """True for the five-field ROLLBACK form, those fields plus RESTART, or plus SCALE and its replica count."""
    if not isinstance(evidence, dict):
        return False
    fields = set(evidence)
    if fields == _EVIDENCE_FIELDS:
        return True
    if fields == _EVIDENCE_FIELDS | {"action"}:
        return evidence["action"] in _NAMED_ACTIONS - {"SCALE"}
    replicas = evidence.get("replicas")
    return (fields == _EVIDENCE_FIELDS | {"action", "replicas"} and evidence["action"] == "SCALE"
            and type(replicas) is int and 1 <= replicas <= _MAX_REPLICAS)


def _evidence_action(evidence: Mapping[str, Any]) -> Any:
    return evidence.get("action", "ROLLBACK")


def action_attempt(annotation: Optional[str], proposal: Mapping[str, Any]) -> Optional[str]:
    """Return the attempt nonce if the annotation is exact evidence for this action.

    Matching is at action level: action ID, action type, proposal hash, target
    UID and template hash (and a SCALE's replica count) must all match, so a
    peer's attempt of the same action counts. A copied marker for another action
    or a changed proposal does not.
    """
    if not isinstance(annotation, str) or len(annotation) > 4096:
        return None
    try:
        evidence = json.loads(annotation)
        if not evidence_fields_valid(evidence) or canonical_json(evidence) != annotation:
            return None
        attempt = evidence["attempt_id"]
        matches = (evidence["action_id"] == proposal["action_id"]
                   and _evidence_action(evidence) == proposal["action"]
                   and evidence["proposal_sha256"] == fingerprint(proposal)
                   and evidence["target_uid"] == proposal["target"]["uid"]
                   and evidence["template_sha256"] == proposal["parameters"]["template_sha256"]
                   and evidence.get("replicas") == _written_replicas(proposal)
                   and isinstance(attempt, str) and _NONCE.fullmatch(attempt) is not None)
    except (KeyError, TypeError, ValueError, RecursionError):
        return None
    return attempt if matches else None


def _written_replicas(proposal: Mapping[str, Any]) -> Optional[int]:
    """The replica count a SCALE writes; None for every other action."""
    if proposal["action"] != "SCALE":
        return None
    value = proposal["parameters"]["desired_replicas"]
    return value if type(value) is int else None


def _write_still_in_place(proposal: Mapping[str, Any], observation: Observation) -> bool:
    """The object still shows what this action wrote: its template, and a SCALE's replica count."""
    if observation.template_sha256 != proposal["parameters"]["template_sha256"]:
        return False
    replicas = _written_replicas(proposal)
    return replicas is None or observation.replicas == replicas


def classify(row: Optional[Mapping[str, Any]], proposal: Mapping[str, Any],
             observation: Optional[Observation], rejection: Any = None, *,
             fence_nonces: frozenset[str] = frozenset(), fence_confirmed: bool = False) -> Decision:
    """Evaluate rows R0-R6 in order; the first match wins.

    row is the local ledger entry or None. V0 is its expected_version, or the
    proposal's based_on_resource_version for a stateless or adopted row.
    fence_nonces are the fence nonces this host journaled before sending them;
    fence_confirmed is True once a fence's own success response showed it
    committed, so R4 still holds if another writer later strips the marker.
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
        if _write_still_in_place(proposal, observation):
            return Decision("APPLIED", Basis.EVIDENCE_MATCH, version, writer=writer)
        return Decision("SUPERSEDED", Basis.COMMITTED_THEN_OVERWRITTEN, version, writer=writer)
    if fence_confirmed or fence_nonce(observation.fence_annotation, proposal["action_id"]) in fence_nonces:  # R4
        if version == base:
            # A committed fence always advances the version, like our own write.
            return Decision("NEEDS_RECONCILIATION", Basis.INCONSISTENT, version)
        return Decision("CONFLICT", Basis.FENCED, version)
    if version != base:  # R5
        if rejection is not None:
            return Decision("CONFLICT", Basis.DEFINITE_REJECTION, version)
        return Decision("SUPERSEDED", Basis.ADVANCED_WITHOUT_EVIDENCE, version)
    if rejection is not None:  # R6
        return Decision("FAILED", Basis.DEFINITE_REJECTION, version)
    return Decision("NEEDS_RECONCILIATION", Basis.UNCHANGED_IN_DOUBT, version, may_fence=True)
