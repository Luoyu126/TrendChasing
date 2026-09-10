"""PostgreSQL runtime for the existing imported trendradar schema.

Only the small SQL dialect used by the content/paper stores is adapted. Business
state stays in PostgreSQL; SQLite is never downloaded or uploaded at runtime.
A session advisory lock on this very connection fences all business mutations.
Use a direct connection or Supabase session pooler, never transaction pooling.
"""
import os
import re
from pathlib import Path

from .supabase_sync import postgres_ddl

LOCK_ID = 734901264182  # Also excludes the initial snapshot importer.


class Row(tuple):
    def __new__(cls, values, names):
        obj = super().__new__(cls, values)
        obj.names = names
        return obj

    def keys(self):
        return self.names

    def __getitem__(self, key):
        return super().__getitem__(self.names.index(key) if isinstance(key, str) else key)


def row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(values, names)


def translate(sql):
    sql = sql.strip().rstrip(';')
    ignore = bool(re.match(r'INSERT OR IGNORE\b', sql, re.I))
    sql = re.sub(r'^INSERT OR IGNORE\b', 'INSERT', sql, flags=re.I)
    if re.search(r'\bOR REPLACE\b', sql, re.I):
        raise ValueError('use_explicit_upsert')
    if sql.upper() == 'BEGIN IMMEDIATE':
        return 'BEGIN'
    if sql.upper().startswith('CREATE '):
        sql = postgres_ddl(sql)
    # These are the only scalar min/max expressions in the shared stores.
    for old, new in (
        ('min(pool_entries.history,excluded.history)', 'LEAST(pool_entries.history,excluded.history)'),
        ('max(version,excluded.version)', 'GREATEST(work_records.version,excluded.version)'),
        ('max(not_before,excluded.not_before)', 'GREATEST(llm_limits.not_before,excluded.not_before)'),
        ('failures=failures+1', 'failures=llm_backoff.failures+1'),
    ):
        sql = sql.replace(old, new)
    parts = re.split(r"('(?:''|[^'])*')", sql)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r':([a-z_][a-z_0-9]*)', r'%(\1)s', parts[i].replace('?', '%s'))
    sql = ''.join(parts)
    if ignore:
        sql += ' ON CONFLICT DO NOTHING'
    # PostgreSQL requires selected created_at to be grouped, even on imported PKs.
    sql = sql.replace('GROUP BY b.id', 'GROUP BY b.id,b.created_at')
    return sql


class Cursor:
    def __init__(self, cursor, identity=False):
        self.cursor = cursor
        self.lastrowid = cursor.fetchone()[0] if identity else None

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)


class Database:
    def __init__(self, connection):
        self.connection = connection

    @property
    def in_transaction(self):
        from psycopg.pq import TransactionStatus
        return self.connection.info.transaction_status != TransactionStatus.IDLE

    def execute(self, sql, params=None):
        sql = translate(sql)
        # Match sqlite's implicit write transactions; reads don't hold snapshots
        # over slow API calls. Explicit SAVEPOINT also needs a transaction in PG.
        if not self.in_transaction and re.match(r'INSERT|UPDATE|DELETE|SAVEPOINT', sql, re.I):
            self.connection.execute('BEGIN')
        identity = bool(re.match(r'INSERT INTO fetch_runs\b', sql, re.I))
        if identity:
            sql += ' RETURNING id'
        return Cursor(self.connection.execute(sql, params), identity)

    def executemany(self, sql, rows):
        for row in rows:
            self.execute(sql, row)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        self.rollback() if kind else self.commit()

    def close(self):
        # Session locks survive a pooler's client disconnect: explicitly unlock.
        try:
            self.rollback()
            self.connection.execute('SELECT pg_advisory_unlock(%s)', (LOCK_ID,))
        finally:
            self.connection.close()


def open_postgres():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    uri = os.environ.get('SUPABASE_DATABASE_URL', '')
    if not uri:
        raise ValueError('missing_database_url')
    if conninfo_to_dict(uri).get('port') == '6543':
        raise ValueError('session_pooler_required_not_transaction_pooler')
    root = Path(__file__).resolve().parents[2]
    connection = psycopg.connect(
        uri, autocommit=True, connect_timeout=10, prepare_threshold=None,
        sslmode='verify-full',
        sslrootcert=os.environ.get('PGSSLROOTCERT', str(root / 'config/certs/supabase-ca.crt')),
        row_factory=row_factory,
    )
    db = Database(connection)
    try:
        connection.execute("SET statement_timeout='30s'")
        connection.execute("SET extra_float_digits=3")
        connection.execute("SET lock_timeout='5s'")
        if not connection.execute('SELECT pg_try_advisory_lock(%s)', (LOCK_ID,)).fetchone()[0]:
            raise RuntimeError('another_content_run_is_active')
        connection.execute('SET search_path = trendradar, pg_catalog')
        # Require the user's migrated schema; never silently create a second DB.
        if not connection.execute("SELECT to_regclass('trendradar.records')").fetchone()[0]:
            raise ValueError('supabase_import_required')
        from .store import SCHEMA
        from trendradar.papers.state import SCHEMA as PAPER_SCHEMA
        with db:
            connection.execute('BEGIN')
            for statement in (SCHEMA + PAPER_SCHEMA).split(';'):
                if statement.strip():
                    db.execute(statement)
        return db
    except BaseException:
        db.close()
        raise
