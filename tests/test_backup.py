import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts.backup_postgres import backup, database_env, r2_settings, upload_r2, failure_classification

SETTINGS = {
    'BACKUP_DATABASE_URL': 'postgresql://u:p@db.example.test/wasla?sslmode=require&channel_binding=require',
    'R2_ENDPOINT_URL': 'https://' + 'a' * 32 + '.r2.cloudflarestorage.com',
    'R2_BUCKET': 'wasla-backups',
    'R2_PREFIX': 'production/postgres',
    'R2_ACCESS_KEY_ID': 'test-id',
    'R2_SECRET_ACCESS_KEY': 'test-secret',
}


class BackupSafety(unittest.TestCase):
    def test_safe_failure_classification_cannot_echo_credentials(self):
        error = b'pg_dump: error: password authentication failed for user private-secret'
        self.assertEqual(failure_classification(error, 'db'), 'DB_AUTH')
        self.assertEqual(failure_classification(b'connection timed out', 'db'), 'DB_NETWORK_OR_TIMEOUT')
        self.assertEqual(failure_classification(b'AccessDenied: private-token', 'r2'), 'R2_AUTH')
        self.assertEqual(failure_classification(b'unrecognized private error', 'r2'), 'R2_OTHER')

    def test_credentials_are_environment_only_and_tls_required(self):
        env = database_env('postgresql://operator:private%21@db.example.test/wasla?sslmode=require&channel_binding=require')
        self.assertEqual(env['PGPASSWORD'], 'private!')
        self.assertEqual(env['PGDATABASE'], 'wasla')
        self.assertEqual(env['PGCHANNELBINDING'], 'require')
        for url in (
            'postgresql://u:p@db-pooler.example.test/wasla',
            'postgresql://u:p@db.example.test/wasla?sslmode=disable',
            'postgresql://u@db.example.test/wasla?sslmode=require',
        ):
            with self.assertRaises(ValueError):
                database_env(url)

    def test_failed_pg_dump_never_uploads(self):
        def fail_dump(args, **kwargs):
            self.assertEqual(args[0], 'pg_dump')
            kwargs['stdout'].write(b'PGDMPpartial')
            return SimpleNamespace(returncode=1)

        with patch.dict(os.environ, SETTINGS), patch('subprocess.run', side_effect=fail_dump) as run:
            with self.assertRaises(RuntimeError):
                backup()
            self.assertEqual(run.call_count, 1)

    def test_bad_dump_header_never_uploads(self):
        def bad_dump(args, **kwargs):
            self.assertEqual(args[0], 'pg_dump')
            kwargs['stdout'].write(b'not-a-valid-dump')
            return SimpleNamespace(returncode=0)

        with patch.dict(os.environ, SETTINGS), patch('subprocess.run', side_effect=bad_dump) as run:
            with self.assertRaises(ValueError):
                backup()
            self.assertEqual(run.call_count, 1)

    def test_wrong_endpoints_rejected_before_dump(self):
        for endpoint in (
            'https://storage.googleapis.com', 'https://s3.amazonaws.com',
            'http://' + 'a' * 32 + '.r2.cloudflarestorage.com',
            SETTINGS['R2_ENDPOINT_URL'] + '.evil.test',
        ):
            with patch.dict(os.environ, {**SETTINGS, 'R2_ENDPOINT_URL': endpoint}), patch('subprocess.run') as run:
                with self.assertRaises(ValueError):
                    backup()
                run.assert_not_called()

    def test_missing_r2_credentials_rejected(self):
        with patch.dict(os.environ, {**SETTINGS, 'R2_SECRET_ACCESS_KEY': ''}):
            with self.assertRaises(ValueError):
                r2_settings()

    def test_foreign_aws_environment_is_not_inherited(self):
        with patch.dict(os.environ, {**SETTINGS, 'AWS_SESSION_TOKEN': 'old-token', 'AWS_PROFILE': 'other'}):
            env = r2_settings()[3]
            self.assertNotIn('AWS_SESSION_TOKEN', env)
            self.assertNotIn('AWS_PROFILE', env)
            self.assertNotIn('BACKUP_DATABASE_URL', env)
            self.assertEqual(env['AWS_ACCESS_KEY_ID'], 'test-id')

    def exercise_upload(self, dump_code=0, upload_code=0, head_code=0, mismatch=False):
        content = b'PGDMP' + b'test-archive-contents'
        def run_command(args, **kwargs):
            if args[0] == 'pg_dump':
                self.assertEqual(args[1:], ['--format=custom', '--no-owner', '--no-acl'])
                self.assertEqual(stat.S_IMODE(os.fstat(kwargs['stdout'].fileno()).st_mode), 0o600)
                kwargs['stdout'].write(content)
                return SimpleNamespace(returncode=dump_code)
            self.assertEqual(args[:3], ['aws', '--endpoint-url', SETTINGS['R2_ENDPOINT_URL']])
            self.assertEqual(kwargs['env']['AWS_DEFAULT_REGION'], 'auto')
            self.assertNotIn('test-secret', ' '.join(args))
            if 'put-object' in args:
                self.assertEqual(args[args.index('--if-none-match') + 1], '*')
                self.assertTrue(args[args.index('--key') + 1].endswith('.dump'))
                self.assertFalse(args[args.index('--key') + 1].endswith('.dump.age'))
                self.assertNotIn('--server-side-encryption', args)
                self.assertEqual(args[args.index('--metadata') + 1], 'sha256=' + hashlib.sha256(content).hexdigest())
                return SimpleNamespace(returncode=upload_code)
            self.assertIn('head-object', args)
            meta = {
                'ContentLength': len(content) + (1 if mismatch else 0),
                'Metadata': {'sha256': hashlib.sha256(content).hexdigest()}
            }
            return SimpleNamespace(returncode=head_code, stdout=json.dumps(meta))

        with patch.dict(os.environ, SETTINGS), patch('subprocess.run', side_effect=run_command) as run, patch('builtins.print') as output:
            if dump_code or upload_code or head_code or mismatch:
                with self.assertRaises(RuntimeError):
                    backup()
                self.assertFalse(any('R2_UPLOAD_METADATA_OK' in str(c) for c in output.call_args_list))
                if dump_code:
                    self.assertEqual(run.call_count, 1)
                elif upload_code:
                    self.assertEqual(run.call_count, 2)
            else:
                backup()
                self.assertEqual(output.call_count, 5)
                self.assertEqual(run.call_count, 3)

    def test_private_dump_upload_to_r2(self):
        self.exercise_upload()

    def test_r2_upload_failure_never_reports_success(self):
        self.exercise_upload(upload_code=1)

    def test_r2_head_failure_never_reports_success(self):
        self.exercise_upload(head_code=1)

    def test_r2_metadata_mismatch_never_reports_success(self):
        self.exercise_upload(mismatch=True)

    def test_empty_or_oversized_backup_never_uploads(self):
        with patch.dict(os.environ, SETTINGS), tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'empty.dump'
            path.touch()
            for size in (0, 5 * 1024 ** 3 + 1):
                with patch.object(Path, 'stat', return_value=SimpleNamespace(st_size=size)), patch('subprocess.run') as run:
                    with self.assertRaises(ValueError):
                        upload_r2(path)
                    run.assert_not_called()
