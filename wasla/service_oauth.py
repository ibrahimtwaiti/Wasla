"""Instagram Login only. OAuth state is single-use and bound to tenant, user and session."""
import hashlib
import json
import os
import secrets
import re
import time
from urllib.parse import urlencode
import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from .db import transaction, one, audit, now, uid, postgres
from .main import user
from .security import permit, digest
from .service_api import router, get

from .runtime_configuration import value as configured_value

SCOPES={'instagram_business_basic','instagram_business_manage_messages'}

def cipher():
    try: return Fernet(os.environ['WASLA_TOKEN_ENCRYPTION_KEY'].encode())
    except (KeyError,ValueError): raise HTTPException(503,'oauth_secret_storage_not_configured')

def unseal(value):
    try:return cipher().decrypt(value.encode()).decode()
    except (InvalidToken,UnicodeError,ValueError):raise HTTPException(503,'encrypted_data_unavailable')

def read_token(account):
    if not account.get('token_ref','').startswith('vault:'): return ''
    from .db import postgres, path
    if postgres():
        with transaction() as db: row=one(db,'SELECT ciphertext FROM service_secrets WHERE account_id=?',(account['id'],))
    else:
        import sqlite3
        with sqlite3.connect(path()) as db:
            db.row_factory=sqlite3.Row
            row=one(db,'SELECT ciphertext FROM service_secrets WHERE account_id=?',(account['id'],))
    if not row: return ''
    try: return cipher().decrypt(row['ciphertext'].encode()).decode()
    except InvalidToken: raise HTTPException(503,'oauth_secret_unavailable')

def redirect_uri():
    origin=configured_value('WASLA_OAUTH_ORIGIN',os.getenv('WASLA_ORIGIN','')).rstrip('/')
    if not origin.startswith('https://'): raise HTTPException(503,'oauth_requires_https_origin')
    return origin+'/api/v2/oauth/instagram/callback'

def consume(state,request,u):
    with transaction() as db:
        row=one(db,'SELECT s.*,a.tenant_id FROM service_oauth_states s JOIN service_accounts a ON a.id=s.account_id WHERE s.state_hash=?',(digest(state),))
        if not row or (u['role']!='provider' and row['tenant_id']!=u['tenant_id']) or row['user_id']!=u['id'] or row['session_hash']!=digest(request.cookies.get('wasla_session','')) or row['expires_at']<=int(time.time()) or row['consumed_at']:
            raise HTTPException(403,'invalid_or_expired_oauth_state')
        changed=db.execute('UPDATE service_oauth_states SET consumed_at=? WHERE state_hash=? AND consumed_at IS NULL',(int(time.time()),digest(state)))
        if changed.rowcount!=1: raise HTTPException(403,'oauth_state_already_used')
        return get(db,'service_accounts',row['account_id'],{**u,'tenant_id':row['tenant_id']})

@router.post('/api/v2/accounts/{ident}/oauth/start')
def start(ident:str,request:Request,u=Depends(user)):
    permit(u,'settings'); cipher(); callback=redirect_uri(); app_id=configured_value('INSTAGRAM_APP_ID')
    if not app_id or not configured_value('INSTAGRAM_APP_SECRET'): raise HTTPException(503,'instagram_app_not_configured')
    with transaction() as db:
        a=get(db,'service_accounts',ident,u)
        if a['channel']!='instagram': raise HTTPException(409,'instagram_login_only')
        state=secrets.token_urlsafe(32)
        db.execute('DELETE FROM service_oauth_states WHERE expires_at<?',(int(time.time())-86400,))
        db.execute('INSERT INTO service_oauth_states VALUES(?,?,?,?,?,NULL)',(digest(state),ident,u['id'],digest(request.cookies.get('wasla_session','')),int(time.time())+600))
        audit(db,u,'service.oauth.started',ident)
    return {'authorization_url':'https://www.instagram.com/oauth/authorize?'+urlencode({'client_id':app_id,'redirect_uri':callback,'response_type':'code','scope':','.join(sorted(SCOPES)),'state':state})}

@router.get('/api/v2/oauth/instagram/callback')
def landing():
    # The first cross-site redirect carries no Strict session cookie. Resume with a
    # same-origin authenticated POST; do not weaken the existing cookie policy.
    return HTMLResponse('<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>Wasla authorization</title><p id="oauth-status">جارٍ إكمال الربط / Completing authorization…</p><a href="/app/channels">العودة إلى القنوات / Back to channels</a><script src="/static/service-oauth.js" defer></script></html>',headers={'Referrer-Policy':'no-referrer'})

