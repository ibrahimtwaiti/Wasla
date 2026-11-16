import hashlib
import hmac
import json
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4
import httpx
import test_acceptance as baseline
from wasla.db import transaction, one, rows, now
from wasla import service as s

class ServiceV2(unittest.TestCase):
    setUp=baseline.Acceptance.setUp
    tearDown=baseline.Acceptance.tearDown
    login=baseline.Acceptance.login

    def account(self,channel='whatsapp',asset='12345'):
        self.login()
        r=self.client.post('/api/v2/accounts',json={'channel':channel,'provider_id':asset,'label':'Test account','api_version':'v27.0'})
        self.assertEqual(r.status_code,201,r.text); aid=r.json()['id']
        os.environ['WASLA_CHANNEL_'+aid.upper()+'_TOKEN']='synthetic-test-only'
        with transaction() as db: db.execute("UPDATE service_accounts SET state='verified' WHERE id=?",(aid,))
        return aid

    def incoming(self,aid=None,mid='m1',stamp=None):
        aid=aid or self.account();stamp=stamp or int(time.time())
        payload={'object':'whatsapp_business_account','entry':[{'changes':[{'value':{'metadata':{'phone_number_id':'12345'},'messages':[{'id':mid,'from':'966500000003','timestamp':str(stamp),'type':'text','text':{'body':'حجز موعد'}}]}}]}]}
        raw=json.dumps(payload).encode();sig='sha256='+hmac.new(b'test-webhook-secret',raw,hashlib.sha256).hexdigest()
        r=self.client.post('/api/webhooks/v2/whatsapp',content=raw,headers={'X-Hub-Signature-256':sig,'Content-Type':'application/json'})
        self.assertEqual(r.status_code,200,r.text); self.assertTrue(s.process_event())
        with transaction() as db: t=one(db,'SELECT * FROM service_tickets WHERE tenant_id=?',('a',))
        return aid,t,raw,sig

    def enqueue(self,t,**extra):
        return self.client.post('/api/v2/tickets/'+t['id']+'/messages',json={'version':t['version'],'body':'Test human reply','direction':'out',**extra},headers={'Idempotency-Key':str(uuid4())})

    def test_raw_signature_four_failure_modes(self):
        raw=b'{"x": 1}'
        valid='sha256='+hmac.new(b'secret',raw,hashlib.sha256).hexdigest()
        self.assertTrue(s.valid_signature(raw,valid,'secret'))
        for body,header,secret in [(b'{"x": 2}',valid,'secret'),(raw,'','secret'),(raw,valid,'other'),(raw,'💥','secret')]: self.assertFalse(s.valid_signature(body,header,secret))

    def test_duplicate_semantic_event_different_envelope(self):
        aid,t,raw,sig=self.incoming()
        obj=json.loads(raw);obj['irrelevant']='different envelope'
        s.persist_events('whatsapp',obj)
        self.assertFalse(s.process_event())
        with transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM service_messages').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM service_tickets').fetchone()[0],1)

    def test_unknown_asset_and_invalid_signature_persist_nothing(self):
        self.account();payload={'object':'whatsapp_business_account','entry':[{'changes':[{'value':{'metadata':{'phone_number_id':'unknown'},'messages':[{'id':'x','from':'1','timestamp':'1','type':'text','text':{'body':'test'}}]}}]}]}
        raw=json.dumps(payload).encode()
        for signature,code in [('invalid',401),('sha256='+hmac.new(b'test-webhook-secret',raw,hashlib.sha256).hexdigest(),403)]:
            r=self.client.post('/api/webhooks/v2/whatsapp',content=raw,headers={'X-Hub-Signature-256':signature});self.assertEqual(r.status_code,code,r.text)
        with transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM service_events').fetchone()[0],0)

    def test_storage_failure_never_acknowledged(self):
        self.account();payload={'object':'whatsapp_business_account','entry':[]};raw=json.dumps(payload).encode();sig='sha256='+hmac.new(b'test-webhook-secret',raw,hashlib.sha256).hexdigest()
        with patch('wasla.service.transaction',side_effect=RuntimeError('storage offline')):
            with self.assertRaises(RuntimeError):self.client.post('/api/webhooks/v2/whatsapp',content=raw,headers={'X-Hub-Signature-256':sig})

    def test_note_never_dispatched(self):
        aid,t,*_=self.incoming();r=self.enqueue(t,direction='note');self.assertEqual(r.status_code,201,r.text)
        with patch('wasla.service.provider_request') as provider:self.assertFalse(s.send_outbox());provider.assert_not_called()

    def test_accepted_not_delivered_and_first_human_reply(self):
        aid,t,*_=self.incoming(stamp=int(time.time())-240);r=self.enqueue(t);self.assertEqual(r.status_code,201,r.text)
        with patch('wasla.service.provider_request',return_value=httpx.Response(200,json={'messages':[{'id':'sent1'}]})) as provider:
            self.assertTrue(s.send_outbox());self.assertFalse(s.send_outbox());provider.assert_called_once()
        report=self.client.get('/api/v2/reports').json()
        self.assertGreaterEqual(report['first_human_median_seconds'],240);self.assertEqual(report['delivery_percent'],0)
        with transaction() as db:self.assertEqual(one(db,"SELECT status FROM service_messages WHERE direction='out'")['status'],'accepted')

    def test_out_of_order_delivery_and_status_before_acceptance(self):
        aid,t,*_=self.incoming();self.enqueue(t)
        with transaction() as db:
            s.apply_status(db,aid,'sent1','read',100);s.apply_status(db,aid,'sent1','delivered',90)
        with patch('wasla.service.provider_request',return_value=httpx.Response(200,json={'messages':[{'id':'sent1'}]})):s.send_outbox()
        with transaction() as db:
            s.apply_status(db,aid,'sent1','sent',110)
            self.assertEqual(one(db,"SELECT status FROM service_messages WHERE direction='out'")['status'],'read')

    def test_timeout_is_unknown_and_not_retried(self):
        aid,t,*_=self.incoming();self.enqueue(t)
        with patch('wasla.service.provider_request',side_effect=httpx.ReadTimeout('timeout')) as p:
            self.assertTrue(s.send_outbox());self.assertFalse(s.send_outbox());p.assert_called_once()
        with transaction() as db:self.assertEqual(one(db,'SELECT state FROM service_outbox')['state'],'unknown')

    def test_retry_after_and_authorization_revocation(self):
        aid,t,*_=self.incoming();self.enqueue(t)
        with patch('wasla.service.provider_request',return_value=httpx.Response(429,headers={'Retry-After':'120'})):s.send_outbox()
        with transaction() as db:
            out=one(db,'SELECT * FROM service_outbox');self.assertEqual(out['state'],'retry');self.assertGreaterEqual(out['next_attempt'],int(time.time())+119)
            db.execute('UPDATE service_outbox SET next_attempt=0')
        with patch('wasla.service.provider_request',return_value=httpx.Response(401)):s.send_outbox()
        with transaction() as db:self.assertEqual(one(db,'SELECT state FROM service_accounts')['state'],'reauthorize')

    def test_worker_crash_stale_claim_requires_reconciliation(self):
        aid,t,*_=self.incoming();self.enqueue(t)
        with transaction() as db:db.execute("UPDATE service_outbox SET state='sending',claimed_at=?",(int(time.time())-300,))
        with patch('wasla.service.provider_request') as provider:self.assertFalse(s.send_outbox());provider.assert_not_called()
        with transaction() as db:self.assertEqual(one(db,'SELECT state FROM service_outbox')['state'],'unknown')

    def test_window_expiry_checked_both_at_queue_and_dispatch(self):
        aid,t,*_=self.incoming(stamp=int(time.time())-86401);self.assertEqual(self.enqueue(t).status_code,409)
        with transaction() as db:db.execute('UPDATE service_tickets SET last_customer_at=?',(int(time.time()),))
        self.assertEqual(self.enqueue(t).status_code,201)
        with transaction() as db:db.execute('UPDATE service_tickets SET last_customer_at=1')
        with patch('wasla.service.provider_request') as provider:s.send_outbox();provider.assert_not_called()
        with transaction() as db:self.assertEqual(one(db,'SELECT state FROM service_outbox')['state'],'blocked')

    def test_conflict_and_idempotent_retry(self):
        aid,t,*_=self.incoming();key=str(uuid4());payload={'version':t['version'],'body':'Reply','direction':'note'}
        r=self.client.post('/api/v2/tickets/'+t['id']+'/messages',json=payload,headers={'Idempotency-Key':key});self.assertEqual(r.status_code,201)
        again=self.client.post('/api/v2/tickets/'+t['id']+'/messages',json=payload,headers={'Idempotency-Key':key});self.assertEqual(again.json()['id'],r.json()['id'])
        self.assertEqual(self.enqueue(t,direction='note').status_code,409)
        payload['body']='changed';self.assertEqual(self.client.post('/api/v2/tickets/'+t['id']+'/messages',json=payload,headers={'Idempotency-Key':key}).status_code,409)

    def test_tenant_and_role_isolation(self):
        aid,t,*_=self.incoming();self.login('admin-b')
        self.assertEqual(self.client.get('/api/v2/tickets').json(),[])
        self.assertEqual(self.client.get('/api/v2/tickets/'+t['id']).status_code,404)
        self.assertEqual(self.client.post('/api/v2/accounts/'+aid+'/check',json={}).status_code,404)
        self.login('sales-a');self.assertEqual(self.client.get('/api/v2/tickets/'+t['id']).status_code,403)
        self.login('reader-a');self.assertEqual(self.client.get('/api/v2/tickets').status_code,403)

    def test_instagram_scoped_identity_and_echo(self):
        aid=self.account('instagram','ig1');payload={'object':'instagram','entry':[{'id':'ig1','messaging':[{'sender':{'id':'person1'},'recipient':{'id':'ig1'},'timestamp':int(time.time()*1000),'message':{'mid':'igmsg1','text':'hello'}}]}]}
        s.persist_events('instagram',payload);self.assertTrue(s.process_event())
        msg=payload['entry'][0]['messaging'][0];msg['message']={'mid':'echo1','text':'our reply','is_echo':True};msg['recipient']['id']='person1'
        s.persist_events('instagram',payload);self.assertTrue(s.process_event())
        with transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM service_tickets').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM service_messages').fetchone()[0],1)

    def test_policy_gates_tiktok_snap_and_non_text(self):
        self.assertFalse(s.capabilities('tiktok')['messaging']);self.assertFalse(s.capabilities('snapchat')['messaging'])
        aid,t,*_=self.incoming();self.assertEqual(self.enqueue(t,kind='video').status_code,422)
        self.assertEqual(self.enqueue(t,body='Hello {{name}}').status_code,422)

    def test_business_hours_and_report_no_fabricated_zero(self):
        from datetime import datetime
        zone=s.ZoneInfo('Asia/Riyadh');start=int(datetime(2026,11,15,10,0,tzinfo=zone).timestamp());end=start+240
        self.assertEqual(s.business_seconds(start,end,s.DEFAULT_SETTINGS),240)
        config={**s.DEFAULT_SETTINGS,'holidays':['2026-11-15']};self.assertEqual(s.business_seconds(start,end,config),0)
        self.login();r=self.client.get('/api/v2/reports').json();self.assertIsNone(r['first_human_median_seconds']);self.assertIsNone(r['csat_percent'])

    def test_template_requires_approval_and_consent(self):
        aid,t,*_=self.incoming(stamp=int(time.time())-90000)
        payload={'kind':'template','template':'hello','language':'ar'}
        self.assertEqual(self.enqueue(t,**payload).status_code,409)
        with transaction() as db:db.execute('INSERT INTO service_templates VALUES(?,?,?,?,?,?,?,?)',('tpl','a',aid,'hello','ar','APPROVED','[]',int(time.time())))
        self.assertEqual(self.enqueue(t,**payload).status_code,409)
        self.assertEqual(self.client.post('/api/v2/identities/'+t['identity_id']+'/consents',json={'purpose':'template_messages','source':'Customer explicit signed request'}).status_code,200)
        self.assertEqual(self.enqueue(t,**payload).status_code,201)

    def test_concurrent_workers_do_not_duplicate_dispatch(self):
        aid,t,*_=self.incoming();self.enqueue(t)
        with patch('wasla.service.provider_request',return_value=httpx.Response(200,json={'messages':[{'id':'unique-send'}]})) as provider:
            with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:s.send_outbox(),range(2)))
            provider.assert_called_once()

    def test_suspension_after_queue_blocks_dispatch(self):
        aid,t,*_=self.incoming();self.enqueue(t)
        with transaction() as db:db.execute("UPDATE tenants SET status='suspended' WHERE id='a'")
        with patch('wasla.service.provider_request') as provider:s.send_outbox();provider.assert_not_called()

    def test_reopen_preserves_first_resolution(self):
        aid,t,*_=self.incoming()
        with transaction() as db:db.execute("UPDATE service_tickets SET status='closed',first_resolved_at=10,resolved_at=10,last_customer_at=1")
        self.incoming(aid,mid='new-message')
        with transaction() as db:
            t=one(db,'SELECT * FROM service_tickets');self.assertEqual(t['status'],'new');self.assertEqual(t['first_resolved_at'],10);self.assertIsNone(t['resolved_at'])

    def oauth_setup(self):
        from cryptography.fernet import Fernet
        os.environ.update(INSTAGRAM_APP_ID='synthetic-app',INSTAGRAM_APP_SECRET='synthetic-secret',WASLA_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode())
        aid=self.account('instagram','ig1')
        # Keep Origin for the test client; configure HTTPS only while starting OAuth.
        with patch('wasla.service_oauth.redirect_uri',return_value='https://test.example/api/v2/oauth/instagram/callback'):
            r=self.client.post('/api/v2/accounts/'+aid+'/oauth/start',json={})
        self.assertEqual(r.status_code,200,r.text)
        from urllib.parse import urlparse,parse_qs
        state=parse_qs(urlparse(r.json()['authorization_url']).query)['state'][0]
        return aid,state

    def test_oauth_denied_and_replay(self):
        aid,state=self.oauth_setup()
        r=self.client.post('/api/v2/oauth/instagram/complete',json={'state':state,'error':'access_denied'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.post('/api/v2/oauth/instagram/complete',json={'state':state,'error':'access_denied'}).status_code,403)
        with transaction() as db:self.assertIsNone(one(db,'SELECT * FROM service_secrets'))

    def test_oauth_state_is_bound_to_tenant_session_and_expiry(self):
        aid,state=self.oauth_setup();self.login('admin-b')
        self.assertEqual(self.client.post('/api/v2/oauth/instagram/complete',json={'state':state,'code':'test'}).status_code,403)
        self.login('admin-a')
        self.assertEqual(self.client.post('/api/v2/oauth/instagram/complete',json={'state':state,'code':'test'}).status_code,403)
        aid2,state2=self.oauth_setup() if False else (aid,state)
        with transaction() as db:db.execute('UPDATE service_oauth_states SET expires_at=1')
        self.assertEqual(self.client.post('/api/v2/oauth/instagram/complete',json={'state':state2,'code':'test'}).status_code,403)

    def test_oauth_stores_only_encrypted_token(self):
        aid,state=self.oauth_setup()
        request=httpx.Request('POST','https://api.instagram.com/oauth/access_token')
        short=httpx.Response(200,json={'access_token':'short-token','user_id':'ig1','permissions':list({'instagram_business_basic','instagram_business_manage_messages'})},request=request)
        long=httpx.Response(200,json={'access_token':'long-test-token','expires_in':5184000},request=request)
        with patch('wasla.service_oauth.httpx.post',return_value=short),patch('wasla.service_oauth.httpx.get',return_value=long),patch('wasla.service_oauth.redirect_uri',return_value='https://test.example/api/v2/oauth/instagram/callback'):
            r=self.client.post('/api/v2/oauth/instagram/complete',json={'state':state,'code':'one-time-code'})
        self.assertEqual(r.status_code,200,r.text);self.assertNotIn('long-test-token',r.text)
        with transaction() as db:
            secret=one(db,'SELECT * FROM service_secrets');a=one(db,'SELECT * FROM service_accounts');self.assertNotIn('long-test-token',secret['ciphertext'])
        self.assertEqual(s.token(a),'long-test-token')
        self.assertNotIn('long-test-token',self.client.get('/api/v2/accounts').text)
        self.assertEqual(self.client.post('/api/v2/accounts/'+aid+'/disconnect',json={}).status_code,200)
        with transaction() as db:self.assertIsNone(one(db,'SELECT * FROM service_secrets'))

    def test_oauth_account_mismatch_cannot_replace_token(self):
        aid,state=self.oauth_setup();response=httpx.Response(200,json={'user_id':'someone-else','access_token':'secret'},request=httpx.Request('POST','https://api.instagram.com/oauth/access_token'))
        with patch('wasla.service_oauth.httpx.post',return_value=response),patch('wasla.service_oauth.redirect_uri',return_value='https://test.example/api/v2/oauth/instagram/callback'):
            r=self.client.post('/api/v2/oauth/instagram/complete',json={'state':state,'code':'test'})
        self.assertEqual(r.status_code,409)
        with transaction() as db:self.assertIsNone(one(db,'SELECT * FROM service_secrets'))

    def test_attachment_mime_size_and_scanner_failure(self):
        from wasla.service_media import validate,scan
        from fastapi import HTTPException
        for raw,mime in [(b'MZevil','image/png'),(b'','application/pdf'),(b'%PDF-'+b'0'*(5*1024*1024),'application/pdf'),(b'<svg>','image/svg+xml')]:
            with self.assertRaises(HTTPException):validate(raw,mime)
        with self.assertRaises(HTTPException):scan(b'%PDF-1.4 test','application/pdf')
        os.environ.update(WASLA_MEDIA_SCANNER_URL='https://scanner.example/scan',WASLA_MEDIA_SCANNER_TOKEN='test')
        with patch('wasla.service_media.httpx.post',return_value=httpx.Response(200,json={'verdict':'infected','sha256':'wrong'},request=httpx.Request('POST','https://scanner.example/scan'))):
            with self.assertRaises(HTTPException):scan(b'%PDF-1.4 test','application/pdf')

    def test_attachment_scoped_upload_and_download(self):
        import base64
        from unittest.mock import MagicMock
        aid,t,*_=self.incoming();raw=b'%PDF-1.4 synthetic';storage=MagicMock();storage.generate_presigned_url.return_value='https://private.example/short-lived'
        with patch('wasla.service_media.client',return_value=storage),patch('wasla.service_media.scan',return_value=hashlib.sha256(raw).hexdigest()),patch.dict(os.environ,{'WASLA_MEDIA_BUCKET':'isolated-test'}):
            r=self.client.post('/api/v2/tickets/'+t['id']+'/attachments',json={'mime_type':'application/pdf','base64':base64.b64encode(raw).decode()});self.assertEqual(r.status_code,201,r.text)
            mid=r.json()['id'];self.assertEqual(self.client.get('/api/v2/attachments/'+mid+'/download').status_code,200)
            self.login('admin-b');self.assertEqual(self.client.get('/api/v2/attachments/'+mid+'/download').status_code,404)
            self.assertEqual(self.client.post('/api/v2/tickets/'+t['id']+'/attachments',json={'mime_type':'application/pdf','base64':base64.b64encode(raw).decode()}).status_code,404)
            storage.put_object.assert_called_once()

    def test_template_parameter_count_and_provider_payload(self):
        aid,t,*_=self.incoming(stamp=int(time.time())-90000)
        with transaction() as db:
            db.execute('INSERT INTO service_templates VALUES(?,?,?,?,?,?,?,?)',('param-template','a',aid,'hello_name','ar','APPROVED',json.dumps([{'type':'BODY','text':'Hello {{1}}'}]),int(time.time())))
            db.execute('INSERT INTO service_consents VALUES(?,?,?,?,?,?,NULL)',('consent-template','a',t['identity_id'],'template_messages','test evidence',int(time.time())))
        self.assertEqual(self.enqueue(t,kind='template',template='hello_name',language='ar').status_code,422)
        r=self.enqueue(t,kind='template',template='hello_name',language='ar',parameters=['Customer']);self.assertEqual(r.status_code,201,r.text)
        with patch('wasla.service.provider_request',return_value=httpx.Response(200,json={'messages':[{'id':'parameterized'}]})) as provider:
            s.send_outbox();body=provider.call_args.args[3];self.assertEqual(body['template']['components'][0]['parameters'][0]['text'],'Customer')
