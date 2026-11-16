import unittest
import test_acceptance as base
import test_service_v2 as fixtures
from wasla.db import transaction
from wasla.service_health import queue_health

class QueueHealth(unittest.TestCase):
    setUp=base.Acceptance.setUp
    tearDown=base.Acceptance.tearDown
    login=base.Acceptance.login

    def test_age_retries_and_workspace_scope(self):
        aid=fixtures.ServiceV2.account(self)
        fixtures.ServiceV2.incoming(self,aid)
        with transaction() as db:db.execute("UPDATE service_events SET state='pending',received_at=100,next_attempt=0")
        self.assertFalse(queue_health('a',clock=1000)['healthy'])
        self.assertTrue(queue_health('b',clock=1000)['healthy'])
        with transaction() as db:db.execute('UPDATE service_events SET next_attempt=1200')
        self.assertTrue(queue_health('a',clock=1000)['healthy'])
        self.assertEqual(queue_health('a',clock=1000)['queues']['inbound']['pending'],1)
        with transaction() as db:db.execute("UPDATE service_events SET state='dead'")
        self.assertFalse(queue_health('a',clock=1000)['healthy'])

    def test_paused_outgoing_does_not_hide_unknown_result(self):
        aid=fixtures.ServiceV2.account(self);_,t,_,_=fixtures.ServiceV2.incoming(self,aid)
        fixtures.ServiceV2.enqueue(self,t)
        c=self.client.get('/api/v2/settings').json();c['sending_paused']=True
        self.client.put('/api/v2/settings',json=c)
        with transaction() as db:db.execute('UPDATE service_outbox SET created_at=100')
        self.assertTrue(queue_health('a',clock=1000)['healthy'])
        with transaction() as db:db.execute("UPDATE service_outbox SET state='unknown'")
        self.assertFalse(queue_health('a',clock=1000)['healthy'])
        self.login('provider')
        r=self.client.get('/api/admin/workspaces/a/integration-operations').json()
        self.assertEqual(r['health']['queues']['outbound']['needs_review'],1)
