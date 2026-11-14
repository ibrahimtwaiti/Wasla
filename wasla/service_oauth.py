"""Instagram Login only. OAuth state is single-use and bound to tenant, user and session."""
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode
import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from .db import transaction, one, audit, now
from .main import user
from .security import permit, digest
from .service_api import router, get

SCOPES={'instagram_business_basic','instagram_business_manage_messages'}

def cipher():
    try: return Fernet(os.environ['WASLA_TOKEN_ENCRYPTION_KEY'].encode())
    except (KeyError,ValueError): raise HTTPException(503,'oauth_secret_storage_not_configured')

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
    origin=os.getenv('WASLA_ORIGIN','').rstrip('/')
    if not origin.startswith('https://'): raise HTTPException(503,'oauth_requires_https_origin')
    return origin+'/api/v2/oauth/instagram/callback'

def consume(state,request,u):
    with transaction() as db:
        row=one(db,'SELECT s.*,a.tenant_id FROM service_oauth_states s JOIN service_accounts a ON a.id=s.account_id WHERE s.state_hash=?',(digest(state),))
        if not row or row['tenant_id']!=u['tenant_id'] or row['user_id']!=u['id'] or row['session_hash']!=digest(request.cookies.get('wasla_session','')) or row['expires_at']<=int(time.time()) or row['consumed_at']:
            raise HTTPException(403,'invalid_or_expired_oauth_state')
        changed=db.execute('UPDATE service_oauth_states SET consumed_at=? WHERE state_hash=? AND consumed_at IS NULL',(int(time.time()),digest(state)))
        if changed.rowcount!=1: raise HTTPException(403,'oauth_state_already_used')
        return get(db,'service_accounts',row['account_id'],u)

@router.post('/api/v2/accounts/{ident}/oauth/start')
def start(ident:str,request:Request,u=Depends(user)):
    permit(u,'settings'); cipher(); callback=redirect_uri(); app_id=os.getenv('INSTAGRAM_APP_ID','')
    if not app_id or not os.getenv('INSTAGRAM_APP_SECRET'): raise HTTPException(503,'instagram_app_not_configured')
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
    permit(u,'settings'); a=consume(str(data.get('state','')),request,u)
    if data.get('error'):

        with transaction() as db:audit(db,u,'service.oauth.denied',a['id'])
        return {'redirect':'/app/channels?oauth=denied'}
    code=data.get('code','')
    if not isinstance(code,str) or not code or len(code)>4096: raise HTTPException(400,'authorization_code_missing')
    try:
        response=httpx.post('https://api.instagram.com/oauth/access_token',data={'client_id':os.environ['INSTAGRAM_APP_ID'],'client_secret':os.environ['INSTAGRAM_APP_SECRET'],'grant_type':'authorization_code','redirect_uri':redirect_uri(),'code':code},timeout=15)
        response.raise_for_status(); result=response.json()
        if 'data' in result:
            if len(result['data'])!=1: raise ValueError()
            result=result['data'][0]
        if str(result['user_id'])!=a['provider_id']: raise HTTPException(409,'authorized_account_mismatch')
        granted=result.get('permissions',[])
        if isinstance(granted,str):granted=granted.split(',')
        if not SCOPES.issubset(set(granted)): raise HTTPException(409,'required_permissions_not_granted')
        extended=httpx.get('https://graph.instagram.com/access_token',params={'grant_type':'ig_exchange_token','client_secret':os.environ['INSTAGRAM_APP_SECRET'],'access_token':result['access_token']},timeout=15)
        extended.raise_for_status(); long=extended.json(); expires=int(long['expires_in'])
        if expires<=0: raise ValueError()
        encrypted=cipher().encrypt(long['access_token'].encode()).decode()
    except HTTPException: raise
    except Exception: raise HTTPException(502,'oauth_exchange_failed_reconnect')
    with transaction() as db:
        # A successful authorization still requires explicit connection and live-message checks.
        db.execute('INSERT INTO service_secrets VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE SET ciphertext=excluded.ciphertext,issued_at=excluded.issued_at',(a['id'],encrypted,int(time.time())))
        db.execute("UPDATE service_accounts SET token_ref=?,scopes=?,expires_at=?,state='unverified' WHERE id=?",('vault:'+a['id'],json.dumps(sorted(SCOPES)),int(time.time())+expires,a['id']))
        audit(db,u,'service.oauth.connected',a['id'])
    return {'redirect':'/app/channels?oauth=connected'}

@router.post('/api/v2/accounts/{ident}/oauth/refresh')
def refresh(ident:str,u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:
        a=get(db,'service_accounts',ident,u); record=one(db,'SELECT issued_at FROM service_secrets WHERE account_id=?',(ident,))
    clock=int(time.time())
    if a['channel']!='instagram' or not record or clock-record['issued_at']<86400 or not a['expires_at'] or a['expires_at']<=clock:
        raise HTTPException(409,'token_not_eligible_for_refresh')
    try:
        response=httpx.get('https://graph.instagram.com/refresh_access_token',params={'grant_type':'ig_refresh_token','access_token':read_token(a)},timeout=15)
        response.raise_for_status(); result=response.json(); expires=int(result['expires_in'])
        if expires<=0:raise ValueError()
        encrypted=cipher().encrypt(result['access_token'].encode()).decode()
    except Exception: raise HTTPException(502,'token_refresh_failed_reauthorize')
    with transaction() as db:
        db.execute('UPDATE service_secrets SET ciphertext=?,issued_at=? WHERE account_id=?',(encrypted,clock,ident))
        db.execute('UPDATE service_accounts SET expires_at=? WHERE id=?',(clock+expires,ident));audit(db,u,'service.oauth.refreshed',ident)
    return {'expires_at':clock+expires}
