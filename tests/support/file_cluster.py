"""Test-only Deployment API shared by several processes through one SQLite file.

It reuses SimAPIServer from tests/support/sim_apiserver.py for the object
semantics, so the file cluster and the in-process fakes agree on them:
RFC 6902 JSON Patch (test, add, replace) applied atomically, and an etcd-like
revision counter that gives every committed change a resourceVersion that is
never reused. Each command runs inside one BEGIN IMMEDIATE transaction, so
processes on one host see one serial order of changes.

FileKubectl answers the kubectl commands KubectlDeploymentAPI runs (get and
patch of the named Deployment), so the production adapter, with its patch
file, its UID and resourceVersion tests and its response checks, runs
unchanged against this file. Every patch request is recorded with its action,
its outcome and the wall-clock time of the decision, for invariant checks.

Nothing here contacts a cluster. The worker service accepts a file cluster
only when lab fault points are enabled (runtime/fault_points.py).
"""
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import time

from services.executor.core import ACTION_ANNOTATION, TargetAbsent
from services.executor.store import fingerprint
from tests.support.sim_apiserver import FENCE_ANNOTATION, SimAPIServer, SimKubectl


def _utc_text(now=None):
    return (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")


def _annotation_value(operations, key):
    """The value a patch writes to one annotation, or None."""
    escaped = "/metadata/annotations/" + key.replace("~", "~0").replace("/", "~1")
    for operation in operations if isinstance(operations, list) else []:
        if not isinstance(operation, dict):
            continue
        if operation.get("path") == escaped:
            return operation.get("value")
        if operation.get("path") == "/metadata/annotations" and isinstance(operation.get("value"), dict):
            if key in operation["value"]:
                return operation["value"][key]
    return None


def patch_action(operations):
    """(kind, action_id) of an Executor patch: a template write or a fence."""
    for kind, key in (("write", ACTION_ANNOTATION), ("fence", FENCE_ANNOTATION)):
        value = _annotation_value(operations, key)
        if isinstance(value, str):
            try:
                action_id = json.loads(value).get("action_id")
            except (ValueError, AttributeError):
                action_id = None
            return kind, action_id if isinstance(action_id, str) else "unknown"
    return "other", "unknown"


class FileCluster:
    """One Deployment in a SQLite file. Safe to open from many processes."""

    def __init__(self, path):
        self.path = Path(path)
        self.db = sqlite3.connect(str(self.path), isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cluster_state (
                id INTEGER PRIMARY KEY CHECK(id=1), cluster_id TEXT NOT NULL,
                deployment TEXT, last TEXT, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS cluster_requests (
                seq INTEGER PRIMARY KEY, host TEXT NOT NULL, action_id TEXT NOT NULL,
                kind TEXT NOT NULL, state TEXT NOT NULL, from_version TEXT,
                resource_version TEXT, template_sha256 TEXT, decided_at REAL NOT NULL);
        """)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @classmethod
    def create(cls, path, snapshot):
        """Start a new file from an observation snapshot; refuse to overwrite one."""
        cluster = cls(path)
        try:
            with cluster._transaction(load=False):
                if cluster.db.execute("SELECT 1 FROM cluster_state").fetchone():
                    raise ValueError("The file cluster already exists")
                cluster._save(SimAPIServer.from_snapshot(snapshot), insert=True)
        except BaseException:
            cluster.close()
            raise
        return cluster

    def _load(self):
        row = self.db.execute("SELECT * FROM cluster_state WHERE id=1").fetchone()
        if row is None:
            raise ValueError("The file cluster has not been created")
        server = SimAPIServer(cluster_id=row["cluster_id"],
                              deployment=json.loads(row["deployment"]) if row["deployment"] else None,
                              revision=row["revision"])
        server.last = json.loads(row["last"]) if row["last"] else None
        return server

    def _save(self, server, *, insert=False):
        values = (server.cluster_id, json.dumps(server.deployment) if server.deployment is not None else None,
                  json.dumps(server.last) if server.last is not None else None, server.revision)
        if insert:
            self.db.execute("INSERT INTO cluster_state(id,cluster_id,deployment,last,revision) VALUES(1,?,?,?,?)",
                            values)
        else:
            self.db.execute("UPDATE cluster_state SET cluster_id=?,deployment=?,last=?,revision=? WHERE id=1",
                            values)

    @contextmanager
    def _transaction(self, *, load=True):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self._load() if load else None
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def run(self, command, *, host):
        """Answer one kubectl command atomically, recording any patch request."""
        with self._transaction() as server:
            simulator = SimKubectl(server, host=host)
            reply = simulator(command)
            for request in server.requests:
                kind, action_id = patch_action(request.operations)
                template = next((operation["value"] for operation in request.operations
                                 if operation.get("path") == "/spec/template"), None)
                tests = {operation.get("path"): operation.get("value") for operation in request.operations
                         if operation.get("op") == "test"}
                self.db.execute("""INSERT INTO cluster_requests(host,action_id,kind,state,from_version,
                    resource_version,template_sha256,decided_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (host, action_id, kind, request.state, tests.get("/metadata/resourceVersion"),
                     request.resource_version, fingerprint(template) if isinstance(template, dict) else None,
                     time.time()))
            self._save(server)
        return reply

    def kubectl(self, host):
        """A subprocess.run stand-in for KubectlDeploymentAPI(runner=...)."""
        def runner(command, **_kwargs):
            return self.run([str(item) for item in command], host=host)
        return runner

    def snapshot(self, target=None, *, now=None):
        """The Deployment as a live-state snapshot, captured now (or at the given clock time)."""
        snapshot = self._load().snapshot(_utc_text(now))  # One SELECT reads a committed state.
        if snapshot is None:
            raise TargetAbsent("Deployment does not exist")
        deployment = snapshot["deployment"]["metadata"]
        if target is not None and (deployment["name"], deployment["namespace"]) != (target["name"], target["namespace"]):
            raise ValueError("The file cluster holds a different Deployment")
        return snapshot

    def external_write(self, mutator, actor="third-party"):
        """Another writer's change, committed under a new revision."""
        with self._transaction() as server:
            changed = server.external_write(mutator, actor=actor)
            self._save(server)
        return changed

    def health_sample(self, target, expected_template_sha256, dependencies, *, now=None):
        """One reading: healthy only while the template is the expected one."""
        snapshot = self.snapshot(target, now=now)
        healthy = fingerprint(snapshot["deployment"]["spec"]["template"]) == expected_template_sha256
        return {"at": snapshot["captured_at"], "service": target["name"],
                "http_status": 200 if healthy else 503, "ready_replicas": 1 if healthy else 0,
                "desired_replicas": 1, "dependencies": {name: True for name in dependencies}}

    def verifier_readers(self, config, *, clock):
        """(sample_reader, dependency_reader) for a lab Verifier worker.

        One pass takes samples_per_pass readings, sample_spacing_seconds apart,
        stopping at verifier.mid_collection after the first. Findings use the
        trusted dependency graph named by config["dependency_graph"].
        """
        from runtime import fault_points
        from services.verifier.core import inspect_dependencies
        baseline = json.loads(Path(config["baseline"]).read_text(encoding="utf-8-sig"))
        graph = json.loads(Path(config["dependency_graph"]).read_text(encoding="utf-8-sig"))
        count, spacing = config.get("samples_per_pass", 3), config.get("sample_spacing_seconds", 1.0)
        if type(count) is not int or not 1 <= count <= 20 or not 0 < spacing <= 10:
            raise ValueError("Lab sampling needs 1 to 20 readings at most 10 s apart")

        def samples(execution):
            target = execution["target"]
            names = [node for node in graph["nodes"] if node != target["name"]]
            readings = []
            for index in range(count):
                if index:
                    if index == 1:
                        fault_points.checkpoint("verifier.mid_collection")
                    time.sleep(spacing)
                readings.append(self.health_sample(target, baseline["template_sha256"], names, now=clock()))
            return readings

        def dependencies(proposal):
            return inspect_dependencies(proposal, graph, self.snapshot(proposal["target"], now=clock()), now=clock())
        return samples, dependencies

    def requests(self, kind=None):
        query, parameters = "SELECT * FROM cluster_requests", ()
        if kind is not None:
            query, parameters = query + " WHERE kind=?", (kind,)
        return [dict(row) for row in self.db.execute(query + " ORDER BY seq", parameters)]

    def committed_writes(self):
        """Committed template writes, the quantity invariant I1 bounds per action."""
        return [row for row in self.requests("write") if row["state"] == "committed"]
