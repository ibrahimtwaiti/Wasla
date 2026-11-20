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
            db.execute("INSERT INTO channels(id,tenant_id,phone_id,token_env,status) VALUES(?,?,?,?,'verified')",
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

    def test_v2_durable_inbound_and_outbox_concurrency(self):
        from wasla import service
        from wasla.db import one
        from unittest.mock import patch
        import httpx,time
        aid=uid();asset=uid()
        with transaction() as db:
            db.execute('INSERT INTO service_accounts(id,tenant_id,channel,provider_id,label,api_version,state,created_at) VALUES(?,?,?,?,?,?,?,?)',(aid,self.a,'whatsapp',asset,'PG test','v27.0','verified',now()))
        payload={'object':'whatsapp_business_account','entry':[{'changes':[{'value':{'metadata':{'phone_number_id':asset},'messages':[{'id':'pg-v2-'+uid(),'from':'966500000001','timestamp':str(int(time.time())),'type':'text','text':{'body':'test'}}]}}]}]}
        service.persist_events('whatsapp',payload);service.persist_events('whatsapp',payload)
        with ThreadPoolExecutor(2) as pool:result=list(pool.map(lambda _:service.process_event(),range(2)))
        self.assertEqual(sorted(result),[False,True])
        with transaction() as db:
            ticket=one(db,'SELECT * FROM service_tickets WHERE tenant_id=?',(self.a,));self.assertIsNotNone(ticket)
        with TestClient(app) as client:
            client.headers['Origin']='http://localhost:8000'
            client.post('/api/auth/login',json={'email':self.email_a,'password':self.password});client.headers['X-CSRF-Token']=client.get('/api/me').json()['csrf']
            with patch.dict(os.environ,{'WASLA_CHANNEL_'+aid.upper()+'_TOKEN':'test-only'}):
                queued=client.post('/api/v2/tickets/'+ticket['id']+'/messages',json={'direction':'out','body':'test reply','version':ticket['version']},headers={'Idempotency-Key':uid()});self.assertEqual(queued.status_code,201,queued.text)
                with patch('wasla.service.provider_request',return_value=httpx.Response(200,json={'messages':[{'id':'pg-out-'+uid()}]})) as provider:
                    with ThreadPoolExecutor(2) as pool:list(pool.map(lambda _:service.send_outbox(),range(2)))
                    provider.assert_called_once()
        # Media claims use SKIP LOCKED and one clean object per message.
        from wasla.service_media import process_media
        from unittest.mock import MagicMock
        with transaction() as db:
            message=one(db,"SELECT id FROM service_messages WHERE ticket_id=? AND direction='in'",(ticket['id'],))
            db.execute('INSERT INTO service_media_jobs(id,tenant_id,message_id,account_id,descriptor) VALUES(?,?,?,?,?)',(uid(),self.a,message['id'],aid,'{"id":"777"}'))
        storage=MagicMock()
        with patch.dict(os.environ,{'WASLA_MEDIA_BUCKET':'test'}),patch('wasla.service_media.client',return_value=storage),patch('wasla.service_media.fetch_provider_media',return_value=(b'fixture','image/png')),patch('wasla.service_media.scan',return_value='testhash'):
            with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:process_media(),range(2)))
            self.assertEqual(sorted(results),[False,True]);storage.put_object.assert_called_once()
        with transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM service_media WHERE ticket_id=?',(ticket['id'],)).fetchone()[0],1)
            db.execute('INSERT INTO service_retention_policy VALUES(?,?,0,?,?)',(self.a,180,'CI disabled policy',now()))
            db.execute('INSERT INTO service_channel_snapshots VALUES(?,?,?,?,?)',(aid,self.a,'{}','{}',now()))

    def test_saas_quota_concurrent_and_grant_tenant_fk(self):
        from wasla.saas import consume_message
        from fastapi import HTTPException
        from wasla.db import one
        import psycopg
        plan=uid();aid=uid()
        with transaction() as db:
            db.execute('INSERT INTO service_plans VALUES(?,?,?,?,?,1,?)',(plan,'PG sandbox','SAR',0,1,now()))
            db.execute('INSERT INTO service_subscriptions VALUES(?,?,?,?,?)',(self.b,plan,json.dumps({'message_limit':1}),'trial',now()))
            db.execute('INSERT INTO service_accounts(id,tenant_id,channel,provider_id,label,api_version,created_at) VALUES(?,?,?,?,?,?,?)',(aid,self.a,'whatsapp',uid(),'PG lifecycle','v26.0',now()))
        def reserve(_):
            try:
                with transaction() as db:consume_message(db,self.b)
                return 200
            except HTTPException as error:return error.status_code
        with ThreadPoolExecutor(2) as pool:self.assertEqual(sorted(pool.map(reserve,range(2))),[200,429])
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with transaction() as db:db.execute('INSERT INTO meta_grants VALUES(?,?,?,?,NULL,?)',(aid,self.b,'123','hash','unchecked'))

    def test_rate_limit_is_atomic_under_concurrency(self):
        from wasla.security import limit
        from fastapi import HTTPException
        key='pg-rate-'+uid()
        def attempt(_):
            try:
                with transaction() as db:limit(db,key,5,600)
                return 200
            except HTTPException as error:return error.status_code
        with ThreadPoolExecutor(8) as pool:results=list(pool.map(attempt,range(20)))
        self.assertEqual(results.count(200),5);self.assertEqual(results.count(429),15)
        with transaction() as db:self.assertEqual(db.execute('SELECT count FROM rate_limits WHERE key=?',(key,)).fetchone()[0],5)

