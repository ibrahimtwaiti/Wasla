"""Encrypt a consistent PostgreSQL dump before sending it to owner-configured Google Cloud Storage.
Credentials come only from environment. Private age identity stays with the operator.
No plaintext backup is uploaded, and a failed dump/encryption never reaches GCS.
"""
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


def backup():
    env = database_env(os.environ['BACKUP_DATABASE_URL'])
    recipient = os.environ['BACKUP_AGE_RECIPIENT']
    destination = os.environ['BACKUP_GCS_PREFIX'].rstrip('/')
    if not re.fullmatch(r'age1[0-9a-z]{58}', recipient):
        raise ValueError('An age public recipient is required')
    if not re.fullmatch(r'gs://[a-z0-9][a-z0-9.-]+/[A-Za-z0-9/_-]+', destination):
        raise ValueError('Use a private GCS bucket and a dedicated prefix')
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
        target = destination + '/' + encrypted.name
        with encrypted.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        # WIF credentials are supplied by google-github-actions/auth, never arguments.
        # Generation zero prevents overwriting an existing live object.
        result = subprocess.run(['gcloud', 'storage', 'cp', str(encrypted), target,
                                 '--if-generation-match=0',
                                 '--custom-metadata=sha256=' + digest,
                                 '--quiet'], capture_output=True, timeout=300)
        if result.returncode:
            raise RuntimeError('Encrypted backup upload failed')
        print('Encrypted backup uploaded:', encrypted.name)
        print('Ciphertext SHA256:', digest)



if __name__ == '__main__':
    try:
        backup()
    except Exception:
        raise SystemExit('Backup failed. Check required settings and provider access; no success recorded.')
