"""Transactional storage for a single-node V1 pilot; never use an ephemeral disk."""
import os
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

@contextmanager
def transaction():
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

def initialize():
    Path(path()).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path()) as db:
        db.execute('PRAGMA journal_mode=WAL')
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
        if not exists:
            db.executescript((ROOT / 'migrations/001_initial.sql').read_text())

def audit(db, user, action, entity, detail=''):
    db.execute('INSERT INTO audit VALUES(?,?,?,?,?,?,?)',
               (uid(), user['tenant_id'], user['id'], action, entity, detail, now()))

def one(db, sql, args=()):
    row = db.execute(sql, args).fetchone()
    return dict(row) if row else None

def rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]
