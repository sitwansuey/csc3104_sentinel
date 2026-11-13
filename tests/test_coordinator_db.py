"""The Coordinator's Database port (P5.01 stage 8.1): SQLite unchanged, Postgres a dialect only.

Offline only. The contract tests are parameterised over the backends; the
Postgres row is skipped because no Postgres driver is in the hash-locked
requirements (owners' go/no-go for P5.01), so nothing here shows a Postgres
server running the Coordinator. The unchanged-behaviour digests were recorded
before the port existed (on integration/phase4 at 2ef594a and 1adcfe3, and on
integration/phase5 at 7f86bd6; see UNCHANGED), and pass there too; the port,
dialect, refusal and seam tests fail there (services/coordinator/db.py and
SQLiteStore(database=...) did not exist).
"""
import ast
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from runtime.worker_transport import Delivery
from services.coordinator.db import (
    POSTGRES, POSTGRES_DEFERRED, POSTGRES_RESERVED, RESERVED_COLUMN_NAMES, SQLITE, BackendUnavailable, Database,
    NotPortable, PostgresDatabase, SQLiteDatabase, open_database,
)
from services.coordinator.store import SCHEMA_VERSION, SQLiteStore, schema_version
from tests.support import coordinator_fingerprint as fingerprints

ROOT = Path(__file__).resolve().parents[1]

# (file fingerprint, digest of the SQL text sent, number of statements). The first three were recorded on
# integration/phase4 at 2ef594a and recomputed, identical, on 1adcfe3 (which added the CONFLICT re-plan,
# core.py::_conflict_replan_declined); conflict-replan was recorded on 1adcfe3, before its three queries
# were moved onto the port. integration/phase5 at 7f86bd6, the last commit before the port was merged,
# sends one more statement in the two offline flows (a SELECT of the action in the Verifier pipeline,
# runtime/verifier_pipeline.py::_bind_expectations, added with SCALE in P5.07); their SQL digests and
# counts were recomputed there and the file digests came out the same. The transport and conflict-replan
# values are identical at 7f86bd6. All were computed from clean exports of those commits with this file's
# helper, tests/support/coordinator_fingerprint.py.
UNCHANGED = {
    "failed-rollout": ("296915e7d3a179f9ca202c53bc78bf25745e914c28209155f970b2a4ebfdeedc",
                       "98a0b60b354581c269ace1f567aa9d7d0a5188b800d065806bd167dac98cc1aa", 289),
    "pod-failure": ("cfb777407f607a7c551221219df3b3f7271bad7152f1d54362ecff102697b730",
                    "edf92aa5b499e4e99ca8ece34ecd70336e340cb07bb2deac77c3749a36753ebe", 288),
    "transport": ("800b2b79311b16409d30ee91a290fb603f0cf97a5bc61e55284b97315474f7f0",
                  "3f642a42596fd4dad5aa729ab022a2e7b5693014c797c0c860dd130f0c2fcd08", 365),
    "conflict-replan": ("ca87201b03a821e66a7b760433ae13d871158667696c757e9f04fec563940c11",
                        "84b2fedc0b8011e05f29fba54533147886bc4799dc552968fc31350f6e813e7f", 116),
}

POSTGRES_SKIP = "No Postgres driver is locked; the Postgres backend waits for the owners' go/no-go (P5.01)"
BACKENDS = (
    ("sqlite", lambda directory: SQLiteDatabase(Path(directory) / "coordinator.db")),
    ("postgres", None),
)

