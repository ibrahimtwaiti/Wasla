"""Integration checks against a disposable PostgreSQL CI instance only."""
import os
import json
import unittest
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from wasla.db import initialize, now, uid, transaction, postgres
from wasla.main import app
from wasla.security import hash_password
from wasla.worker import process_one

TEST_URL=os.getenv('WASLA_TEST_DATABASE_URL')

@unittest.skipUnless(TEST_URL,'Needs disposable PostgreSQL WASLA_TEST_DATABASE_URL')
class PostgreSQLAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old=dict(os.environ)
        os.environ.update(DATABASE_URL=TEST_URL,WASLA_ENV='development',
                          WASLA_ORIGIN='http://localhost:8000')
        initialize()
        cls.a=uid(); cls.b=uid()
        cls.email_a='a-'+uid()+'@example.test'
        cls.email_b='b-'+uid()+'@example.test'
        cls.password='test-postgresql-password-only'
        hashed=hash_password(cls.password)
        with transaction() as db:
            for ident in (cls.a,cls.b):
                db.execute('INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)',
                           (ident,'company-'+ident,now()))
            for ident,address in ((cls.a,cls.email_a),(cls.b,cls.email_b)):
                db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',
                           (uid(),ident,address,'Admin',hashed,'admin',now()))
    @classmethod
    def tearDownClass(cls):
        os.environ.clear(); os.environ.update(cls.old)
    def test_connect_migration_and_legacy_index_access(self):
        self.assertTrue(postgres())
        with transaction() as db:
            self.assertEqual(db.execute('SELECT version FROM schema_migrations WHERE version=?',(1,)).fetchone()[0],1)
            self.assertIsNotNone(db.execute("SELECT 1 FROM information_schema.columns WHERE table_name='webhook_events' AND column_name='next_attempt_at'").fetchone())
    def test_tenant_cross_reads_writes_and_duplicate_phone(self):
        with TestClient(app) as a,TestClient(app) as b:
            for c in (a,b): c.headers.update({'Origin':'http://localhost:8000'})
            for client,address in ((a,self.email_a),(b,self.email_b)):
                response=client.post('/api/auth/login',json={'email':address,'password':self.password})
                self.assertEqual(response.status_code,200,response.text)
                client.headers['X-CSRF-Token']=client.get('/api/me').json()['csrf']
            number='+966500012345'
            first=a.post('/api/contacts',json={'name':'Tenant A','phone':number,'tags':'pg'})
            self.assertEqual(first.status_code,201,first.text); contact=first.json()['id']
            self.assertEqual(b.get('/api/contacts').json(),[])
            self.assertEqual(b.patch('/api/contacts/'+contact+'/consent',json={'marketing_consent':False}).status_code,404)
            other=b.post('/api/contacts',json={'name':'Tenant B','phone':number,'tags':'pg'})
            self.assertEqual(other.status_code,201,other.text)
            self.assertNotEqual(contact,other.json()['id'])
            self.assertEqual(b.post('/api/opportunities',json={'title':'forbidden','contact_id':contact,'currency':'SAR'}).status_code,404)
    def test_worker_concurrency_idempotent(self):
        channel_id=uid(); phone_id=str(int(uuid4().hex[:10],16))
        event_ids=[]
        with transaction() as db:
            db.execute('INSERT INTO channels(id,tenant_id,phone_id,token_env) VALUES(?,?,?,?)',
                       (channel_id,self.a,phone_id,''))
            for i in range(2):
                payload={'entry':[{'changes':[{'value':{'metadata':{'phone_number_id':phone_id},
                         'contacts':[],'messages':[{'id':'wamid.pg.'+uid(),
                         'from':'966500022222','timestamp':'1700000000','type':'text',
                         'text':{'body':'hello '+str(i)}}]}}]}]}
                ident=uid(); event_ids.append(ident)
                db.execute('INSERT INTO webhook_events(id,payload,created_at) VALUES(?,?,?)',
                           (ident,json.dumps(payload),now()))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _: process_one(),range(2)))
        self.assertEqual(results,[True,True])
        with transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM messages WHERE tenant_id=?',(self.a,)).fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM messages WHERE tenant_id=?',(self.b,)).fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM webhook_events WHERE status='processed'").fetchone()[0],2)
    def test_job_auth_without_secret(self):
        with TestClient(app) as c:
            self.assertEqual(c.post('/api/internal/drain-webhooks').status_code,401)
    def test_first_provider_concurrent_activation(self):
        from unittest.mock import patch
        from time import time
        with patch.dict(os.environ,{'WASLA_SETUP_TOKEN':'p'*48,'WASLA_SETUP_EXPIRES':str(int(time())+600)}):
            data={'token':'p'*48,'name':'PG Owner','email':'pg-owner@example.test','password':self.password,'legal_entity':'PG Test','contact_email':'public@example.test'}
            def activate(_):
                with TestClient(app) as client:
                    return client.post('/api/auth/setup',json=data,headers={'Origin':'http://localhost:8000'}).status_code
            with ThreadPoolExecutor(2) as pool: codes=list(pool.map(activate,range(2)))
            self.assertEqual(sorted(codes),[201,409])
