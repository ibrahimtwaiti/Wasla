"""Encrypt a consistent PostgreSQL dump before sending it to owner-configured Cloudflare R2.
Credentials come only from environment. Private age identity stays with the operator.
No plaintext backup is uploaded, and a failed dump/encryption never reaches R2.
"""
import json
import hashlib
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit, unquote, parse_qs


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
    if not env['PGDATABASE'] or not env['PGUSER']:
        raise ValueError('Database and user are required')
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
    # Do not inherit credentials, profiles, endpoints or session tokens for other providers.
    env = {k: v for k, v in os.environ.items() if not k.startswith(('AWS_', 'R2_', 'BACKUP_', 'PG'))}
    env.update(AWS_ACCESS_KEY_ID=access_key, AWS_SECRET_ACCESS_KEY=secret_key,
               AWS_DEFAULT_REGION='auto', AWS_EC2_METADATA_DISABLED='true',
               AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull,
               AWS_REQUEST_CHECKSUM_CALCULATION='when_required',
               AWS_RESPONSE_CHECKSUM_VALIDATION='when_required', AWS_PAGER='')
    return endpoint, bucket, prefix, env


def upload_r2(encrypted):
    endpoint, bucket, prefix, env = r2_settings()
    size = encrypted.stat().st_size
    # This V1 path deliberately uses one conditional PutObject (no multipart overwrite race).
    if size == 0 or size > 5 * 1024 ** 3:
        raise ValueError('Encrypted backup must be nonempty and at most 5 GiB')
    with encrypted.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    key = prefix + '/' + encrypted.name
    base = ['aws', '--endpoint-url', endpoint, '--region', 'auto', 's3api']
    upload = subprocess.run(base + ['put-object', '--bucket', bucket, '--key', key,
                                    '--body', str(encrypted), '--if-none-match', '*',
                                    '--metadata', 'sha256=' + digest, '--output', 'json'],
                            env=env, capture_output=True, timeout=300)
    if upload.returncode:
        raise RuntimeError('Encrypted R2 backup upload failed')
    check = subprocess.run(base + ['head-object', '--bucket', bucket, '--key', key,
                                   '--output', 'json'],
                           env=env, capture_output=True, timeout=60)
    if check.returncode:
        raise RuntimeError('R2 upload verification failed')
    metadata = json.loads(check.stdout)
    if metadata.get('ContentLength') != size or metadata.get('Metadata', {}).get('sha256') != digest:
        raise RuntimeError('R2 backup metadata mismatch')
    # This is a metadata check, not a replacement for downloading and restoring the object.
    print('Encrypted backup uploaded and metadata verified:', encrypted.name)
    print('Ciphertext SHA256:', digest)


def backup():
    env = database_env(os.environ['BACKUP_DATABASE_URL'])
    recipient = os.environ['BACKUP_AGE_RECIPIENT']
    r2_settings()  # Fail before reading the database if storage is not configured.
    if not re.fullmatch(r'age1[0-9a-z]{58}', recipient):
        raise ValueError('An age public recipient is required')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    with tempfile.TemporaryDirectory(prefix='wasla-backup-') as folder:
        encrypted = Path(folder) / ('wasla-' + stamp + '-' + uuid4().hex + '.dump.age')
        # Stream the dump directly to age; plaintext never lands on disk.
        with tempfile.TemporaryFile() as errors:
            dump = subprocess.Popen(['pg_dump', '--format=custom', '--no-owner', '--no-acl'],
                                    env=env, stdout=subprocess.PIPE, stderr=errors)
            try:
                result = subprocess.run(['age', '-r', recipient, '-o', str(encrypted)],
                                        stdin=dump.stdout, stderr=errors, timeout=600)
                dump.stdout.close()
                code = dump.wait(timeout=600)
                if code or result.returncode:
                    raise RuntimeError('Dump or encryption failed; nothing uploaded')
            finally:
                if dump.poll() is None:
                    dump.kill()
                    dump.wait()
        upload_r2(encrypted)



if __name__ == '__main__':
    try:
        backup()
    except Exception:
        raise SystemExit('Backup failed. Check required settings and provider access; no success recorded.')