# Statements the four flows send that the Postgres dialect refuses, apart from
# schema statements and PRAGMAs (counted separately below), with a fragment of
# each reason. A new SQLite-only statement of a kind the dialect knows fails
# here. This is a lower bound on the work for a Postgres store, not the whole
# list: only the flows' statements are classified, and the dialect is textual,
# so a statement it translates has not been checked by a Postgres parser.
NOT_PORTABLE_QUERIES = {
    "INSERT OR IGNORE INTO store_meta(id,generation,commit_counter,created_at) VALUES(1, lower(hex(randomblob(16))), 0, "
    "strftime('%Y-%m-%dT%H:%M:%fZ','now'))": ("SQLite function",),
    "INSERT OR REPLACE INTO dependency_revalidations VALUES(?,?)": ("conflict target",),
    "SELECT action_id FROM actions WHERE incident_id=? ORDER BY rowid": ("sequence column",),
    "SELECT last_insert_rowid()": ("RETURNING",),
    "SELECT rowid,* FROM incidents ORDER BY rowid": ("sequence column",),
    "SELECT rowid,* FROM incidents WHERE incident_id=?": ("sequence column",),
    "UPDATE messages SET kind=json_extract(payload,'$.kind'), action_id=json_extract(payload,'$.action_id') "
    "WHERE json_valid(payload)": ("SQLite function",),
    "UPDATE messages SET seq=rowid WHERE seq IS NULL": ("sequence column",),
    # SQLiteStore.claim_target: epoch=epoch+1 is ambiguous next to EXCLUDED.
    "INSERT INTO target_ownership(target_key,uid,epoch,holder,delivery_id,event_id) VALUES(?,?,1,?,?,?) "
    "ON CONFLICT(target_key) DO UPDATE SET uid=excluded.uid, epoch=epoch+1, holder=excluded.holder, "
    "delivery_id=excluded.delivery_id, event_id=excluded.event_id": ("reads epoch without a table qualifier",),
    # SQLiteStore.record_worker_clock: the column offset, and two bare references.
    "INSERT INTO worker_clocks(identity,offset,uncertainty,max_abs_offset,reports,reported_at) "
    "VALUES(?,?,?,abs(?),1,?) ON CONFLICT(identity) DO UPDATE SET offset=excluded.offset, "
    "uncertainty=excluded.uncertainty, max_abs_offset=max(max_abs_offset, excluded.max_abs_offset), "
    "reports=reports+1, reported_at=excluded.reported_at":
        ("offset is a reserved word", "reads max_abs_offset, reports without a table qualifier"),
}
# Of those, the ones migrations send; the rest are runtime queries.
MIGRATION_QUERIES = 3


def normalise(sql):
    return " ".join(sql.split())


def run_flow(name, directory):
    if name == "transport":
        return fingerprints.transport_flow(directory)
    if name == "conflict-replan":
        return fingerprints.conflict_replan_flow(directory)
    return fingerprints.offline_flow(directory, name)


