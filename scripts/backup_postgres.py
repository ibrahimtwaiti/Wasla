"""Back up PostgreSQL to a private Cloudflare R2 bucket without client-side encryption.

The PostgreSQL custom-format archive is sensitive. It is created as a mode-0600
temporary file, uploaded only after pg_dump exits successfully, and removed when
the temporary directory is cleaned up. HTTPS/TLS and R2 access controls remain
mandatory; Cloudflare handles encryption at rest.
"""
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4


def database_env(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('postgresql', 'postgres') or not parsed.hostname:
        raise ValueError('A direct PostgreSQL URL is required')
    if '-pooler.' in parsed.hostname:
        raise ValueError('Use the direct database endpoint for pg_dump')
    query = parse_qs(parsed.query)
    sslmode = query.get('sslmode', ['verify-full'])[0]
    if sslmode not in ('require', 'verify-ca', 'verify-full'):
        raise ValueError('TLS is required for backups')
    env = {k: v for k, v in os.environ.items() if not k.startswith('PG')}
    env.update(PGHOST=parsed.hostname, PGPORT=str(parsed.port or 5432),
               PGDATABASE=unquote(parsed.path.lstrip('/')), PGUSER=unquote(parsed.username or ''),
               PGPASSWORD=unquote(parsed.password or ''), PGSSLMODE=sslmode, PGCONNECT_TIMEOUT='15')
    if not env['PGDATABASE'] or not env['PGUSER'] or not env['PGPASSWORD']:
        raise ValueError('Database, user and password are required')
    channel_binding = query.get('channel_binding', [''])[0]
    if channel_binding in ('require', 'prefer', 'disable'):
        env['PGCHANNELBINDING'] = channel_binding
    return env


def r2_settings():
    endpoint = os.environ['R2_ENDPOINT_URL'].rstrip('/')
    if not re.fullmatch(r'https://[a-f0-9]{32}(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com', endpoint):
        raise ValueError('Use the HTTPS S3 API endpoint from the R2 dashboard')
    bucket = os.environ['R2_BUCKET']
    prefix = os.environ.get('R2_PREFIX', 'production/postgres').strip('/')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket):
        raise ValueError('A valid R2 bucket name is required')
    if not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', prefix):
        raise ValueError('A dedicated R2 prefix is required')
    access_key = os.environ['R2_ACCESS_KEY_ID']
    secret_key = os.environ['R2_SECRET_ACCESS_KEY']
    if not access_key or not secret_key:
        raise ValueError('R2 credentials are required')
    # Exclude credentials, profiles, endpoints or session tokens for other providers.
    env = {k: v for k, v in os.environ.items() if not k.startswith(('AWS_', 'R2_', 'BACKUP_', 'PG'))}
    env.update(AWS_ACCESS_KEY_ID=access_key, AWS_SECRET_ACCESS_KEY=secret_key,
               AWS_DEFAULT_REGION='auto', AWS_EC2_METADATA_DISABLED='true',
               AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull,
               AWS_REQUEST_CHECKSUM_CALCULATION='when_required',
               AWS_RESPONSE_CHECKSUM_VALIDATION='when_required', AWS_PAGER='')
    return endpoint, bucket, prefix, env


def upload_r2(dump_path):
    endpoint, bucket, prefix, env = r2_settings()
    size = dump_path.stat().st_size
    # Single conditional PutObject; V1 deliberately rejects files needing multipart.
    if size == 0 or size > 5 * 1024 ** 3:
        raise ValueError('Backup must be nonempty and at most 5 GiB')
    with dump_path.open('rb') as stream:
        if stream.read(5) != b'PGDMP':
            raise ValueError('Backup is not a PostgreSQL custom-format archive')
        stream.seek(0)
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    key = prefix + '/' + dump_path.name
    base = ['aws', '--endpoint-url', endpoint, '--region', 'auto', 's3api']
    upload = subprocess.run(base + ['put-object', '--bucket', bucket, '--key', key,
                                    '--body', str(dump_path), '--if-none-match', '*',
                                    '--metadata', 'sha256=' + digest, '--output', 'json'],
                            env=env, capture_output=True, timeout=300)
    if upload.returncode:
        raise RuntimeError('R2 backup upload failed')
    check = subprocess.run(base + ['head-object', '--bucket', bucket, '--key', key,
                                   '--output', 'json'],
                           env=env, capture_output=True, timeout=60)
    if check.returncode:
        raise RuntimeError('R2 upload verification failed')
    metadata = json.loads(check.stdout)
    if metadata.get('ContentLength') != size or metadata.get('Metadata', {}).get('sha256') != digest:
        raise RuntimeError('R2 backup metadata mismatch')
    # Metadata verification is not a replacement for a separate download and restore.
    print('PostgreSQL backup uploaded and metadata verified:', dump_path.name)
    print('Backup SHA256:', digest)
    return key, digest


def backup():
    env = database_env(os.environ['BACKUP_DATABASE_URL'])
    r2_settings()  # Fail before reading production if destination is not configured.
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    with tempfile.TemporaryDirectory(prefix='wasla-backup-') as folder:
        dump_path = Path(folder) / ('wasla-' + stamp + '-' + uuid4().hex + '.dump')
        # A private, non-shared runner must be used; the temporary file holds raw DB data.
        fd = os.open(dump_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            result = subprocess.run(['pg_dump', '--format=custom', '--no-owner', '--no-acl'],
                                    env=env, stdout=output, stderr=subprocess.PIPE, timeout=600)
        if result.returncode:
            raise RuntimeError('pg_dump failed; no backup uploaded')
        key, digest = upload_r2(dump_path)
        output_path = os.environ.get('GITHUB_OUTPUT')
        if output_path:
            # Only public object locator and digest go to job outputs, never DB contents.
            with open(output_path, 'a', encoding='utf-8') as output:
                output.write('object_key=' + key + '\n')
                output.write('sha256=' + digest + '\n')


if __name__ == '__main__':
    try:
        backup()
    except Exception:
        raise SystemExit('Backup failed. Check required settings and provider access; no success recorded.')
