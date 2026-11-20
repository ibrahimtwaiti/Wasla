"""Opt-in drill against the dedicated protected Wasla preview, with a loopback SMTP inbox.

Run as a module from the repository with the extra test dependency aiosmtpd==1.4.6.
The runner needs a temporary trusted localhost TLS certificate and dedicated staging secrets.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import ssl
import time
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from urllib.parse import urlsplit

import httpx
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult
import browser_journey
from staging_database import exclusive_drill
from wasla import email_verification, privacy
from wasla.db import one, now, transaction, uid
from wasla.service_oauth import cipher

ORIGIN = 'https://wasla-git-feat-meta-provider-readiness-al-habib-center.vercel.app'
DATABASE_HOST = 'ep-holy-breeze-b98rfxye.c-7.eu-central-1.aws.neon.tech'
MAIL_PORT = 18025


def guard():
    uri = urlsplit(os.environ['DATABASE_URL'])
    grant = urlsplit(os.environ['STAGING_ACCESS_URL'])
    if (
        os.environ.get('WASLA_ENV') != 'staging'
        or os.environ.get('WASLA_ORIGIN') != ORIGIN
        or grant.scheme != 'https'
        or grant.netloc != urlsplit(ORIGIN).netloc
        or uri.hostname != DATABASE_HOST
        or uri.path != '/wasla_staging'
        or os.environ.get('WASLA_MEDIA_BUCKET') != 'wasla-v2-staging-media'
    ):
        raise RuntimeError('Dedicated staging bindings required')
    with transaction() as db:
        if db.execute(
            'SELECT EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM tenants) OR EXISTS (SELECT 1 FROM service_accounts)'
        ).fetchone()[0]:
            raise RuntimeError('Run only against an empty staging workspace inventory')
        if db.execute('SELECT current_database()').fetchone()[0] != 'wasla_staging':
            raise RuntimeError('Unexpected staging database')


def expect(condition, step):
    if not condition:
        raise RuntimeError(step)
    print('PASS', step, flush=True)


@contextmanager
def temporary_settings(values):
    encrypted = {k: cipher().encrypt(v.encode()).decode() for k, v in values.items()}
    with transaction() as db:
        for key, secret in encrypted.items():
            # A collision rejects the entire transaction, preserving operator settings.
            db.execute('INSERT INTO provider_configuration VALUES(?,?,?)', (key, secret, now()))
    try:
        yield
    finally:
        with transaction() as db:
            for key, secret in encrypted.items():
                db.execute(
                    'DELETE FROM provider_configuration WHERE key=? AND ciphertext=?', (key, secret)
                )


class Inbox:
    def __init__(self, recipient):
        self.recipient = recipient
        self.message = None

    async def handle_DATA(self, server, session, envelope):
        if not session.ssl or not session.authenticated or envelope.rcpt_tos != [self.recipient]:
            return '550 Private test inbox only'
        self.message = BytesParser(policy=policy.default).parsebytes(envelope.content)
        return '250 Accepted'


def request(http, method, path, status=200, **kwargs):
    response = http.request(method, path, **kwargs)
    if response.status_code != status:
        # Never print query strings, tokens, message bodies or response headers.
        raise RuntimeError(
            f'{method} {urlsplit(path).path}: expected {status}, received {response.status_code}'
        )
    return response


def drill():
    guard()
    address = 'qa-' + secrets.token_hex(8) + '@example.invalid'
    password = secrets.token_urlsafe(32)
    mail_password = secrets.token_urlsafe(32)
    verify_secret, signing_secret = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    asset, plan, tid = 'fixture-' + uid(), uid(), None
    inbox = Inbox(address)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(os.environ['STAGING_TLS_CERT'], os.environ['STAGING_TLS_KEY'])

    def authenticate(server, session, envelope, mechanism, auth):
        valid = getattr(auth, 'login', b'') == b'wasla-drill' and hmac.compare_digest(
            getattr(auth, 'password', b''), mail_password.encode()
        )
        return AuthResult(success=valid, handled=False)

    smtp = Controller(
        inbox,
        hostname='127.0.0.1',
        port=MAIL_PORT,
        tls_context=tls,
        require_starttls=True,
        auth_required=True,
        authenticator=authenticate,
    )
    values = {
        'WASLA_SELF_SIGNUP_MODE': 'test',
        'WASLA_EMAIL_MODE': 'smtp',
        'WASLA_EMAIL_ORIGIN': ORIGIN,
        'WASLA_SMTP_HOST': 'localhost',
        'WASLA_SMTP_PORT': str(MAIL_PORT),
        'WASLA_SMTP_USER': 'wasla-drill',
        'WASLA_SMTP_PASSWORD': mail_password,
        'WASLA_SMTP_FROM': 'verification@example.invalid',
        'META_VERIFY_TOKEN': verify_secret,
        'META_APP_SECRET': signing_secret,
        'WASLA_MEDIA_SCANNER_URL': os.environ['STAGING_SCANNER_URL'],
        'WASLA_MEDIA_SCANNER_TOKEN': os.environ['STAGING_SCANNER_TOKEN'],
    }
    smtp.start()
    try:
        browser = browser_journey.browser_session(
            os.getenv('STAGING_BROWSER'), os.environ['STAGING_ACCESS_URL']
        )
        with temporary_settings(values), browser as page, httpx.Client(
            base_url=ORIGIN, timeout=70, headers={'Origin': ORIGIN}
        ) as http:
            # The immutable preview keeps Vercel Authentication enabled.
            response = http.get(os.environ['STAGING_ACCESS_URL'])
            expect(
                response.status_code in (200, 302, 307) and bool(http.cookies),
                'protected preview session',
            )
            with transaction() as db:
                db.execute(
                    "INSERT INTO service_plans VALUES(?,?,'SAR',0,20,1,?)",
                    (plan, 'Staging verification', now()),
                )
            result = request(http, 'GET', '/api/public/plans').json()
            expect(
                result['signup_enabled'] and any(p['id'] == plan for p in result['plans']),
                'available trial plan',
            )
            data = {
                'company': 'Synthetic staging store',
                'name': 'QA Owner',
                'email': address,
                'password': password,
                'plan_id': plan,
                'terms_version': result['terms_version'],
            }
            if page:
                browser_journey.sign_up(page, ORIGIN, data)
            else:
                request(http, 'POST', '/api/auth/register', 202, json=data)
            with transaction() as db:
                user = one(db, 'SELECT id,tenant_id,active FROM users WHERE email=?', (address,))
                expect(user and not user['active'], 'created isolated workspace and inactive owner')
                tid = user['tenant_id']
            request(
                http, 'POST', '/api/auth/login', 401, json={'email': address, 'password': password}
            )
            expect(
                email_verification.process_email() and inbox.message is not None,
                'worker delivered authenticated STARTTLS mail',
            )
            message = inbox.message.get_body(preferencelist=('plain',)).get_content()
            verification = next(
                line for line in message.splitlines() if line.startswith(ORIGIN + '/verify-email#')
            )
            token = urlsplit(verification).fragment
            request(http, 'GET', '/verify-email')
            if page:
                browser_journey.activate_and_login(page, ORIGIN, verification, address, password)
            else:
                request(http, 'POST', '/api/auth/verification/complete', json={'token': token})
            request(http, 'POST', '/api/auth/verification/complete', 409, json={'token': token})
            login = request(
                http, 'POST', '/api/auth/login', json={'email': address, 'password': password}
            )
            expect(
                'secure' in login.headers['set-cookie'].lower()
                and 'httponly' in login.headers['set-cookie'].lower(),
                'verified login and secure session',
            )
            me = request(http, 'GET', '/api/me').json()
            expect(me['tenant_id'] == tid, 'session belongs to new workspace')
            http.headers['X-CSRF-Token'] = me['csrf']
            request(http, 'GET', '/app')
            request(http, 'POST', '/api/v2/billing/plan', json={'plan_id': plan, 'status': 'trial'})
            billing = request(http, 'GET', '/api/v2/billing').json()
            expect(
                billing['subscription']['plan_id'] == plan
                and billing['payment_collection_enabled'] is False,
                'trial and billing API',
            )
            request(http, 'GET', '/app/channels')
            expect(
                not request(http, 'GET', '/api/v2/accounts').json()['accounts'],
                'no prelinked customer channels',
            )
            gate = request(http, 'POST', '/api/v2/meta/whatsapp/start', 409, json={}).json()
            expect(
                gate['detail'] == 'meta_signup_pending_provider_configuration_and_approval',
                'official signup gate awaiting Meta',
            )
            # Explicit synthetic ownership fixture. No provider credentials or OAuth result are invented.
            with transaction() as db:
                aid = uid()
                db.execute(
                    "INSERT INTO service_accounts(id,tenant_id,channel,provider_id,label,api_version,state,created_at) VALUES(?,?,'whatsapp',?,'Synthetic event fixture','v26.0','verified',?)",
                    (aid, tid, asset, now()),
                )
            expect(
                request(http, 'GET', '/api/v2/accounts').json()['accounts'][0]['id'] == aid,
                'dashboard API lists scoped synthetic channel',
            )
            endpoint = '/api/webhooks/v2/whatsapp'
            q = {
                'hub.mode': 'subscribe',
                'hub.challenge': 'wasla-staging-challenge',
                'hub.verify_token': verify_secret,
            }
            expect(
                request(http, 'GET', endpoint, params=q).text == q['hub.challenge'],
                'HTTPS webhook challenge',
            )
            msg = {
                'id': uid(),
                'from': '15550100000',
                'timestamp': str(int(time.time())),
                'type': 'text',
                'text': {'body': 'Wasla staging message'},
            }
            raw = json.dumps(
                {
                    'object': 'whatsapp_business_account',
                    'entry': [
                        {
                            'changes': [
                                {
                                    'value': {
                                        'metadata': {'phone_number_id': asset},
                                        'messages': [msg],
                                    }
                                }
                            ]
                        }
                    ],
                }
            ).encode()
            sig = 'sha256=' + hmac.new(signing_secret.encode(), raw, hashlib.sha256).hexdigest()
            request(
                http,
                'POST',
                endpoint,
                401,
                content=raw,
                headers={'Content-Type': 'application/json'},
            )
            for _ in range(2):
                request(
                    http,
                    'POST',
                    endpoint,
                    content=raw,
                    headers={'Content-Type': 'application/json', 'X-Hub-Signature-256': sig},
                )
            worker_headers = {'Authorization': 'Bearer ' + os.environ['WASLA_WORKER_SECRET']}
            request(http, 'POST', '/api/internal/drain-webhooks', headers=worker_headers, json={})
            tickets = request(http, 'GET', '/api/v2/tickets').json()
            expect(len(tickets) == 1, 'signed delivery and duplicate produced one ticket')
            ticket = tickets[0]['id']
            path = '/api/v2/tickets/' + ticket
            expect(
                len(request(http, 'GET', path).json()['messages']) == 1,
                'one persisted incoming message',
            )
            request(http, 'GET', '/api/v2/meta/usage')
            request(http, 'GET', '/api/v2/reports')
            media = {
                'mime_type': 'application/pdf',
                'base64': base64.b64encode(b'%PDF-1.4\n% Wasla staging fixture\n%%EOF').decode(),
            }
            attachment = request(http, 'POST', path + '/attachments', 201, json=media).json()
            expect(
                attachment['scan_state'] == 'clean', 'hosted upload passed the live ClamAV gateway'
            )
            download = request(
                http, 'GET', '/api/v2/attachments/' + attachment['id'] + '/download'
            ).json()
            signed = download['url']
            expect(
                urlsplit(signed).hostname.endswith('.r2.cloudflarestorage.com'),
                'private R2 signed download',
            )
            with httpx.Client(timeout=20) as storage:
                expect(
                    storage.get(signed).content == base64.b64decode(media['base64']),
                    'downloaded original attachment bytes',
                )
            with transaction() as db:
                db.execute(
                    "UPDATE service_tickets SET status='closed',updated_at=? WHERE id=? AND tenant_id=?",
                    (int(time.time()) - 40 * 86400, ticket, tid),
                )
            request(
                http,
                'PUT',
                '/api/v2/retention',
                json={
                    'days': 30,
                    'enabled': True,
                    'evidence': 'Disposable Wasla QA retention drill',
                },
            )
            request(http, 'POST', path + '/hold', json={'reason': 'Synthetic retention review'})
            expect(
                not request(http, 'POST', '/api/v2/retention/run', json={}).json()[
                    'redacted_tickets'
                ],
                'hold protected due ticket',
            )
            request(
                http,
                'POST',
                path + '/hold',
                json={'reason': 'Synthetic hold release', 'release': True},
            )
            expect(
                request(http, 'POST', '/api/v2/retention/run', json={}).json()['redacted_tickets']
                == 1,
                'released hold permitted redaction',
            )
            with httpx.Client(timeout=20) as storage:
                expect(
                    storage.get(signed).status_code == 404,
                    'retention removed the private R2 object',
                )
            request(http, 'POST', '/api/v2/accounts/' + aid + '/disconnect', json={})
            expect(
                request(http, 'GET', '/api/v2/accounts').json()['accounts'][0]['state']
                == 'disconnected',
                'local fixture disconnected',
            )
            if page:
                deletion = {'id': browser_journey.delete_workspace(page, ORIGIN, password)}
            else:
                deletion = request(
                    http,
                    'POST',
                    '/api/v2/privacy/deletions',
                    202,
                    json={'scope': 'tenant', 'password': password, 'confirm': 'DELETE'},
                ).json()
            for _ in range(3):
                request(
                    http, 'POST', '/api/internal/drain-webhooks', headers=worker_headers, json={}
                )
                result = request(http, 'GET', '/api/privacy/deletions/' + deletion['id']).json()
                if result['state'] == 'completed':
                    break
            expect(result['state'] == 'completed', 'durable workspace deletion completed')
            request(http, 'GET', '/api/me', 401)
            if page:
                page.locator('#status-refresh').click()
                expect(
                    page.request.get(ORIGIN + '/api/me').status == 401,
                    'browser session invalidated after erasure',
                )
            with transaction() as db:
                for table in [
                    'users',
                    'service_accounts',
                    'service_tickets',
                    'service_messages',
                    'service_media',
                    'service_subscriptions',
                ]:
                    expect(
                        not one(db, 'SELECT 1 FROM ' + table + ' WHERE tenant_id=?', (tid,)),
                        'erased ' + table,
                    )
                expect(not one(db, 'SELECT 1 FROM tenants WHERE id=?', (tid,)), 'erased workspace')
    finally:
        smtp.stop()
        # Cleanup touches only this uniquely identified synthetic registration, even on failed assertions.
        with transaction() as db:
            user = one(db, 'SELECT id,tenant_id FROM users WHERE email=?', (address,))
            if user:
                pending = one(
                    db,
                    "SELECT id FROM deletion_jobs WHERE tenant_id=? AND scope='tenant' AND state!='completed'",
                    (user['tenant_id'],),
                )
                job = (
                    pending['id']
                    if pending
                    else privacy.queue(db, user['tenant_id'], user['id'], 'tenant')
                )
        if user:
            for _ in range(12):
                if not privacy.process_deletion(job):
                    break
        with transaction() as db:
            db.execute(
                'DELETE FROM service_plans WHERE id=? AND NOT EXISTS (SELECT 1 FROM service_subscriptions WHERE plan_id=?)',
                (plan, plan),
            )


if __name__ == '__main__':
    with exclusive_drill():
        drill()
