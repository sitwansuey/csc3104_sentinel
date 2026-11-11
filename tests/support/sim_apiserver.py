"""Test-only simulated API server for one Deployment, with requests in flight.

It models only what the Executor's safety argument relies on:

- RFC 6902 JSON Patch with test, add and replace, applied atomically. Any
  failed operation commits nothing, like an HTTP 422 from the API server.
- One global revision counter, like etcd's. Every committed change gets a new
  resourceVersion that is never reused, also across delete and recreate.
- Requests that stay in flight after the client gave up. Each one may commit
  at a later step, be rejected when it arrives, or never arrive.
- Third-party writes, delete and recreate with a new UID.

SimDeploymentAPI is the Executor-facing adapter. It mirrors
KubectlDeploymentAPI: a pre-read, the same four-operation patch and the same
error mapping. SimKubectl answers the subset of kubectl commands that the real
adapter and the live wedge harness run, so production code can be driven
against the simulator. Nothing here contacts a cluster.
"""

from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess

from services.executor import core as _core


ACTION_ANNOTATION = _core.ACTION_ANNOTATION
ConditionalWriteConflict = _core.ConditionalWriteConflict
ReadUnavailable = _core.ReadUnavailable
# Older executors have no TargetAbsent; their kubectl read failed on NotFound.
TargetAbsent = getattr(_core, "TargetAbsent", None)


class PatchFailed(ValueError):
    """The patch did not apply, so nothing was committed (HTTP 422 or 404)."""


class SimTimeout(TimeoutError):
    """The client stopped waiting; the request may still be in flight."""


class SimWriteRejected(RuntimeError):
    """The server answered with an error. The client cannot tell why."""


def deepcopy(value):
    """Copy JSON data; much faster than copy.deepcopy for these documents."""
    return json.loads(json.dumps(value))


