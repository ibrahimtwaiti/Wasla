import os
import tempfile
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from wasla.main import app

class SetupAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'WASLA_DB':self.tmp.name+'/setup.sqlite3','WASLA_ENV':'development','WASLA_ORIGIN':'http://testserver','WASLA_SETUP_TOKEN':'s'*48,'WASLA_SETUP_EXPIRES':str(int(time.time())+600)})
        self.env.start();self.client=TestClient(app);self.client.__enter__();self.client.headers['Origin']='http://testserver'
        self.data={'token':'s'*48,'name':'Owner','email':'owner@example.test','password':'private-test-password'}
    def tearDown(self):
        self.client.__exit__(None,None,None);self.env.stop();self.tmp.cleanup()
    def test_setup_activates_requests_and_login_once(self):
        self.assertFalse(self.client.get('/api/catalog').json()['request_ready'])
        self.assertEqual(self.client.post('/api/auth/setup',json=self.data).status_code,201)
        self.assertTrue(self.client.get('/api/catalog').json()['request_ready'])
        self.assertEqual(self.client.post('/api/auth/setup',json=self.data).status_code,409)
        self.assertEqual(self.client.post('/api/auth/login',json=self.data).status_code,200)
        self.assertEqual(self.client.get('/api/me').json()['role'],'provider')
    def test_invalid_expired_and_missing_capability_fail(self):
        for value in ['',None,'wrong']:
            self.assertEqual(self.client.post('/api/auth/setup',json={**self.data,'token':value}).status_code,403)
        with patch.dict(os.environ,{'WASLA_SETUP_EXPIRES':'1'}):
            self.assertEqual(self.client.post('/api/auth/setup',json=self.data).status_code,403)
    def test_origin_and_password_required(self):
        self.assertEqual(self.client.post('/api/auth/setup',json=self.data,headers={'Origin':'https://evil.test'}).status_code,403)
        self.assertEqual(self.client.post('/api/auth/setup',json={**self.data,'password':'short'}).status_code,422)
    def test_concurrent_activation_has_one_winner(self):
        with ThreadPoolExecutor(2) as pool:
            codes=list(pool.map(lambda _:self.client.post('/api/auth/setup',json=self.data).status_code,range(2)))
        self.assertEqual(sorted(codes),[201,409])
    def test_english_catalog_preserves_ids_and_scope(self):
        ar=self.client.get('/api/catalog').json();en=self.client.get('/api/catalog?lang=en').json()
        self.assertEqual([s['id'] for s in ar['services']],[s['id'] for s in en['services']])
        self.assertEqual(en['services'][0]['name'],'WhatsApp sales management')
        self.assertIn('not enabled in V1',en['services'][3]['limit'])
