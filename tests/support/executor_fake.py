"""Test-only in-memory Deployment API; never imported by live execution."""

from copy import deepcopy
from threading import RLock

from services.executor.core import ACTION_ANNOTATION, ConditionalWriteConflict, TargetAbsent, encode_evidence
from services.executor.reconciliation import FENCE_ANNOTATION
from services.executor.store import canonical_json


class FakeDeploymentAPI:
    """Apply the same UID/version preconditions required of a live adapter.

    Template replacement and action evidence commit together under one lock.
    A fence commits only its marker annotation under the same preconditions.
    The optional clock refreshes observation times without changing object state.
    Tests may subclass this adapter to interrupt a request before or after commit.
    """

    def __init__(self, snapshot, *, clock=None):
        self.snapshot = deepcopy(snapshot)
        self.clock = clock
        self.write_attempts = []
        self.writes = []
        self.fence_attempts = []
        self.fences = []
        self.deleted = None
        self._lock = RLock()
        self._version = 0

    def _snapshot(self):
        snapshot = deepcopy(self.snapshot)
        if self.clock is not None:
            snapshot["captured_at"] = self.clock().isoformat().replace("+00:00", "Z")
        return snapshot

    def _advance(self, current):
        """Return a fresh resource version; like etcd, versions never repeat."""
        self._version += 1
        next_version = f"fake-write-{self._version}"
        if next_version == current:
            self._version += 1
            next_version = f"fake-write-{self._version}"
        return next_version

    def read(self, target):
        """Return the named object, including its actual UID if recreated."""
        with self._lock:
            if self.snapshot is None:
                raise TargetAbsent("Deployment does not exist")
            return self._snapshot()

    def delete(self):
        """Remove the object, as an administrator deleting the Deployment would."""
        with self._lock:
            self.deleted = self.snapshot
            self.snapshot = None

    def recreate(self, uid):
        """Recreate a deleted (or live) object under the same name with a new UID."""
        with self._lock:
            candidate = deepcopy(self.snapshot if self.snapshot is not None else self.deleted)
            metadata = candidate["deployment"]["metadata"]
            metadata["uid"] = uid
            metadata["resourceVersion"] = self._advance(metadata["resourceVersion"])
            self.snapshot = candidate

    def external_write(self, mutator=None):
        """Apply another writer's change; any committed change advances the version."""
        with self._lock:
            candidate = deepcopy(self.snapshot)
            if mutator is not None:
                mutator(candidate["deployment"])
            metadata = candidate["deployment"]["metadata"]
            metadata["resourceVersion"] = self._advance(metadata["resourceVersion"])
            self.snapshot = candidate
            return self._snapshot()

    def _precondition(self, target, version):
        """Raise ConditionalWriteConflict unless the object is target at version."""
        if self.snapshot is None:
            raise ConditionalWriteConflict("Deployment does not exist")
        deployment = self.snapshot["deployment"]
        metadata = deployment["metadata"]
        actual = {
            "cluster_id": self.snapshot["cluster_id"], "kind": deployment["kind"],
            "namespace": metadata["namespace"], "name": metadata["name"],
            "uid": metadata["uid"],
        }
        if target != actual or version != metadata["resourceVersion"]:
            raise ConditionalWriteConflict("Deployment UID or resource version changed")

    def fence(self, *, target, version, marker):
        """Atomically add only the fence marker, under the same UID/version tests."""
        with self._lock:
            self.fence_attempts.append(deepcopy({"target": target, "version": version, "marker": marker}))
            self._precondition(target, version)
            candidate = deepcopy(self.snapshot)
            updated = candidate["deployment"]["metadata"]
            updated.setdefault("annotations", {})[FENCE_ANNOTATION] = canonical_json(marker)
            updated["resourceVersion"] = self._advance(version)
            self.snapshot = candidate
            result = self._snapshot()
            self.fences.append(deepcopy(result))
            return result

    def conditional_restore(self, *, target, version, template, evidence):
        """Atomically restore an exact template and its Deployment annotation."""
        with self._lock:
            self.write_attempts.append(deepcopy({
                "target": target, "version": version,
                "template": template, "evidence": evidence,
            }))
            self._precondition(target, version)

            # Prepare a copy before committing, including serialization that may fail.
            candidate = deepcopy(self.snapshot)
            candidate["deployment"]["spec"]["template"] = deepcopy(template)
            updated = candidate["deployment"]["metadata"]
            updated.setdefault("annotations", {})[ACTION_ANNOTATION] = encode_evidence(evidence)
            updated["resourceVersion"] = self._advance(version)
            self.snapshot = candidate
            result = self._snapshot()
            self.writes.append(deepcopy(result))
            return result