def _same_json(left, right):
    """JSON equality for test ops: true/false are not the numbers 1/0."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same_json(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same_json(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def _tokens(pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise PatchFailed("Only non-root JSON pointers are modelled")
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]


def _index(container, token, *, append=False):
    if append and token == "-":
        return len(container)
    if not token.isdigit() or (token != "0" and token.startswith("0")):
        raise PatchFailed("Invalid array index")
    index = int(token)
    if index > len(container) or (not append and index == len(container)):
        raise PatchFailed("Array index out of range")
    return index


def _parent(document, pointer):
    tokens = _tokens(pointer)
    node = document
    for token in tokens[:-1]:
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list):
            node = node[_index(node, token)]
        else:
            raise PatchFailed("Path does not exist")
    if not isinstance(node, (dict, list)):
        raise PatchFailed("Path parent is not a container")
    return node, tokens[-1]


def apply_json_patch(document, operations):
    """Return a patched copy, or raise PatchFailed and leave document unchanged."""
    if not isinstance(operations, list):
        raise PatchFailed("A JSON Patch is a list of operations")
    result = deepcopy(document)
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("op") not in {"test", "add", "replace"}:
            raise PatchFailed("Only test, add and replace are modelled")
        if "value" not in operation:
            raise PatchFailed("Operation has no value")
        parent, token = _parent(result, operation.get("path"))
        kind, value = operation["op"], deepcopy(operation["value"])
        if isinstance(parent, dict):
            exists = token in parent
            if kind == "test":
                if not exists or not _same_json(parent[token], value):
                    raise PatchFailed("Test operation failed")
            elif kind == "replace" and not exists:
                raise PatchFailed("Replace target does not exist")
            else:
                parent[token] = value
        else:
            if kind == "add":
                parent.insert(_index(parent, token, append=True), value)
                continue
            index = _index(parent, token)
            if kind == "test":
                if not _same_json(parent[index], value):
                    raise PatchFailed("Test operation failed")
            else:
                parent[index] = value
    return result


@dataclass
class Request:
    """One mutating request. state: in_flight, committed, rejected or dropped."""

    number: int
    host: str
    action_id: str
    operations: list
    state: str = "in_flight"
    resource_version: str | None = None


@dataclass
class SimAPIServer:
    """One named Deployment with an etcd-like monotonic revision."""

    cluster_id: str
    deployment: dict | None
    revision: int = 1000
    requests: list = field(default_factory=list)
    log: list = field(default_factory=list)
    last: dict | None = None

    @classmethod
    def from_snapshot(cls, snapshot):
        deployment = deepcopy(snapshot["deployment"])
        version = deployment["metadata"]["resourceVersion"]
        revision = int(version) if version.isdigit() else 1000
        return cls(cluster_id=snapshot["cluster_id"], deployment=deployment, revision=revision,
                   last=deepcopy(deployment))

    @property
    def present(self):
        return self.deployment is not None

    @property
    def uid(self):
        return self.deployment["metadata"]["uid"] if self.present else None

    @property
    def resource_version(self):
        return self.deployment["metadata"]["resourceVersion"] if self.present else None

    def snapshot(self, captured_at="2026-10-10T12:00:40Z"):
        if not self.present:
            return None
        return {"cluster_id": self.cluster_id, "captured_at": captured_at, "deployment": deepcopy(self.deployment)}

    def _commit(self, candidate, actor):
        """Store a changed object under a fresh revision. An unchanged one is a no-op."""
        if candidate == self.deployment:
            return False
        self.revision += 1
        metadata = candidate["metadata"]
        if self.deployment is not None and candidate.get("spec") != self.deployment.get("spec"):
            metadata["generation"] = metadata.get("generation", 1) + 1
        metadata["resourceVersion"] = str(self.revision)
        self.deployment = candidate
        self.last = candidate  # Stored objects are replaced, never changed in place.
        self.log.append((actor, metadata["resourceVersion"]))
        return True

    def patch(self, operations, actor="client"):
        """Apply a patch now. Raise PatchFailed if absent or any operation fails."""
        if not self.present:
            raise PatchFailed("Deployment does not exist")
        return self._commit(apply_json_patch(self.deployment, operations), actor)

    # Requests in flight -------------------------------------------------

    def submit(self, host, action_id, operations):
        request = Request(len(self.requests) + 1, host, action_id, deepcopy(operations))
        self.requests.append(request)
        return request

    def deliver(self, request):
        """The request reaches the server: commit it if every test still passes."""
        if request.state != "in_flight":
            raise ValueError("Only an in-flight request can be delivered")
        try:
            self.patch(request.operations, actor=f"{request.host}:{request.action_id}#{request.number}")
        except PatchFailed:
            request.state = "rejected"
            return False
        request.state = "committed"
        request.resource_version = self.resource_version
        return True

    def drop(self, request):
        """The request never reaches the server."""
        if request.state != "in_flight":
            raise ValueError("Only an in-flight request can be dropped")
        request.state = "dropped"

    def can_commit(self, request):
        """True if delivering this request now would commit it (a dry run)."""
        if request.state != "in_flight" or not self.present:
            return False
        try:
            apply_json_patch(self.deployment, request.operations)
        except PatchFailed:
            return False
        return True

    def in_flight(self, action_id=None, host=None):
        return [r for r in self.requests if r.state == "in_flight"
                and (action_id is None or r.action_id == action_id) and (host is None or r.host == host)]

    def committed(self, action_id=None, host=None):
        return [r for r in self.requests if r.state == "committed"
                and (action_id is None or r.action_id == action_id) and (host is None or r.host == host)]

    # Other writers ------------------------------------------------------

    def external_write(self, mutator, actor="third-party"):
        """Another writer's change; it commits under a new revision if it changes anything."""
        if not self.present:
            raise PatchFailed("Deployment does not exist")
        candidate = deepcopy(self.deployment)
        mutator(candidate)
        return self._commit(candidate, actor)

    def delete(self, actor="third-party"):
        if not self.present:
            return False
        self.revision += 1  # A delete is an etcd write, too.
        self.deployment = None
        self.log.append((actor + ":delete", str(self.revision)))
        return True

    def create(self, deployment, uid, actor="third-party"):
        """Create the object (replacing any live one) with a new UID and generation 1."""
        candidate = deepcopy(deployment)
        candidate["metadata"]["uid"] = uid
        candidate["metadata"]["generation"] = 1
        self.deployment = None
        self._commit(candidate, actor + ":create")
        return True

    def recreate(self, uid, actor="third-party"):
        """Create the same name again from its last stored state, with a new UID."""
        return self.create(self.last, uid, actor)