@unittest.skipUnless(TEST_URL,'Needs disposable PostgreSQL WASLA_TEST_DATABASE_URL')
class PostgreSQLCompletion(unittest.TestCase):
    def setUp(self):
        from cryptography.fernet import Fernet
        self.old=dict(os.environ);os.environ.update(DATABASE_URL=TEST_URL,WASLA_ENV='development',WASLA_ORIGIN='http://localhost:8000',WASLA_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(),WASLA_EMAIL_MODE='local')
        initialize();self.tid=uid();self.user=uid()
        with transaction() as db:
            db.execute('INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)',(self.tid,'Completion PG',now()))
            db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',(self.user,self.tid,uid()+'@example.test','Admin',hash_password('postgres-completion-password'),'admin',now()))
    def tearDown(self):os.environ.clear();os.environ.update(self.old)
    def test_migration_is_repeatable_and_fk_rejects_cross_tenant_records(self):
        import psycopg
        initialize()
        with transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM schema_migrations WHERE version=10').fetchone()[0],1)
            aid=uid();db.execute('INSERT INTO meta_data_connections(id,tenant_id,kind,provider_id,label,api_version,ciphertext,app_id,subject_hash,scopes) VALUES(?,?,?,?,?,?,?,?,?,?)',(aid,self.tid,'catalogs',uid(),'Catalog','v26.0','encrypted','123','subject','[]'))
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with transaction() as db:db.execute('INSERT INTO meta_data_records VALUES(?,?,?,?,?,?)',(aid,uid(),'1','products','encrypted',1))
    def test_concurrent_seat_and_api_reservations(self):
        from fastapi import HTTPException
        from wasla.entitlements import capacity,consume_api
        with transaction() as db:
            plan=uid();db.execute('INSERT INTO service_plans VALUES(?,?,?,?,?,1,?)',(plan,'Concurrent','SAR',0,10,now()))
            db.execute('INSERT INTO service_subscriptions VALUES(?,?,?,?,?)',(self.tid,plan,json.dumps({'message_limit':10,'limits':{'seats':2,'api_requests':1}}),'trial',now()))
        def seat(_):
            try:
                with transaction() as db:
                    capacity(db,self.tid,'seat')
                    db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',(uid(),self.tid,uid()+'@example.test','Seat','not-login','reader',now()))
                return 200
            except HTTPException as e:return e.status_code
        with ThreadPoolExecutor(4) as pool:self.assertEqual(sorted(pool.map(seat,range(4))),[200,409,409,409])
        def reserve(_):
            try:
                with transaction() as db:consume_api(db,self.tid)
                return 200
            except HTTPException as e:return e.status_code
        with ThreadPoolExecutor(4) as pool:self.assertEqual(sorted(pool.map(reserve,range(4))),[200,429,429,429])
    def test_literal_percent_and_bound_parameters(self):
        with transaction() as db:
            row=db.execute("SELECT ? AS arg, 'stage%' AS pattern, 7 % 3 AS remainder",('50%',)).fetchone()
            self.assertEqual((row['arg'],row['pattern'],row['remainder']),('50%','stage%',1))
            self.assertEqual(db.execute("SELECT 'restore:%'").fetchone()[0],'restore:%')
        from scripts.deletion_ledger import export_ledger
        self.assertEqual(export_ledger()['format'],'wasla-erasure-ledger-v1')

    def test_email_verification_single_use_under_concurrency(self):
        from wasla.email_verification import enqueue
        from wasla.service_oauth import cipher
        from wasla.db import one
        with transaction() as db:
            db.execute('UPDATE users SET active=0 WHERE id=?',(self.user,))
            db.execute("INSERT INTO service_registrations VALUES(?,?,'pending','sandbox-v1','',?)",(self.tid,self.user,now()))
            enqueue(db,self.user)
            token=json.loads(cipher().decrypt(one(db,'SELECT ciphertext FROM email_jobs WHERE user_id=?',(self.user,))['ciphertext'].encode()))['url'].split('#')[1]
        def verify(_):
            with TestClient(app) as client:return client.post('/api/auth/verification/complete',json={'token':token},headers={'Origin':'http://localhost:8000'}).status_code
        with ThreadPoolExecutor(4) as pool:self.assertEqual(sorted(pool.map(verify,range(4))),[200,409,409,409])
    def test_lead_dedup_and_exclusive_worker(self):
        from wasla import meta_data
        from wasla.db import one
        from wasla.service_oauth import cipher
        from unittest.mock import patch
        import time
        aid=uid();pageid=str(int(uuid4().hex[:10],16));leadid='123456';credential=cipher().encrypt(b'pg-only-token').decode()
        with transaction() as db:db.execute('INSERT INTO meta_data_connections(id,tenant_id,kind,provider_id,label,api_version,ciphertext,app_id,subject_hash,scopes) VALUES(?,?,?,?,?,?,?,?,?,?)',(aid,self.tid,'leads',pageid,'Page','v26.0',credential,'123','subject','[]'))
        payload={'object':'page','entry':[{'id':pageid,'changes':[{'field':'leadgen','value':{'page_id':pageid,'leadgen_id':leadid}}]}]}
        with ThreadPoolExecutor(4) as pool:list(pool.map(lambda _:meta_data.ingest_leads(payload),range(4)))
        with transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM meta_lead_events WHERE connection_id=?',(aid,)).fetchone()[0],1)
            account=one(db,'SELECT * FROM meta_data_connections WHERE id=?',(aid,))
        def fetch(*args,**kwargs):time.sleep(.1);return {'id':leadid,'field_data':[]}
        with patch('wasla.meta_data.connection',return_value=(account,'pg-only-token')),patch('wasla.meta_data.graph',side_effect=fetch) as graph:
            with ThreadPoolExecutor(2) as pool:list(pool.map(lambda _:meta_data.process_lead(),range(2)))
            graph.assert_called_once()
        with transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM meta_data_records WHERE connection_id=?',(aid,)).fetchone()[0],1)
    def test_complete_erasure_with_all_completion_foreign_keys(self):
        from wasla import privacy,email_verification
        from wasla.db import one
        aid=uid()
        with transaction() as db:
            email_verification.enqueue(db,self.user)
            db.execute('INSERT INTO meta_data_connections(id,tenant_id,kind,provider_id,label,api_version,ciphertext,app_id,subject_hash,scopes) VALUES(?,?,?,?,?,?,?,?,?,?)',(aid,self.tid,'catalogs',uid(),'Catalog','v26.0','','123','subject','[]'))
            db.execute('INSERT INTO meta_data_records VALUES(?,?,?,?,?,?)',(aid,self.tid,'item','products','ciphertext',1))
            db.execute('INSERT INTO meta_lead_events(id,connection_id,tenant_id,lead_id) VALUES(?,?,?,?)',(uid(),aid,self.tid,'lead'))
            db.execute('INSERT INTO meta_data_sessions(state_hash,tenant_id,user_id,session_hash,kind,app_id,config_id,api_version,expires_at) VALUES(?,?,?,?,?,?,?,?,?)',(uid(),self.tid,self.user,'hash','catalogs','123','456','v26.0',1))
            job=privacy.queue(db,self.tid,self.user,'tenant')
        with ThreadPoolExecutor(2) as pool:list(pool.map(lambda _:privacy.process_deletion(),range(2)))
        with transaction() as db:
            self.assertIsNone(one(db,'SELECT id FROM tenants WHERE id=?',(self.tid,)))
            self.assertEqual(one(db,'SELECT state FROM deletion_jobs WHERE id=?',(job,))['state'],'completed')
            self.assertIsNone(one(db,'SELECT id FROM users WHERE id=?',(self.user,)))
    def test_page_ownership_is_atomic_across_messenger_and_leads(self):
        from fastapi import HTTPException
        from wasla.entitlements import page_ownership
        page_id=str(int(uuid4().hex[:10],16));other=uid()
        with transaction() as db:db.execute('INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)',(other,'Other PG',now()))
        def connect(kind):
            tid=self.tid if kind=='messenger' else other
            try:
                with transaction() as db:
                    page_ownership(db,tid,page_id)
                    if kind=='messenger':db.execute("INSERT INTO service_accounts(id,tenant_id,channel,provider_id,label,api_version,created_at) VALUES(?,?,'messenger',?,'Page','v26.0',?)",(uid(),tid,page_id,now()))
                    else:db.execute("INSERT INTO meta_data_connections(id,tenant_id,kind,provider_id,label,api_version,ciphertext,app_id,subject_hash,scopes) VALUES(?,?,'leads',?,'Page','v26.0','','123','subject','[]')",(uid(),tid,page_id))
                return 200
            except HTTPException as e:return e.status_code
        with ThreadPoolExecutor(2) as pool:self.assertEqual(sorted(pool.map(connect,['messenger','leads'])),[200,409])
