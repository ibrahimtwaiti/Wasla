import os
import unittest
from unittest.mock import patch, MagicMock
from scripts.backup_postgres import backup, database_env

class BackupSafety(unittest.TestCase):
    def test_credentials_are_environment_only_and_tls_required(self):
        env = database_env('postgresql://operator:private%21@db.example.test/wasla?sslmode=require')
        self.assertEqual(env['PGPASSWORD'], 'private!')
        self.assertEqual(env['PGDATABASE'], 'wasla')
        for url in ('postgresql://u:p@db-pooler.example.test/wasla',
                    'postgresql://u:p@db.example.test/wasla?sslmode=disable'):
            with self.assertRaises(ValueError): database_env(url)

    def test_failed_dump_never_uploads_partial_backup(self):
        values={'BACKUP_DATABASE_URL':'postgresql://u:p@db.example.test/wasla',
                'BACKUP_AGE_RECIPIENT':'age1'+'a'*58,
                'BACKUP_GCS_PREFIX':'gs://owned-bucket/wasla'}
        process=MagicMock();process.wait.return_value=1;process.poll.return_value=1
        with patch.dict(os.environ,values), patch('subprocess.Popen',return_value=process), patch('subprocess.run') as run:
            run.return_value.returncode=0
            with self.assertRaises(RuntimeError): backup()
            self.assertEqual(run.call_count,1)
            self.assertEqual(run.call_args.args[0][0],'age')

    def test_failed_encryption_never_uploads(self):
        values={'BACKUP_DATABASE_URL':'postgresql://u:p@db.example.test/wasla',
                'BACKUP_AGE_RECIPIENT':'age1'+'a'*58,
                'BACKUP_GCS_PREFIX':'gs://owned-bucket/wasla'}
        process=MagicMock();process.wait.return_value=0;process.poll.return_value=0
        with patch.dict(os.environ,values), patch('subprocess.Popen',return_value=process), patch('subprocess.run') as run:
            run.return_value.returncode=1
            with self.assertRaises(RuntimeError): backup()
            self.assertEqual(run.call_count,1)

    def test_legacy_s3_destination_is_rejected_before_dump(self):
        values={'BACKUP_DATABASE_URL':'postgresql://u:p@db.example.test/wasla',
                'BACKUP_AGE_RECIPIENT':'age1'+'a'*58,
                'BACKUP_GCS_PREFIX':'s3://owned-bucket/wasla'}
        with patch.dict(os.environ, values), patch('subprocess.Popen') as spawn:
            with self.assertRaises(ValueError): backup()
            spawn.assert_not_called()

    def exercise_upload(self, upload_code):
        from pathlib import Path
        from types import SimpleNamespace
        values={'BACKUP_DATABASE_URL':'postgresql://u:p@db.example.test/wasla',
                'BACKUP_AGE_RECIPIENT':'age1'+'a'*58,
                'BACKUP_GCS_PREFIX':'gs://owned-bucket/wasla'}
        process=MagicMock();process.wait.return_value=0;process.poll.return_value=0
        def run_command(args, **kwargs):
            if args[0] == 'age':
                Path(args[args.index('-o')+1]).write_bytes(b'encrypted-test-content')
                return SimpleNamespace(returncode=0)
            self.assertEqual(args[:3], ['gcloud', 'storage', 'cp'])
            self.assertTrue(args[4].startswith('gs://owned-bucket/wasla/'))
            self.assertTrue(args[4].endswith('.dump.age'))
            self.assertIn('--if-generation-match=0', args)
            self.assertTrue(any(x.startswith('--custom-metadata=sha256=') for x in args))
            self.assertNotIn('postgresql://', ' '.join(args))
            return SimpleNamespace(returncode=upload_code)
        with patch.dict(os.environ, values), patch('subprocess.Popen',return_value=process), patch('subprocess.run',side_effect=run_command), patch('builtins.print') as output:
            if upload_code:
                with self.assertRaises(RuntimeError): backup()
                output.assert_not_called()
            else:
                backup()
                self.assertEqual(output.call_count,2)

    def test_encrypted_upload_to_gcs(self):
        self.exercise_upload(0)

    def test_gcs_failure_never_reports_success(self):
        self.exercise_upload(1)
