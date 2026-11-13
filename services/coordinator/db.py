"""The Coordinator's Database port and its SQL dialects (P5.01 stage 8.1).

Every statement the Coordinator store, its transport and its approval and
review helpers run goes through a Database: execute, insert (a row's new id),
begin, commit, rollback, in_transaction and total_changes. SQLiteDatabase is
the default and the only backend that runs. Its execute is the sqlite3
connection's own execute, and begin, commit and rollback send the same
statements as before the port, so SQLite receives the same SQL text in the same
order and stores the same rows (tests/test_coordinator_db.py compares both with
digests recorded before the port existed).

The Postgres side is a dialect only. PostgresDialect.translate rewrites the
store's SQLite statements into Postgres text, or raises NotPortable naming what
still has to change. Nothing here has run against a Postgres server: no Postgres
driver is in the hash-locked requirements, and adding one, with CloudNativePG,
is the owners' go/no-go decision for P5.01. PostgresDatabase and
open_database("postgresql://...") therefore refuse with BackendUnavailable.
"""
import re
import sqlite3


class BackendUnavailable(RuntimeError):
    """The selected database backend cannot be used by this build."""


class NotPortable(ValueError):
    """A SQLite statement has no mechanical translation into the target dialect."""

    def __init__(self, dialect, statement, reason):
        super().__init__(f"{dialect}: {reason}")
        self.dialect, self.statement, self.reason = dialect, statement, reason


class Dialect:
    """SQLite, the default: statements pass through unchanged."""

    name = "sqlite"
    begin_write = "BEGIN IMMEDIATE"
    # SQLite waits for its writer lock (the connection timeout) instead of
    # aborting a transaction, so no error asks for a retry.
    retryable_sqlstates = frozenset()

    def translate(self, sql):
        return sql

    def returning(self, sql, key):
        """The INSERT that reports the new row's key; SQLite reads it from the cursor instead."""
        return sql

    def is_retryable(self, error):
        """True when error is a serialization failure or deadlock that the whole transaction may retry."""
        return getattr(error, "sqlstate", None) in self.retryable_sqlstates


# Single- or double-quoted SQL literals and identifiers, with doubled quotes inside.
_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NOT_PORTABLE = (
    (re.compile(r"^\s*PRAGMA\b", re.I),
     "PRAGMA is SQLite-only; the schema version needs a Postgres migration table"),
    (re.compile(r"^\s*(CREATE|ALTER|DROP)\b", re.I),
     "schema statements are per dialect; the Postgres schema (explicit BIGSERIAL keys) is its own migration set"),
    (re.compile(r"\blast_insert_rowid\s*\(", re.I),
     "last_insert_rowid() has no Postgres equivalent; use Database.insert, which adds RETURNING"),
    (re.compile(r"\browid\b", re.I),
     "rowid has no Postgres equivalent; order by an explicit sequence column"),
    (re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.I),
     "INSERT OR REPLACE needs an explicit conflict target: INSERT ... ON CONFLICT (key) DO UPDATE"),
    (re.compile(r"\b(json_extract|json_valid|randomblob|strftime|hex)\s*\(", re.I),
     "SQLite function with no same-name Postgres equivalent"),
)
_INSERT_OR_IGNORE = re.compile(r"^(\s*)INSERT\s+OR\s+IGNORE\s+INTO\b", re.I)
_ON_CONFLICT = re.compile(r"\bON\s+CONFLICT\b", re.I)
_IS_PARAMETER = re.compile(r"\bIS\s+(NOT\s+)?\?", re.I)
# Two-argument max()/min() are SQLite's scalar forms; one argument stays an aggregate.
_SCALAR_EXTREME = re.compile(r"\b(max|min)\(([^(),]+),([^(),]+)\)", re.I)


def _outside_quotes(sql, rewrite):
    """Apply rewrite to the parts of sql that are not inside quotes."""
    parts, last = [], 0
    for match in _QUOTED.finditer(sql):
        parts += [rewrite(sql[last:match.start()]), match.group(0)]
        last = match.end()
    return "".join(parts + [rewrite(sql[last:])])


def _scalar_extreme(match):
    return f"{'GREATEST' if match.group(1).lower() == 'max' else 'LEAST'}({match.group(2)},{match.group(3)})"


def _is_parameter(match):
    return "IS DISTINCT FROM ?" if match.group(1) else "IS NOT DISTINCT FROM ?"


