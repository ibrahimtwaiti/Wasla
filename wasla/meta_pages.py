"""Facebook Login for Business -> explicit Page selection -> shared Messenger inbox.

Test workspaces only. Page tokens never leave the backend. App Review and live
message delivery remain external launch gates.
"""
import json,re,secrets,time
from fastapi import Depends,HTTPException,Request
from pydantic import BaseModel,Field,SecretStr,ConfigDict
from .db import transaction,one,rows,uid,now,audit,postgres
from .main import user
from .security import permit,digest
from .runtime_configuration import value
from .service_api import router,get
from .service_oauth import cipher,unseal
from .meta_onboarding import graph,throttle,SUPPORTED_VERSIONS

SCOPES={'pages_show_list','pages_read_engagement','pages_manage_metadata','pages_messaging'}
class Exchange(BaseModel):
    model_config=ConfigDict(extra='forbid')
    state:str=Field(min_length=32,max_length=128)
    code:SecretStr
class SelectPage(BaseModel):
    model_config=ConfigDict(extra='forbid')
    state:str=Field(min_length=32,max_length=128)
    page_id:str=Field(pattern=r'^[0-9]{1,40}$')

def config(u):
    permit(u,'settings')
    from .launch import meta_gate
    meta_gate(u['tenant_id'],'messenger')
    c={k:value(k) for k in ['META_APP_ID','META_APP_SECRET','META_FACEBOOK_CONFIG_ID','META_GRAPH_VERSION','META_SYSTEM_USER_TOKEN']}
    if not all(c.values()) or c['META_GRAPH_VERSION'] not in SUPPORTED_VERSIONS:raise HTTPException(503,'facebook_configuration_required')
    cipher();return c

def session(db,state,request,u,stage):
    from .saas import lock_tenant
    lock_tenant(db,u['tenant_id'])
    r=one(db,'SELECT * FROM meta_page_sessions WHERE state_hash=?'+(' FOR UPDATE' if postgres() else ''),(digest(state),))
    if not r or r['tenant_id']!=u['tenant_id'] or r['user_id']!=u['id'] or r['session_hash']!=digest(request.cookies.get('wasla_session','')) or r['expires_at']<=int(time.time()) or r['stage']!=stage:raise HTTPException(403,'invalid_facebook_session')
    if r['app_id']!=value('META_APP_ID') or r['config_id']!=value('META_FACEBOOK_CONFIG_ID') or r['api_version']!=value('META_GRAPH_VERSION'):raise HTTPException(409,'facebook_configuration_changed')
    return r

@router.get('/api/v2/meta/pages')
def listing(u=Depends(user)):
    permit(u,'settings')
    try:config(u);available=True
    except HTTPException:available=False
    with transaction() as db:accounts=rows(db,"SELECT a.id,a.label,a.provider_id,a.state,a.expires_at,p.subscription_state FROM service_accounts a JOIN meta_page_connections p ON p.account_id=a.id WHERE a.tenant_id=?",(u['tenant_id'],))
    return {'signup_available':available,'mode':value('META_SIGNUP_MODE'),'meta_approval_verified':False,'accounts':accounts,'supported':['page_profile','messenger_text','message_delivery_events','message_read_events'],'optional_permission_features':{'page_publishing':'pages_manage_posts'}}

@router.post('/api/v2/meta/pages/start')
def start(request:Request,u=Depends(user)):
    c=config(u);throttle(u['tenant_id']);state=secrets.token_urlsafe(32)
    with transaction() as db:
        db.execute('DELETE FROM meta_page_sessions WHERE expires_at<?',(int(time.time()),))
        db.execute('INSERT INTO meta_page_sessions(state_hash,tenant_id,user_id,session_hash,app_id,config_id,api_version,expires_at) VALUES(?,?,?,?,?,?,?,?)',(digest(state),u['tenant_id'],u['id'],digest(request.cookies.get('wasla_session','')),c['META_APP_ID'],c['META_FACEBOOK_CONFIG_ID'],c['META_GRAPH_VERSION'],int(time.time())+600))
    return {'state':state,'app_id':c['META_APP_ID'],'config_id':c['META_FACEBOOK_CONFIG_ID'],'api_version':c['META_GRAPH_VERSION']}

