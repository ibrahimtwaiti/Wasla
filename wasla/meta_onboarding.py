"""Tenant-bound WhatsApp Embedded Signup. Closed to public launch pending review.

Only standard Cloud API onboarding is implemented; coexistence is deliberately
excluded. OAuth completion verifies assets before creating any account mapping.
"""
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import httpx
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from .db import transaction, one, rows, now, uid, audit, postgres
from .main import user
from .security import permit, digest, limit
from .service_api import router, get
from .service_oauth import cipher
from .runtime_configuration import value

SCOPES = {'whatsapp_business_management', 'whatsapp_business_messaging'}
SUPPORTED_VERSIONS = {'v25.0', 'v26.0'}

class MetaLogFilter(logging.Filter):
    def filter(self, record):
        # HTTPX logs full URLs at INFO. Never let OAuth query credentials escape.
        message = record.getMessage()
        if 'graph.facebook.com' in message or 'graph.instagram.com' in message:
            record.msg = re.sub(r'(https://graph\.(?:facebook|instagram)\.com/[^\s?"\']*)\?[^\s"\']+', r'\1?[REDACTED]', message)
            record.args = ()
        return True

logging.getLogger('httpx').addFilter(MetaLogFilter())

class CompleteSignup(BaseModel):
    model_config = ConfigDict(extra='forbid')
    state: str = Field(min_length=32, max_length=128)
    code: SecretStr
    business_account_id: str = Field(pattern=r'^[0-9]{1,40}$')
    phone_number_id: str = Field(pattern=r'^[0-9]{1,40}$')

