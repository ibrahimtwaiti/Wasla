import json,os,time,unittest
from unittest.mock import patch,MagicMock
from cryptography.fernet import Fernet
from fastapi import HTTPException
import httpx
import test_acceptance as base
from wasla.db import transaction,one,rows,uid,now
from wasla import email_verification as mail,privacy,entitlements,meta_data,service
from wasla.service_oauth import cipher

class Completion(unittest.TestCase):
    def setUp(self):
        base.Acceptance.setUp(self)
        os.environ.update(WASLA_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(),WASLA_EMAIL_MODE='local',WASLA_SELF_SIGNUP_MODE='test',META_APP_ID='123',META_GRAPH_VERSION='v26.0',META_SYSTEM_USER_TOKEN='inspector',META_SIGNUP_MODE='test',META_TEST_TENANTS='a,b',META_MARKETING_CONFIG_ID='456',META_CATALOG_CONFIG_ID='457',META_LEADS_CONFIG_ID='458')
    tearDown=base.Acceptance.tearDown
    login=base.Acceptance.login
    def plan(self,**limits):
        self.login('provider');r=self.client.post('/api/admin/commerce/plans',json={'name':'Test plan','currency':'SAR','monthly_amount_minor':0,'message_limit':10,'limits':limits});self.assertEqual(r.status_code,201,r.text);return r.json()['id']
    def register(self):
        plan=self.plan(seats=2,channels=1,meta_accounts=1,api_requests=10)
        r=self.client.post('/api/auth/register',json={'company':'Test shop','name':'Owner','email':'owner-new@example.test','password':'long-new-password-123','terms_version':'sandbox-v1','plan_id':plan});self.assertEqual(r.status_code,202,r.text)
        with transaction() as db:
            job=one(db,'SELECT * FROM email_jobs');account=one(db,"SELECT * FROM users WHERE email='owner-new@example.test'")
        token=json.loads(cipher().decrypt(job['ciphertext'].encode()))['url'].split('#')[1]
        return account,token
    def account(self,channel='messenger'):
        aid=uid()
        with transaction() as db:
            db.execute('INSERT INTO service_accounts(id,tenant_id,channel,provider_id,label,api_version,state,created_at) VALUES(?,?,?,?,?,?,?,?)',(aid,'a',channel,'900','Test','v26.0','verified',now()))
        return aid
    def test_email_expiry_replay_resend_and_login(self):
        account,token=self.register();mail.process_email()
        with transaction() as db:self.assertEqual(one(db,'SELECT state FROM email_jobs')['state'],'local')
        self.assertEqual(self.client.post('/api/auth/login',json={'email':account['email'],'password':'long-new-password-123'}).status_code,401)
        self.assertEqual(self.client.post('/api/auth/verification/resend',json={'email':account['email']}).status_code,202)
        self.assertEqual(self.client.post('/api/auth/verification/complete',json={'token':token}).status_code,409)
        with transaction() as db:
            job=one(db,"SELECT * FROM email_jobs WHERE state='pending'");new=json.loads(cipher().decrypt(job['ciphertext'].encode()))['url'].split('#')[1]
        self.assertEqual(self.client.post('/api/auth/verification/complete',json={'token':new}).status_code,200)
        self.assertEqual(self.client.post('/api/auth/verification/complete',json={'token':new}).status_code,409)
        self.assertEqual(self.client.post('/api/auth/login',json={'email':account['email'],'password':'long-new-password-123'}).status_code,200)
        with transaction() as db:self.assertEqual(db.execute("SELECT count(*) FROM email_jobs WHERE ciphertext<>''").fetchone()[0],0)
    def test_email_failed_delivery_retry_and_expiration(self):
        account,token=self.register();os.environ.update(WASLA_EMAIL_MODE='smtp',WASLA_EMAIL_ORIGIN='https://preview.example.test',WASLA_SMTP_HOST='smtp.example.test',WASLA_SMTP_USER='test',WASLA_SMTP_PASSWORD='test-secret',WASLA_SMTP_FROM='noreply@example.test')
        with patch('wasla.email_verification.smtplib.SMTP',side_effect=OSError('private-details')):mail.process_email()
        with transaction() as db:
            job=one(db,'SELECT * FROM email_jobs');self.assertEqual(job['state'],'retry');self.assertNotIn('private-details',job['error'])
            db.execute('UPDATE email_jobs SET next_attempt=0');db.execute('UPDATE email_verifications SET expires_at=0')
        mail.process_email();self.assertEqual(self.client.post('/api/auth/verification/complete',json={'token':token}).status_code,409)
        os.environ['WASLA_ENV']='production';os.environ['WASLA_EMAIL_MODE']='local'
        with self.assertRaises(HTTPException):mail.configuration()
    def test_smtp_provider_uses_tls_and_does_not_retain_sent_token(self):
        self.register();os.environ.update(WASLA_EMAIL_MODE='smtp',WASLA_EMAIL_ORIGIN='https://preview.example.test',WASLA_SMTP_HOST='smtp.example.test',WASLA_SMTP_USER='test',WASLA_SMTP_PASSWORD='test-secret',WASLA_SMTP_FROM='noreply@example.test')
        smtp=MagicMock()
        with patch('wasla.email_verification.smtplib.SMTP',return_value=smtp):mail.process_email()
        smtp.__enter__.return_value.starttls.assert_called_once();smtp.__enter__.return_value.send_message.assert_called_once()
        with transaction() as db:self.assertEqual(one(db,'SELECT ciphertext FROM email_jobs')['ciphertext'],'')
        os.environ['WASLA_EMAIL_MODE']='local'
        for origin in ['ftp://localhost','http://localhost/path','http://user@localhost','http://localhost/#fragment']:
            os.environ['WASLA_EMAIL_ORIGIN']=origin
            with self.assertRaises(HTTPException):mail.configuration()
    def test_limits_block_direct_api_reactivation_and_meta_requests(self):
        plan=self.plan(seats=4,channels=0,meta_accounts=0,api_requests=1)
        self.assertEqual(self.client.put('/api/admin/commerce/subscriptions/a',json={'plan_id':plan,'status':'trial'}).status_code,200)
        self.login()
        r=self.client.post('/api/team',json={'name':'Extra','email':'extra@example.test','role':'reader','password':'long-team-password-123'})
        self.assertEqual(r.status_code,409,r.text)
        self.assertEqual(self.client.post('/api/v2/accounts',json={'channel':'whatsapp','provider_id':'555','label':'Extra','api_version':'v26.0'}).status_code,409)
        self.assertEqual(self.client.post('/api/channels',json={'phone_id':'555'}).status_code,409)
        with transaction() as db:entitlements.consume_api(db,'a')
        with self.assertRaises(HTTPException) as error:
            with transaction() as db:entitlements.consume_api(db,'a')
        self.assertEqual(error.exception.status_code,429)
    def test_tenant_deletion_files_holds_and_isolation(self):
        aid=self.account();payload={'object':'page','entry':[{'id':'900','messaging':[{'sender':{'id':'800'},'recipient':{'id':'900'},'timestamp':int(time.time()*1000),'message':{'mid':'m','text':'private'}}]}]}
        service.persist_events('messenger',payload);service.process_event()
        with transaction() as db:
            ticket=one(db,'SELECT id FROM service_tickets')['id']
            db.execute('INSERT INTO service_media VALUES(?,?,?,?,?,?,?,?,?)',(uid(),'a',ticket,'a/private-object','image/png',1,'hash','clean',int(time.time())))
            db.execute("INSERT INTO retention_holds VALUES('tenant','a','legal dispute',NULL,?)",(now(),))
            db.execute("INSERT INTO retention_holds VALUES('ticket',?,'expired private reason','2000-01-01',?)",(ticket,now()))
        self.login();r=self.client.post('/api/v2/privacy/deletions',json={'scope':'tenant','password':base.PASSWORD,'confirm':'DELETE'});self.assertEqual(r.status_code,202,r.text);ident=r.json()['id']
        self.assertEqual(self.client.get('/api/me').status_code,401)
        privacy.process_deletion();self.assertEqual(self.client.get('/api/privacy/deletions/'+ident).json()['state'],'blocked_hold')
        with transaction() as db:db.execute("DELETE FROM retention_holds WHERE entity_id='a'");db.execute('UPDATE deletion_jobs SET next_attempt=0')
        with patch('wasla.service_media.client',side_effect=RuntimeError('storage down')):privacy.process_deletion()
        with transaction() as db:
            self.assertIsNotNone(one(db,"SELECT id FROM tenants WHERE id='a'"));db.execute('UPDATE deletion_jobs SET next_attempt=0')
        storage=MagicMock()
        with patch('wasla.service_media.client',return_value=storage):
            for _ in range(4):privacy.process_deletion()
        storage.delete_object.assert_called_once()
        self.assertEqual(self.client.get('/api/privacy/deletions/'+ident).json()['state'],'completed')
        with transaction() as db:
            self.assertIsNone(one(db,"SELECT id FROM tenants WHERE id='a'"));self.assertIsNotNone(one(db,"SELECT id FROM tenants WHERE id='b'"))
            self.assertIsNone(one(db,'SELECT entity_id FROM retention_holds WHERE entity_id=?',(ticket,)))
            for table in ['service_messages','service_accounts','users','contacts','audit']:
                self.assertEqual(db.execute('SELECT count(*) FROM '+table+' WHERE tenant_id=?',('a',)).fetchone()[0],0)
    def test_user_deletion_last_admin_and_reauthentication(self):
        self.login();body={'scope':'user','password':base.PASSWORD,'confirm':'DELETE'}
        self.assertEqual(self.client.post('/api/v2/privacy/deletions',json=body).status_code,409)
        self.login('sales-a');self.assertEqual(self.client.post('/api/v2/privacy/deletions',json={**body,'password':'wrong'}).status_code,403)
        r=self.client.post('/api/v2/privacy/deletions',json=body);self.assertEqual(r.status_code,202,r.text);privacy.process_deletion()
        with transaction() as db:
            u=one(db,"SELECT * FROM users WHERE id='sales-a'");self.assertEqual(u['active'],0);self.assertEqual(u['name'],'Deleted user');self.assertEqual(u['password_hash'],'')
    def test_account_erasure_keeps_shared_waba_until_last_phone_is_deleted(self):
        first=self.account('whatsapp');second=uid()
        with transaction() as db:
            db.execute("INSERT INTO service_accounts(id,tenant_id,channel,provider_id,label,api_version,state,created_at) VALUES(?,'a','whatsapp','901','Second','v26.0','verified',?)",(second,now()))
            db.execute("INSERT INTO meta_business_accounts VALUES('waba','a',?)",(now(),))
            for aid in [first,second]:db.execute("INSERT INTO meta_connections(account_id,tenant_id,business_account_id,connected_at) VALUES(?,'a','waba',?)",(aid,now()))
        self.login()
        for index,aid in enumerate([first,second]):
            r=self.client.post('/api/v2/privacy/deletions',json={'scope':'account','account_id':aid,'password':base.PASSWORD,'confirm':'DELETE'});self.assertEqual(r.status_code,202,r.text)
            privacy.process_deletion();self.assertEqual(self.client.get('/api/privacy/deletions/'+r.json()['id']).json()['state'],'completed')
            with transaction() as db:self.assertEqual(bool(one(db,"SELECT id FROM meta_business_accounts WHERE id='waba'")),index==0)
    def data_connect(self,kind='marketing'):
        self.login();state=self.client.post('/api/v2/meta/data/'+kind+'/start',json={}).json()['state']
        info={'is_valid':True,'app_id':'123','user_id':'700','scopes':sorted(meta_data.SPECS[kind]['scopes']),'expires_at':int(time.time())+3600}
        responses=[{'access_token':'customer-secret'},{'data':info}]
        if kind=='catalogs':responses += [{'data':[{'id':'10','name':'Business'}]},{'data':[{'id':'20','name':'Catalog'}]},{'data':[]}];asset='20'
        elif kind=='leads':responses += [{'data':[{'id':'30','name':'Page','tasks':['ADVERTISE'],'access_token':'page-secret'}]}];asset='30'
        else:responses += [{'data':[{'id':'act_40','name':'Ads'}]}];asset='act_40'
        with patch('wasla.meta_onboarding.httpx.request',side_effect=[httpx.Response(200,json=x) for x in responses]):
            r=self.client.post('/api/v2/meta/data/exchange',json={'state':state,'code':'secret-code'})
        self.assertEqual(r.status_code,200,r.text);self.assertNotIn('secret',r.text)
        r=self.client.post('/api/v2/meta/data/connect',json={'state':state,'asset_id':asset});self.assertEqual(r.status_code,200,r.text);return r.json()['id']
    def test_catalog_and_marketing_sync_real_adapter_mock_transport(self):
        for kind,record in [('catalogs',{'id':'p1','name':'Product','price':'100','currency':'SAR'}),('marketing',{'account_id':'40','impressions':'100','date_start':'2026-11-01','date_stop':'2026-11-18'})]:
            aid=self.data_connect(kind)
            with patch('wasla.meta_onboarding.httpx.request',return_value=httpx.Response(200,json={'data':[record]})) as call:
                r=self.client.post('/api/v2/meta/data/'+aid+'/sync',json={});self.assertEqual(r.status_code,200,r.text)
                self.assertEqual(call.call_args.args[0],'GET')
            data=self.client.get('/api/v2/meta/data/'+aid+'/records').json()['data'];self.assertEqual(data[0]['value'],record)
            with transaction() as db:self.assertNotIn(record.get('name','impressions'),one(db,'SELECT ciphertext FROM meta_data_records WHERE connection_id=?',(aid,))['ciphertext'])
            self.login('admin-b');self.assertEqual(self.client.get('/api/v2/meta/data/'+aid+'/records').status_code,404)
    def test_leads_signature_dedup_fetch_and_retry(self):
        aid=self.data_connect('leads')
        payload={'object':'page','entry':[{'id':'30','changes':[{'field':'leadgen','value':{'page_id':'30','leadgen_id':'31'}}]}]}
        import hashlib,hmac
        raw=json.dumps(payload).encode();signature='sha256='+hmac.new(os.environ['META_APP_SECRET'].encode(),raw,hashlib.sha256).hexdigest()
        self.assertEqual(self.client.post('/api/webhooks/meta/leads',content=raw,headers={'X-Hub-Signature-256':'bad'}).status_code,401)
        for _ in range(2):self.assertEqual(self.client.post('/api/webhooks/meta/leads',content=raw,headers={'X-Hub-Signature-256':signature}).status_code,200)
        with patch('wasla.meta_onboarding.httpx.request',return_value=httpx.Response(429,json={'error':{'code':4}})):meta_data.process_lead()
        with transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM meta_lead_events').fetchone()[0],1);db.execute('UPDATE meta_lead_events SET next_attempt=0')
        with patch('wasla.meta_onboarding.httpx.request',return_value=httpx.Response(200,json={'id':'31','field_data':[{'name':'email','values':['lead@example.test']}]})):meta_data.process_lead()
        data=self.client.get('/api/v2/meta/data/'+aid+'/records').json()['data'];self.assertEqual(len(data),1);self.assertEqual(data[0]['value']['id'],'31')
    def test_lead_disconnect_and_erasure_preserve_other_page_subscriptions(self):
        aid=self.data_connect('leads');path='/api/v2/meta/data/'+aid
        def responses():
            request=httpx.Request('GET','https://graph.facebook.com/v26.0/30/subscribed_apps')
            return [httpx.Response(200,json={'data':[{'id':'123','subscribed_fields':['messages','message_reads','leadgen']}]},request=request),httpx.Response(200,json={'success':True},request=request)]
        with patch('wasla.meta_onboarding.httpx.request',side_effect=responses()) as request:
            r=self.client.post(path+'/disconnect',json={});self.assertEqual(r.json()['remote_unsubscribe'],'confirmed',r.text)
            self.assertEqual(request.call_args.kwargs['json'],{'subscribed_fields':['message_reads','messages']})
        aid=self.data_connect('leads')
        r=self.client.post('/api/v2/privacy/deletions',json={'scope':'account','account_id':aid,'password':base.PASSWORD,'confirm':'DELETE'});self.assertEqual(r.status_code,202,r.text)
        with patch('wasla.privacy.httpx.request',side_effect=responses()) as request:
            privacy.process_deletion();self.assertEqual(request.call_args.kwargs['json'],{'subscribed_fields':['message_reads','messages']})
        privacy.process_deletion();self.assertEqual(self.client.get('/api/privacy/deletions/'+r.json()['id']).json()['state'],'completed')
    def test_lead_disconnect_erases_token_on_provider_failure(self):
        aid=self.data_connect('leads')
        with patch('wasla.meta_onboarding.httpx.request',side_effect=httpx.ConnectError('private provider failure')):
            r=self.client.post('/api/v2/meta/data/'+aid+'/disconnect',json={});self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['remote_unsubscribe'],'not_confirmed')
        with transaction() as db:self.assertEqual(one(db,'SELECT ciphertext FROM meta_data_connections WHERE id=?',(aid,))['ciphertext'],'')
    def test_messenger_read_watermarks_are_monotonic_and_tenant_scoped(self):
        from wasla.messenger_metrics import apply_watermark
        aid=self.account();clock=int(time.time())
        payload={'object':'page','entry':[{'id':'900','messaging':[{'sender':{'id':'800'},'recipient':{'id':'900'},'timestamp':clock*1000,'message':{'mid':'customer','text':'hello'}}]}]}
        service.persist_events('messenger',payload);service.process_event()
        with transaction() as db:
            ticket=one(db,'SELECT id FROM service_tickets')['id']
            for i,stamp in enumerate([clock-10,clock]):db.execute("INSERT INTO service_messages(id,tenant_id,ticket_id,account_id,provider_id,direction,body,status,occurred_at,created_at) VALUES(?,?,?,?,?,'out','reply','accepted',?,?)",(uid(),'a',ticket,aid,'out-'+str(i),stamp,clock))
            apply_watermark(db,aid,'different-peer',clock*1000)
            self.assertEqual(db.execute("SELECT count(*) FROM service_messages WHERE status='read'").fetchone()[0],0)
        receipt={'object':'page','entry':[{'id':'900','messaging':[{'sender':{'id':'800'},'recipient':{'id':'900'},'timestamp':clock*1000,'read':{'watermark':clock*1000}}]}]}
        for _ in range(2):service.persist_events('messenger',receipt)
        service.process_event();self.login();metrics=self.client.get('/api/v2/meta/messenger/metrics').json()
        self.assertEqual(metrics['accounts'][0]['read'],1);self.assertEqual(metrics['accounts'][0]['observed_read_rate'],.5)
        self.assertIsNone(metrics['unavailable']['global_reach'])
        with transaction() as db:
            apply_watermark(db,aid,'800',(clock-20)*1000)
            self.assertEqual(one(db,"SELECT watermark_ms FROM meta_read_watermarks WHERE peer_id='800'")['watermark_ms'],clock*1000)
        self.login('admin-b');self.assertEqual(self.client.get('/api/v2/meta/messenger/metrics').json()['accounts'],[])
    def test_data_expiry_health_revocation_and_pagination(self):
        aid=self.data_connect('catalogs');path='/api/v2/meta/data/'+aid
        with patch('wasla.meta_onboarding.httpx.request',return_value=httpx.Response(200,json={'data':[{'id':'1'}],'paging':{'next':'https://evil.example/steal','cursors':{'after':'abc'}}})):
            self.assertEqual(self.client.post(path+'/sync',json={}).json()['next_cursor'],'abc')
        with patch('wasla.meta_onboarding.httpx.request',return_value=httpx.Response(200,json={'data':[]})) as request:
            self.assertEqual(self.client.post(path+'/sync',json={'cursor':'abc'}).status_code,200)
            self.assertIn('graph.facebook.com',request.call_args.args[1]);self.assertEqual(request.call_args.kwargs['params']['after'],'abc')
        with patch('wasla.meta_onboarding.httpx.request',return_value=httpx.Response(200,json={'data':{'is_valid':False}})):
            self.assertEqual(self.client.post(path+'/token-health',json={}).json()['health'],'reauthorize')
        with transaction() as db:self.assertEqual(one(db,'SELECT ciphertext FROM meta_data_connections WHERE id=?',(aid,))['ciphertext'],'')
        self.assertEqual(self.client.post(path+'/sync',json={}).status_code,409)
    def test_meta_signed_deletion_callback_replay_and_full_cleanup(self):
        import hashlib,hmac,base64
        from wasla.security import digest
        aid=self.account();os.environ['WASLA_OAUTH_ORIGIN']='https://wasla.example.test'
        with transaction() as db:db.execute('INSERT INTO meta_grants VALUES(?,?,?,?,NULL,?)',(aid,'a','123',digest('777'),'valid'))
        encoded=base64.urlsafe_b64encode(json.dumps({'algorithm':'HMAC-SHA256','user_id':'777','issued_at':int(time.time())}).encode()).decode().rstrip('=')
        signature=base64.urlsafe_b64encode(hmac.new(os.environ['META_APP_SECRET'].encode(),encoded.encode(),hashlib.sha256).digest()).decode().rstrip('=')
        headers={'Content-Type':'application/x-www-form-urlencoded'};data='signed_request='+signature+'.'+encoded
        r=self.client.post('/api/webhooks/meta/data-deletion',content=data,headers=headers);self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.post('/api/webhooks/meta/data-deletion',content=data,headers=headers).json(),r.json())
        ident=r.json()['confirmation_code'];privacy.process_deletion()
        self.assertEqual(self.client.get('/api/privacy/deletions/'+ident).json()['state'],'completed')
        with transaction() as db:self.assertIsNone(one(db,'SELECT id FROM service_accounts WHERE id=?',(aid,)));self.assertIsNotNone(one(db,"SELECT id FROM tenants WHERE id='a'"))
        self.assertEqual(self.client.post('/api/webhooks/meta/data-deletion',content='signed_request=bad.'+encoded,headers=headers).status_code,403)
    def test_page_publishing_permission_idempotency_and_unknown_dispatch(self):
        from wasla.page_posts import process_post
        os.environ['META_FACEBOOK_CONFIG_ID']='456';aid=self.account();self.login();url='/api/v2/meta/pages/'+aid+'/posts';body={'body':'A page update','confirmation':'PUBLISH'};headers={'Idempotency-Key':'post-operation-001'}
        self.assertEqual(self.client.post(url,json=body,headers=headers).status_code,409)
        with transaction() as db:
            db.execute('UPDATE service_accounts SET scopes=?,token_ref=? WHERE id=?',(json.dumps(['pages_manage_posts','pages_read_engagement','pages_show_list']),'vault:'+aid,aid));db.execute('INSERT INTO service_secrets VALUES(?,?,?)',(aid,cipher().encrypt(b'page-secret').decode(),int(time.time())))
        r=self.client.post(url,json=body,headers=headers);self.assertEqual(r.status_code,202,r.text);self.assertEqual(self.client.post(url,json=body,headers=headers).json(),r.json())
        self.assertEqual(self.client.post(url,json={**body,'body':'Changed'},headers=headers).status_code,409)
        with patch('wasla.meta_onboarding.httpx.request',side_effect=httpx.ReadTimeout('timeout secret')) as call:process_post();self.assertFalse(process_post());call.assert_called_once()
        self.assertEqual(self.client.get(url).json()['posts'][0]['state'],'unknown')
        self.assertEqual(self.client.post(url,json=body,headers={'Idempotency-Key':'post-operation-002'}).status_code,202)
        with patch('wasla.meta_onboarding.httpx.request',return_value=httpx.Response(200,json={'id':'900_111'})):process_post()
        self.assertIn('published',[p['state'] for p in self.client.get(url).json()['posts']])
        self.login('admin-b');self.assertEqual(self.client.get(url).status_code,404)
    def test_instagram_discovers_identity_without_manual_account_and_blocks_takeover(self):
        from urllib.parse import parse_qs,urlparse
        from wasla.service_oauth import SCOPES
        os.environ.update(INSTAGRAM_APP_ID='789',INSTAGRAM_APP_SECRET='test-instagram-secret',WASLA_OAUTH_ORIGIN='https://preview.example.test')
        def connect(identity):
            started=self.client.post('/api/v2/meta/instagram/start',json={});self.assertEqual(started.status_code,200,started.text)
            state=parse_qs(urlparse(started.json()['authorization_url']).query)['state'][0]
            request=httpx.Request('GET','https://graph.instagram.com/access_token')
            short=httpx.Response(200,json={'data':[{'user_id':identity,'permissions':','.join(SCOPES),'access_token':'short-only'}]},request=request)
            long=httpx.Response(200,json={'access_token':'long-instagram-secret','expires_in':5184000},request=request)
            with patch('wasla.service_oauth.httpx.post',return_value=short),patch('wasla.service_oauth.httpx.get',return_value=long):result=self.client.post('/api/v2/oauth/instagram/complete',json={'state':state,'code':'one-use-code'})
            self.assertEqual(self.client.post('/api/v2/oauth/instagram/complete',json={'state':state,'code':'one-use-code'}).status_code,403)
            return result
        self.login();r=connect('700');self.assertEqual(r.status_code,200,r.text);self.assertNotIn('secret',r.text)
        with transaction() as db:
            a=one(db,"SELECT * FROM service_accounts WHERE channel='instagram'");self.assertEqual(a['provider_id'],'700');self.assertEqual(a['tenant_id'],'a');self.assertNotIn('long-instagram-secret',one(db,'SELECT ciphertext FROM service_secrets')['ciphertext'])
        self.login('admin-b');self.assertEqual(connect('700').status_code,409)
    def test_worker_is_fair_and_authenticated(self):
        from wasla.jobs import tick
        os.environ['WASLA_WORKER_SECRET']='w'*40
        self.assertEqual(self.client.post('/api/internal/drain-webhooks',json={}).status_code,401)
        with patch('wasla.page_posts.process_post',return_value=True) as page,patch('wasla.service.process_event',return_value=True) as inbox:
            outcomes=[tick(max_jobs=1)['queues'] for _ in range(4)]
            self.assertTrue(any(r['inbound'] for r in outcomes));self.assertTrue(any(r['page_posts'] for r in outcomes))
        r=self.client.post('/api/internal/drain-webhooks',json={},headers={'Authorization':'Bearer '+'w'*40});self.assertEqual(r.status_code,200,r.text)
    def test_stale_graph_error_cannot_erase_reconnected_token(self):
        aid=self.data_connect('catalogs')
        with transaction() as db:old=one(db,'SELECT ciphertext FROM meta_data_connections WHERE id=?',(aid,))['ciphertext'];db.execute('UPDATE meta_data_connections SET ciphertext=? WHERE id=?',('new-ciphertext',aid))
        meta_data.record_error(aid,HTTPException(502,'meta_190'),old)
        with transaction() as db:
            a=one(db,'SELECT state,ciphertext FROM meta_data_connections WHERE id=?',(aid,));self.assertEqual(a,{'state':'connected','ciphertext':'new-ciphertext'})
    def test_subscription_upgrade_and_plan_edit_snapshot(self):
        plan=self.plan(seats=4);self.client.put('/api/admin/commerce/subscriptions/a',json={'plan_id':plan,'status':'trial'})
        self.assertEqual(self.client.patch('/api/admin/commerce/plans/'+plan,json={'name':'Updated','currency':'SAR','monthly_amount_minor':0,'message_limit':10,'limits':{'seats':5}}).status_code,200)
        self.login();self.assertEqual(self.client.get('/api/v2/billing').json()['limits']['seats'],4)
        self.assertEqual(self.client.post('/api/v2/billing/plan',json={'plan_id':plan,'status':'trial'}).status_code,200)
        self.assertEqual(self.client.get('/api/v2/billing').json()['limits']['seats'],5)
    def test_data_connection_erasure_covers_records_and_jobs(self):
        aid=self.data_connect('catalogs')
        with patch('wasla.meta_onboarding.httpx.request',return_value=httpx.Response(200,json={'data':[{'id':'1','name':'private product'}]})):self.client.post('/api/v2/meta/data/'+aid+'/sync',json={})
        r=self.client.post('/api/v2/privacy/deletions',json={'scope':'account','account_id':aid,'password':base.PASSWORD,'confirm':'DELETE'});self.assertEqual(r.status_code,202,r.text)
        privacy.process_deletion()
        self.assertEqual(self.client.get('/api/privacy/deletions/'+r.json()['id']).json()['state'],'completed')
        with transaction() as db:self.assertIsNone(one(db,'SELECT id FROM meta_data_connections WHERE id=?',(aid,)));self.assertEqual(db.execute('SELECT count(*) FROM meta_data_records').fetchone()[0],0)
    def test_erasure_ledger_replay_is_private_and_idempotent(self):
        from scripts.deletion_ledger import export_ledger,replay_ledger
        self.login();r=self.client.post('/api/v2/privacy/deletions',json={'scope':'tenant','password':base.PASSWORD,'confirm':'DELETE'});self.assertEqual(r.status_code,202)
        privacy.process_deletion();ledger=export_ledger();self.assertEqual(len(ledger['deletions']),1);self.assertNotIn('password',json.dumps(ledger))
        # Simulate a restore of a prior backup in this disposable database.
        with transaction() as db:db.execute("INSERT INTO tenants(id,name,created_at) VALUES('a','restored',?)",(now(),))
        dry=replay_ledger(ledger);self.assertEqual(len(dry['jobs']),1)
        with transaction() as db:self.assertNotEqual(one(db,"SELECT status FROM tenants WHERE id='a'")['status'],'suspended')
        with patch.dict(os.environ,{'WASLA_ENV':'restore'}):
            first=replay_ledger(ledger,True);self.assertEqual(first,replay_ledger(ledger,True))
            from scripts.deletion_ledger import drain_replay
            self.assertEqual(drain_replay(first['jobs']),0)
        with transaction() as db:self.assertIsNone(one(db,"SELECT id FROM tenants WHERE id='a'"))
    def test_provider_data_delegation_and_secret_validation(self):
        aid=self.data_connect('catalogs');path='/api/admin/workspaces/a/meta-data'
        self.assertEqual(self.client.get(path).status_code,403)
        self.login('provider');self.assertEqual(self.client.get(path).status_code,200)
        self.assertNotIn('ciphertext',self.client.get(path).text)
        self.assertEqual(self.client.get('/api/admin/workspaces/b/meta-data/'+aid+'/records').status_code,404)
        r=self.client.post(path+'/exchange',json={'state':'x','code':'must-not-leak'});self.assertEqual(r.status_code,422);self.assertNotIn('must-not-leak',r.text)
        self.assertEqual(self.client.post(path+'/'+aid+'/disconnect',json={}).status_code,200)
    def test_openapi_auth_boundaries_and_expired_staging_cleanup(self):
        from wasla.jobs import cleanup_expired_security_metadata
        self.assertEqual(self.client.get('/api/v2/openapi.json').status_code,401)
        self.login();r=self.client.get('/api/v2/openapi.json');self.assertEqual(r.status_code,200,r.text[:100]);schema=r.json()
        self.assertEqual(schema['paths']['/api/auth/verification/complete']['post']['security'],[])
        self.assertEqual(schema['paths']['/api/v2/privacy/deletions']['post']['security'],[{'SessionCookie':[],'CSRF':[]}])
        self.assertEqual(schema['paths']['/api/internal/drain-webhooks']['post']['security'],[{'WorkerBearer':[]}])
        with transaction() as db:db.execute("INSERT INTO meta_data_sessions(state_hash,tenant_id,user_id,session_hash,kind,app_id,config_id,api_version,expires_at,ciphertext) VALUES('expired','a','admin-a','session','catalogs','123','456','v26.0',1,'sensitive-encrypted-staged-token')")
        cleanup_expired_security_metadata()
        with transaction() as db:self.assertIsNone(one(db,"SELECT state_hash FROM meta_data_sessions WHERE state_hash='expired'"))
    def test_launch_configuration_requires_evidence_per_service_and_current_terms(self):
        plan=self.plan(seats=2)
        path='/api/admin/integrations/configuration'
        self.assertEqual(self.client.put(path,json={'values':{'META_SIGNUP_MODE':'enabled','WASLA_SELF_SIGNUP_MODE':'enabled'}}).status_code,200)
        self.assertFalse(self.client.get('/api/public/plans').json()['signup_enabled'])
        self.login();self.assertFalse(any(s['available'] for s in self.client.get('/api/v2/meta/data').json()['services']))
        self.login('provider')
        values={'META_APPROVAL_REFERENCE':'synthetic review reference - test only','META_APPROVED_SERVICES':'catalogs','WASLA_SIGNUP_APPROVAL_REFERENCE':'synthetic owner review - test only','WASLA_SIGNUP_TERMS_VERSION':'terms-2026-11'}
        self.assertEqual(self.client.put(path,json={'values':values}).status_code,200)
        self.login();services={s['kind']:s['available'] for s in self.client.get('/api/v2/meta/data').json()['services']};self.assertTrue(services['catalogs']);self.assertFalse(services['marketing']);self.assertFalse(services['leads'])
        policy=self.client.get('/api/public/plans').json();self.assertTrue(policy['signup_enabled']);self.assertFalse(policy['payment_collection_enabled'])
        body={'company':'Reviewed shop','name':'Owner','email':'reviewed@example.test','password':'reviewed-password-123456','terms_version':'sandbox-v1','plan_id':plan}
        self.assertEqual(self.client.post('/api/auth/register',json=body).status_code,409)
        r=self.client.post('/api/auth/register',json={**body,'terms_version':policy['terms_version']});self.assertEqual(r.status_code,202,r.text);self.assertEqual(r.json()['mode'],'trial')
        with transaction() as db:self.assertEqual(one(db,"SELECT terms_version FROM service_registrations r JOIN users u ON u.id=r.user_id WHERE u.email='reviewed@example.test'")['terms_version'],'terms-2026-11')

    def test_hosted_sessions_and_health_identify_running_release(self):
        from wasla.main import app
        for env in ({'WASLA_ENV':'staging'}, {'VERCEL_ENV':'preview'}):
            with self.subTest(env=env),patch.dict(os.environ,env):
                self.client.headers['Origin']='https://testserver' if 'VERCEL_ENV' in env else 'http://localhost:8000'
                r=self.client.post('/api/auth/login',json={'email':'admin-a@example.test','password':base.PASSWORD})
                self.assertEqual(r.status_code,200)
                self.assertIn('Secure',r.headers['set-cookie'])
                self.assertIn('HttpOnly',r.headers['set-cookie'])
                self.assertIn('max-age=',r.headers['strict-transport-security'])
                self.assertEqual(self.client.get('/api/health').json()['version'],app.version)

    def test_signup_availability_requires_plan_and_mail_and_verifies_trial(self):
        path='/api/public/plans'
        self.assertFalse(self.client.get(path).json()['signup_enabled'])
        plan=self.plan()
        with patch.dict(os.environ,{'WASLA_EMAIL_MODE':'disabled'}):
            self.assertFalse(self.client.get(path).json()['signup_enabled'])
        with patch.dict(os.environ,{'WASLA_SELF_SIGNUP_MODE':'enabled','WASLA_SIGNUP_APPROVAL_REFERENCE':'reviewed test trial','WASLA_SIGNUP_TERMS_VERSION':'trial-v2'}):
            data={'company':'Trial workspace','name':'Owner','email':'trial-owner@example.test','password':'trial-password-long-123','terms_version':'trial-v2','plan_id':plan}
            self.assertEqual(self.client.post('/api/auth/register',json=data).status_code,202)
            with transaction() as db:
                token=json.loads(cipher().decrypt(one(db,'SELECT ciphertext FROM email_jobs')['ciphertext'].encode()))['url'].split('#')[1]
            r=self.client.post('/api/auth/verification/complete',json={'token':token})
            self.assertEqual(r.json()['mode'],'trial')
            with transaction() as db:self.assertEqual(one(db,"SELECT status FROM tenants WHERE name='Trial workspace'")['status'],'active')

    def test_restore_replay_with_stale_credentials_files_and_unrelated_pending_job(self):
        from scripts.deletion_ledger import replay_ledger,drain_replay
        aid=self.account()
        ledger={'format':'wasla-erasure-ledger-v1','deletions':[{'id':'archive-job','tenant_id':'a','requester_id':'admin-a','scope':'tenant','targets':json.dumps({'accounts':[aid],'data_accounts':[]})}]}
        with transaction() as db:
            db.execute('INSERT INTO service_secrets VALUES(?,?,?)',(aid,'retired-encryption-key',1))
            other=privacy.queue(db,'b','admin-b','tenant')
        with self.assertRaisesRegex(ValueError,'isolated_restore'):
            replay_ledger(ledger,True)
        with patch.dict(os.environ,{'WASLA_ENV':'restore'}):
            jobs=replay_ledger(ledger,True)['jobs']
            with transaction() as db:
                db.execute('UPDATE deletion_jobs SET remote_ciphertext=? WHERE id=?',('old-ciphertext',jobs[0]))
                db.execute('INSERT INTO deletion_objects VALUES(?,?)',(jobs[0],'source/object'))
            with patch('wasla.service_media.client') as storage,patch('wasla.privacy.httpx.request') as meta:
                self.assertEqual(drain_replay(jobs),0);storage.assert_not_called();meta.assert_not_called()
            with transaction() as db:
                self.assertIsNone(one(db,"SELECT id FROM tenants WHERE id='a'"))
                self.assertEqual(one(db,'SELECT state FROM deletion_jobs WHERE id=?',(other,))['state'],'pending')

    def test_restored_original_job_is_local_and_respects_hold(self):
        from scripts.deletion_ledger import replay_ledger,drain_replay
        with transaction() as db:
            ident=privacy.queue(db,'a','admin-a','tenant')
            db.execute('INSERT INTO deletion_objects VALUES(?,?)',(ident,'source/file'))
            db.execute("INSERT INTO retention_holds VALUES('tenant','a','documented case',NULL,?)",(now(),))
        row={'id':ident,'tenant_id':'a','requester_id':'admin-a','scope':'tenant','targets':'{"accounts":[],"data_accounts":[]}'}
        with patch.dict(os.environ,{'WASLA_ENV':'restore'}):
            ledger={'format':'wasla-erasure-ledger-v1','deletions':[row]}
            self.assertEqual(replay_ledger(ledger,True)['jobs'],[ident])
            self.assertEqual(drain_replay([ident]),1)
            with transaction() as db:
                self.assertEqual(one(db,'SELECT state FROM deletion_jobs WHERE id=?',(ident,))['state'],'blocked_hold')
                self.assertIsNone(one(db,'SELECT * FROM deletion_objects WHERE job_id=?',(ident,)))
                db.execute("DELETE FROM retention_holds WHERE entity_type='tenant' AND entity_id='a'")
            self.assertEqual(drain_replay(replay_ledger(ledger,True)['jobs']),0)

    def test_manual_meta_setup_requires_provider_and_inbound_requires_proof(self):
        self.login()
        data={'provider_id':'staging_asset','channel':'whatsapp','api_version':'v26.0','label':'Test fixture'}
        self.assertEqual(self.client.post('/api/v2/accounts',json={**data,'provider_delegated':True}).status_code,409)
        self.login('provider')
        r=self.client.post('/api/admin/workspaces/a/integrations',json=data)
        self.assertEqual(r.status_code,201); aid=r.json()['id']
        payload={'object':'whatsapp_business_account','entry':[{'changes':[{'value':{'metadata':{'phone_number_id':data['provider_id']},'messages':[{'id':'fixture','from':'12345678901','timestamp':'1','type':'text','text':{'body':'test'}}]}}]}]}
        with self.assertRaises(HTTPException):service.persist_events('whatsapp',payload)
        with transaction() as db:
            db.execute("INSERT INTO meta_grants VALUES(?,?,?,?,?,'valid')",(aid,'a','test-app','subject',1))
        self.assertEqual(service.persist_events('whatsapp',payload),1)
        with transaction() as db:db.execute("UPDATE meta_grants SET health='revoked' WHERE account_id=?",(aid,))
        self.assertFalse(service.process_event())
        with transaction() as db:self.assertFalse(one(db,'SELECT id FROM service_messages WHERE account_id=?',(aid,)))