@router.post('/api/v2/oauth/instagram/complete')
def callback(data:dict,request:Request,u=Depends(user)):
    if u['role']!='provider':permit(u,'settings')
    state=str(data.get('state',''))
    with transaction() as db:discovery=one(db,"SELECT state_hash FROM meta_data_sessions WHERE state_hash=? AND kind='instagram'",(digest(state),))
    a=consume_discovery(state,request,u) if discovery else consume(state,request,u)
    if data.get('error'):

        with transaction() as db:audit(db,u,'service.oauth.denied',a['id'])
        return {'redirect':('/app/integrations?oauth=denied' if u['role']=='provider' else '/app/channels?oauth=denied')}
    code=data.get('code','')
    if not isinstance(code,str) or not code or len(code)>4096: raise HTTPException(400,'authorization_code_missing')
    try:
        response=ig_request(a['tenant_id'],'POST','https://api.instagram.com/oauth/access_token',data={'client_id':configured_value('INSTAGRAM_APP_ID'),'client_secret':configured_value('INSTAGRAM_APP_SECRET'),'grant_type':'authorization_code','redirect_uri':redirect_uri(),'code':code},timeout=15)
        response.raise_for_status(); result=response.json()
        if 'data' in result:
            if len(result['data'])!=1: raise ValueError()
            result=result['data'][0]
        if not re.fullmatch('[0-9]{1,40}',str(result['user_id'])):raise HTTPException(502,'invalid_instagram_identity')
        if a.get('provider_id') and str(result['user_id'])!=a['provider_id']: raise HTTPException(409,'authorized_account_mismatch')
        granted=result.get('permissions',[])
        if isinstance(granted,str):granted=granted.split(',')
        if not SCOPES.issubset(set(granted)): raise HTTPException(409,'required_permissions_not_granted')
        extended=ig_request(a['tenant_id'],'GET','https://graph.instagram.com/access_token',params={'grant_type':'ig_exchange_token','client_secret':configured_value('INSTAGRAM_APP_SECRET'),'access_token':result['access_token']},timeout=15)
        extended.raise_for_status(); long=extended.json(); expires=int(long['expires_in'])
        if expires<=0: raise ValueError()
        encrypted=cipher().encrypt(long['access_token'].encode()).decode()
    except HTTPException: raise
    except Exception: raise HTTPException(502,'oauth_exchange_failed_reconnect')
    with transaction() as db:
        # A successful authorization still requires explicit connection and live-message checks.
        from .entitlements import capacity
        from .saas import lock_tenant
        lock_tenant(db,a['tenant_id'])
        from .entitlements import subscription
        subscription(db,a['tenant_id'])
        if not one(db,'SELECT id FROM users WHERE id=? AND active=1',(u['id'],)):raise HTTPException(403,'authorization_changed')
        table='meta_data_sessions' if discovery else 'service_oauth_states'
        if not one(db,'SELECT state_hash FROM '+table+' WHERE state_hash=?',(digest(state),)):raise HTTPException(409,'meta_connection_changed')
        if not a['id']:
            # Discover the authorized asset from Meta; never trust a browser-entered ID.
            instagram_configuration(u)
            current=one(db,"SELECT * FROM service_accounts WHERE channel='instagram' AND provider_id=?",(str(result['user_id']),))
            if current and current['tenant_id']!=u['tenant_id']:raise HTTPException(409,'meta_asset_unavailable')
            if not current:
                capacity(db,u['tenant_id'],'channel','instagram');a['id']=uid()
                db.execute("INSERT INTO service_accounts(id,tenant_id,channel,provider_id,label,api_version,state,created_at) VALUES(?,?,'instagram',?,?,?,'unverified',?)",(a['id'],u['tenant_id'],str(result['user_id']),'Instagram '+str(result['user_id']),a['api_version'],now()))
            else:a['id']=current['id']
        current=get(db,'service_accounts',a['id'],{**u,'tenant_id':a['tenant_id']})
        if current['state']=='disconnected':capacity(db,a['tenant_id'],'channel','instagram')
        db.execute('INSERT INTO service_secrets VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE SET ciphertext=excluded.ciphertext,issued_at=excluded.issued_at',(a['id'],encrypted,int(time.time())))
        db.execute("UPDATE service_accounts SET token_ref=?,scopes=?,expires_at=?,state='unverified' WHERE id=?",('vault:'+a['id'],json.dumps(sorted(SCOPES)),int(time.time())+expires,a['id']))
        if discovery:db.execute("INSERT INTO meta_grants VALUES(?,?,?,?,?,'valid') ON CONFLICT(account_id) DO UPDATE SET app_id=excluded.app_id,subject_hash=excluded.subject_hash,health='valid',checked_at=excluded.checked_at",(a['id'],a['tenant_id'],configured_value('INSTAGRAM_APP_ID'),digest(str(result['user_id'])),int(time.time())))
        audit(db,u,'service.oauth.connected',a['id'])
    return {'redirect':('/app/integrations?oauth=connected' if u['role']=='provider' else '/app/channels?oauth=connected')}

