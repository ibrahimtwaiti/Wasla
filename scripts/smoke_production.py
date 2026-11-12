"""Read-only deployment checks plus an unauthenticated worker rejection.
Usage: python -m scripts.smoke_production https://your-production-origin
Never creates accounts, requests or customer records.
"""
import sys
import httpx
origin=sys.argv[1].rstrip('/')
if not origin.startswith('https://'):raise SystemExit('HTTPS origin required')
with httpx.Client(base_url=origin,timeout=30,follow_redirects=False) as client:
    for path in ['/','/services','/privacy','/terms','/login','/setup','/robots.txt','/sitemap.xml','/static/brand/logo.png','/static/fonts/Tajawal-Regular.ttf','/static/en.js','/api/brand.css']:
        response=client.get(path);assert response.status_code==200,(path,response.status_code)
    health=client.get('/api/health');assert health.json()['status']=='ok'
    assert 'no-store' in health.headers['cache-control']
    assert "script-src 'self'" in health.headers['content-security-policy']
    assert 'max-age=' in health.headers['strict-transport-security']
    assert client.get('/api/me').status_code==401
    assert client.post('/api/internal/drain-webhooks',json={}).status_code==401
    assert client.get('/missing-smoke-page').status_code==404
    assert 'noindex,nofollow' in client.get('/setup').text
    en=client.get('/api/catalog?lang=en').json()
    assert en['brand']['primary'].lower()=='#834dfb'
    assert len(en['services'])==11
    assert en['services'][0]['name']=='WhatsApp sales management'
    print('Production checks passed: pages, assets, database health, security headers, private routes, worker authorization, 404 and English catalog.')
    print('Service request intake enabled:',en['request_ready'])
