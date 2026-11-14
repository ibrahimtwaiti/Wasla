"""Compare every public table after restoration into a disposable CI database.

Both URLs are supplied by the test runner. Prints only table counts, never rows.
"""
import hashlib
import json
import os
import psycopg
from psycopg import sql

def fingerprint(conn,table):
    data=conn.execute(sql.SQL('SELECT to_jsonb(t)::text FROM {} t').format(sql.Identifier(table))).fetchall()
    return len(data),hashlib.sha256('\n'.join(sorted(r[0] for r in data)).encode()).hexdigest()

def main():
    source=os.environ['WASLA_TEST_DATABASE_URL'];target=os.environ['WASLA_RESTORE_TEST_URL']
    if source==target:raise SystemExit('Source and restore target must differ')
    with psycopg.connect(source) as a,psycopg.connect(target) as b:
        tables=[r[0] for r in a.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
        restored=[r[0] for r in b.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
        if tables!=restored:raise SystemExit('Restored table list differs')
        for table in tables:
            if fingerprint(a,table)!=fingerprint(b,table):raise SystemExit('Restore comparison failed: '+table)
        print('Restore verified: all rows match across',len(tables),'tables')
if __name__=='__main__':main()
