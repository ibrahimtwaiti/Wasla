"""SQLite local storage; PostgreSQL storage for serverless deployments."""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent

def now():
    return datetime.now(timezone.utc).isoformat()

def uid():
    return uuid4().hex

def path():
    return os.environ.get('WASLA_DB', str(ROOT / 'data/wasla.sqlite3'))

def pg_url():
    return os.environ.get('DATABASE_URL') or os.environ.get('WASLA_DATABASE_URL')

def postgres():
    return bool(pg_url())

class PgRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

def _pg_row(cursor):
    columns = [column.name for column in (cursor.description or ())]
    def factory(values):
        return PgRow(zip(columns, values))
    return factory

def _pg_sql(sql):
    # SQL is internally authored, never user controlled. Parameters stay bound.
    sql = sql.replace('?', '%s')
    sql = re.sub(r'\bIS %s\b', 'IS NOT DISTINCT FROM %s', sql)
    sql = sql.replace('max(last_inbound,%s)', 'GREATEST(last_inbound,%s)')
    return sql

class PgAdapter:
    def __init__(self, connection):
        self.connection = connection
    def execute(self, sql, args=()):
        return self.connection.execute(_pg_sql(sql), args)

@contextmanager
def transaction():
    if postgres():
        import psycopg
        with psycopg.connect(pg_url(), connect_timeout=10, prepare_threshold=None,
                             row_factory=_pg_row) as conn:
            with conn.transaction():
                yield PgAdapter(conn)
    else:
        db = sqlite3.connect(path(), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

def migrate():
    # Never migrate automatically inside Vercel requests.
    if not postgres():
        return initialize()
    import psycopg
    direct = os.environ.get('DATABASE_URL_UNPOOLED') or pg_url()
    with psycopg.connect(direct, connect_timeout=15, prepare_threshold=None) as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('wasla-schema-v1'))")
            initialized = conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0]
            if not initialized or conn.execute('SELECT version FROM schema_migrations WHERE version=1').fetchone() is None:
                script = (ROOT / 'migrations/002_postgres.sql').read_text()
                for statement in script.split(';'):
                    if statement.strip():
                        conn.execute(statement)
            if conn.execute('SELECT version FROM schema_migrations WHERE version=2').fetchone() is None:
                script = (ROOT / 'migrations/003_session_activity.sql').read_text()
                for statement in script.split(';'):
                    if statement.strip():
                        conn.execute(statement)

def initialize():
    if postgres():
        with transaction() as db:
            if not one(db, 'SELECT version FROM schema_migrations WHERE version=1'):
                raise RuntimeError('PostgreSQL migrations missing: python -m wasla.cli init')
            if not one(db, 'SELECT version FROM schema_migrations WHERE version=2'):
                raise RuntimeError('Security migration missing: python -m wasla.cli init')
        return
    Path(path()).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path()) as db:
        db.execute('PRAGMA journal_mode=WAL')
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
        if not exists:
            db.executescript((ROOT / 'migrations/001_initial.sql').read_text())
        # Safe upgrade of existing local SQLite development databases.
        db.execute("CREATE TABLE IF NOT EXISTS session_activity(token_hash TEXT PRIMARY KEY REFERENCES sessions(token_hash) ON DELETE CASCADE, issued_at BIGINT NOT NULL,last_seen_at BIGINT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(2,?)", (now(),))
        db.commit()

def audit(db, user, action, entity, detail=''):
    db.execute('INSERT INTO audit VALUES(?,?,?,?,?,?,?)',
               (uid(), user['tenant_id'], user['id'], action, entity, detail, now()))

def one(db, sql, args=()):
    row = db.execute(sql, args).fetchone()
    return dict(row) if row else None

def rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]