def restore_patch(target, version, template, evidence, annotations):
    """The exact operations KubectlDeploymentAPI.conditional_restore submits."""
    annotation = _core.encode_evidence(evidence)
    evidence_operation = (
        {"op": "add", "path": "/metadata/annotations", "value": {ACTION_ANNOTATION: annotation}}
        if annotations is None else
        {"op": "add", "path": "/metadata/annotations/" + ACTION_ANNOTATION.replace("~", "~0").replace("/", "~1"),
         "value": annotation}
    )
    return [{"op": "test", "path": "/metadata/uid", "value": target["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": version},
            {"op": "replace", "path": "/spec/template", "value": template}, evidence_operation]


class SimDeploymentAPI:
    """Executor adapter for one host, mirroring KubectlDeploymentAPI's behaviour.

    fate decides what happens to the next write: ok (delivered, answered),
    lost (delivered, answer lost), queued (still in flight when the client
    times out), race (another writer bumps the version just before delivery,
    like a 422 test-op race) or peer-race (a peer host's in-flight write of
    this action, if any can commit, lands just before our pre-send read, so
    ours is refused as a conditional conflict). point(name) is called at each adapter step so a
    model checker can crash the process there.
    """

    def __init__(self, server, host, *, action_id, clock=None, point=None):
        self.server, self.host, self.action_id = server, host, action_id
        self.clock = clock
        self.point = point or (lambda name: None)
        self.fate = "ok"
        self.outage = False
        self.last_read_ok = False
        self.stats = {"read_attempts": 0, "write_attempts": 0, "successful_writes": 0}

    def _captured_at(self):
        return self.clock().isoformat().replace("+00:00", "Z") if self.clock else "2026-10-10T12:00:40Z"

    def read(self, target):
        self.point("read")
        self.stats["read_attempts"] += 1
        self.last_read_ok = False
        if self.outage:
            raise ReadUnavailable("Simulated read outage")
        if not self.server.present:
            if TargetAbsent is None:
                raise ReadUnavailable("Deployment not found")
            self.last_read_ok = True
            raise TargetAbsent("Deployment does not exist")
        metadata = self.server.deployment["metadata"]
        if metadata["name"] != target["name"] or metadata["namespace"] != target["namespace"]:
            raise ReadUnavailable("Unexpected object")
        self.last_read_ok = True
        return self.server.snapshot(self._captured_at())

    def conditional_restore(self, *, target, version, template, evidence):
        fate, self.fate = self.fate, "ok"
        if fate == "peer-race":
            # A peer host's in-flight write of this action lands after our pre-read, before our submission.
            live = [r for r in self.server.in_flight(self.action_id)
                    if r.host != self.host and self.server.can_commit(r)]
            if live:
                self.server.deliver(live[0])
        try:
            snapshot = self.read(target)
        except Exception as exc:
            if TargetAbsent is not None and isinstance(exc, TargetAbsent):
                raise ConditionalWriteConflict("Deployment no longer exists before submission") from None
            raise
        metadata = snapshot["deployment"]["metadata"]
        if metadata["uid"] != target["uid"] or metadata["resourceVersion"] != version:
            raise ConditionalWriteConflict("Deployment UID or resource version changed before submission")
        operations = restore_patch(target, version, template, evidence, metadata.get("annotations"))
        if fate == "race":
            self.server.external_write(_bump("race"), actor="third-party:race")
        self.point("send.before")
        self.stats["write_attempts"] += 1
        request = self.server.submit(self.host, self.action_id, operations)
        self.point("send.after")
        if fate == "queued":
            raise SimTimeout("Request timed out; it may still be in flight")
        committed = self.server.deliver(request)
        self.point("commit.after")
        if fate == "lost":
            raise SimTimeout("Response was lost")
        if not committed:
            raise SimWriteRejected("Write returned an error")
        self.stats["successful_writes"] += 1
        return self.server.snapshot(self._captured_at())


def _bump(value):
    def mutate(deployment):
        deployment["metadata"].setdefault("annotations", {})["example.org/bump"] = value
    return mutate


class SimKubectl:
    """A subprocess.run stand-in that answers kubectl commands from a SimAPIServer.

    Supported: get/patch/annotate/delete deployment, and apply/create/delete for
    whole-namespace bookkeeping. before(verb, arguments) runs first and may
    raise, return a CompletedProcess to short-circuit, or return None.
    Every call is recorded in calls.
    """

    def __init__(self, server, *, host="kubectl", action_id=None, before=None):
        self.server, self.host, self.action_id = server, host, action_id
        self.before = before
        self.calls = []

    @staticmethod
    def _split(command):
        options, words = {}, []
        for item in command[1:]:
            if item.startswith("--") and "=" in item:
                key, value = item[2:].split("=", 1)
                options[key] = value
            else:
                words.append(item)
        return options, words

    def _reply(self, command, returncode=0, stdout=b"", stderr=b""):
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    def __call__(self, command, **kwargs):
        command = [str(item) for item in command]
        self.calls.append(command)
        options, words = self._split(command)
        verb = words[0] if words else ""
        if self.before is not None:
            early = self.before(verb, command)
            if early is not None:
                return early
        if verb == "get" and words[1:2] == ["deployment"]:
            if not self.server.present:
                if "--ignore-not-found" in command:
                    return self._reply(command)
                return self._reply(command, 1, stderr=b"Error from server (NotFound)")
            return self._reply(command, stdout=json.dumps(self.server.deployment).encode("utf-8"))
        if verb == "patch" and words[1:2] == ["deployment"] and options.get("type") == "json":
            path = words[words.index("--patch-file") + 1]
            operations = json.loads(Path(path).read_text(encoding="utf-8"))
            request = self.server.submit(self.host, self.action_id or "unknown", operations)
            if not self.server.deliver(request):
                return self._reply(command, 1, stderr=b"The request is invalid")
            return self._reply(command, stdout=json.dumps(self.server.deployment).encode("utf-8"))
        if verb == "annotate" and words[1:2] == ["deployment"]:
            key, value = words[3].split("=", 1)
            try:
                self.server.external_write(
                    lambda d: d["metadata"].setdefault("annotations", {}).__setitem__(key, value), actor="annotate")
            except PatchFailed:
                return self._reply(command, 1, stderr=b"Error from server (NotFound)")
            return self._reply(command)
        if verb == "delete" and words[1:2] == ["deployment"]:
            return self._reply(command, 0 if self.server.delete(actor="delete") else 1)
        return self._reply(command, 1, stderr=b"Command is not modelled")