class PostgresDialect(Dialect):
    """Rewrites the store's SQLite statements into Postgres text; not run against a server.

    The text is for a driver with %s placeholders (psycopg), always called with
    a parameter sequence, so every literal % is doubled.
    """

    name = "postgres"
    begin_write = "BEGIN ISOLATION LEVEL SERIALIZABLE"
    # serialization_failure and deadlock_detected: Postgres aborted the
    # transaction and the whole of it may run again.
    retryable_sqlstates = frozenset({"40001", "40P01"})

    def translate(self, sql):
        unquoted = _QUOTED.sub("''", sql)
        for pattern, reason in _NOT_PORTABLE:
            if pattern.search(unquoted):
                raise NotPortable(self.name, sql, reason)
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            return self.begin_write
        text = sql
        if _INSERT_OR_IGNORE.search(text):
            if _ON_CONFLICT.search(unquoted):
                raise NotPortable(self.name, sql, "INSERT OR IGNORE combined with ON CONFLICT")
            text = _INSERT_OR_IGNORE.sub(r"\1INSERT INTO", text).rstrip() + " ON CONFLICT DO NOTHING"
        text = text.replace("%", "%%")

        def rewrite(part):
            part = _IS_PARAMETER.sub(_is_parameter, part)
            part = _SCALAR_EXTREME.sub(_scalar_extreme, part)
            return part.replace("?", "%s")
        return _outside_quotes(text, rewrite)

    def returning(self, sql, key):
        if not _IDENTIFIER.fullmatch(key):
            raise ValueError("The returned key must be a plain column name")
        return f"{self.translate(sql).rstrip()} RETURNING {key}"


SQLITE = Dialect()
POSTGRES = PostgresDialect()


class Database:
    """The port: what the Coordinator store needs from a database connection.

    execute(sql, parameters) returns a cursor whose rows are read by column
    name and by position. insert(sql, parameters, key) runs an INSERT and
    returns the new row's key. total_changes counts rows changed since the
    connection opened (restore detection reads it: SQLiteStore.count_commit).
    One instance is one connection, used by one thread at a time.
    """

    dialect = SQLITE

    def execute(self, sql, parameters=()):
        raise NotImplementedError

    def insert(self, sql, parameters=(), key="id"):
        raise NotImplementedError

    @property
    def in_transaction(self):
        raise NotImplementedError

    @property
    def total_changes(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def begin(self):
        """Start a write transaction that holds the writer (SQLite) or is serializable (Postgres)."""
        self.execute(self.dialect.begin_write)

    def commit(self):
        self.execute("COMMIT")

    def rollback(self):
        self.execute("ROLLBACK")


class SQLiteDatabase(Database):
    """One sqlite3 connection, opened the way SQLiteStore has always opened it."""

    dialect = SQLITE

    def __init__(self, path=":memory:", *, timeout=10):
        self.connection = sqlite3.connect(str(path), isolation_level=None, timeout=timeout)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        # The connection's own bound method: no wrapper sits between the store and SQLite.
        self.execute = self.connection.execute

    def insert(self, sql, parameters=(), key="id"):
        return self.execute(sql, parameters).lastrowid

    @property
    def in_transaction(self):
        return self.connection.in_transaction

    @property
    def total_changes(self):
        return self.connection.total_changes

    def close(self):
        self.connection.close()


POSTGRES_DEFERRED = ("The Postgres backend is not implemented: no Postgres driver is in the hash-locked "
                     "requirements, and adding one is the owners' go/no-go decision for P5.01. "
                     "Use the SQLite default.")


class PostgresDatabase(Database):
    """Placeholder for the Postgres adapter behind the same port; it refuses to open."""

    dialect = POSTGRES

    def __init__(self, url):
        del url  # Never echoed: a connection URL can carry a password.
        raise BackendUnavailable(POSTGRES_DEFERRED)


def open_database(url):
    """Open a Database from a path, a sqlite:/// URL or a postgresql:// URL (which refuses).

    Error messages never include the URL.
    """
    text = str(url)
    scheme, separator, rest = text.partition("://")
    if not separator or len(scheme) == 1:  # A plain path, including a Windows drive letter.
        return SQLiteDatabase(text)
    scheme = scheme.lower()
    if scheme == "sqlite":
        if not rest.startswith("/") or rest == "/":
            raise ValueError("A sqlite URL is sqlite:///relative/path or sqlite:////absolute/path")
        return SQLiteDatabase(rest[1:])
    if scheme in {"postgres", "postgresql"}:
        return PostgresDatabase(text)
    raise ValueError("Unsupported database URL scheme; expected a path, sqlite:/// or postgresql://")
