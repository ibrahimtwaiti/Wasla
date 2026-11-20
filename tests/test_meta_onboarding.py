import logging
import os
import time
import unittest
from unittest.mock import patch
import httpx
from cryptography.fernet import Fernet
import test_acceptance as base
from wasla.db import transaction, one
from wasla import meta_onboarding as meta
from wasla.security import digest

class MetaOnboarding(unittest.TestCase):
    setUp=base.Acceptance.setUp
    tearDown=base.Acceptance.tearDown
    login=base.Acceptance.login

    def configure(self):
        os.environ.update(WASLA_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(),META_APP_ID='123',META_SIGNUP_CONFIG_ID='456',META_GRAPH_VERSION='v26.0',META_SIGNUP_MODE='test',META_TEST_TENANTS='a,b',META_SYSTEM_USER_TOKEN='system-secret',WASLA_OAUTH_ORIGIN='https://app.example.test')
        self.login()

    def start(self):
        r=self.client.post('/api/v2/meta/whatsapp/start',json={});self.assertEqual(r.status_code,200,r.text)
        self.assertNotIn('secret',r.text);return r.json()['state']

    def responses(self,**overrides):
        info={'user_id':'700','app_id':'123','is_valid':True,'scopes':sorted(meta.SCOPES),'expires_at':int(time.time())+3600,'granular_scopes':[{'scope':'whatsapp_business_management','target_ids':['900']}]}
        info.update(overrides)
        return [httpx.Response(200,json={'access_token':'customer-secret'}),httpx.Response(200,json={'data':info}),httpx.Response(200,json={'data':[{'id':'901','verified_name':'Customer'}]})]

    def complete(self,state,responses=None,**overrides):
        with patch('wasla.meta_onboarding.httpx.request',side_effect=responses or self.responses()) as call:
            r=self.client.post('/api/v2/meta/whatsapp/complete',json={'state':state,'code':'code-secret','business_account_id':'900','phone_number_id':'901',**overrides})
        return r,call

    def connected(self):
        self.configure();r,_=self.complete(self.start());self.assertEqual(r.status_code,200,r.text);return r.json()['account_id']

    def test_closed_default_and_rbac_csrf(self):
        self.login();self.assertFalse(self.client.get('/api/v2/meta/readiness').json()['signup_available'])
        self.assertEqual(self.client.post('/api/v2/meta/whatsapp/start',json={}).status_code,409)
        self.configure();self.login('sales-a');self.assertEqual(self.client.post('/api/v2/meta/whatsapp/start',json={}).status_code,403)
        self.login();self.client.headers.pop('X-CSRF-Token');self.assertEqual(self.client.post('/api/v2/meta/whatsapp/start',json={}).status_code,403)

    def test_complete_encrypted_and_unverified(self):
        aid=self.connected()
        with transaction() as db:
            a=one(db,'SELECT * FROM service_accounts WHERE id=?',(aid,));secret=one(db,'SELECT * FROM service_secrets WHERE account_id=?',(aid,))
            self.assertEqual(a['tenant_id'],'a');self.assertEqual(a['state'],'unverified');self.assertNotIn('customer-secret',secret['ciphertext'])
        ready=self.client.get('/api/v2/meta/readiness');self.assertNotIn('customer-secret',ready.text);self.assertFalse(ready.json()['production_approved'])
        totals=self.client.get('/api/v2/meta/usage').json()['totals'];self.assertEqual(sum(x['requests'] for x in totals),3)
        self.login('admin-b');self.assertEqual(self.client.get('/api/v2/meta/readiness').json()['connections'],[]);self.assertEqual(self.client.get('/api/v2/meta/usage').json()['totals'],[])
        self.assertEqual(self.client.post('/api/v2/meta/accounts/'+aid+'/subscribe',json={}).status_code,404)

    def test_state_tenant_session_expiry_and_replay(self):
        self.configure();state=self.start();self.login('admin-b');r,c=self.complete(state);self.assertEqual(r.status_code,403);c.assert_not_called()
        self.login();r,c=self.complete(state);self.assertEqual(r.status_code,403);c.assert_not_called() # different session
        state=self.start()
        with transaction() as db:db.execute('UPDATE meta_signup_sessions SET expires_at=0 WHERE state_hash=?',(digest(state),))
        r,c=self.complete(state);self.assertEqual(r.status_code,403);c.assert_not_called()
        state=self.start();r,_=self.complete(state);self.assertEqual(r.status_code,200)
        r,c=self.complete(state);self.assertEqual(r.status_code,403);c.assert_not_called()

    def test_missing_scope_wrong_app_expired_or_unshared_asset(self):
        self.configure()
        for override in [{'scopes':[]},{'app_id':'other'},{'is_valid':False},{'expires_at':1},{'granular_scopes':[]}]:
            r,_=self.complete(self.start(),self.responses(**override));self.assertEqual(r.status_code,409,r.text)
        with transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM service_accounts').fetchone()[0],0)

    def test_wrong_phone_and_cross_tenant_claim_rejected(self):
        aid=self.connected();self.login('admin-b')
        r,_=self.complete(self.start());self.assertEqual(r.status_code,409,r.text)
        self.assertEqual(self.client.get('/api/v2/meta/readiness').json()['connections'],[])
        self.login();responses=self.responses();responses[-1]=httpx.Response(200,json={'data':[{'id':'999'}]})
        r,_=self.complete(self.start(),responses);self.assertEqual(r.status_code,409)

    def test_subscribe_register_and_disconnect(self):
        aid=self.connected();path='/api/v2/meta/accounts/'+aid
        with patch('wasla.meta_onboarding.httpx.request',return_value=httpx.Response(200,json={'success':True})):
            self.assertEqual(self.client.post(path+'/subscribe',json={}).status_code,200)
            self.assertEqual(self.client.post(path+'/register',json={'pin':'123456'}).status_code,200)
        with transaction() as db:
            m=one(db,'SELECT * FROM meta_connections WHERE account_id=?',(aid,));self.assertEqual(m['subscription_state'],'subscribed');self.assertEqual(m['registration_state'],'registered')
            self.assertNotIn('123456',str([dict(x) for x in db.execute('SELECT * FROM audit').fetchall()]))
        self.client.post('/api/v2/accounts/'+aid+'/disconnect',json={})
        self.assertEqual(self.client.post(path+'/subscribe',json={}).status_code,409)
        from wasla.service import token
        os.environ['WASLA_CHANNEL_'+aid.upper()+'_TOKEN']='legacy-secret'
        with transaction() as db:self.assertEqual(token(one(db,'SELECT * FROM service_accounts WHERE id=?',(aid,))),'')

    def test_provider_errors_are_redacted_and_not_retried(self):
        self.configure();responses=[httpx.Response(400,json={'error':{'code':190,'message':'customer-secret'}})]
        r,call=self.complete(self.start(),responses);self.assertEqual(r.status_code,502);self.assertEqual(call.call_count,1);self.assertNotIn('customer-secret',r.text)
        self.assertEqual(self.client.get('/api/v2/meta/usage').json()['errors'][0]['error_code'],'meta_190')
        record=logging.LogRecord('httpx',20,'',0,'GET https://graph.facebook.com/v26.0/debug_token?input_token=secret "200"',(),None)
        meta.MetaLogFilter().filter(record);self.assertNotIn('input_token',record.getMessage());self.assertNotIn('=secret',record.getMessage())

    def test_validation_does_not_reflect_credentials(self):
        self.configure();r=self.client.post('/api/v2/meta/whatsapp/complete',json={'state':'short','code':'sensitive-invalid-code'})
        self.assertEqual(r.status_code,422);self.assertNotIn('sensitive-invalid-code',r.text)
        self.assertEqual(self.client.get('/api/v2/meta/openapi.json').status_code,200)

    def test_configuration_change_and_rate_limit(self):
        self.configure();state=self.start();os.environ['META_SIGNUP_CONFIG_ID']='789'
        r,c=self.complete(state);self.assertEqual(r.status_code,409);c.assert_not_called()
        for _ in range(28):self.start()
        self.assertEqual(self.client.post('/api/v2/meta/whatsapp/start',json={}).status_code,429)

    def test_csp_allows_sdk_only_on_channels(self):
        self.assertNotIn('connect.facebook.net',self.client.get('/').headers['Content-Security-Policy'])
        self.assertIn('connect.facebook.net',self.client.get('/app/channels').headers['Content-Security-Policy'])

    def test_configuration_validation(self):
        self.configure();self.login('provider')
        self.assertEqual(self.client.put('/api/admin/integrations/configuration',json={'values':{'META_APP_ID':'','META_GRAPH_VERSION':'','META_SIGNUP_MODE':''}}).status_code,200)
        for key,val in [('META_SIGNUP_MODE','production'),('META_GRAPH_VERSION','v999.0'),('META_APP_ID','host/path')]:
            self.assertEqual(self.client.put('/api/admin/integrations/configuration',json={'values':{key:val}}).status_code,422)

    def test_incomplete_onboarding_blocks_sending(self):
        from wasla.service import policy
        from fastapi import HTTPException
        aid=self.connected()
        with transaction() as db:
            account=one(db,'SELECT * FROM service_accounts WHERE id=?',(aid,))
            with self.assertRaises(HTTPException) as error:policy(db,account,{}, {})
            self.assertEqual(error.exception.detail,'meta_onboarding_incomplete')

    def test_failed_subscription_does_not_become_connected(self):
        aid=self.connected()
        with patch('wasla.meta_onboarding.httpx.request',return_value=httpx.Response(200,json={'success':False})):
            r=self.client.post('/api/v2/meta/accounts/'+aid+'/subscribe',json={})
        self.assertEqual(r.status_code,502)
        with transaction() as db:self.assertEqual(one(db,'SELECT subscription_state FROM meta_connections WHERE account_id=?',(aid,))['subscription_state'],'pending')

    def signed(self, subject='700', **changes):
        import base64,hashlib,hmac,json
        data={'user_id':subject,'algorithm':'HMAC-SHA256','issued_at':int(time.time()),**changes}
        enc=lambda b:base64.urlsafe_b64encode(b).rstrip(b'=').decode()
        encoded=enc(json.dumps(data).encode())
        return enc(hmac.new(os.environ['META_APP_SECRET'].encode(),encoded.encode(),hashlib.sha256).digest())+'.'+encoded

    def test_signed_deauthorization_and_replay(self):
        aid=self.connected();signed=self.signed()
        self.client.headers.pop('X-CSRF-Token')
        self.client.cookies.clear()
        for _ in range(2):
            response=self.client.post('/api/webhooks/meta/deauthorize',data={'signed_request':signed})
            self.assertEqual(response.status_code,200,response.text)
        with transaction() as db:
            self.assertEqual(one(db,'SELECT state FROM service_accounts WHERE id=?',(aid,))['state'],'disconnected')
            self.assertIsNone(one(db,'SELECT * FROM service_secrets WHERE account_id=?',(aid,)))
            self.assertEqual(db.execute('SELECT count(*) FROM meta_callback_receipts').fetchone()[0],1)

    def test_callback_signature_subject_algorithm_and_future(self):
        aid=self.connected()
        for signed in ['tampered.'+self.signed().split('.')[1], self.signed(algorithm='none'),self.signed(issued_at=int(time.time())+3600)]:
            self.assertEqual(self.client.post('/api/webhooks/meta/deauthorize',data={'signed_request':signed}).status_code,403)
        self.assertEqual(self.client.post('/api/webhooks/meta/deauthorize',data={'signed_request':self.signed('999')}).status_code,200)
        with transaction() as db:self.assertIsNotNone(one(db,'SELECT * FROM service_secrets WHERE account_id=?',(aid,)))

    def test_health_invalid_erases_secret_and_requires_signup(self):
        aid=self.connected();path='/api/v2/meta/accounts/'+aid+'/token-health'
        with patch('wasla.meta_onboarding.httpx.request',return_value=httpx.Response(200,json={'data':{'is_valid':False}})):
            result=self.client.post(path,json={});self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['health'],'reauthorize')
        with transaction() as db:self.assertIsNone(one(db,'SELECT * FROM service_secrets WHERE account_id=?',(aid,)))
        self.assertEqual(self.client.post(path,json={}).status_code,409)

    def test_health_outage_preserves_token_and_tenant_isolation(self):
        aid=self.connected();path='/api/v2/meta/accounts/'+aid+'/token-health'
        with patch('wasla.meta_onboarding.httpx.request',return_value=httpx.Response(503,json={'error':{'code':2}})):
            self.assertEqual(self.client.post(path,json={}).status_code,502)
        with transaction() as db:self.assertIsNotNone(one(db,'SELECT * FROM service_secrets WHERE account_id=?',(aid,)))
        self.login('admin-b');self.assertEqual(self.client.post(path,json={}).status_code,404)
