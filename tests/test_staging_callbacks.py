import base64
import hashlib
import hmac
import json
import os
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import test_acceptance as base
from ops import staging_callbacks as callbacks


class StagingCallbackAcceptance(unittest.TestCase):
    def setUp(self):
        base.Acceptance.setUp(self)
        self.gateway = TestClient(callbacks.app)
        self.addCleanup(self.gateway.close)

    tearDown = base.Acceptance.tearDown

    def test_binding_and_private_routes(self):
        source = 'postgresql://qa@' + callbacks.STAGING_HOST + '/wasla_staging'
        with patch.dict(os.environ, DATABASE_URL=source, WASLA_ENV='staging', VERCEL_ENV='preview'):
            self.assertTrue(callbacks.staging_bound())
            for path in ('/app', '/api/me', '/setup', '/api/auth/register', '/api/internal/drain-webhooks',
                         '/api/admin/privacy/deletions', '/docs', '/static/app.js'):
                self.assertEqual(self.gateway.get(path).status_code, 404)
            with patch.dict(os.environ, VERCEL_ENV='production'):
                self.assertEqual(self.gateway.get('/api/webhooks/v2/whatsapp').status_code, 503)
            with patch.dict(os.environ, DATABASE_URL='postgresql://qa@localhost/wasla_staging'):
                self.assertFalse(callbacks.staging_bound())

    def test_challenge_signed_delivery_and_size_limit(self):
        with patch.object(callbacks, 'staging_bound', return_value=True):
            path = '/api/webhooks/v2/whatsapp'
            query = {'hub.mode': 'subscribe', 'hub.verify_token': 'test-verify', 'hub.challenge': '123'}
            response = self.gateway.get(path, params=query)
            self.assertEqual((response.status_code, response.text), (200, '123'))
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            raw = b'{"object":"whatsapp_business_account","entry":[]}'
            signature = hmac.new(b'test-webhook-secret', raw, hashlib.sha256).hexdigest()
            self.assertEqual(self.gateway.post(path, content=raw).status_code, 401)
            valid = self.gateway.post(path, content=raw, headers={'X-Hub-Signature-256': 'sha256='+signature})
            self.assertEqual(valid.status_code, 200)
            self.assertEqual(self.gateway.post(path, content=b'x'*65537).status_code, 413)

    def test_signed_deletion_returns_public_private_status(self):
        origin = 'https://hooks.example.invalid'
        with patch.object(callbacks, 'staging_bound', return_value=True), patch.dict(
                os.environ, WASLA_DELETION_STATUS_ORIGIN=origin, META_APP_ID='123'):
            def encode(data):
                return base64.urlsafe_b64encode(data).rstrip(b'=')
            body = json.dumps({'user_id': '123', 'algorithm': 'HMAC-SHA256', 'issued_at': int(time.time())})
            payload = encode(body.encode())
            mac = encode(hmac.new(b'test-webhook-secret', payload, hashlib.sha256).digest())
            signed = (mac+b'.'+payload).decode()
            response = self.gateway.post('/api/webhooks/meta/data-deletion', data={'signed_request': signed})
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertEqual(result['url'], origin+'/deletion-status?request='+result['confirmation_code'])
            page = self.gateway.get(result['url'])
            self.assertEqual(page.status_code, 200)
            self.assertIn('حالة طلب الحذف', page.text)
            self.assertNotIn(result['confirmation_code'], page.text)
            self.assertEqual(self.gateway.get('/deletion-status?request=bad').status_code, 404)
