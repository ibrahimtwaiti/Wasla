"""Import an authorized, consistent V1 SQLite backup into an EMPTY PostgreSQL DB.

Run migrations first. Default mode prints counts only. --apply additionally requires
the exact target database name; no data is deleted or overwritten.
Sessions and rate-limit counters are intentionally not migrated.
"""
import argparse
import os
import sqlite3
from pathlib import Path

TABLES = (
    'tenants', 'users', 'requests', 'provider_opportunities', 'projects',
    'milestones', 'contacts', 'opportunities', 'tasks', 'channels',
    'conversations', 'messages', 'webhook_events', 'notifications',
    'audit', 'content'
)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Consistent offline SQLite backup')
    parser.add_argument('--apply', action='store_true', help='Actually import the source data')
    parser.add_argument('--target-database', help='Expected PostgreSQL database name (required with --apply)')
    args = parser.parse_args()
    source = Path(args.source).resolve()
    if not source.is_file():
        raise SystemExit('SQLite source file not found')
    src = sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)
    try:
        if src.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise SystemExit('Source SQLite integrity check failed')
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(TABLES).issubset(tables):
            raise SystemExit('SQLite source does not contain the complete Wasla V1 schema')
        counts = {table: src.execute('SELECT count(*) FROM "'+table+'"').fetchone()[0] for table in TABLES}
        if not args.apply:
            print('Dry run only; data NOT copied. Table counts:', counts)
            print('Sessions and rate-limit counters are never migrated.')
            return
        if not args.target_database:
            raise SystemExit('Provide --target-database when using --apply')
        direct = os.environ.get('DATABASE_URL_UNPOOLED', '')
        if not direct or '-pooler.' in direct:
            raise SystemExit('Set DATABASE_URL_UNPOOLED to a direct PostgreSQL connection')
        import psycopg
        from psycopg import sql
        with psycopg.connect(direct, connect_timeout=15, prepare_threshold=None) as conn:
            with conn.transaction():
                actual = conn.execute('SELECT current_database()').fetchone()[0]
                if actual != args.target_database:
                    raise SystemExit('Expected database does not match actual target')
                version = conn.execute('SELECT version FROM schema_migrations WHERE version=1').fetchone()
                if not version:
                    raise SystemExit('Migrate PostgreSQL schema before importing')
                for table in TABLES:
                    current = conn.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(table))).fetchone()[0]
                    if current:
                        raise SystemExit('Import requires all destination data tables to be EMPTY: '+table)
                for table in TABLES:
                    if not counts[table]:
                        continue
                    cursor = src.execute('SELECT * FROM "'+table+'"')
                    fields = [d[0] for d in cursor.description]
                    q = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
                        sql.Identifier(table),
                        sql.SQL(',').join(map(sql.Identifier, fields)),
                        sql.SQL(',').join(sql.Placeholder() for _ in fields))
                    while True:
                        batch = cursor.fetchmany(250)
                        if not batch:
                            break
                        with conn.cursor() as pg:
                            pg.executemany(q, batch)
                for table in TABLES:
                    imported = conn.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(table))).fetchone()[0]
                    if imported != counts[table]:
                        raise RuntimeError('Count mismatch; rolling back: '+table)
        print('Atomic import verified. Table counts:', counts)
        print('Sessions and rate limits were intentionally not copied.')
    finally:
        src.close()

if __name__ == '__main__':
    main()