class UnchangedSQLiteTests(unittest.TestCase):
    """SQLite stores the same rows and receives the same SQL text, in the same order, as before the port."""

    def test_each_flow_leaves_the_same_file_and_sends_the_same_statements(self):
        for name, (file_digest, sql_digest, count) in UNCHANGED.items():
            with self.subTest(flow=name), tempfile.TemporaryDirectory() as directory:
                with fingerprints.statements() as sent:
                    path = run_flow(name, directory)
                self.assertEqual(len(sent), count)
                self.assertEqual(fingerprints.digest_lines(sent), sql_digest)
                self.assertEqual(fingerprints.fingerprint(path), file_digest)

    def test_the_fingerprint_notices_a_changed_row_and_a_changed_statement(self):
        with tempfile.TemporaryDirectory() as directory:
            with fingerprints.statements() as sent:
                path = run_flow("transport", directory)
            db = sqlite3.connect(str(path))
            try:
                db.execute("UPDATE worker_clocks SET reports=reports+1 WHERE identity='p'")
                db.commit()
            finally:
                db.close()
            self.assertNotEqual(fingerprints.fingerprint(path), UNCHANGED["transport"][0])
            sent[0] = sent[0] + " "
            self.assertNotEqual(fingerprints.digest_lines(sent), UNCHANGED["transport"][1])

    def test_sqlite_execute_is_the_connections_own_method(self):
        database = SQLiteDatabase()
        self.addCleanup(database.close)
        self.assertEqual(database.execute, database.connection.execute)
        self.assertIs(database.connection.row_factory, sqlite3.Row)
        self.assertEqual(database.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with SQLiteStore() as store:
            self.assertIs(store.db, store.database.connection)
            self.assertEqual(store.db.isolation_level, None)

    def test_transactions_send_the_same_statements(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coordinator.db"
            with fingerprints.statements() as sent, SQLiteStore(path) as store:
                del sent[:]  # Opening and migrating.
                with store.transaction():
                    store.enqueue("planner.request", {"n": 1})
                with self.assertRaises(RuntimeError), store.transaction():
                    raise RuntimeError("roll back")
            self.assertEqual(sent[0], "BEGIN IMMEDIATE")
            self.assertEqual(sent[-1], "ROLLBACK")
            self.assertEqual(sent[-2], "BEGIN IMMEDIATE")
            self.assertIn("COMMIT", sent)


class DatabasePortContractTests(unittest.TestCase):
    """What the store needs from any backend, run against each one that can run offline."""

    def each_backend(self, body):
        for name, factory in BACKENDS:
            with self.subTest(backend=name):
                if factory is None:
                    self.skipTest(POSTGRES_SKIP)
                with tempfile.TemporaryDirectory() as directory:
                    database = factory(directory)
                    try:
                        body(directory, database)
                    finally:
                        database.close()

    def test_rows_read_by_name_and_position_and_inserts_return_their_key(self):
        def body(_directory, database):
            database.execute("CREATE TABLE things (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            first = database.insert("INSERT INTO things(name) VALUES(?)", ("a",))
            second = database.insert("INSERT INTO things(name) VALUES(?)", ("b",))
            self.assertLess(first, second)
            row = database.execute("SELECT id, name FROM things WHERE id=?", (second,)).fetchone()
            self.assertEqual((row["id"], row["name"]), (second, "b"))
            self.assertEqual((row[0], row[1]), (second, "b"))
        self.each_backend(body)

    def test_commit_keeps_rollback_discards_and_only_writes_count_as_changes(self):
        def body(_directory, database):
            database.execute("CREATE TABLE things (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            self.assertFalse(database.in_transaction)
            database.begin()
            self.assertTrue(database.in_transaction)
            before = database.total_changes
            database.insert("INSERT INTO things(name) VALUES(?)", ("kept",))
            self.assertEqual(database.total_changes, before + 1)
            database.commit()
            self.assertFalse(database.in_transaction)
            database.begin()
            database.insert("INSERT INTO things(name) VALUES(?)", ("discarded",))
            database.rollback()
            unchanged = database.total_changes
            names = [row["name"] for row in database.execute("SELECT name FROM things ORDER BY id")]
            self.assertEqual(names, ["kept"])
            self.assertEqual(database.total_changes, unchanged)
        self.each_backend(body)

    def test_a_store_opened_on_the_port_migrates_and_serves_the_transport(self):
        def body(directory, database):
            store = SQLiteStore(database=database)
            self.assertIs(store.database, database)
            self.assertEqual(schema_version(store.database), SCHEMA_VERSION)
            incident = fingerprints._sample("messages/incident.json")
            delivery = Delivery(store, clock=lambda: 1.0, lease_seconds=5, retention_seconds=20)
            self.assertEqual(delivery.ingest("observer", incident), {"accepted": True, "replay": False})
            claimed = delivery.claim("p", "planner")["delivery"]
            self.assertEqual(claimed["payload"]["incident"], incident)
            self.assertEqual(store.position()[1], 2)
            # Another connection on the same file sees what the port committed.
            with SQLiteStore(Path(directory) / "coordinator.db") as other:
                self.assertEqual(other.incident(incident["incident_id"])["state"], "OPEN")
        self.each_backend(body)

    def test_the_sqlite_writer_is_held_for_the_whole_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coordinator.db"
            first, second = SQLiteDatabase(path), SQLiteDatabase(path, timeout=0.05)
            try:
                first.begin()
                with self.assertRaises(sqlite3.OperationalError):
                    second.begin()
                first.rollback()
                second.begin()
                second.rollback()
            finally:
                first.close()
                second.close()


def raw_connection_reads(source):
    """Line numbers where source reads some object's .db (the raw sqlite3 connection).

    Allowed: args.db (a command-line path) and assigning .db (the store sets it).
    Reading it at all is refused, so an alias (db = store.db) is caught too.
    """
    return [node.lineno for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Attribute) and node.attr == "db" and isinstance(node.ctx, ast.Load)
            and not (isinstance(node.value, ast.Name) and node.value.id == "args")]


class SeamTests(unittest.TestCase):
    """Coordinator SQL goes through the port, not around it."""

    COORDINATOR_MODULES = ("services/coordinator/store.py", "services/coordinator/core.py",
                           "services/coordinator/approvals.py", "runtime/worker_transport.py",
                           "runtime/worker_http.py", "runtime/approval_http.py", "runtime/coordinator_server.py")

    def test_coordinator_modules_do_not_call_the_raw_connection(self):
        for name in self.COORDINATOR_MODULES:
            with self.subTest(module=name):
                source = (ROOT / name).read_text(encoding="utf-8")
                self.assertEqual(raw_connection_reads(source), [])
                self.assertNotIn("sqlite3.connect", source)

    def test_the_seam_check_catches_an_alias_of_the_raw_connection(self):
        self.assertEqual(raw_connection_reads("db = store.db\ndb.execute('SELECT 1')\n"), [1])
        self.assertEqual(raw_connection_reads("store.db.execute('SELECT 1')\n"), [1])
        self.assertEqual(raw_connection_reads("with SQLiteStore(args.db) as store:\n    pass\n"), [])
        self.assertEqual(raw_connection_reads("self.db = database.connection\n"), [])

    def test_every_statement_of_a_store_reaches_sqlite_through_the_port(self):
        class Recording(SQLiteDatabase):
            def __init__(self, path):
                super().__init__(path)
                self.seen, inner = [], self.execute

                def execute(sql, parameters=()):
                    self.seen.append(sql)
                    return inner(sql, parameters)
                self.execute = execute

        incident = fingerprints._sample("messages/incident.json")
        proposal = fingerprints._sample("messages/proposal.json")
        with tempfile.TemporaryDirectory() as directory, fingerprints.statements() as sent:
            database = Recording(Path(directory) / "coordinator.db")
            with SQLiteStore(database=database) as store:
                delivery = Delivery(store, clock=lambda: 1.0, lease_seconds=5, retention_seconds=20,
                                    dependency_requests=True)
                delivery.ingest("observer", incident)
                claimed = delivery.claim("p", "planner")["delivery"]
                delivery.finish("p", "planner", claimed["delivery_id"], claimed["lease_token"], proposal)
                delivery.claim("v", "verifier")
        self.assertGreater(len(database.seen), 50)
        self.assertEqual(sent, ["PRAGMA foreign_keys = ON", *database.seen])

    def test_the_conflict_replan_rule_reads_through_the_port(self):
        """core.py::_conflict_replan_declined's three queries reach SQLite through the store's Database."""
        opened = []

        class Recording(SQLiteDatabase):
            def __init__(self, path):
                super().__init__(path)
                opened.append(self)
                self.seen, inner = [], self.execute

                def execute(sql, parameters=()):
                    self.seen.append(sql)
                    return inner(sql, parameters)
                self.execute = execute

        with tempfile.TemporaryDirectory() as directory, fingerprints.statements() as sent,                 patch("services.coordinator.store.SQLiteDatabase", Recording):
            fingerprints.conflict_replan_flow(directory)
        [database] = opened
        self.assertEqual(sent, ["PRAGMA foreign_keys = ON", *database.seen])
        for fragment in ("kind='execution_result' AND action_id=? LIMIT 1",
                         "count(*) FROM transitions WHERE action_id=? AND to_state='READY'",
                         "topic='executor.seal' AND action_id=? LIMIT 1"):
            with self.subTest(fragment=fragment):
                self.assertEqual(sum(fragment in sql for sql in database.seen), 1)


class RefusalTests(unittest.TestCase):
    """Nothing selects or half-opens a backend that is not there."""

    def test_a_store_refuses_a_postgres_database_before_running_anything(self):
        class FakePostgres(Database):
            dialect = POSTGRES

            def __init__(self):
                self.calls = []

            def execute(self, sql, parameters=()):
                self.calls.append(sql)
                raise AssertionError("must not run")

        database = FakePostgres()
        with self.assertRaises(BackendUnavailable):
            SQLiteStore(database=database)
        self.assertEqual(database.calls, [])

    def test_a_database_passed_as_the_path_is_refused_before_anything_opens(self):
        database = SQLiteDatabase()
        self.addCleanup(database.close)
        with tempfile.TemporaryDirectory() as directory,                 patch("services.coordinator.store.SQLiteDatabase") as opened:
            with self.assertRaisesRegex(TypeError, "database="):
                SQLiteStore(database)
            with self.assertRaisesRegex(TypeError, "database="):
                SQLiteStore(database, clock=lambda: None)
            self.assertEqual(list(Path(directory).iterdir()), [])
        opened.assert_not_called()

    def test_a_store_refuses_a_sqlite_database_without_a_sqlite3_connection(self):
        class NoConnection(Database):
            dialect = SQLITE

            def __init__(self):
                self.calls = []

            def execute(self, sql, parameters=()):
                self.calls.append(sql)
                raise AssertionError("must not run")

        database = NoConnection()
        with self.assertRaises(TypeError) as raised:
            SQLiteStore(database=database)
        self.assertIn("SQLiteDatabase", str(raised.exception))
        self.assertEqual(database.calls, [])

    def test_a_store_takes_a_path_or_a_database_not_both(self):
        database = SQLiteDatabase()
        self.addCleanup(database.close)
        with self.assertRaises(ValueError):
            SQLiteStore(":memory:", database=database)

    def test_a_failed_migration_closes_the_database_it_was_given(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coordinator.db"
            newer = sqlite3.connect(str(path))
            newer.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            newer.close()
            database = SQLiteDatabase(path)
            with self.assertRaises(ValueError):
                SQLiteStore(database=database)
            with self.assertRaises(sqlite3.ProgrammingError):
                database.execute("SELECT 1")

    def test_postgres_refuses_without_echoing_the_url(self):
        # Not a credential: a made-up placeholder, so the test can show the URL is never echoed.
        url = "postgresql://sentinel:placeholder-not-a-secret@db.example.invalid:5432/coordinator"
        for opener in (PostgresDatabase, open_database):
            with self.subTest(opener=opener.__name__), self.assertRaises(BackendUnavailable) as raised:
                opener(url)
            self.assertEqual(str(raised.exception), POSTGRES_DEFERRED)
            self.assertNotIn("placeholder-not-a-secret", str(raised.exception))
        with self.assertRaises(BackendUnavailable):
            open_database("POSTGRES://x")

    def test_open_database_accepts_paths_and_sqlite_urls_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coordinator.db"
            for url in (str(path), "sqlite:///" + str(path)):
                with self.subTest(url=url):
                    database = open_database(url)
                    try:
                        self.assertIsInstance(database, SQLiteDatabase)
                        database.execute("CREATE TABLE IF NOT EXISTS t (x)")
                    finally:
                        database.close()
            self.assertTrue(path.exists())
        memory = open_database("sqlite:///:memory:")
        self.addCleanup(memory.close)
        self.assertEqual(memory.execute("SELECT 1").fetchone()[0], 1)
        for url in ("sqlite://relative.db", "sqlite:///", "mysql://user:placeholder-not-a-secret@x/db"):
            with self.subTest(url=url), self.assertRaises(ValueError) as raised:
                open_database(url)
            self.assertNotIn("placeholder-not-a-secret", str(raised.exception))


class DialectTests(unittest.TestCase):
    def test_sqlite_passes_statements_through(self):
        statement = "INSERT OR REPLACE INTO dependency_revalidations VALUES(?,?)"
        self.assertIs(SQLITE.translate(statement), statement)
        self.assertIs(SQLITE.returning(statement, "id"), statement)
        self.assertEqual(SQLITE.begin_write, "BEGIN IMMEDIATE")
        self.assertFalse(SQLITE.is_retryable(type("E", (Exception,), {"sqlstate": "40001"})()))

    def test_postgres_rewrites_placeholders_and_sqlite_forms(self):
        cases = {
            "BEGIN IMMEDIATE": "BEGIN ISOLATION LEVEL SERIALIZABLE",
            "SELECT * FROM outbox WHERE id=?": "SELECT * FROM outbox WHERE id=%s",
            "SELECT '?', \"a?b\" FROM t WHERE x=?": "SELECT '?', \"a?b\" FROM t WHERE x=%s",
            "SELECT x FROM t WHERE y LIKE 'a%' AND z=?": "SELECT x FROM t WHERE y LIKE 'a%%' AND z=%s",
            "INSERT OR IGNORE INTO guard_reports(decision_sha256,action_id,report) VALUES(?,?,?)":
                "INSERT INTO guard_reports(decision_sha256,action_id,report) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
            "SELECT payload FROM messages WHERE kind=? AND action_id IS ? ORDER BY seq DESC LIMIT 1":
                "SELECT payload FROM messages WHERE kind=%s AND action_id IS NOT DISTINCT FROM %s "
                "ORDER BY seq DESC LIMIT 1",
            "SELECT 1 FROM t WHERE a IS NOT ? AND b IS NULL": "SELECT 1 FROM t WHERE a IS DISTINCT FROM %s AND b IS NULL",
            "UPDATE w SET m=max(m, excluded.m), n=min(n,excluded.n)":
                "UPDATE w SET m=GREATEST(m, excluded.m), n=LEAST(n,excluded.n)",
            "SELECT coalesce(max(id),0) FROM outbox": "SELECT coalesce(max(id),0) FROM outbox",
            "SELECT 'rowid' FROM t": "SELECT 'rowid' FROM t",
            # OFFSET the clause, a quoted "offset" and a name that only contains it are fine.
            "SELECT id FROM t ORDER BY id LIMIT ? OFFSET ?": "SELECT id FROM t ORDER BY id LIMIT %s OFFSET %s",
            "SELECT id FROM t LIMIT 10 OFFSET 20": "SELECT id FROM t LIMIT 10 OFFSET 20",
            'UPDATE w SET "offset"=? WHERE identity=?': 'UPDATE w SET "offset"=%s WHERE identity=%s',
            "SELECT max_abs_offset FROM worker_clocks": "SELECT max_abs_offset FROM worker_clocks",
            # Qualified references in DO UPDATE, and bare ones in an ordinary UPDATE.
            "INSERT INTO t(k,n) VALUES(?,1) ON CONFLICT(k) DO UPDATE SET n=t.n+excluded.n, m=coalesce(t.m, 0)":
                "INSERT INTO t(k,n) VALUES(%s,1) ON CONFLICT(k) DO UPDATE SET n=t.n+excluded.n, m=coalesce(t.m, 0)",
            "INSERT INTO t(k,s) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET s=excluded.s, flag=1, note=NULL":
                "INSERT INTO t(k,s) VALUES(%s,%s) ON CONFLICT(k) DO UPDATE SET s=excluded.s, flag=1, note=NULL",
            "UPDATE worker_clocks SET reports=reports+1 WHERE identity=?":
                "UPDATE worker_clocks SET reports=reports+1 WHERE identity=%s",
        }
        for sqlite_text, postgres_text in cases.items():
            with self.subTest(sql=sqlite_text):
                self.assertEqual(POSTGRES.translate(sqlite_text), postgres_text)
        self.assertEqual(POSTGRES.returning("INSERT INTO outbox(topic) VALUES(?)", "id"),
                         "INSERT INTO outbox(topic) VALUES(%s) RETURNING id")
        with self.assertRaises(ValueError):
            POSTGRES.returning("INSERT INTO outbox(topic) VALUES(?)", "id; DROP TABLE outbox")

    def test_postgres_refuses_what_it_cannot_translate(self):
        cases = {
            "PRAGMA user_version": "PRAGMA",
            "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)": "schema",
            "ALTER TABLE outbox ADD COLUMN trace_context TEXT": "schema",
            "SELECT last_insert_rowid()": "RETURNING",
            "SELECT rowid,* FROM incidents ORDER BY rowid": "sequence column",
            "INSERT OR REPLACE INTO dependency_revalidations VALUES(?,?)": "conflict target",
            "UPDATE messages SET kind=json_extract(payload,'$.kind')": "SQLite function",
            "INSERT OR IGNORE INTO t(a) VALUES(?) ON CONFLICT(a) DO NOTHING": "ON CONFLICT",
            "INSERT INTO worker_clocks(identity,offset) VALUES(?,?)": "offset is a reserved word",
            "UPDATE worker_clocks SET offset=? WHERE identity=?": "offset is a reserved word",
            "SELECT offset FROM worker_clocks": "offset is a reserved word",
            "INSERT INTO t(k,n) VALUES(?,1) ON CONFLICT(k) DO UPDATE SET n=n+1": "reads n without a table qualifier",
            "INSERT INTO t(k,n) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET n=GREATEST(n, excluded.n)":
                "reads n without a table qualifier",
            "INSERT INTO t(k,n) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET n=excluded.n WHERE n < excluded.n":
                "reads n without a table qualifier",
            "INSERT INTO t(k,g,c) VALUES(?,?,0) ON CONFLICT(k) DO UPDATE SET "
            "c=CASE WHEN g IS excluded.g THEN c+1 ELSE 0 END": "reads g, c without a table qualifier",
        }
        for statement, reason in cases.items():
            with self.subTest(sql=statement), self.assertRaises(NotPortable) as raised:
                POSTGRES.translate(statement)
            self.assertIn(reason, raised.exception.reason)
            self.assertEqual(raised.exception.statement, statement)

    def test_every_reason_is_named_when_several_apply(self):
        with self.assertRaises(NotPortable) as raised:
            POSTGRES.translate("INSERT INTO w(i,offset,r) VALUES(?,?,1) ON CONFLICT(i) DO UPDATE SET "
                               "offset=excluded.offset, r=r+1")
        self.assertIn("offset is a reserved word", raised.exception.reason)
        self.assertIn("reads r without a table qualifier", raised.exception.reason)

    def test_the_schemas_reserved_column_names_are_the_ones_the_dialect_knows(self):
        with SQLiteStore() as store:
            tables = [row[0] for row in store.database.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            names = {row[1].lower() for table in tables
                     for row in store.database.execute(f"PRAGMA table_info({table})")} | {t.lower() for t in tables}
        self.assertEqual(names & POSTGRES_RESERVED, set(RESERVED_COLUMN_NAMES))
        self.assertIn("offset", POSTGRES_RESERVED)

    def test_only_serialization_failures_and_deadlocks_are_retryable(self):
        def error(sqlstate):
            return type("E", (Exception,), {"sqlstate": sqlstate})()
        self.assertTrue(POSTGRES.is_retryable(error("40001")))
        self.assertTrue(POSTGRES.is_retryable(error("40P01")))
        self.assertFalse(POSTGRES.is_retryable(error("23505")))
        self.assertFalse(POSTGRES.is_retryable(RuntimeError("no sqlstate")))
        self.assertEqual(POSTGRES.begin_write, "BEGIN ISOLATION LEVEL SERIALIZABLE")


class PortabilityInventoryTests(unittest.TestCase):
    """Every statement the flows send, classified by the Postgres dialect."""

    def test_what_a_postgres_store_still_needs_is_listed(self):
        statements = set()
        for name in UNCHANGED:
            with tempfile.TemporaryDirectory() as directory, fingerprints.statements() as sent:
                run_flow(name, directory)
            statements |= {normalise(sql) for sql in sent}
        translated, schema, queries = 0, 0, {}
        for statement in sorted(statements):
            self.assertIs(SQLITE.translate(statement), statement)
            try:
                POSTGRES.translate(statement)
                translated += 1
            except NotPortable as error:
                if error.reason.startswith(("PRAGMA", "schema")):
                    schema += 1
                else:
                    queries[statement] = error.reason
        self.assertEqual(set(queries), set(NOT_PORTABLE_QUERIES))
        for statement, fragments in NOT_PORTABLE_QUERIES.items():
            for fragment in fragments:
                with self.subTest(sql=statement[:60], reason=fragment):
                    self.assertIn(fragment, queries[statement])
        # Seven runtime queries and three migration statements.
        self.assertEqual(len(NOT_PORTABLE_QUERIES) - MIGRATION_QUERIES, 7)
        # PRAGMAs (foreign keys, versions, file identity) and the CREATE/ALTER
        # statements of migrations 1-6 and the transport tables.
        self.assertEqual(schema, 37)
        self.assertEqual(translated, len(statements) - schema - len(NOT_PORTABLE_QUERIES))
        self.assertGreater(translated, 60)


if __name__ == "__main__":
    unittest.main()
