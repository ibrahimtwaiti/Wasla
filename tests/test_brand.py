import base64
import unittest
import test_acceptance
from wasla.brand import DEFAULTS

class BrandAcceptance(unittest.TestCase):
    setUp=test_acceptance.Acceptance.setUp
    tearDown=test_acceptance.Acceptance.tearDown
    login=test_acceptance.Acceptance.login
    def save(self,**values):
        return self.client.put('/api/admin/brand',json={**DEFAULTS,**values})
    def test_public_tokens_and_defaults(self):
        self.assertEqual(self.client.get('/api/catalog').json()['brand']['primary'],'#834DFB')
        r=self.client.get('/api/brand.css');self.assertEqual(r.status_code,200)
        self.assertIn('--brand:#834DFB',r.text)
        self.assertIn('text/css',r.headers['content-type'])
    def test_anonymous_and_company_admin_cannot_change_identity(self):
        self.assertEqual(self.save().status_code,401)
        self.login();self.assertEqual(self.save().status_code,403)
    def test_provider_save_persists_and_updates_css(self):
        self.login('provider');self.assertEqual(self.save(primary='#123456',name='تجربة',sections=['contact','services']).status_code,200)
        self.assertIn('--brand:#123456',self.client.get('/api/brand.css').text)
        self.assertEqual(self.client.get('/api/catalog').json()['brand']['sections'],['contact','services'])
    def test_csrf_required(self):
        self.login('provider');del self.client.headers['X-CSRF-Token'];self.assertEqual(self.save().status_code,403)
    def test_css_and_url_injection_rejected(self):
        self.login('provider')
        for values in [dict(primary='red;}body{display:none'),dict(font_ar='url(evil)'),dict(social_x='javascript:alert(1)'),dict(radius=999),dict(sections=['services','services'])]:
            self.assertEqual(self.save(**values).status_code,422)
    def test_svg_script_and_external_references_rejected(self):
        self.login('provider')
        for svg in ['<svg><script>alert(1)</script></svg>','<svg onload="alert(1)"/>','<svg><image href="https://example.com/a"/></svg>','<!DOCTYPE svg><svg/>']:
            data='data:image/svg+xml;base64,'+base64.b64encode(svg.encode()).decode()
            self.assertEqual(self.save(logo_light=data).status_code,422)
    def test_safe_svg_and_reset(self):
        self.login('provider');data='data:image/svg+xml;base64,'+base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><path fill="#834DFB" d="M0 0h20v20H0Z"/></svg>').decode()
        self.assertEqual(self.save(logo_light=data).status_code,200)
        self.assertEqual(self.client.get('/api/catalog').json()['brand']['logo_light'],data)
        self.assertEqual(self.save(logo_light='').status_code,200)
    def test_invalid_image_and_oversize_rejected(self):
        self.login('provider')
        self.assertEqual(self.save(logo_light='data:image/png;base64,'+base64.b64encode(b'not a png').decode()).status_code,422)
        self.assertEqual(self.save(logo_light='data:image/png;base64,'+'A'*700001).status_code,422)
