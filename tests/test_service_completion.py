import json
import os
import time
from unittest.mock import patch, MagicMock
import unittest
import httpx
from fastapi import HTTPException
import test_service_v2 as base
from wasla import service as s
from wasla.db import transaction, one, uid, now
from wasla.service_templates import pages, components
from wasla.service_media import process_media, fetch_provider_media

class Completion(unittest.TestCase):
    setUp=base.ServiceV2.setUp
    tearDown=base.ServiceV2.tearDown
    login=base.ServiceV2.login
    account=base.ServiceV2.account
    incoming=base.ServiceV2.incoming
    enqueue=base.ServiceV2.enqueue

    def media_job(self):
        aid,t,*_=self.incoming()
        with transaction() as db:
            m=one(db,'SELECT * FROM service_messages WHERE ticket_id=?',(t['id'],))
            db.execute('INSERT INTO service_media_jobs(id,tenant_id,message_id,account_id,descriptor) VALUES(?,?,?,?,?)',('job','a',m['id'],aid,json.dumps({'id':'777'})))
        return aid,t,m

    def test_media_event_normalization_and_dedup(self):
        aid=self.account()
        payload={'object':'whatsapp_business_account','entry':[{'changes':[{'value':{'metadata':{'phone_number_id':'12345'},'messages':[{'id':'image1','from':'123','timestamp':str(int(time.time())),'type':'image','image':{'id':'777','mime_type':'image/png'}}]}}]}]}
        s.persist_events('whatsapp',payload);self.assertTrue(s.process_event());s.persist_events('whatsapp',payload);self.assertFalse(s.process_event())
        with transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM service_media_jobs').fetchone()[0],1)

    def test_media_scan_before_storage_and_replay(self):
        aid,t,m=self.media_job();storage=MagicMock()
        with patch.dict(os.environ,{'WASLA_MEDIA_BUCKET':'private-test'}),patch('wasla.service_media.client',return_value=storage),patch('wasla.service_media.fetch_provider_media',return_value=(b'\x89PNG\r\n\x1a\nfixture','image/png')),patch('wasla.service_media.scan',return_value='hash') as scan:
            self.assertTrue(process_media());self.assertFalse(process_media());scan.assert_called_once();storage.put_object.assert_called_once()
        self.assertEqual(len(self.client.get('/api/v2/tickets/'+t['id']).json()['attachments']),1)

    def test_media_rejection_never_stores(self):
        self.media_job();storage=MagicMock()
        with patch('wasla.service_media.client',return_value=storage),patch('wasla.service_media.fetch_provider_media',side_effect=HTTPException(422,'untrusted_provider_media_url')):
            self.assertTrue(process_media());storage.put_object.assert_not_called()
        with transaction() as db:self.assertEqual(one(db,'SELECT state FROM service_media_jobs')['state'],'rejected')

    def test_media_outage_retries_bounded(self):
        self.media_job()
        with patch('wasla.service_media.client',side_effect=HTTPException(503,'storage_unavailable')):
            for _ in range(5):
                with transaction() as db:db.execute('UPDATE service_media_jobs SET next_attempt=0')
                self.assertTrue(process_media())
        with transaction() as db:self.assertEqual(one(db,'SELECT state FROM service_media_jobs')['state'],'dead')
        self.assertEqual(self.client.post('/api/v2/media-jobs/job/retry',json={}).status_code,200)

    def test_media_url_blocks_ssrf_and_foreign_hosts(self):
        a={'channel':'instagram'}
        for url in ['http://scontent.cdninstagram.com/x','https://localhost/x','https://cdninstagram.com.evil.test/x','https://x.cdninstagram.com:8443/x','https://user:pass@x.cdninstagram.com/x']:
            with self.assertRaises(HTTPException):fetch_provider_media(a,{'url':url})
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]),self.assertRaises(HTTPException):fetch_provider_media(a,{'url':'https://x.cdninstagram.com/x'})

    def test_pagination_uses_cursor_not_untrusted_next_url(self):
        responses=[httpx.Response(200,json={'data':[{'id':'1'}],'paging':{'next':'https://attacker.test/steal','cursors':{'after':'a/b?secret'}}}),httpx.Response(200,json={'data':[{'id':'2'}]})]
        with patch('wasla.service.provider_request',side_effect=responses) as call:
            self.assertEqual(len(pages({},'phone_numbers')),2)
            self.assertEqual(call.call_args.args[2],'/phone_numbers?limit=100&after=a%2Fb%3Fsecret')

    def test_pagination_loop_fails_without_partial_approval(self):
        response=httpx.Response(200,json={'data':[],'paging':{'next':'x','cursors':{'after':'same'}}})
        with patch('wasla.service.provider_request',return_value=response),self.assertRaises(HTTPException):pages({},'message_templates')

    def test_template_header_buttons_validation(self):
        aid,t,*_=self.incoming()
        schema=[{'type':'BODY','text':'Hello {{1}}'},{'type':'HEADER','format':'TEXT','text':'Order {{1}}'},{'type':'BUTTONS','buttons':[{'type':'URL','url':'https://example.test/{{1}}'},{'type':'QUICK_REPLY','text':'Yes'}]}]
        p={'parameters':['Ali'],'header_parameters':['123'],'button_parameters':{'0':'123','1':'confirm'}}
        with transaction() as db:
            out=components(db,schema,p,t);self.assertEqual(len(out),4)
            self.assertEqual(out[2]['sub_type'],'url')
            with self.assertRaises(HTTPException):components(db,schema,{**p,'button_parameters':{'2':'wrong'}},t)
            with self.assertRaises(HTTPException):components(db,schema,{**p,'header_parameters':[]},t)

    def old_ticket(self):
        aid,t,*_=self.incoming()
        with transaction() as db:db.execute("UPDATE service_tickets SET status='closed',updated_at=? WHERE id=?",(int(time.time())-200*86400,t['id']))
        r=self.client.put('/api/v2/retention',json={'days':180,'enabled':True,'evidence':'approved test policy reference'});self.assertEqual(r.status_code,200,r.text)
        return aid,t

    def test_retention_preview_hold_redaction_and_dedup(self):
        aid,t=self.old_ticket()
        self.assertEqual(self.client.get('/api/v2/retention').json()['eligible_ticket_ids'],[t['id']])
        self.client.post('/api/v2/tickets/'+t['id']+'/hold',json={'reason':'unresolved complaint retention'})
        self.assertEqual(self.client.post('/api/v2/retention/run',json={}).json()['redacted_tickets'],0)
        self.client.post('/api/v2/tickets/'+t['id']+'/hold',json={'reason':'complaint resolved documented','release':True})
        self.assertEqual(self.client.post('/api/v2/retention/run',json={}).json()['redacted_tickets'],1)
        self.assertEqual(self.client.post('/api/v2/retention/run',json={}).json()['redacted_tickets'],0)
        with transaction() as db:
            self.assertIn('removed',one(db,'SELECT body FROM service_messages')['body'])
            self.assertEqual(one(db,'SELECT payload FROM service_events')['payload'],'{}')
        # Replay remains a duplicate even after content redaction.
        self.incoming(aid=aid,mid='m2')
        with transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM service_tickets').fetchone()[0],1)

    def test_retention_storage_failure_keeps_content_and_hold_scope(self):
        aid,t=self.old_ticket()
        with transaction() as db:db.execute('INSERT INTO service_media VALUES(?,?,?,?,?,?,?,?,?)',('media','a',t['id'],'key','image/png',1,'hash','clean',1))
        storage=MagicMock();storage.delete_object.side_effect=RuntimeError('offline')
        with patch.dict(os.environ,{'WASLA_MEDIA_BUCKET':'test'}),patch('wasla.service_media.client',return_value=storage):
            with self.assertRaises(RuntimeError):self.client.post('/api/v2/retention/run',json={})
        with transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM service_redactions').fetchone()[0],0)
        self.login('admin-b');self.assertEqual(self.client.get('/api/v2/retention').json()['eligible_ticket_ids'],[])
        self.assertEqual(self.client.post('/api/v2/tickets/'+t['id']+'/hold',json={'reason':'cross tenant invalid hold'}).status_code,404)

    def test_snap_read_only_sync_scopes_profile(self):
        aid=self.account('snapchat','snap123')
        self.assertEqual(self.client.post('/api/v2/accounts/'+aid+'/snap/sync',json={}).status_code,409)
        self.client.put('/api/v2/accounts/'+aid+'/approval',json={'reference':'test official allowlist evidence'})
        data={'request_status':'SUCCESS','public_profiles':[{'sub_request_status':'SUCCESS','public_profile':{'id':'snap123','display_name':'Test'}}]}
        with patch('wasla.service_channels.httpx.get',return_value=httpx.Response(200,json=data)):
            self.assertEqual(self.client.post('/api/v2/accounts/'+aid+'/snap/sync',json={}).status_code,200)
        r=self.client.get('/api/v2/accounts/'+aid+'/snap').json();self.assertFalse(r['customer_messaging']);self.assertIsNotNone(r['profile'])
        self.login('admin-b');self.assertEqual(self.client.get('/api/v2/accounts/'+aid+'/snap').status_code,404)

    def test_readiness_never_exposes_token(self):
        self.account();r=self.client.get('/api/v2/readiness');self.assertEqual(r.status_code,200);self.assertNotIn('synthetic-test-only',r.text);self.assertFalse(r.json()['production_approved'])

    def test_crm_link_idempotency_and_tenant_isolation(self):
        aid,t,*_=self.incoming();path='/api/v2/identities/'+t['identity_id']+'/crm';payload={'name':'Verified test','phone':'+966500000555','evidence':'verified customer identity test'}
        r=self.client.post(path,json=payload);self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.post(path,json=payload).json(),r.json())
        self.login('admin-b');self.assertEqual(self.client.post(path,json=payload).status_code,404)

    def test_suggestions_are_drafts_from_same_tenant_only(self):
        aid,t,*_=self.incoming()
        self.client.post('/api/v2/replies',json={'title':'موعد','body':'حجز موعد يحتاج تأكيد الموظف'})
        r=self.client.get('/api/v2/tickets/'+t['id']+'/suggestions');self.assertEqual(r.status_code,200)
        self.assertFalse(r.json()['automatic_send']);self.assertTrue(r.json()['requires_human_review']);self.assertEqual(len(r.json()['suggestions']),1)
        with transaction() as db:self.assertEqual(db.execute('SELECT count(*) FROM service_outbox').fetchone()[0],0)
