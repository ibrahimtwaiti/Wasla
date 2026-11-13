import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from unittest.mock import patch
from fastapi.testclient import TestClient
from wasla.main import app
from wasla.db import initialize, transaction, now, uid, path
from wasla.security import hash_password
from wasla.worker import process_one
from scripts.retention_cleanup import execute as apply_retention

PASSWORD='test-only-long-password'
HASH=hash_password(PASSWORD)

class Acceptance(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.environment=dict(os.environ)
        os.environ.update(WASLA_DB=self.temp.name+'/test.sqlite3',WASLA_ENV='development',WASLA_ORIGIN='http://localhost:8000',META_APP_SECRET='test-webhook-secret',META_VERIFY_TOKEN='test-verify')
        initialize()
        with transaction() as db:
            for tid in ['a','b']: db.execute('INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)',(tid,'Company '+tid,now()))
            for ident,tid,role in [('provider',None,'provider'),('admin-a','a','admin'),('admin-b','b','admin'),('sales-a','a','sales'),('services-a','a','services'),('reader-a','a','reader')]:
                db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',(ident,tid,ident+'@example.test',ident,HASH,role,now()))
        self.client=TestClient(app); self.client.__enter__()
        self.client.headers.update({'Origin':'http://localhost:8000'})
    def tearDown(self):
        self.client.__exit__(None,None,None);os.environ.clear();os.environ.update(self.environment);self.temp.cleanup()
    def login(self,ident='admin-a'):
        r=self.client.post('/api/auth/login',json={'email':ident+'@example.test','password':PASSWORD});self.assertEqual(r.status_code,200,r.text)
        self.client.headers['X-CSRF-Token']=self.client.get('/api/me').json()['csrf']
    def post(self,url,data): return self.client.post('/api'+url,json=data)
    def patch(self,url,data): return self.client.patch('/api'+url,json=data)
    def request_data(self):return {'name':'عميل','company':'الشركة','phone':'+966500000001','country':'السعودية','activity':'تجارة','email':'a@example.test','service':'crm','need':'تنظيم فريق المبيعات','source':'test','contact_consent':True,'marketing_consent':False}
    def create_contact(self,name='عميل',number='+966500000002'):
        r=self.post('/contacts',{'name':name,'phone':number,'tags':'اختبار'});self.assertEqual(r.status_code,201,r.text);return r.json()['id']
    def create_opportunity(self,cid):
        r=self.post('/opportunities',{'title':'خدمة','contact_id':cid,'currency':'SAR','value_minor':15000});self.assertEqual(r.status_code,201,r.text);return r.json()['id']
    def seed_conversation(self):
        with transaction() as db:
            db.execute("INSERT INTO channels(id,tenant_id,phone_id,token_env) VALUES('channel-a','a','12345','')")
        payload={'object':'whatsapp_business_account','entry':[{'changes':[{'value':{'metadata':{'phone_number_id':'12345'},'contacts':[{'wa_id':'966500000003','profile':{'name':'اختبار'}}],'messages':[{'id':'wamid.test','from':'966500000003','timestamp':'1700000000','type':'text','text':{'body':'مرحبًا'}}]}}]}]}
        raw=json.dumps(payload).encode(); signature='sha256='+hmac.new(b'test-webhook-secret',raw,hashlib.sha256).hexdigest()
        r=self.client.post('/api/webhooks/whatsapp',content=raw,headers={'X-Hub-Signature-256':signature,'Content-Type':'application/json'})
        self.assertEqual(r.status_code,200,r.text);self.assertTrue(process_one())
        with transaction() as db:return db.execute('SELECT id FROM conversations').fetchone()[0],raw,signature
    def test_health_and_static_pages(self):
        for url in ['/api/health','/','/services','/login','/static/app.js','/static/styles.css']:
            self.assertEqual(self.client.get(url).status_code,200)
        self.assertEqual(self.client.get('/api/not-real').status_code,404)
    def test_catalog_contains_all_eleven_services(self):
        self.assertEqual(len(self.client.get('/api/catalog').json()['services']),11)
    def test_request_saved_with_reference_owner_opportunity_and_notification(self):
        r=self.client.post('/api/requests',json=self.request_data(),headers={'Idempotency-Key':uuid4().hex});self.assertEqual(r.status_code,201,r.text)
        self.assertTrue(r.json()['reference'].startswith('WS-'))
        with transaction() as db:
            for table in ['requests','provider_opportunities','notifications','audit']:self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],1)
            row=db.execute('SELECT * FROM requests').fetchone();self.assertEqual(row['owner_id'],'provider');self.assertEqual(row['marketing_consent'],0)
    def test_duplicate_request_is_idempotent(self):
        key=uuid4().hex
        a=self.client.post('/api/requests',json=self.request_data(),headers={'Idempotency-Key':key})
        b=self.client.post('/api/requests',json=self.request_data(),headers={'Idempotency-Key':key})
        self.assertEqual(a.json(),b.json())
        with transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0],1)
    def test_changed_payload_same_key_conflicts(self):
        key=uuid4().hex;d=self.request_data()
        self.client.post('/api/requests',json=d,headers={'Idempotency-Key':key});d['need']='different'
        self.assertEqual(self.client.post('/api/requests',json=d,headers={'Idempotency-Key':key}).status_code,409)
    def test_contact_consent_required(self):
        d=self.request_data();d['contact_consent']=False
        self.assertEqual(self.client.post('/api/requests',json=d,headers={'Idempotency-Key':uuid4().hex}).status_code,422)
    def test_no_provider_fails_without_losing_request(self):
        with transaction() as db:db.execute("UPDATE users SET active=0 WHERE role='provider'")
        self.assertEqual(self.client.post('/api/requests',json=self.request_data(),headers={'Idempotency-Key':uuid4().hex}).status_code,503)
        with transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0],0)
    def test_anonymous_protected(self):self.assertEqual(self.client.get('/api/contacts').status_code,401)
    def test_bilingual_public_legal_text_is_available(self):
        ar=self.client.get('/api/catalog?lang=ar').json()['content']
        en=self.client.get('/api/catalog?lang=en').json()['content']
        self.assertIn('سياسة الخصوصية', ar['privacy'])
        self.assertIn('شروط استخدام', ar['terms'])
        self.assertIn('Privacy Policy', en['privacy'])
        self.assertIn('Terms of Use', en['terms'])

    def test_retention_respects_holds_and_removes_only_stale_unconverted_requests(self):
        response=self.client.post('/api/requests',json=self.request_data(),headers={'Idempotency-Key':uuid4().hex})
        self.assertEqual(response.status_code,201,response.text)
        stale=(datetime.now(timezone.utc)-timedelta(days=220)).isoformat()
        with transaction() as db:
            ident=db.execute('SELECT id FROM requests').fetchone()[0]
            db.execute('UPDATE requests SET created_at=?,updated_at=? WHERE id=?',(stale,stale,ident))
            db.execute("INSERT INTO retention_holds VALUES('request',?, 'legal review', NULL,?)",(ident,now()))
            dry=apply_retention(db,datetime.now(timezone.utc))
            self.assertEqual(dry['counts']['unconverted_leads'],0)
            db.execute("DELETE FROM retention_holds WHERE entity_type='request' AND entity_id=?",(ident,))
            dry=apply_retention(db,datetime.now(timezone.utc))
            self.assertEqual(dry['counts']['unconverted_leads'],1)
            self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0],1)
            applied=apply_retention(db,datetime.now(timezone.utc),apply=True)
            self.assertEqual(applied['counts']['unconverted_leads'],1)
            self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM provider_opportunities').fetchone()[0],0)

    def test_retention_preserves_pending_webhooks_and_audited_legal_holds(self):
        stale=(datetime.now(timezone.utc)-timedelta(days=40)).isoformat()
        with transaction() as db:
            db.execute("INSERT INTO webhook_events(id,payload,status,created_at) VALUES('processed-old','{}','processed',?)",(stale,))
            db.execute("INSERT INTO webhook_events(id,payload,status,created_at) VALUES('pending-old','{}','pending',?)",(stale,))
            db.execute("INSERT INTO retention_holds VALUES('webhook','processed-old','incident',NULL,?)",(now(),))
            dry=apply_retention(db,datetime.now(timezone.utc))
            self.assertEqual(dry['counts']['processed_webhooks'],0)
            db.execute("DELETE FROM retention_holds WHERE entity_type='webhook'")
            applied=apply_retention(db,datetime.now(timezone.utc),apply=True)
            self.assertEqual(applied['counts']['processed_webhooks'],1)
            self.assertEqual(db.execute("SELECT count(*) FROM webhook_events WHERE id='processed-old'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM webhook_events WHERE id='pending-old'").fetchone()[0],1)

    def test_retention_rechecks_eligibility_before_removing_references(self):
        response=self.client.post('/api/requests',json=self.request_data(),headers={'Idempotency-Key':uuid4().hex})
        self.assertEqual(response.status_code,201,response.text)
        stale=(datetime.now(timezone.utc)-timedelta(days=220)).isoformat()
        with transaction() as db:
            ident=db.execute('SELECT id FROM requests').fetchone()[0]
            db.execute("UPDATE requests SET created_at=?,updated_at=?,status='accepted' WHERE id=?",(stale,stale,ident))
            # Simulate a stale candidate selected while another operation accepted it.
            with patch('scripts.retention_cleanup.eligible',side_effect=lambda d,n,c,stamp,limit: [ident] if n=='unconverted_leads' else []):
                apply_retention(db,datetime.now(timezone.utc),apply=True)
            self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM provider_opportunities').fetchone()[0],1)

    def test_idle_session_expires_after_30_minutes(self):
        self.login()
        with transaction() as db:
            db.execute('UPDATE session_activity SET last_seen_at=?',(int(time.time())-1801,))
        self.assertEqual(self.client.get('/api/me').status_code,401)

    def test_absolute_session_expires_after_12_hours(self):
        self.login()
        with transaction() as db:
            db.execute('UPDATE session_activity SET issued_at=?',(int(time.time())-43201,))
        self.assertEqual(self.client.get('/api/me').status_code,401)

    def test_recent_session_renews_activity(self):
        self.login()
        with transaction() as db:
            db.execute('UPDATE session_activity SET last_seen_at=?',(int(time.time())-90,))
        self.assertEqual(self.client.get('/api/me').status_code,200)
        with transaction() as db:
            activity=db.execute('SELECT last_seen_at FROM session_activity').fetchone()[0]
        self.assertGreater(activity,int(time.time())-10)

    def test_login_cookie_and_logout(self):
        self.login();self.assertIn('wasla_session',self.client.cookies)
        self.assertEqual(self.post('/auth/logout',{}).status_code,200)
        self.assertEqual(self.client.get('/api/me').status_code,401)
    def test_bad_password_is_generic(self):
        r=self.post('/auth/login',{'email':'admin-a@example.test','password':'incorrect'});self.assertEqual(r.status_code,401)
    def test_origin_and_csrf_required(self):
        self.login();self.client.headers.pop('X-CSRF-Token')
        self.assertEqual(self.post('/contacts',{'name':'X','phone':'+966500000001'}).status_code,403)
        self.assertEqual(self.client.post('/api/requests',json=self.request_data(),headers={'Origin':'https://evil.test','Idempotency-Key':uuid4().hex}).status_code,403)
    def test_login_rate_limit_persists_failed_attempts(self):
        for _ in range(10):self.post('/auth/login',{'email':'admin-a@example.test','password':'wrong'})
        self.assertEqual(self.post('/auth/login',{'email':'admin-a@example.test','password':PASSWORD}).status_code,429)
    def test_body_limit(self):self.assertEqual(self.post('/requests',{'need':'a'*70000}).status_code,413)
    def test_cross_tenant_contact_read_and_mutation_denied(self):
        self.login();cid=self.create_contact();self.login('admin-b')
        self.assertEqual(self.client.get('/api/contacts').json(),[])
        self.assertEqual(self.patch('/contacts/'+cid+'/consent',{'marketing_consent':False}).status_code,404)
        self.assertEqual(self.post('/opportunities',{'title':'attack','contact_id':cid,'currency':'SAR'}).status_code,404)
    def test_same_phone_allowed_between_tenants_only(self):
        self.login();self.create_contact()
        self.assertEqual(self.post('/contacts',{'name':'duplicate','phone':'+966500000002'}).status_code,409)
        self.login('admin-b');self.create_contact()
    def test_phone_normalization(self):
        self.login();self.create_contact(number='٠٠٩٦٦ ٥٠٠٠٠٠٠٠٢')
        self.assertEqual(self.client.get('/api/contacts').json()[0]['phone'],'+966500000002')
    def test_invalid_phone_rejected(self):
        self.login();self.assertEqual(self.post('/contacts',{'name':'test','phone':'0500000000'}).status_code,422)
    def test_consent_requires_evidence_and_tracks_withdrawal(self):
        self.login();cid=self.create_contact()
        self.assertEqual(self.patch('/contacts/'+cid+'/consent',{'marketing_consent':True}).status_code,422)
        self.assertEqual(self.patch('/contacts/'+cid+'/consent',{'marketing_consent':True,'consent_source':'signed form'}).status_code,200)
        self.patch('/contacts/'+cid+'/consent',{'marketing_consent':False})
        self.assertIsNotNone(self.client.get('/api/contacts').json()[0]['withdrawn_at'])
    def test_provider_cannot_read_company_contacts(self):
        self.login('provider');self.assertEqual(self.client.get('/api/contacts').status_code,403)
    def test_sales_cannot_export_or_manage_team(self):
        self.login('sales-a');self.assertEqual(self.client.get('/api/contacts/export').status_code,403)
        self.assertEqual(self.post('/team',{'name':'a'}).status_code,403)
        self.assertEqual(self.client.get('/api/admin/requests').status_code,403)
    def test_reader_cannot_mutate(self):
        self.login('reader-a');self.assertEqual(self.post('/contacts',{'name':'X','phone':'+966500000001'}).status_code,403)
        self.assertEqual(self.client.get('/api/reports').status_code,200)
    def test_opportunity_close_requires_evidence(self):
        self.login();oid=self.create_opportunity(self.create_contact())
        for stage in ['won','lost']:self.assertEqual(self.patch('/opportunities/'+oid,{'stage':stage}).status_code,422)
        self.assertEqual(self.patch('/opportunities/'+oid,{'stage':'won','reason':'contract 123'}).status_code,200)
    def test_sales_only_reads_owned_opportunities(self):
        self.login();self.create_opportunity(self.create_contact());self.login('sales-a')
        self.assertEqual(self.client.get('/api/opportunities').json(),[])
    def test_cross_tenant_owner_forbidden(self):
        self.login();oid=self.create_opportunity(self.create_contact())
        self.assertEqual(self.patch('/opportunities/'+oid,{'stage':'qualified','owner_id':'admin-b'}).status_code,422)
    def test_currency_totals_separate(self):
        self.login();cid=self.create_contact();self.create_opportunity(cid)
        self.post('/opportunities',{'title':'USD deal','contact_id':cid,'currency':'USD','value_minor':900})
        r=self.client.get('/api/reports').json();self.assertEqual({p['currency'] for p in r['pipeline']},{'SAR','USD'})
    def test_request_acceptance_creates_one_project(self):
        self.client.post('/api/requests',json=self.request_data(),headers={'Idempotency-Key':uuid4().hex});self.login('provider')
        ident=self.client.get('/api/admin/requests').json()[0]['id']
        self.assertEqual(self.patch('/admin/requests/'+ident,{'status':'accepted'}).status_code,422)
        for _ in range(2):self.assertEqual(self.patch('/admin/requests/'+ident,{'status':'accepted','quote':'Approved scope'}).status_code,200)
        data=self.client.get('/api/projects').json();self.assertEqual(len(data),1);self.assertEqual(len(data[0]['milestones']),4)
    def test_tenant_and_user_provisioning(self):
        self.login('provider');r=self.post('/admin/tenants',{'name':'New company','owner':'Owner','email':'new@example.test','password':PASSWORD});self.assertEqual(r.status_code,201,r.text)
        self.login();r=self.post('/team',{'name':'Member','email':'member@example.test','password':PASSWORD,'role':'marketing'});self.assertEqual(r.status_code,201,r.text)
        self.assertEqual(self.patch('/team/'+r.json()['id'],{'role':'marketing','active':False}).status_code,200)
    def test_suspended_tenant_blocks_existing_session(self):
        self.login()
        with transaction() as db:db.execute("UPDATE tenants SET status='suspended' WHERE id='a'")
        self.assertEqual(self.client.get('/api/contacts').status_code,403)
    def test_password_change_revokes_sessions(self):
        self.login();self.assertEqual(self.post('/auth/password',{'current_password':PASSWORD,'new_password':'another-long-password'}).status_code,200)
        self.assertEqual(self.client.get('/api/me').status_code,401)
    def test_export_formula_escaped_and_audited(self):
        self.login();self.create_contact(name='=SUM(1,2)');r=self.client.get('/api/contacts/export');self.assertEqual(r.status_code,200);self.assertIn("'=SUM",r.text)
        self.assertTrue(any(a['action']=='contacts.export' for a in self.client.get('/api/audit').json()))
    def test_signed_webhook_persists_before_processing(self):
        vid,raw,sig=self.seed_conversation()
        self.client.post('/api/webhooks/whatsapp',content=raw,headers={'X-Hub-Signature-256':sig,'Content-Type':'application/json'})
        self.assertFalse(process_one())
        with transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],1)
    def test_duplicate_message_different_event_not_duplicated(self):
        vid,raw,sig=self.seed_conversation();payload=json.loads(raw);payload['extra']='retry';raw=json.dumps(payload).encode();sig='sha256='+hmac.new(b'test-webhook-secret',raw,hashlib.sha256).hexdigest()
        self.client.post('/api/webhooks/whatsapp',content=raw,headers={'X-Hub-Signature-256':sig,'Content-Type':'application/json'});self.assertTrue(process_one())
        with transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],1)
    def test_invalid_webhook_signature(self):
        self.assertEqual(self.client.post('/api/webhooks/whatsapp',content=b'{}',headers={'X-Hub-Signature-256':'invalid'}).status_code,401)
        self.assertEqual(self.client.get('/api/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=test-verify&hub.challenge=hello').text,'hello')
    def test_inbox_isolation_and_note_idempotency(self):
        vid,_,_=self.seed_conversation();self.login('admin-b')
        self.assertEqual(self.client.get('/api/conversations').json(),[])
        self.assertEqual(self.client.get('/api/conversations/'+vid+'/messages').status_code,404)
        self.login('admin-a');key=uuid4().hex
        for _ in range(2):self.assertEqual(self.client.post('/api/conversations/'+vid+'/messages',json={'body':'internal note','direction':'note'},headers={'Idempotency-Key':key}).status_code,200)
        self.assertEqual(len(self.client.get('/api/conversations/'+vid+'/messages').json()),2)
    def test_external_send_never_fakes_success(self):
        vid,_,_=self.seed_conversation();self.login()
        self.assertEqual(self.client.post('/api/conversations/'+vid+'/messages',json={'body':'hello','direction':'out'},headers={'Idempotency-Key':uuid4().hex}).status_code,409)
    def test_unassigned_conversation_not_visible_to_sales(self):
        vid,_,_=self.seed_conversation();self.login('sales-a');self.assertEqual(self.client.get('/api/conversations').json(),[])
        self.assertEqual(self.client.get('/api/conversations/'+vid+'/messages').status_code,403)
    def test_bad_event_reaches_dead_letter(self):
        with transaction() as db:db.execute('INSERT INTO webhook_events(id,payload,created_at) VALUES(?,?,?)',('bad','{"entry":[null]}',now()))
        for _ in range(5):self.assertFalse(process_one())
        with transaction() as db:self.assertEqual(db.execute("SELECT status FROM webhook_events WHERE id='bad'").fetchone()[0],'dead')
    def test_backup_restores_data(self):
        self.login();self.create_contact()
        dest=self.temp.name+'/backup.sqlite3'
        with sqlite3.connect(path()) as src,sqlite3.connect(dest) as dst:src.backup(dst)
        with sqlite3.connect(dest) as restored:
            self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(restored.execute('SELECT count(*) FROM contacts').fetchone()[0],1)
    def test_task_dates_and_ownership(self):
        self.login();self.assertEqual(self.post('/tasks',{'title':'bad','due_at':'yesterday'}).status_code,422)
        r=self.post('/tasks',{'title':'followup','due_at':'2026-12-01T10:00:00Z'});self.assertEqual(r.status_code,200)
        self.assertEqual(self.patch('/tasks/'+r.json()['id'],{'status':'done'}).status_code,200)
    def test_site_content_provider_only(self):
        self.login();self.assertEqual(self.client.put('/api/admin/content',json={'key':'about','value':'text'}).status_code,403)
        self.login('provider');self.assertEqual(self.client.put('/api/admin/content',json={'key':'about','value':'وصلة'}).status_code,200)
        self.assertEqual(self.client.get('/api/catalog').json()['content']['about'],'وصلة')

if __name__=='__main__':unittest.main()
