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
                'BACKUP_S3_PREFIX':'s3://owned-bucket/wasla'}
        process=MagicMock();process.wait.return_value=1;process.poll.return_value=1
        with patch.dict(os.environ,values), patch('subprocess.Popen',return_value=process), patch('subprocess.run') as run:
            run.return_value.returncode=0
            with self.assertRaises(RuntimeError): backup()
            self.assertEqual(run.call_count,1)
            self.assertEqual(run.call_args.args[0][0],'age')

    def test_failed_encryption_never_uploads(self):
        values={'BACKUP_DATABASE_URL':'postgresql://u:p@db.example.test/wasla',
                'BACKUP_AGE_RECIPIENT':'age1'+'a'*58,
                'BACKUP_S3_PREFIX':'s3://owned-bucket/wasla'}
        process=MagicMock();process.wait.return_value=0;process.poll.return_value=0
        with patch.dict(os.environ,values), patch('subprocess.Popen',return_value=process), patch('subprocess.run') as run:
            run.return_value.returncode=1
            with self.assertRaises(RuntimeError): backup()
            self.assertEqual(run.call_count,1)