@router.post('/api/v2/meta/pages/exchange')
def exchange(data:Exchange,request:Request,u=Depends(user)):
    c=config(u);throttle(u['tenant_id'])
    if not 1<=len(data.code.get_secret_value())<=4096:raise HTTPException(422,'invalid_code')
    with transaction() as db:
        session(db,data.state,request,u,'started')
        db.execute("UPDATE meta_page_sessions SET stage='consumed' WHERE state_hash=?",(digest(data.state),))
    tid=u['tenant_id'];version=c['META_GRAPH_VERSION']
    credential=graph(tid,version,'facebook_exchange','oauth/access_token',params={'client_id':c['META_APP_ID'],'client_secret':c['META_APP_SECRET'],'code':data.code.get_secret_value()}).get('access_token')
    if not isinstance(credential,str) or not credential:raise HTTPException(502,'facebook_token_missing')
    info=graph(tid,version,'facebook_inspect','debug_token',token=c['META_SYSTEM_USER_TOKEN'],params={'input_token':credential}).get('data',{})
    if not isinstance(info,dict) or info.get('is_valid') is not True or str(info.get('app_id'))!=c['META_APP_ID'] or not re.fullmatch(r'[0-9]{1,40}',str(info.get('user_id',''))):raise HTTPException(409,'facebook_invalid_grant')
    if not isinstance(info.get('scopes'),list) or not SCOPES.issubset({s for s in info['scopes'] if isinstance(s,str)}):raise HTTPException(409,'facebook_permissions_missing')
    try:expiries=[int(info[k]) for k in ('expires_at','data_access_expires_at') if info.get(k)]
    except (ValueError,TypeError):raise HTTPException(502,'facebook_invalid_expiry')
    expires=min(expiries) if expiries else None
    if expires and expires<=int(time.time()):raise HTTPException(409,'facebook_expired_grant')
    pages=[];after=None
    for _ in range(20):
        params={'fields':'id,name,access_token,tasks','limit':100}
        if after:params['after']=after
        result=graph(tid,version,'facebook_pages','me/accounts',token=credential,params=params)
        if not isinstance(result.get('data'),list):raise HTTPException(502,'facebook_invalid_page_response')
        for p in result['data']:
            if isinstance(p,dict) and re.fullmatch(r'[0-9]{1,40}',str(p.get('id',''))) and isinstance(p.get('access_token'),str) and p['access_token'] and 'MESSAGING' in p.get('tasks',[]):pages.append({'id':str(p['id']),'name':str(p.get('name','Page'))[:120],'token':p['access_token'],'expires_at':expires,'scopes':sorted({s for s in info['scopes'] if isinstance(s,str) and (s!='pages_manage_posts' or 'CREATE_CONTENT' in p.get('tasks',[]) or 'MANAGE' in p.get('tasks',[]))})})
        after=result.get('paging',{}).get('cursors',{}).get('after') if result.get('paging',{}).get('next') else None
        if not after:break
    if after:raise HTTPException(409,'meta_discovery_too_large_narrow_grant')
    if not pages:raise HTTPException(409,'no_eligible_messaging_pages')
    with transaction() as db:
        session(db,data.state,request,u,'consumed')
        db.execute("UPDATE meta_page_sessions SET stage='select',ciphertext=?,subject_hash=? WHERE state_hash=?",(cipher().encrypt(json.dumps(pages).encode()).decode(),digest(str(info['user_id'])),digest(data.state)))
    return {'pages':[{'id':p['id'],'name':p['name']} for p in pages]}

