"""Round-trip a synthetic Wasla Staging database through encrypted R2 storage.

Needs a fresh, local wasla_restore_drill database, pg_dump, pg_restore and age.
Use only the short-lived credential for the dedicated staging backup bucket.
"""

import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import ClientError
import psycopg

from scripts.backup_postgres import database_env
from scripts.deletion_ledger import export_ledger
from scripts.verify_restore import fingerprint
from scripts.restore_postgres import restore
from wasla import privacy
from wasla.db import now, transaction, uid
from staging_database import exclusive_drill

HOST = 'ep-holy-breeze-b98rfxye.c-7.eu-central-1.aws.neon.tech'
BUCKET = 'wasla-v2-staging-backups'
ENDPOINT = 'https://cbd764cddd5790396878771139794161.r2.cloudflarestorage.com'
TARGET_DB = os.environ.get('STAGING_RESTORE_DATABASE', 'wasla_restore_drill')
if not re.fullmatch(r'wasla_restore_drill(?:_[a-z0-9]{1,32})?', TARGET_DB):
    raise ValueError('Use a dedicated local restore database')
TARGET = 'postgresql:///' + TARGET_DB


def command(args, **kwargs):
    # Provider connection secrets never enter process arguments or logs.
    return subprocess.run(args, check=True, timeout=120, capture_output=True, **kwargs)


def erase(tenant):
    with transaction() as db:
        if not db.execute('SELECT id FROM tenants WHERE id=?', (tenant,)).fetchone():
            return
        job = privacy.queue(db, tenant, None, 'tenant')
    for _ in range(8):
        if not privacy.process_deletion(job):
            break
    with transaction() as db:
        if (
            db.execute('SELECT state FROM deletion_jobs WHERE id=?', (job,)).fetchone()[0]
            != 'completed'
        ):
            raise RuntimeError('Synthetic deletion did not finish')


def compare(source):
    query = "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    with psycopg.connect(source) as live, psycopg.connect(TARGET) as restored:
        tables = live.execute(query).fetchall()
        if not tables or tables != restored.execute(query).fetchall():
            raise RuntimeError('Restore table inventory mismatch')
        for (table,) in tables:
            if fingerprint(live, table) != fingerprint(restored, table):
                raise RuntimeError('Restore mismatch: ' + table)
    print(f'PASS restored all rows in {len(tables)} tables', flush=True)


def drill():
    source = os.environ['DATABASE_URL']
    uri = urlsplit(source)
    if (
        os.environ.get('WASLA_ENV') != 'staging'
        or uri.hostname != HOST
        or uri.path != '/wasla_staging'
        or os.environ.get('WASLA_MEDIA_ENDPOINT') != ENDPOINT
    ):
        raise RuntimeError('Dedicated Staging source required')
    with psycopg.connect(source) as db, psycopg.connect(TARGET) as target:
        if db.execute('SELECT current_database()').fetchone()[0] != 'wasla_staging':
            raise RuntimeError('Wrong source database')
        if db.execute(
            'SELECT EXISTS (SELECT 1 FROM tenants) OR EXISTS (SELECT 1 FROM users)'
        ).fetchone()[0]:
            raise RuntimeError('Drill requires an empty Staging workspace inventory')
        if (
            target.execute('SELECT current_database(),inet_server_addr()').fetchone()
            != (TARGET_DB, None)
            or target.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0]
        ):
            raise RuntimeError('A fresh local Unix-socket restore database is required')
    s3 = boto3.client(
        's3',
        region_name='auto',
        endpoint_url=ENDPOINT,
        aws_access_key_id=os.environ['STAGING_BACKUP_ID'],
        aws_secret_access_key=os.environ['STAGING_BACKUP_SECRET'],
    )
    tenants = [uid(), uid()]
    object_key = 'drills/' + uid() + '.dump.age'
    uploaded = False
    with tempfile.TemporaryDirectory(prefix='wasla-restore-') as folder:
        root = Path(folder)
        identity, archive, encrypted, download, decrypted = [
            root / name for name in ('identity', 'archive', 'encrypted', 'download', 'restored')
        ]
        try:
            with transaction() as db:
                for tenant in tenants:
                    db.execute(
                        'INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)',
                        (tenant, 'Synthetic recovery control', now()),
                    )
            command(['age-keygen', '-o', str(identity)])
            public = command(['age-keygen', '-y', str(identity)]).stdout.decode().strip()
            command(
                ['pg_dump', '-Fc', '--no-acl', '--no-owner', '-f', str(archive)],
                env=database_env(source),
            )
            command(['age', '-r', public, '-o', str(encrypted), str(archive)])
            with encrypted.open('rb') as handle:
                digest = hashlib.file_digest(handle, 'sha256').hexdigest()
            with encrypted.open('rb') as data:
                s3.put_object(
                    Bucket=BUCKET,
                    Key=object_key,
                    Body=data,
                    IfNoneMatch='*',
                    Metadata={'sha256': digest},
                )
            uploaded = True
            # A replayed write must leave the original untouched.
            try:
                s3.put_object(Bucket=BUCKET, Key=object_key, Body=b'duplicate', IfNoneMatch='*')
            except ClientError as exc:
                if exc.response['ResponseMetadata']['HTTPStatusCode'] != 412:
                    raise
            else:
                raise RuntimeError('Backup object allowed overwrite')
            meta = s3.head_object(Bucket=BUCKET, Key=object_key)
            if (
                meta['Metadata'].get('sha256') != digest
                or meta['ContentLength'] != encrypted.stat().st_size
            ):
                raise RuntimeError('Archive metadata mismatch')
            s3.download_file(BUCKET, object_key, str(download))
            with download.open('rb') as handle:
                if hashlib.file_digest(handle, 'sha256').hexdigest() != digest:
                    raise RuntimeError('Archive checksum mismatch')
            print(
                'PASS private R2 conditional write, overwrite rejection and downloaded SHA-256',
                flush=True,
            )
            command(['age', '-d', '-i', str(identity), '-o', str(decrypted), str(download)])
            omitted = restore(
                decrypted,
                {
                    'PATH': os.environ['PATH'],
                    'PGHOST': '/var/run/postgresql',
                    'PGDATABASE': TARGET_DB,
                },
            )
            print(
                f'PASS atomic offline restore; omitted {omitted} Neon column settings', flush=True
            )
            compare(source)
            erase(tenants[0])
            ledger = root / 'erasure.json'
            ledger.write_text(json.dumps(export_ledger()))
            # The replay process gets only its local database and executable PATH.
            env = {
                'PATH': os.environ['PATH'],
                'DATABASE_URL': TARGET,
                'WASLA_ENV': 'restore',
                'PYTHONPATH': os.getcwd(),
            }
            command(
                [sys.executable, '-m', 'scripts.deletion_ledger', 'replay', str(ledger), '--apply'],
                env=env,
            )
            with psycopg.connect(TARGET) as db:
                if db.execute('SELECT id FROM tenants ORDER BY id').fetchall() != [(tenants[1],)]:
                    raise RuntimeError('Restore replay failed isolation')
            print(
                'PASS offline erasure replay removed one restored workspace and retained its control',
                flush=True,
            )
        finally:
            for tenant in tenants:
                erase(tenant)
            if uploaded:
                s3.delete_object(Bucket=BUCKET, Key=object_key)
                if s3.list_objects_v2(Bucket=BUCKET, Prefix=object_key)['KeyCount']:
                    raise RuntimeError('Synthetic backup object was not removed')
                print('PASS drill archive cleanup', flush=True)


if __name__ == '__main__':
    with exclusive_drill():
        drill()