@router.post('/api/v2/accounts/{ident}/oauth/refresh')
def refresh(ident:str,u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:
        a=get(db,'service_accounts',ident,u); record=one(db,'SELECT issued_at,ciphertext FROM service_secrets WHERE account_id=?',(ident,))
    clock=int(time.time())
    if a['channel']!='instagram' or not record or clock-record['issued_at']<86400 or not a['expires_at'] or a['expires_at']<=clock:
        raise HTTPException(409,'token_not_eligible_for_refresh')
    try:
        response=ig_request(a['tenant_id'],'GET','https://graph.instagram.com/refresh_access_token',params={'grant_type':'ig_refresh_token','access_token':read_token(a)},timeout=15)
        response.raise_for_status(); result=response.json(); expires=int(result['expires_in'])
        if expires<=0:raise ValueError()
        encrypted=cipher().encrypt(result['access_token'].encode()).decode()
    except Exception: raise HTTPException(502,'token_refresh_failed_reauthorize')
    with transaction() as db:
        current=get(db,'service_accounts',ident,u,True)
        if current['state']=='disconnected' or one(db,'SELECT issued_at,ciphertext FROM service_secrets WHERE account_id=?',(ident,))!=record:raise HTTPException(409,'meta_connection_changed')
        db.execute('UPDATE service_secrets SET ciphertext=?,issued_at=? WHERE account_id=?',(encrypted,clock,ident))
        db.execute('UPDATE service_accounts SET expires_at=? WHERE id=?',(clock+expires,ident));audit(db,u,'service.oauth.refreshed',ident)
    return {'expires_at':clock+expires}


def ig_request(tid,method,url,**kwargs):
    from .entitlements import consume_api
    with transaction() as db:consume_api(db,tid)
    started=time.monotonic();status=0;error=''
    try:
        response=(httpx.post if method=='POST' else httpx.get)(url,**kwargs);status=response.status_code
        if status>=400:error='instagram_request_failed'
        return response
    except httpx.HTTPError:
        error='instagram_transport_error';raise HTTPException(502,error)
    finally:
        with transaction() as db:
            if one(db,'SELECT id FROM tenants WHERE id=?',(tid,)):db.execute('INSERT INTO meta_api_usage VALUES(?,?,?,?,?,?,?)',(uid(),tid,'instagram_token_exchange_or_refresh',status,error,int((time.monotonic()-started)*1000),int(time.time())))

def instagram_configuration(u):
    permit(u,'settings')
    from .meta_onboarding import SUPPORTED_VERSIONS
    from .launch import meta_gate
    meta_gate(u['tenant_id'],'instagram')
    appid=configured_value('INSTAGRAM_APP_ID');version=configured_value('META_GRAPH_VERSION');callback=redirect_uri()
    if not appid or not configured_value('INSTAGRAM_APP_SECRET') or version not in SUPPORTED_VERSIONS:raise HTTPException(503,'instagram_app_not_configured')
    cipher();return appid,version,callback

@router.get('/api/v2/meta/instagram')
def instagram_readiness(u=Depends(user)):
    permit(u,'settings')
    try:instagram_configuration(u);available=True
    except HTTPException:available=False
    return {'signup_available':available,'live_verified':False}

@router.post('/api/v2/meta/instagram/start')
def start_discovery(request:Request,u=Depends(user)):
    from .meta_onboarding import throttle
    appid,version,callback=instagram_configuration(u);throttle(u['tenant_id']);state=secrets.token_urlsafe(32)
    with transaction() as db:
        db.execute('DELETE FROM meta_data_sessions WHERE expires_at<?',(int(time.time()),))
        db.execute('INSERT INTO meta_data_sessions(state_hash,tenant_id,user_id,session_hash,kind,app_id,config_id,api_version,expires_at) VALUES(?,?,?,?,?,?,?,?,?)',(digest(state),u['tenant_id'],u['id'],digest(request.cookies.get('wasla_session','')),'instagram',appid,callback,version,int(time.time())+600))
    return {'authorization_url':'https://www.instagram.com/oauth/authorize?'+urlencode({'client_id':appid,'redirect_uri':callback,'response_type':'code','scope':','.join(sorted(SCOPES)),'state':state,'force_reauth':'true'})}

def consume_discovery(state,request,u):
    appid,version,callback=instagram_configuration(u)
    with transaction() as db:
        from .saas import lock_tenant
        lock_tenant(db,u['tenant_id'])
        row=one(db,'SELECT * FROM meta_data_sessions WHERE state_hash=?'+(' FOR UPDATE' if postgres() else ''),(digest(state),))
        if not row or row['kind']!='instagram' or row['user_id']!=u['id'] or row['tenant_id']!=u['tenant_id'] or row['session_hash']!=digest(request.cookies.get('wasla_session','')) or row['expires_at']<=int(time.time()) or row['stage']!='started':raise HTTPException(403,'invalid_or_expired_oauth_state')
        if (row['app_id'],row['api_version'],row['config_id'])!=(appid,version,callback):raise HTTPException(409,'meta_configuration_changed')
        db.execute("UPDATE meta_data_sessions SET stage='consumed' WHERE state_hash=?",(digest(state),))
    return {'id':'','tenant_id':u['tenant_id'],'api_version':version}