@router.post('/api/v2/meta/pages/connect')
def connect(data:SelectPage,request:Request,u=Depends(user)):
    c=config(u);throttle(u['tenant_id']);tid=u['tenant_id']
    with transaction() as db:
        r=session(db,data.state,request,u,'select');pages=json.loads(unseal(r['ciphertext']))
        selected=next((p for p in pages if p['id']==data.page_id),None)
        if not selected:raise HTTPException(403,'page_not_granted')
        tidrow=one(db,'SELECT status FROM tenants WHERE id=?'+(' FOR UPDATE' if postgres() else ''),(tid,))
        if tidrow['status']=='suspended':raise HTTPException(403,'workspace_suspended')
        from .entitlements import capacity,page_ownership
        page_ownership(db,tid,selected['id'])
        existing=one(db,"SELECT state FROM service_accounts WHERE channel='messenger' AND provider_id=?",(selected['id'],))
        if not existing or existing['state']=='disconnected':capacity(db,tid,'channel','messenger')
        aid=uid()
        db.execute("INSERT INTO service_accounts(id,tenant_id,channel,provider_id,label,api_version,state,created_at) VALUES(?,?,'messenger',?,?,?,'unverified',?) ON CONFLICT(channel,provider_id) DO NOTHING",(aid,tid,selected['id'],selected['name'],c['META_GRAPH_VERSION'],now()))
        a=one(db,"SELECT * FROM service_accounts WHERE channel='messenger' AND provider_id=?"+(' FOR UPDATE' if postgres() else ''),(selected['id'],))
        if a['tenant_id']!=tid:raise HTTPException(409,'meta_asset_unavailable')
        aid=a['id'];clock=int(time.time())
        db.execute('INSERT INTO service_secrets VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE SET ciphertext=excluded.ciphertext,issued_at=excluded.issued_at',(aid,cipher().encrypt(selected['token'].encode()).decode(),clock))
        db.execute("UPDATE service_accounts SET token_ref=?,state='unverified',scopes=?,expires_at=?,api_version=? WHERE id=?",('vault:'+aid,json.dumps(selected.get('scopes',sorted(SCOPES))),selected['expires_at'],c['META_GRAPH_VERSION'],aid))
        db.execute("INSERT INTO meta_grants VALUES(?,?,?,?,?,'valid') ON CONFLICT(account_id) DO UPDATE SET app_id=excluded.app_id,subject_hash=excluded.subject_hash,checked_at=excluded.checked_at,health=excluded.health",(aid,tid,c['META_APP_ID'],r['subject_hash'],clock))
        db.execute("INSERT INTO meta_page_connections VALUES(?,?,'pending',?) ON CONFLICT(account_id) DO UPDATE SET subscription_state='pending',connected_at=excluded.connected_at",(aid,tid,now()))
        db.execute("UPDATE meta_page_sessions SET stage='connected',ciphertext='' WHERE state_hash=?",(digest(data.state),))
        audit(db,u,'meta.page.connected',aid)
    return {'id':aid,'subscription_state':'pending','live_messaging_verified':False}

@router.post('/api/v2/meta/pages/{ident}/subscribe')
def subscribe(ident:str,u=Depends(user)):
    config(u);throttle(u['tenant_id'])
    from .service import token
    with transaction() as db:
        a=get(db,'service_accounts',ident,u)
        if a['channel']!='messenger' or a['state']=='disconnected':raise HTTPException(409,'page_connection_required')
    credential=token(a)
    if not credential:raise HTTPException(409,'page_token_required')
    result=subscribe_fields(a['tenant_id'],a['api_version'],a['provider_id'],credential,{'messages','message_deliveries','message_reads'})
    if result.get('success') is not True:raise HTTPException(502,'page_subscription_not_confirmed')
    with transaction() as db:
        from .meta_onboarding import ensure_credential
        ensure_credential(db,ident,u,credential)
        db.execute("UPDATE meta_page_connections SET subscription_state='subscribed' WHERE account_id=?",(ident,))
        audit(db,u,'meta.page.subscribed',ident)
    return {'subscribed':True,'live_delivery_verified':False}

