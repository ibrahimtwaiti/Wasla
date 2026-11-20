import json,os,unittest
from fastapi import HTTPException
import test_acceptance as base
from wasla.db import transaction,one
from wasla.saas import consume_message,period

class SaaS(unittest.TestCase):
    setUp=base.Acceptance.setUp
    tearDown=base.Acceptance.tearDown
    login=base.Acceptance.login

    def plan(self,limit=2):
        self.login('provider')
        r=self.client.post('/api/admin/commerce/plans',json={'name':'Sandbox','currency':'SAR','monthly_amount_minor':10000,'message_limit':limit})
        self.assertEqual(r.status_code,201,r.text)
        return r.json()['id']

    def assign(self,plan,tenant='a',status='trial'):
        r=self.client.put('/api/admin/commerce/subscriptions/'+tenant,json={'plan_id':plan,'status':status})
        self.assertEqual(r.status_code,200,r.text)

    def test_quota_atomic_rollback_and_pause(self):
        plan=self.plan();self.assign(plan)
        with transaction() as db:consume_message(db,'a')
        with self.assertRaises(RuntimeError):
            with transaction() as db:consume_message(db,'a');raise RuntimeError('rollback outbox')
        with transaction() as db:consume_message(db,'a')
        with self.assertRaises(HTTPException) as error:
            with transaction() as db:consume_message(db,'a')
        self.assertEqual(error.exception.status_code,429)
        with transaction() as db:self.assertEqual(one(db,'SELECT used FROM service_usage_counters WHERE tenant_id=?',('a',))['used'],2)
        self.assign(plan,status='paused')
        with self.assertRaises(HTTPException) as error:
            with transaction() as db:consume_message(db,'a')
        self.assertEqual(error.exception.detail,'subscription_paused')

    def test_invoice_idempotency_isolation_and_snapshot(self):
        plan=self.plan();self.assign(plan);self.login()
        first=self.client.post('/api/v2/billing/invoice',json={});self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json(),self.client.post('/api/v2/billing/invoice',json={}).json())
        r=self.client.get('/api/v2/billing').json();self.assertFalse(r['payment_collection_enabled']);self.assertEqual(len(r['invoices']),1)
        self.login('admin-b');r=self.client.get('/api/v2/billing').json();self.assertEqual(r['invoices'],[]);self.assertIsNone(r['subscription'])
        self.assertEqual(self.client.get('/api/admin/commerce').status_code,403)
        self.login('sales-a');self.assertEqual(self.client.get('/api/v2/billing').status_code,403)

    def test_signup_disabled_then_reviewed_and_no_takeover(self):
        data={'company':'Example','name':'Owner','email':'new@example.test','password':'new-password-123456','terms_version':'sandbox-v1'}
        self.assertEqual(self.client.post('/api/auth/register',json=data).status_code,409)
        from cryptography.fernet import Fernet
        from wasla.service_oauth import cipher
        os.environ.update(WASLA_SELF_SIGNUP_MODE='test',WASLA_EMAIL_MODE='local',WASLA_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode())
        plan=self.plan()
        for _ in range(2):self.assertEqual(self.client.post('/api/auth/register',json=data).status_code,202)
        with transaction() as db:
            u=one(db,'SELECT * FROM users WHERE email=?',(data['email'],));self.assertEqual(u['active'],0);tid=u['tenant_id']
            self.assertEqual(db.execute('SELECT count(*) FROM service_registrations').fetchone()[0],1)
        path='/api/admin/commerce/registrations/'+tid+'/approve'
        self.assertEqual(self.client.post(path,json={'evidence':'Verified email ownership by test support'}).status_code,409)
        self.assign(plan,tenant=tid)
        with transaction() as db:job=one(db,'SELECT ciphertext FROM email_jobs WHERE user_id=?',(u['id'],))
        token=json.loads(cipher().decrypt(job['ciphertext'].encode()))['url'].split('#')[1]
        self.assertEqual(self.client.post('/api/auth/verification/complete',json={'token':token}).status_code,200)
        self.assertEqual(self.client.post(path,json={'evidence':'Verified email ownership by test support'}).status_code,200)
        with transaction() as db:self.assertEqual(one(db,'SELECT active FROM users WHERE email=?',(data['email'],))['active'],1)

    def test_inputs_and_secret_redaction(self):
        self.login('provider')
        self.assertEqual(self.client.post('/api/admin/commerce/plans',json={'name':'P','currency':'SAR','monthly_amount_minor':-1,'message_limit':True}).status_code,422)
        response=self.client.post('/api/auth/register',json={'password':'secret-not-reflected'})
        self.assertEqual(response.status_code,422);self.assertNotIn('secret-not-reflected',response.text)
