import os,time,unittest
from unittest.mock import patch
import httpx
from cryptography.fernet import Fernet
import test_acceptance as base
from wasla import meta_pages,service
from wasla.db import transaction,one

class Pages(unittest.TestCase):
    setUp=base.Acceptance.setUp
    tearDown=base.Acceptance.tearDown
    login=base.Acceptance.login
    def configure(self):
        os.environ.update(WASLA_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(),META_APP_ID='123',META_FACEBOOK_CONFIG_ID='456',META_GRAPH_VERSION='v26.0',META_SIGNUP_MODE='test',META_TEST_TENANTS='a,b',META_SYSTEM_USER_TOKEN='test-inspector')
        self.login()
    def begin(self):
        r=self.client.post('/api/v2/meta/pages/start',json={});self.assertEqual(r.status_code,200,r.text);return r.json()['state']
    def exchange(self,state,scopes=None):
        responses=[httpx.Response(200,json={'access_token':'user-secret'}),httpx.Response(200,json={'data':{'is_valid':True,'app_id':'123','user_id':'700','scopes':list(meta_pages.SCOPES) if scopes is None else scopes,'expires_at':int(time.time())+3600}}),httpx.Response(200,json={'data':[{'id':'900','name':'Test page','tasks':['MESSAGING'],'access_token':'page-secret'}]})]
        with patch('wasla.meta_onboarding.httpx.request',side_effect=responses):return self.client.post('/api/v2/meta/pages/exchange',json={'state':state,'code':'code-secret'})
    def connected(self):
        self.configure();state=self.begin();r=self.exchange(state);self.assertEqual(r.status_code,200,r.text);self.assertNotIn('secret',r.text)
        r=self.client.post('/api/v2/meta/pages/connect',json={'state':state,'page_id':'900'});self.assertEqual(r.status_code,200,r.text);return r.json()['id']
    def test_page_selection_encrypted_and_session_replay(self):
        self.configure();state=self.begin();self.assertEqual(self.exchange(state).status_code,200)
        self.assertEqual(self.client.post('/api/v2/meta/pages/connect',json={'state':state,'page_id':'999'}).status_code,403)
        r=self.client.post('/api/v2/meta/pages/connect',json={'state':state,'page_id':'900'});self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.post('/api/v2/meta/pages/connect',json={'state':state,'page_id':'900'}).status_code,403)
        with transaction() as db:
            saved=one(db,'SELECT * FROM service_secrets WHERE account_id=?',(r.json()['id'],));self.assertNotIn('page-secret',saved['ciphertext'])
            self.assertEqual(one(db,'SELECT ciphertext FROM meta_page_sessions')['ciphertext'],'')
    def test_tenant_takeover_and_permissions_rejected(self):
        aid=self.connected();self.login('admin-b');state=self.begin();self.assertEqual(self.exchange(state).status_code,200)
        self.assertEqual(self.client.post('/api/v2/meta/pages/connect',json={'state':state,'page_id':'900'}).status_code,409)
        self.assertEqual(self.client.get('/api/v2/meta/pages').json()['accounts'],[])
        self.assertEqual(self.exchange(self.begin(),[]).status_code,409)
    def test_messenger_inbound_delivery_and_reply_policy(self):
        aid=self.connected()
        with patch('wasla.meta_onboarding.httpx.request',return_value=httpx.Response(200,json={'success':True})):
            self.assertEqual(self.client.post('/api/v2/meta/pages/'+aid+'/subscribe',json={}).status_code,200)
        with transaction() as db:db.execute("UPDATE service_accounts SET state='verified' WHERE id=?",(aid,))
        incoming={'object':'page','entry':[{'id':'900','messaging':[{'sender':{'id':'800'},'recipient':{'id':'900'},'timestamp':int(time.time()*1000),'message':{'mid':'in-one','text':'hello'}}]}]}
        service.persist_events('messenger',incoming);service.persist_events('messenger',incoming);service.process_event()
        with transaction() as db:ticket=one(db,'SELECT * FROM service_tickets WHERE tenant_id=?',('a',))
        r=self.client.post('/api/v2/tickets/'+ticket['id']+'/messages',json={'direction':'out','body':'reply','version':ticket['version']},headers={'Idempotency-Key':'messenger-test-request'})
        self.assertEqual(r.status_code,201,r.text)
        with patch('wasla.service.provider_request',return_value=httpx.Response(200,json={'message_id':'out-one'})) as send:
            service.send_outbox();self.assertEqual(send.call_args.args[3]['messaging_type'],'RESPONSE')
        delivery={'object':'page','entry':[{'id':'900','messaging':[{'timestamp':int(time.time()*1000),'delivery':{'mids':['out-one']}}]}]}
        service.persist_events('messenger',delivery);service.process_event()
        with transaction() as db:self.assertEqual(one(db,"SELECT status FROM service_messages WHERE provider_id='out-one'")['status'],'delivered')
        self.client.post('/api/v2/accounts/'+aid+'/disconnect',json={})
        with transaction() as db:self.assertEqual(one(db,'SELECT subscription_state FROM meta_page_connections WHERE account_id=?',(aid,))['subscription_state'],'disconnected')
    def test_remote_unsubscribe_failure_still_erases_local_secret(self):
        aid=self.connected()
        with patch('wasla.meta_onboarding.httpx.request',return_value=httpx.Response(503,json={'error':{'code':2}})):
            r=self.client.post('/api/v2/meta/pages/'+aid+'/disconnect-remote',json={})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['remote_unsubscribe'],'not_confirmed')
        with transaction() as db:self.assertIsNone(one(db,'SELECT * FROM service_secrets WHERE account_id=?',(aid,)))

    def test_page_profile_is_filtered(self):
        aid=self.connected()
        with patch('wasla.meta_onboarding.httpx.request',return_value=httpx.Response(200,json={'id':'900','name':'Page','category':'Shop','access_token':'never-return'})):
            r=self.client.get('/api/v2/meta/pages/'+aid+'/profile')
        self.assertEqual(r.status_code,200,r.text);self.assertNotIn('never-return',r.text)

    def test_sqlite_populated_upgrade_preserves_account_and_foreign_keys(self):
        import sqlite3,tempfile
        from pathlib import Path
        from wasla.db import initialize,ROOT
        with tempfile.TemporaryDirectory() as directory:
            path=directory+'/old.sqlite3'
            with sqlite3.connect(path) as db:
                for name in ['001_initial.sql','004_service_v2.sql','005_service_completion.sql','006_provider_console.sql','007_meta_onboarding.sql']:
                    db.executescript((ROOT/'migrations'/name).read_text())
                db.execute("INSERT INTO tenants(id,name,created_at) VALUES('old','Old','now')")
                db.execute("INSERT INTO service_accounts(id,tenant_id,channel,provider_id,label,api_version,created_at) VALUES('old-account','old','whatsapp','123','Old account','v26.0','now')")
                db.execute("INSERT INTO service_secrets VALUES('old-account','ciphertext',1)")
            with patch.dict(os.environ,{'WASLA_DB':path}):initialize();initialize()
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM service_accounts').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT ciphertext FROM service_secrets').fetchone()[0],'ciphertext')
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