class RegisterPhone(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pin: SecretStr


def configuration():
    keys = ['META_APP_ID','META_APP_SECRET','META_SIGNUP_CONFIG_ID','META_GRAPH_VERSION','META_SYSTEM_USER_TOKEN','META_VERIFY_TOKEN','WASLA_OAUTH_ORIGIN']
    return {k: value(k) for k in keys}


def enabled(u):
    permit(u,'settings')
    if not u.get('tenant_id'): raise HTTPException(409,'customer_workspace_required')
    from .launch import meta_gate
    meta_gate(u['tenant_id'],'whatsapp')
    config = configuration()
    if not all(config.values()) or config['META_GRAPH_VERSION'] not in SUPPORTED_VERSIONS or not config['WASLA_OAUTH_ORIGIN'].startswith('https://'):
        raise HTTPException(503,'meta_signup_configuration_incomplete')
    cipher()
    return config


def throttle(tenant_id):
    # Commit counters even when the subsequent external operation fails.
    with transaction() as db: limit(db,'meta-onboarding:'+tenant_id,30,600)


def graph(tenant_id, version, operation, path, token='', method='GET', params=None, payload=None):
    """Fixed host, bounded timeout, redacted error and per-attempt accounting.

    No automatic mutation retry: a timeout can mean the remote write succeeded.
    Callers can reconcile registration/subscription before trying again.
    """
    if version not in SUPPORTED_VERSIONS or not re.fullmatch(r'[A-Za-z0-9_/]+',path):
        raise HTTPException(422,'invalid_meta_request')
    from .entitlements import consume_api
    with transaction() as db:consume_api(db,tenant_id)
    start=time.monotonic();status=0;error='';result=None
    try:
        response=httpx.request(method,'https://graph.facebook.com/'+version+'/'+path,
            headers={'Authorization':'Bearer '+token} if token else {}, params=params,
            json=payload, timeout=httpx.Timeout(15,connect=5), follow_redirects=False)
        status=response.status_code; result=response.json()
        if not isinstance(result,dict): raise ValueError()
        if status>=300 or 'error' in result:
            code=result.get('error',{}).get('code')
            error='meta_'+str(code) if type(code) is int else 'meta_request_failed'
    except (httpx.HTTPError,ValueError,TypeError,AttributeError): error='meta_transport_or_response_error'
    with transaction() as db:
        if one(db,'SELECT id FROM tenants WHERE id=?',(tenant_id,)):db.execute('INSERT INTO meta_api_usage VALUES(?,?,?,?,?,?,?)',(uid(),tenant_id,operation,status,error,int((time.monotonic()-start)*1000),int(time.time())))
    if error:
        raise HTTPException(429 if status==429 else 502, error)
    return result


def validate_token(info,app_id,waba):
    if not isinstance(info,dict) or not isinstance(info.get('scopes',[]),list) or not isinstance(info.get('granular_scopes',[]),list):
        raise HTTPException(502,'meta_invalid_token_inspection')
    if info.get('is_valid') is not True or str(info.get('app_id'))!=app_id:
        raise HTTPException(409,'meta_token_app_or_validity_mismatch')
    if not SCOPES.issubset({s for s in info.get('scopes',[]) if isinstance(s,str)}):
        raise HTTPException(409,'meta_required_permissions_missing')
    if any(not isinstance(g,dict) or not isinstance(g.get('scope'),str) or not isinstance(g.get('target_ids',[]),list) for g in info.get('granular_scopes',[])):
        raise HTTPException(502,'meta_invalid_token_inspection')
    grants={g.get('scope'):set(map(str,g.get('target_ids',[]))) for g in info.get('granular_scopes',[])}
    if waba not in grants.get('whatsapp_business_management',set()):
        raise HTTPException(409,'meta_asset_not_granted')
    if 'whatsapp_business_messaging' in grants and waba not in grants['whatsapp_business_messaging']:
        raise HTTPException(409,'meta_messaging_asset_not_granted')
    try: expiries=[int(info[k]) for k in ('expires_at','data_access_expires_at') if info.get(k)]
    except (ValueError,TypeError): raise HTTPException(502,'meta_invalid_token_expiry')
    expires=min(expiries) if expiries else None
    if expires and expires<=int(time.time()): raise HTTPException(409,'meta_token_expired')
    return expires


@router.get('/api/v2/meta/readiness',tags=['Meta integrations'])
def readiness(u=Depends(user)):
    permit(u,'settings')
    try: enabled(u); available=True
    except HTTPException: available=False
    with transaction() as db:
        connections=rows(db,'''SELECT m.*,a.label,a.provider_id phone_number_id,a.state,a.expires_at,
            a.last_inbound_at,a.last_outbound_at,g.health token_health,g.checked_at token_checked_at FROM meta_connections m JOIN service_accounts a ON a.id=m.account_id LEFT JOIN meta_grants g ON g.account_id=a.id
            WHERE m.tenant_id=?''',(u['tenant_id'],))
    return {'signup_available':available,'mode': value('META_SIGNUP_MODE') if available else 'disabled',
        'production_approved':False,'meta_approval_verified':False,'connections':connections,
        'services':[{'service':'whatsapp','implementation':'standard_cloud_api_test_candidate'},
                    {'service':'instagram','implementation':'existing_instagram_login_candidate'},
                    {'service':'messenger','implementation':'text_messaging_test_candidate'},{'service':'pages','implementation':'page_selection_and_profile_test_candidate'},*[{'service':s,'implementation':'read_only_oauth_sync_pending_live_verification'} for s in ['catalogs','lead_ads','marketing']]]}


@router.post('/api/v2/meta/whatsapp/start',tags=['Meta integrations'])
def start(request:Request,u=Depends(user)):
    config=enabled(u);throttle(u['tenant_id']);state=secrets.token_urlsafe(32);clock=int(time.time())
    with transaction() as db:
        db.execute('DELETE FROM meta_signup_sessions WHERE expires_at<?',(clock-86400,))
        db.execute('INSERT INTO meta_signup_sessions VALUES(?,?,?,?,?,?,?,?,NULL)',
            (digest(state),u['tenant_id'],u['id'],digest(request.cookies.get('wasla_session','')),config['META_APP_ID'],config['META_SIGNUP_CONFIG_ID'],config['META_GRAPH_VERSION'],clock+600))
        audit(db,u,'meta.signup.started',u['tenant_id'])
    return {'state':state,'app_id':config['META_APP_ID'],'config_id':config['META_SIGNUP_CONFIG_ID'],
        'api_version':config['META_GRAPH_VERSION'], 'extras':{'version':'v5','features':[{'name':'whatsapp_business_app_onboarding','enabled':False}]}}


def consume(state,request,u,config):
    clock=int(time.time())
    with transaction() as db:
        r=one(db,'SELECT * FROM meta_signup_sessions WHERE state_hash=?',(digest(state),))
        if not r or r['tenant_id']!=u['tenant_id'] or r['user_id']!=u['id'] or r['session_hash']!=digest(request.cookies.get('wasla_session','')) or r['expires_at']<=clock or r['consumed_at'] is not None:
            raise HTTPException(403,'invalid_or_expired_meta_state')
        if (r['app_id'],r['config_id'],r['api_version']) != (config['META_APP_ID'],config['META_SIGNUP_CONFIG_ID'],config['META_GRAPH_VERSION']):
            raise HTTPException(409,'meta_configuration_changed_restart')
        if db.execute('UPDATE meta_signup_sessions SET consumed_at=? WHERE state_hash=? AND consumed_at IS NULL',(clock,digest(state))).rowcount!=1:
            raise HTTPException(403,'meta_state_already_used')


@router.post('/api/v2/meta/whatsapp/complete',tags=['Meta integrations'])
def complete(data:CompleteSignup,request:Request,u=Depends(user)):
    c=enabled(u);throttle(u['tenant_id'])
    code=data.code.get_secret_value()
    if not 1<=len(code)<=4096: raise HTTPException(422,'authorization_code_required')
    consume(data.state,request,u,c)
    tid=u['tenant_id'];v=c['META_GRAPH_VERSION'];waba=data.business_account_id;phone=data.phone_number_id
    exchanged=graph(tid,v,'code_exchange','oauth/access_token',params={'client_id':c['META_APP_ID'],'client_secret':c['META_APP_SECRET'],'code':code})
    credential=exchanged.get('access_token')
    if not isinstance(credential,str) or not credential: raise HTTPException(502,'meta_exchange_missing_token')
    info=graph(tid,v,'token_inspect','debug_token',token=c['META_SYSTEM_USER_TOKEN'],params={'input_token':credential}).get('data',{})
    expires=validate_token(info,c['META_APP_ID'],waba)
    proof=hmac.new(c['META_APP_SECRET'].encode(),credential.encode(),hashlib.sha256).hexdigest()
    found=None;after=None
    # Never follow provider-supplied URLs; reuse a validated host and cursor.
    for _ in range(20):
        params={'fields':'id,verified_name','limit':100,'appsecret_proof':proof}
        if after:params['after']=after
        page=graph(tid,v,'phone_ownership',waba+'/phone_numbers',token=credential,params=params)
        found=next((p for p in page.get('data',[]) if str(p.get('id'))==phone),None)
        if found:break
        after=page.get('paging',{}).get('cursors',{}).get('after') if page.get('paging',{}).get('next') else None
        if not after:break
    if not found:raise HTTPException(409,'meta_phone_relationship_not_verified')
    encrypted=cipher().encrypt(credential.encode()).decode();aid=uid();clock=int(time.time())
    with transaction() as db:
        # Serialize a tenant's onboarding and fail closed on suspended workspaces.
        t=one(db,'SELECT status FROM tenants WHERE id=?'+(' FOR UPDATE' if postgres() else ''),(tid,))
        if not t or t['status']=='suspended':raise HTTPException(403,'workspace_suspended')
        from .entitlements import capacity
        existing=one(db,"SELECT state FROM service_accounts WHERE channel='whatsapp' AND provider_id=?",(phone,))
        if not existing or existing['state']=='disconnected':capacity(db,tid,'channel','whatsapp')
        db.execute('INSERT INTO meta_business_accounts VALUES(?,?,?) ON CONFLICT(id) DO NOTHING',(waba,tid,now()))
        if one(db,'SELECT tenant_id FROM meta_business_accounts WHERE id=?',(waba,))['tenant_id']!=tid:
            raise HTTPException(409,'meta_asset_unavailable')
        db.execute('''INSERT INTO service_accounts(id,tenant_id,channel,provider_id,label,api_version,state,created_at)
            VALUES(?,?,'whatsapp',?,?,?,'unverified',?) ON CONFLICT(channel,provider_id) DO NOTHING''',
            (aid,tid,phone,str(found.get('verified_name') or 'WhatsApp')[:120],v,now()))
        a=one(db,"SELECT * FROM service_accounts WHERE channel='whatsapp' AND provider_id=?",(phone,))
        if a['tenant_id']!=tid:raise HTTPException(409,'meta_asset_unavailable')
        aid=a['id']
        previous=one(db,'SELECT * FROM meta_connections WHERE account_id=?',(aid,))
        if previous and previous['business_account_id']!=waba:raise HTTPException(409,'meta_account_migration_requires_review')
        db.execute('INSERT INTO service_secrets VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE SET ciphertext=excluded.ciphertext,issued_at=excluded.issued_at',(aid,encrypted,clock))
        db.execute("UPDATE service_accounts SET token_ref=?,scopes=?,expires_at=?,api_version=?,state='unverified',checked_at=NULL WHERE id=?",('vault:'+aid,json.dumps(sorted(SCOPES)),expires,v,aid))
        db.execute("INSERT INTO meta_connections VALUES(?,?,?,'pending','pending',?) ON CONFLICT(account_id) DO UPDATE SET subscription_state='pending',registration_state='pending',connected_at=excluded.connected_at",(aid,tid,waba,now()))
        subject=str(info.get('user_id') or '')
        db.execute('INSERT INTO meta_grants VALUES(?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET app_id=excluded.app_id,subject_hash=excluded.subject_hash,checked_at=excluded.checked_at,health=excluded.health',(aid,tid,c['META_APP_ID'],digest(subject) if subject else '',clock,'valid'))
        audit(db,u,'meta.signup.authorized',aid)
    return {'account_id':aid,'authorization':'verified','registration':'pending','subscription':'pending','live_messaging_verified':False}


def connection(ident,u):
    enabled(u);throttle(u['tenant_id'])
    with transaction() as db:
        a=get(db,'service_accounts',ident,u)
        m=one(db,'SELECT * FROM meta_connections WHERE account_id=? AND tenant_id=?',(ident,u['tenant_id']))
    if not m or a['state']=='disconnected':raise HTTPException(409,'meta_connection_required')
    if a['expires_at'] and a['expires_at']<=int(time.time()):raise HTTPException(409,'meta_token_expired')
    from .service import token
    credential=token(a)
    if not credential:raise HTTPException(409,'meta_reauthorization_required')
    return a,m,credential


def ensure_credential(db,ident,u,credential):
    current=get(db,'service_accounts',ident,u,True)
    saved=one(db,'SELECT ciphertext FROM service_secrets WHERE account_id=?',(ident,))
    if current['state']=='disconnected' or not saved or not hmac.compare_digest(cipher().decrypt(saved['ciphertext'].encode()),credential.encode()):
        raise HTTPException(409,'meta_connection_changed_retry')


@router.post('/api/v2/meta/accounts/{ident}/subscribe',tags=['Meta integrations'])
def subscribe(ident:str,u=Depends(user)):
    a,m,credential=connection(ident,u)
    result=graph(u['tenant_id'],a['api_version'],'subscribe',m['business_account_id']+'/subscribed_apps',token=credential,method='POST',payload={})
    if result.get('success') is not True:raise HTTPException(502,'meta_subscription_not_confirmed')
    with transaction() as db:
        ensure_credential(db,ident,u,credential)
        db.execute("UPDATE meta_connections SET subscription_state='subscribed' WHERE account_id=?",(ident,));audit(db,u,'meta.webhook.subscribed',ident)
    return {'subscription':'subscribed','webhook_delivery_verified':False}


@router.post('/api/v2/meta/accounts/{ident}/register',tags=['Meta integrations'])
def register(ident:str,data:RegisterPhone,u=Depends(user)):
    pin=data.pin.get_secret_value()
    if not re.fullmatch(r'[0-9]{6}',pin):raise HTTPException(422,'six_digit_registration_pin_required')
    a,m,credential=connection(ident,u)
    result=graph(u['tenant_id'],a['api_version'],'register',a['provider_id']+'/register',token=credential,method='POST',payload={'messaging_product':'whatsapp','pin':pin})
    if result.get('success') is not True:raise HTTPException(502,'meta_registration_not_confirmed')
    with transaction() as db:
        ensure_credential(db,ident,u,credential)
        db.execute("UPDATE meta_connections SET registration_state='registered' WHERE account_id=?",(ident,));audit(db,u,'meta.phone.registered',ident)
    return {'registration':'registered','live_messaging_verified':False}


@router.get('/api/v2/meta/usage',tags=['Meta integrations'])
def usage(u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:
        return {'scope':'onboarding_and_service_gateway','window_days':30,'totals':rows(db,'''SELECT operation,count(*) requests,
            SUM(CASE WHEN error_code='' THEN 1 ELSE 0 END) successful,SUM(CASE WHEN error_code<>'' THEN 1 ELSE 0 END) failed
            FROM meta_api_usage WHERE tenant_id=? AND created_at>=? GROUP BY operation''',(u['tenant_id'],int(time.time())-30*86400)),
            'errors':rows(db,"SELECT error_code,count(*) count FROM meta_api_usage WHERE tenant_id=? AND created_at>=? AND error_code<>'' GROUP BY error_code",(u['tenant_id'],int(time.time())-30*86400))}


@router.get('/api/v2/meta/openapi.json',tags=['Meta integrations'])
def openapi(u=Depends(user)):
    permit(u,'settings')
    from fastapi.openapi.utils import get_openapi
    schema=get_openapi(title='Wasla Meta Integration API',version='0.1.0',routes=[r for r in router.routes if r.path.startswith('/api/v2/meta/')])
    schema.setdefault('components',{})['securitySchemes']={
        'SessionCookie':{'type':'apiKey','in':'cookie','name':'wasla_session'},
        'CsrfHeader':{'type':'apiKey','in':'header','name':'X-CSRF-Token'}}
    for methods in schema['paths'].values():
        for method,operation in methods.items():
            operation['security']=[{'SessionCookie':[],**({'CsrfHeader':[]} if method!='get' else {})}]
            for status in (401,403,404,409,429,502,503):
                operation.setdefault('responses',{}).setdefault(str(status),{'description':'Access, state, rate-limit, configuration or sanitized provider error; see API_OPERATIONS_AR.md'})
    return schema
