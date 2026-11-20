"""Public callback-only entry point for the independent Wasla Staging preview."""
import os
import re
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import MutableHeaders

# Import the application once so its shared routers have completed registration.
from wasla.main import meta_data, meta_lifecycle, privacy
from wasla import service_api


STAGING_HOST = 'ep-holy-breeze-b98rfxye.c-7.eu-central-1.aws.neon.tech'
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)


def staging_bound():
    db = urlsplit(os.getenv('DATABASE_URL', ''))
    return (os.getenv('WASLA_ENV') == 'staging'
            and os.getenv('VERCEL_ENV') == 'preview'
            and db.scheme in {'postgres', 'postgresql'}
            and db.hostname in {STAGING_HOST, STAGING_HOST.replace('.c-7.', '-pooler.c-7.')}
            and db.path == '/wasla_staging')


class CallbackBoundary:
    """Limit the body before dispatch, including clients using chunked delivery."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        async def private_response(message):
            if message['type'] == 'http.response.start':
                headers = MutableHeaders(scope=message)
                headers['Cache-Control'] = 'no-store'
                headers['Referrer-Policy'] = 'no-referrer'
                headers['X-Content-Type-Options'] = 'nosniff'
                headers['X-Robots-Tag'] = 'noindex'
            await send(message)
        if not staging_bound():
            return await Response(status_code=503)(scope, receive, private_response)
        if scope['method'] != 'POST':
            return await self.app(scope, receive, private_response)
        body = bytearray()
        async for part in Request(scope, receive).stream():
            if len(body) + len(part) > 65536:
                return await Response(status_code=413)(scope, receive, private_response)
            body.extend(part)

        async def buffered_receive():
            nonlocal body
            if body is None:
                return await receive()
            data, body = bytes(body), None
            return {'type': 'http.request', 'body': data, 'more_body': False}
        return await self.app(scope, buffered_receive, private_response)


app.add_middleware(CallbackBoundary)
# These are the same application handlers that validate the challenge, HMAC,
# tenant grant and idempotency. Mounting the full customer router here is unsafe.
app.add_api_route('/api/webhooks/v2/{channel}', service_api.verify, methods=['GET'])
app.add_api_route('/api/webhooks/v2/{channel}', service_api.receive, methods=['POST'])
app.add_api_route('/api/webhooks/meta/leads', meta_data.verify_leads, methods=['GET'])
app.add_api_route('/api/webhooks/meta/leads', meta_data.receive_leads, methods=['POST'])
for product in ('meta', 'instagram'):
    app.add_api_route('/api/webhooks/'+product+'/deauthorize', meta_lifecycle.deauthorize, methods=['POST'])
    app.add_api_route('/api/webhooks/'+product+'/data-deletion', privacy.meta_delete, methods=['POST'])


@app.get('/deletion-status', response_class=HTMLResponse)
def deletion_status(request: str = ''):
    if not re.fullmatch('[a-f0-9]{32}', request):
        raise HTTPException(404)
    state = privacy.deletion_status(request)['state']
    labels = {'completed': 'اكتمل الحذف', 'blocked_hold': 'الطلب قيد مراجعة الاحتفاظ',
              'failed': 'يتطلب الطلب مراجعة', 'retry': 'ستعاد محاولة معالجة الطلب'}
    message = labels.get(state, 'الطلب قيد المعالجة')
    page = '<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><title>وصلة | حالة الحذف</title><main><h1>حالة طلب الحذف</h1><p>'+message+'</p></main></html>'
    return HTMLResponse(page, headers={'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'"})
