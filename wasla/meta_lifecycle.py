"""Revocable app grants. No automatic WhatsApp token refresh is assumed."""
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs
from fastapi import Depends, HTTPException, Request
from .db import transaction, one, rows, audit, now, postgres
from .main import user
from .security import permit, digest
from .runtime_configuration import value
from .service_api import router, get
from .meta_onboarding import configuration, graph, validate_token, throttle


def disconnect_local(db, ident, reason):
    """Erase credentials and block queued work atomically; in-flight sends may finish."""
    db.execute("UPDATE service_accounts SET state='disconnected',token_ref='',checked_at=? WHERE id=?",(now(),ident))
    db.execute("UPDATE meta_connections SET subscription_state='disconnected',registration_state='pending' WHERE account_id=?",(ident,))
    db.execute("UPDATE meta_grants SET health='revoked',checked_at=? WHERE account_id=?",(int(time.time()),ident))
    db.execute("UPDATE meta_page_connections SET subscription_state='disconnected' WHERE account_id=?",(ident,))
    db.execute("UPDATE meta_page_posts SET state='blocked',error=? WHERE account_id=? AND state IN ('queued','retry')",(reason,ident))
    db.execute('DELETE FROM service_secrets WHERE account_id=?',(ident,))
    db.execute('DELETE FROM service_oauth_states WHERE account_id=?',(ident,))
    db.execute("UPDATE service_outbox SET state='blocked',error=? WHERE state IN ('queued','retry') AND message_id IN (SELECT id FROM service_messages WHERE account_id=?)",(reason,ident))
    db.execute("UPDATE service_messages SET status='blocked' WHERE account_id=? AND id IN (SELECT message_id FROM service_outbox WHERE state='blocked' AND error=?)",(ident,reason))


@router.post('/api/v2/meta/accounts/{ident}/token-health',tags=['Meta integrations'])
def token_health(ident:str,u=Depends(user)):
    permit(u,'settings');throttle(u['tenant_id'])
    with transaction() as db:
        a=get(db,'service_accounts',ident,u)
        binding=one(db,'SELECT * FROM meta_connections WHERE account_id=?',(ident,))
        snapshot=one(db,'SELECT ciphertext FROM service_secrets WHERE account_id=?',(ident,))
    if not binding or not snapshot or a['state']=='disconnected':raise HTTPException(409,'meta_reauthorization_required')
    c=configuration()
    if not c['META_APP_ID'] or not c['META_SYSTEM_USER_TOKEN']:raise HTTPException(503,'meta_inspection_configuration_missing')
    from .service_oauth import cipher
    credential=cipher().decrypt(snapshot['ciphertext'].encode()).decode()
    info=graph(u['tenant_id'],a['api_version'],'token_health','debug_token',token=c['META_SYSTEM_USER_TOKEN'],params={'input_token':credential}).get('data',{})
    expires=None;health='valid'
    try:expires=validate_token(info,c['META_APP_ID'],binding['business_account_id'])
    except HTTPException as error:
        # A malformed inspection or transient provider outage must not revoke a valid grant.
        if error.status_code!=409:raise
        health='reauthorize'
    with transaction() as db:
        current=get(db,'service_accounts',ident,u,True)
        saved=one(db,'SELECT ciphertext FROM service_secrets WHERE account_id=?',(ident,))
        if current['state']=='disconnected' or saved!=snapshot:raise HTTPException(409,'meta_connection_changed_retry')
        db.execute('UPDATE meta_grants SET health=?,checked_at=? WHERE account_id=?',(health,int(time.time()),ident))
        if health!='valid':
            disconnect_local(db,ident,'meta_grant_invalid')
            db.execute("UPDATE service_accounts SET state='reauthorize' WHERE id=?",(ident,))
            db.execute("UPDATE meta_grants SET health='reauthorize' WHERE account_id=?",(ident,))
        else:db.execute('UPDATE service_accounts SET expires_at=?,checked_at=? WHERE id=?',(expires,now(),ident))
        audit(db,u,'meta.token.checked',ident,health)
    return {'health':health,'expires_at':expires,'refresh_supported':False,'next_action':'none' if health=='valid' else 'embedded_signup'}


def verify_signed_request(raw,secret_key='META_APP_SECRET'):
    secret=value(secret_key)
    if not secret:raise HTTPException(503,'meta_callback_not_configured')
    try:
        signature,encoded=raw.split('.')
        def decode(s):return base64.b64decode(s+'='*((-len(s))%4),altchars=b'-_',validate=True)
        expected=hmac.new(secret.encode(),encoded.encode('ascii'),hashlib.sha256).digest()
        if not hmac.compare_digest(decode(signature),expected):raise ValueError()
        data=json.loads(decode(encoded))
        if not isinstance(data,dict) or data.get('algorithm','').upper()!='HMAC-SHA256':raise ValueError()
        subject=data.get('user_id')
        if not isinstance(subject,(str,int)) or not str(subject).isdigit() or len(str(subject))>40:raise ValueError()
        # Signed requests can be retried. Do not reject legitimate old deliveries.
        if type(data.get('issued_at')) is not int or data['issued_at']>int(time.time())+300:raise ValueError()
        return str(subject),digest(encoded)
    except (ValueError,TypeError,UnicodeError,AttributeError):raise HTTPException(403,'invalid_meta_signed_request')


@router.post('/api/webhooks/instagram/deauthorize',tags=['Meta lifecycle callbacks'])
@router.post('/api/webhooks/meta/deauthorize',tags=['Meta lifecycle callbacks'])
async def deauthorize(request:Request):
    if request.headers.get('content-type','').split(';')[0]!='application/x-www-form-urlencoded':raise HTTPException(415,'form_required')
    body=await request.body()
    if len(body)>16384:raise HTTPException(413,'callback_too_large')
    try:
        values=parse_qs(body.decode('ascii'),strict_parsing=True)
        if len(values.get('signed_request',[]))!=1:raise ValueError()
        subject,event=verify_signed_request(values['signed_request'][0],'INSTAGRAM_APP_SECRET' if '/instagram/' in request.url.path else 'META_APP_SECRET')
    except (ValueError,UnicodeError):raise HTTPException(400,'invalid_callback_form')
    app_id=value('INSTAGRAM_APP_ID' if '/instagram/' in request.url.path else 'META_APP_ID')
    if not app_id:raise HTTPException(503,'meta_callback_not_configured')
    event=digest(app_id+':'+event)
    with transaction() as db:
        if db.execute("INSERT INTO meta_callback_receipts VALUES(?,'deauthorize',?) ON CONFLICT(event_hash) DO NOTHING",(event,int(time.time()))).rowcount:
            accounts=rows(db,'SELECT account_id FROM meta_grants WHERE app_id=? AND subject_hash=?',(app_id,digest(subject)))
            for a in accounts:
                # Same lock used by reauthorization/health updates to prevent resurrection.
                one(db,'SELECT id FROM service_accounts WHERE id=?'+(' FOR UPDATE' if postgres() else ''),(a['account_id'],))
                grant=one(db,'SELECT * FROM meta_grants WHERE account_id=?',(a['account_id'],))
                if grant['app_id']==app_id and grant['subject_hash']==digest(subject):disconnect_local(db,a['account_id'],'meta_deauthorized')
            db.execute("UPDATE meta_data_connections SET ciphertext='',state='disconnected' WHERE app_id=? AND subject_hash=?",(app_id,digest(subject)))
    return {'ok':True}