@router.get('/api/v2/meta/pages/{ident}/profile')
def profile(ident:str,u=Depends(user)):
    config(u);throttle(u['tenant_id'])
    from .service import token
    with transaction() as db:a=get(db,'service_accounts',ident,u)
    if a['channel']!='messenger' or a['state']=='disconnected':raise HTTPException(409,'page_connection_required')
    credential=token(a)
    if not credential:raise HTTPException(409,'page_token_required')
    result=graph(u['tenant_id'],a['api_version'],'page_profile',a['provider_id'],token=credential,params={'fields':'id,name,category'})
    if str(result.get('id'))!=a['provider_id']:raise HTTPException(409,'page_identity_mismatch')
    return {k:result.get(k) for k in ('id','name','category')}

@router.post('/api/v2/meta/pages/{ident}/disconnect-remote')
def disconnect_remote(ident:str,u=Depends(user)):
    config(u);throttle(u['tenant_id'])
    from .service import token
    from .meta_lifecycle import disconnect_local
    with transaction() as db:
        a=get(db,'service_accounts',ident,u)
        if a['channel']!='messenger':raise HTTPException(409,'page_connection_required')
        snapshot=one(db,'SELECT ciphertext FROM service_secrets WHERE account_id=?',(ident,))
    credential=token(a);remote='not_confirmed'
    if credential:
        try:
            with transaction() as db:shared=one(db,"SELECT id FROM meta_data_connections WHERE kind='leads' AND provider_id=? AND state='connected'",(a['provider_id'],))
            if shared:
                current=graph(u['tenant_id'],a['api_version'],'page_subscription_read',a['provider_id']+'/subscribed_apps',token=credential)
                fields={'leadgen'}
                for app in current.get('data',[]):
                    if str(app.get('id'))==value('META_APP_ID'):fields.update(x for x in app.get('subscribed_fields',[]) if isinstance(x,str) and x not in {'messages','message_deliveries','message_reads'})
                result=graph(u['tenant_id'],a['api_version'],'page_unsubscribe_messaging',a['provider_id']+'/subscribed_apps',token=credential,method='POST',payload={'subscribed_fields':sorted(fields)})
            else:result=graph(u['tenant_id'],a['api_version'],'page_unsubscribe',a['provider_id']+'/subscribed_apps',token=credential,method='DELETE')
            if result.get('success') is True:remote='confirmed'
        except HTTPException:pass # Still erase local credentials on provider failure.
    with transaction() as db:
        get(db,'service_accounts',ident,u,True)
        if one(db,'SELECT ciphertext FROM service_secrets WHERE account_id=?',(ident,))!=snapshot:raise HTTPException(409,'page_connection_changed_retry')
        disconnect_local(db,ident,'owner_disconnect')
        audit(db,u,'meta.page.disconnected',ident,'remote_unsubscribe_'+remote)
    return {'local':'disconnected','remote_unsubscribe':remote,'user_permissions_revoked':False}


def subscribe_fields(tid,version,page_id,token,requested):
    current=graph(tid,version,'page_subscription_read',page_id+'/subscribed_apps',token=token)
    fields=set(requested)
    for app in current.get('data',[]):
        if str(app.get('id'))==value('META_APP_ID'):
            fields.update(x for x in app.get('subscribed_fields',[]) if isinstance(x,str))
    return graph(tid,version,'page_subscribe',page_id+'/subscribed_apps',token=token,method='POST',payload={'subscribed_fields':sorted(fields)})

def remaining_fields(response,app_id,removed):
    apps=response.get('data')
    if not isinstance(apps,list) or any(not isinstance(app,dict) for app in apps):raise HTTPException(502,'invalid_page_subscription')
    for app in apps:
        if str(app.get('id'))==app_id:
            fields=app.get('subscribed_fields')
            if not isinstance(fields,list) or any(not isinstance(x,str) for x in fields):raise HTTPException(502,'invalid_page_subscription')
            return sorted(set(fields)-set(removed))
    return None

def unsubscribe_fields(tid,version,page_id,token,app_id,removed):
    path=page_id+'/subscribed_apps'
    current=graph(tid,version,'page_subscription_read',path,token=token)
    fields=remaining_fields(current,app_id,removed)
    if fields is None:return {'success':True}
    return graph(tid,version,'page_unsubscribe_fields',path,token=token,method='POST' if fields else 'DELETE',payload={'subscribed_fields':fields} if fields else None)
