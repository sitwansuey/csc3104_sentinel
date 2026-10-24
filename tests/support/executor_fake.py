"""Test-only in-memory Deployment API; never imported by live execution."""

from copy import deepcopy
from threading import RLock

from services.executor import ACTION_ANNOTATION, ConditionalWriteConflict, encode_evidence


class FakeDeploymentAPI:
    """Apply the same UID/version preconditions required of a live adapter.

    Template replacement and action evidence commit together under one lock.
    The optional clock refreshes observation times without changing object state.
    Tests may subclass this adapter to interrupt a request before or after commit.
    """

    def __init__(self, snapshot, *, clock=None):
        self.snapshot = deepcopy(snapshot)
        self.clock = clock
        self.write_attempts = []
        self.writes = []
        self._lock = RLock()
        self._version = 0

    def _snapshot(self):
        snapshot = deepcopy(self.snapshot)
        if self.clock is not None:
            snapshot["captured_at"] = self.clock().isoformat().replace("+00:00", "Z")
        return snapshot

    def read(self, target):
        """Return the named object, including its actual UID if recreated."""
        with self._lock:
            return self._snapshot()

    def conditional_restore(self, *, target, version, template, evidence):
        """Atomically restore an exact template and its Deployment annotation."""
        with self._lock:
            self.write_attempts.append(deepcopy({
                "target": target, "version": version,
                "template": template, "evidence": evidence,
            }))
            deployment = self.snapshot["deployment"]
            metadata = deployment["metadata"]
            actual = {
                "cluster_id": self.snapshot["cluster_id"], "kind": deployment["kind"],
                "namespace": metadata["namespace"], "name": metadata["name"],
                "uid": metadata["uid"],
            }
            if target != actual or version != metadata["resourceVersion"]:
                raise ConditionalWriteConflict("Deployment UID or resource version changed")

            # Prepare a copy before committing, including serialization that may fail.
            candidate = deepcopy(self.snapshot)
            candidate["deployment"]["spec"]["template"] = deepcopy(template)
            updated = candidate["deployment"]["metadata"]
            updated.setdefault("annotations", {})[ACTION_ANNOTATION] = encode_evidence(evidence)
            self._version += 1
            next_version = f"fake-write-{self._version}"
            if next_version == version:
                self._version += 1
                next_version = f"fake-write-{self._version}"
            updated["resourceVersion"] = next_version
            self.snapshot = candidate
            result = self._snapshot()
            self.writes.append(deepcopy(result))
            return result
