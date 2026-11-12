import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from scripts.backup_postgres import backup, database_env, r2_settings, upload_r2

SETTINGS = {
    'BACKUP_DATABASE_URL': 'postgresql://u:p@db.example.test/wasla',
    'BACKUP_AGE_RECIPIENT': 'age1' + 'a' * 58,
    'R2_ENDPOINT_URL': 'https://' + 'a' * 32 + '.r2.cloudflarestorage.com',
    'R2_BUCKET': 'wasla-backups', 'R2_PREFIX': 'production/postgres',
    'R2_ACCESS_KEY_ID': 'test-id', 'R2_SECRET_ACCESS_KEY': 'test-secret',
}

class BackupSafety(unittest.TestCase):
    def test_credentials_are_environment_only_and_tls_required(self):
        env = database_env('postgresql://operator:private%21@db.example.test/wasla?sslmode=require')
        self.assertEqual(env['PGPASSWORD'], 'private!')
        self.assertEqual(env['PGDATABASE'], 'wasla')
        for url in ('postgresql://u:p@db-pooler.example.test/wasla',
                    'postgresql://u:p@db.example.test/wasla?sslmode=disable'):
            with self.assertRaises(ValueError): database_env(url)

    def failed_stage(self, dump_code, age_code):
        process=MagicMock();process.wait.return_value=dump_code;process.poll.return_value=dump_code
        with patch.dict(os.environ,SETTINGS), patch('subprocess.Popen',return_value=process), patch('subprocess.run') as run:
            run.return_value.returncode=age_code
            with self.assertRaises(RuntimeError): backup()
            self.assertEqual(run.call_count,1)
            self.assertEqual(run.call_args.args[0][0],'age')

    def test_failed_dump_never_uploads_partial_backup(self):
        self.failed_stage(1, 0)

    def test_failed_encryption_never_uploads(self):
        self.failed_stage(0, 1)

    def test_wrong_endpoints_rejected_before_dump(self):
        for endpoint in ('https://storage.googleapis.com', 'https://s3.amazonaws.com',
                         'http://'+'a'*32+'.r2.cloudflarestorage.com',
                         SETTINGS['R2_ENDPOINT_URL']+'.evil.test'):
            with patch.dict(os.environ, {**SETTINGS,'R2_ENDPOINT_URL':endpoint}), patch('subprocess.Popen') as spawn:
                with self.assertRaises(ValueError): backup()
                spawn.assert_not_called()

    def test_missing_credentials_rejected(self):
        with patch.dict(os.environ,{**SETTINGS,'R2_SECRET_ACCESS_KEY':''}):
            with self.assertRaises(ValueError): r2_settings()

    def test_foreign_aws_environment_is_not_inherited(self):
        with patch.dict(os.environ,{**SETTINGS,'AWS_SESSION_TOKEN':'old-token','AWS_PROFILE':'other'}):
            env=r2_settings()[3]
            self.assertNotIn('AWS_SESSION_TOKEN',env)
            self.assertNotIn('AWS_PROFILE',env)
            self.assertNotIn('BACKUP_DATABASE_URL',env)
            self.assertEqual(env['AWS_ACCESS_KEY_ID'],'test-id')

    def exercise_upload(self, upload_code=0, head_code=0, mismatch=False):
        content=b'encrypted-test-content'
        process=MagicMock();process.wait.return_value=0;process.poll.return_value=0
        def run_command(args, **kwargs):
            if args[0] == 'age':
                Path(args[args.index('-o')+1]).write_bytes(content)
                return SimpleNamespace(returncode=0)
            self.assertEqual(args[:3],['aws','--endpoint-url',SETTINGS['R2_ENDPOINT_URL']])
            self.assertEqual(kwargs['env']['AWS_DEFAULT_REGION'],'auto')
            self.assertNotIn('test-secret',' '.join(args))
            if 'put-object' in args:
                self.assertEqual(args[args.index('--if-none-match')+1],'*')
                self.assertTrue(args[args.index('--key')+1].endswith('.dump.age'))
                self.assertNotIn('--server-side-encryption',args)
                return SimpleNamespace(returncode=upload_code)
            self.assertIn('head-object',args)
            meta={'ContentLength':len(content)+(1 if mismatch else 0),
                  'Metadata':{'sha256':hashlib.sha256(content).hexdigest()}}
            return SimpleNamespace(returncode=head_code,stdout=json.dumps(meta))
        with patch.dict(os.environ,SETTINGS), patch('subprocess.Popen',return_value=process), patch('subprocess.run',side_effect=run_command) as run, patch('builtins.print') as output:
            if upload_code or head_code or mismatch:
                with self.assertRaises(RuntimeError): backup()
                output.assert_not_called()
                if upload_code: self.assertEqual(run.call_count,2)
            else:
                backup()
                self.assertEqual(output.call_count,2)
                self.assertEqual(run.call_count,3)

    def test_encrypted_upload_to_r2(self): self.exercise_upload()
    def test_r2_upload_failure_never_reports_success(self): self.exercise_upload(upload_code=1)
    def test_r2_head_failure_never_reports_success(self): self.exercise_upload(head_code=1)
    def test_r2_metadata_mismatch_never_reports_success(self): self.exercise_upload(mismatch=True)

    def test_empty_or_oversized_backup_never_uploads(self):
        with patch.dict(os.environ,SETTINGS), tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'empty.dump.age';path.touch()
            for size in (0,5*1024**3+1):
                with patch.object(Path,'stat',return_value=SimpleNamespace(st_size=size)), patch('subprocess.run') as run:
                    with self.assertRaises(ValueError): upload_r2(path)
                    run.assert_not_called()
