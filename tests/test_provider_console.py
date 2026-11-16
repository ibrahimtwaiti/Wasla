import os
import unittest
from unittest.mock import patch
from urllib.parse import urlparse,parse_qs
import httpx
from cryptography.fernet import Fernet
import test_acceptance as base
from wasla.db import transaction,one
from wasla import service
from wasla.runtime_configuration import value

class ProviderConsole(unittest.TestCase):
    setUp=base.Acceptance.setUp
    tearDown=base.Acceptance.tearDown
    login=base.Acceptance.login

    def setup_console(self):
        os.environ['WASLA_TOKEN_ENCRYPTION_KEY']=Fernet.generate_key().decode();self.login('provider')
        r=self.client.post('/api/admin/workspaces/a/integrations',json={'channel':'whatsapp','provider_id':'12345','api_version':'v27.0','label':'Test connection'})
        self.assertEqual(r.status_code,201,r.text);return r.json()['id']

    def test_provider_only_configuration_and_account_operations(self):
        for role in ['admin-a','sales-a','reader-a']:
            self.login(role)
            self.assertEqual(self.client.get('/api/admin/integrations').status_code,403)
            self.assertEqual(self.client.put('/api/admin/integrations/configuration',json={'values':{'META_APP_SECRET':'spoof'}}).status_code,403)
            self.assertEqual(self.client.post('/api/admin/workspaces/a/integrations',json={'role':'provider'}).status_code,403)

    def test_encrypted_token_rotation_check_and_disconnect(self):
        aid=self.setup_console();path='/api/admin/workspaces/a/integrations/'+aid
        r=self.client.patch(path,json={'label':'Updated','api_version':'v27.0','access_token':'secret-account-token'});self.assertEqual(r.status_code,200,r.text)
        with transaction() as db:
            account=one(db,'SELECT * FROM service_accounts WHERE id=?',(aid,));secret=one(db,'SELECT ciphertext FROM service_secrets WHERE account_id=?',(aid,));self.assertNotIn('secret-account-token',secret['ciphertext'])
            self.assertEqual(service.token(account),'secret-account-token')
        view=self.client.get('/api/admin/integrations');self.assertEqual(view.status_code,200,view.text);self.assertNotIn('secret-account-token',view.text)
        with patch('wasla.service.provider_request',return_value=httpx.Response(200,json={'id':'12345'})):
            self.assertEqual(self.client.post(path+'/check',json={}).status_code,200)
        self.client.patch(path,json={'label':'Same secret','api_version':'v27.0','access_token':''})
        with transaction() as db:self.assertEqual(one(db,'SELECT state FROM service_accounts WHERE id=?',(aid,))['state'],'verified')
        self.assertEqual(self.client.patch('/api/admin/workspaces/b/integrations/'+aid,json={'label':'Cross scope','api_version':'v27.0','access_token':'wrong'}).status_code,404)
        self.client.post(path+'/disconnect',json={})
        with transaction() as db:self.assertIsNone(one(db,'SELECT * FROM service_secrets WHERE account_id=?',(aid,)))

    def test_runtime_config_encrypted_masked_and_immediate(self):
        self.setup_console()
        r=self.client.put('/api/admin/integrations/configuration',json={'values':{'META_VERIFY_TOKEN':'new-verify','META_APP_SECRET':'new-app-secret','INSTAGRAM_APP_ID':'123'}});self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(value('META_APP_SECRET'),'new-app-secret')
        view=self.client.get('/api/admin/integrations');self.assertNotIn('new-app-secret',view.text);self.assertNotIn('new-verify',view.text)
        with transaction() as db:self.assertNotIn('new-app-secret',one(db,"SELECT ciphertext FROM provider_configuration WHERE key='META_APP_SECRET'")['ciphertext'])
        self.assertEqual(self.client.get('/api/webhooks/v2/whatsapp?hub.mode=subscribe&hub.verify_token=new-verify&hub.challenge=123').text,'123')
        self.client.put('/api/admin/integrations/configuration',json={'values':{'META_APP_SECRET':''}});self.assertEqual(value('META_APP_SECRET'),'new-app-secret')
        self.assertEqual(self.client.put('/api/admin/integrations/configuration',json={'values':{'DATABASE_URL':'no'}}).status_code,422)
        self.assertEqual(self.client.put('/api/admin/integrations/configuration',json={'values':{'WASLA_OAUTH_ORIGIN':'http://unsafe.test'}}).status_code,422)

    def test_provider_oauth_state_works_without_tenant_impersonation(self):
        self.setup_console()
        self.client.put('/api/admin/integrations/configuration',json={'values':{'INSTAGRAM_APP_ID':'123','INSTAGRAM_APP_SECRET':'test-secret','WASLA_OAUTH_ORIGIN':'https://app.example.test'}})
        r=self.client.post('/api/admin/workspaces/a/integrations',json={'channel':'instagram','provider_id':'54321','api_version':'v27.0','label':'IG test'});aid=r.json()['id']
        r=self.client.post('/api/admin/workspaces/a/integrations/'+aid+'/authorize',json={});self.assertEqual(r.status_code,200,r.text)
        state=parse_qs(urlparse(r.json()['authorization_url']).query)['state'][0]
        r=self.client.post('/api/v2/oauth/instagram/complete',json={'state':state,'error':'access_denied'});self.assertEqual(r.status_code,200,r.text);self.assertIn('/app/integrations',r.json()['redirect'])
        self.assertEqual(self.client.post('/api/v2/oauth/instagram/complete',json={'state':state,'error':'access_denied'}).status_code,403)

    def test_configuration_requires_root_key_and_csrf(self):
        self.login('provider');os.environ.pop('WASLA_TOKEN_ENCRYPTION_KEY',None)
        self.assertEqual(self.client.put('/api/admin/integrations/configuration',json={'values':{'META_APP_SECRET':'test'}}).status_code,503)
        self.client.headers.pop('X-CSRF-Token');self.assertEqual(self.client.put('/api/admin/integrations/configuration',json={'values':{'META_APP_SECRET':'test'}}).status_code,403)

    def test_provider_controls_workspace_sending_without_cross_scope(self):
        self.setup_console();path='/api/admin/workspaces/a/integration-settings'
        config=self.client.get(path).json();config['sending_paused']=True
        self.assertEqual(self.client.put(path,json=config).status_code,200)
        self.assertTrue(self.client.get(path).json()['sending_paused'])
        self.assertFalse(self.client.get('/api/admin/workspaces/b/integration-settings').json()['sending_paused'])
        self.assertEqual(self.client.get('/api/admin/workspaces/unknown/integration-settings').status_code,404)
        self.login('admin-a');self.assertEqual(self.client.get(path).status_code,403)

    def test_provider_recovery_scoped_audited_and_never_resends(self):
        from test_service_v2 import ServiceV2
        aid=ServiceV2.account(self)
        _,ticket,_,_=ServiceV2.incoming(self,aid)
        self.assertEqual(ServiceV2.enqueue(self,ticket).status_code,201)
        with transaction() as db:
            event=one(db,'SELECT id FROM service_events WHERE account_id=?',(aid,))['id']
            out=one(db,'SELECT * FROM service_outbox WHERE tenant_id=?',('a',))
            message=one(db,"SELECT id FROM service_messages WHERE ticket_id=? AND direction='in'",(ticket['id'],))['id']
            db.execute("UPDATE service_events SET state='dead' WHERE id=?",(event,))
            db.execute("UPDATE service_outbox SET state='unknown' WHERE id=?",(out['id'],))
            db.execute("INSERT INTO service_media_jobs(id,tenant_id,message_id,account_id,descriptor,state) VALUES('media-recovery','a',?,?,?,'dead')",(message,aid,'{}'))
        self.login('provider');path='/api/admin/workspaces/a/integration-operations/'
        self.assertEqual(self.client.get('/api/admin/workspaces/a/integration-readiness').status_code,200)
        for kind,ident in [('event-retry',event),('media-retry','media-recovery'),('reconcile',out['id'])]:
            data={'action':'cancel','evidence':'Confirmed cancellation by operator'}
            self.assertEqual(self.client.post('/api/admin/workspaces/b/integration-operations/'+kind+'/'+ident,json=data).status_code,404)
        self.assertEqual(self.client.post(path+'event-retry/'+event,json={}).status_code,200)
        self.assertEqual(self.client.post(path+'event-retry/'+event,json={}).status_code,404)
        self.assertEqual(self.client.post(path+'media-retry/media-recovery',json={}).status_code,200)
        self.assertEqual(self.client.post(path+'media-retry/media-recovery',json={}).status_code,409)
        self.assertEqual(self.client.post(path+'reconcile/'+out['id'],json={'action':'accepted','evidence':'short'}).status_code,422)
        self.assertEqual(self.client.post(path+'reconcile/'+out['id'],json={'action':'accepted','evidence':'Verified in platform console'}).status_code,422)
        self.assertEqual(self.client.post(path+'reconcile/'+out['id'],json={'action':'accepted','evidence':'Verified in platform console','provider_id':'confirmed-provider-id'}).status_code,200)
        with transaction() as db:
            self.assertEqual(one(db,'SELECT state FROM service_outbox WHERE id=?',(out['id'],))['state'],'accepted')
            self.assertEqual(one(db,'SELECT provider_id FROM service_messages WHERE id=?',(out['message_id'],))['provider_id'],'confirmed-provider-id')
            self.assertIsNotNone(one(db,"SELECT * FROM audit WHERE action='provider.integration.reconcile' AND entity_id=?",(out['id'],)))
        self.login('admin-a')
        self.assertEqual(self.client.post(path+'event-retry/'+event,json={}).status_code,403)
        self.assertEqual(self.client.get('/api/admin/workspaces/a/integration-readiness').status_code,403)

    def test_staff_calendar_validation_and_scope(self):
        self.setup_console();path='/api/admin/workspaces/a'
        agents=self.client.get(path+'/integration-agents').json()
        self.assertTrue(agents);self.assertNotIn('password',str(agents));self.assertNotIn('admin-b',str(agents))
        config=self.client.get(path+'/integration-settings').json()
        config.update(workdays=[0,2,6],holidays=['2026-02-30'])
        self.assertEqual(self.client.put(path+'/integration-settings',json=config).status_code,422)
        config.update(holidays=['2026-12-01'],available_agents=[agents[0]['id']])
        self.assertEqual(self.client.put(path+'/integration-settings',json=config).status_code,200)
        self.assertEqual(self.client.get(path+'/integration-settings').json()['holidays'],['2026-12-01'])
        config['available_agents']=['admin-b']
        self.assertEqual(self.client.put(path+'/integration-settings',json=config).status_code,422)
        self.login('admin-a');self.assertEqual(self.client.get(path+'/integration-agents').status_code,403)
