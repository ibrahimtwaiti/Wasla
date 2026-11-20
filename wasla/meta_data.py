"""Read-only Catalogs, Lead Ads and Marketing adapters with official OAuth.

No campaign mutation or advertising spend endpoints exist. Each capability has a
separate Login for Business configuration and permission set.
"""
import json,re,secrets,time,hashlib
from fastapi import Depends,HTTPException,Request
from pydantic import BaseModel,Field,SecretStr,ConfigDict
from .db import transaction,one,rows,uid,now,audit,postgres
from .main import user
from .security import permit,digest
from .service_api import router,get
from .service_oauth import cipher,unseal
from .runtime_configuration import value
from .meta_onboarding import graph,SUPPORTED_VERSIONS,throttle

SPECS={
 'catalogs':{'config':'META_CATALOG_CONFIG_ID','scopes':{'catalog_management','business_management'}},
 'leads':{'config':'META_LEADS_CONFIG_ID','scopes':{'leads_retrieval','pages_show_list','pages_read_engagement','pages_manage_metadata','ads_management'}},
 'marketing':{'config':'META_MARKETING_CONFIG_ID','scopes':{'ads_read'}}}
class Exchange(BaseModel):
    model_config=ConfigDict(extra='forbid')
    state:str=Field(min_length=32,max_length=128)
    code:SecretStr
class Selection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    state:str=Field(min_length=32,max_length=128)
    asset_id:str=Field(pattern=r'^(act_)?[0-9]{1,40}$')
class Sync(BaseModel):
    model_config=ConfigDict(extra='forbid')
    cursor:str=Field(default='',max_length=4096)
    date_preset:str=Field(default='last_30d',pattern=r'^(last_7d|last_30d|this_month)$')

def config(kind,u):
    permit(u,'settings')
    if kind not in SPECS:raise HTTPException(404,'unsupported_meta_service')
    from .launch import meta_gate
    meta_gate(u['tenant_id'],kind)
    c={k:value(k) for k in ['META_APP_ID','META_APP_SECRET','META_GRAPH_VERSION','META_SYSTEM_USER_TOKEN']}
    c['config_id']=value(SPECS[kind]['config'])
    if kind=='marketing':c['META_GRAPH_VERSION']=value('META_MARKETING_API_VERSION','v25.0')
    if kind=='marketing' and c['META_GRAPH_VERSION']!='v25.0':raise HTTPException(503,'marketing_version_review_required')
    if not all(c.values()) or c['META_GRAPH_VERSION'] not in SUPPORTED_VERSIONS:raise HTTPException(503,'meta_service_configuration_required')
    cipher();return c

def page(tid,version,operation,path,token,params=None,cursor=''):
    parameters={'limit':100,**(params or {})}
    if cursor:parameters['after']=cursor
    result=graph(tid,version,operation,path,token=token,params=parameters)
    if not isinstance(result.get('data'),list) or any(not isinstance(x,dict) for x in result['data']):raise HTTPException(502,'invalid_meta_collection')
    next_cursor=result.get('paging',{}).get('cursors',{}).get('after','') if result.get('paging',{}).get('next') else ''
    if not isinstance(next_cursor,str) or len(next_cursor)>4096:raise HTTPException(502,'invalid_meta_cursor')
    return result['data'],next_cursor

def collection(tid,version,operation,path,token,params=None):
    output=[];cursor=''
    for _ in range(50):
        data,cursor=page(tid,version,operation,path,token,params,cursor);output+=data
        if not cursor:return output
    raise HTTPException(409,'meta_discovery_too_large_narrow_grant')

def session(db,state,request,u,stage):
    from .saas import lock_tenant
    lock_tenant(db,u['tenant_id'])
    s=one(db,'SELECT * FROM meta_data_sessions WHERE state_hash=?'+(' FOR UPDATE' if postgres() else ''),(digest(state),))
    if not s or s['tenant_id']!=u['tenant_id'] or s['user_id']!=u['id'] or s['session_hash']!=digest(request.cookies.get('wasla_session','')) or s['expires_at']<=int(time.time()) or s['stage']!=stage:raise HTTPException(403,'invalid_meta_data_state')
    c=config(s['kind'],u)
    if s['app_id']!=c['META_APP_ID'] or s['config_id']!=c['config_id'] or s['api_version']!=c['META_GRAPH_VERSION']:raise HTTPException(409,'meta_configuration_changed')
    return s,c

@router.get('/api/v2/meta/data')
def listing(u=Depends(user)):
    permit(u,'settings');services=[]
    for kind,spec in SPECS.items():
        try:config(kind,u);available=True
        except HTTPException:available=False
        services.append({'kind':kind,'available':available,'permissions':sorted(spec['scopes'])})
    with transaction() as db:data=rows(db,'SELECT id,kind,provider_id,label,state,last_sync,error,expires_at FROM meta_data_connections WHERE tenant_id=? ORDER BY label',(u['tenant_id'],))
    return {'services':services,'connections':data,'production_approved':False,'advertising_mutations_enabled':False}

@router.post('/api/v2/meta/data/{kind}/start')
def start(kind:str,request:Request,u=Depends(user)):
    c=config(kind,u);throttle(u['tenant_id']);state=secrets.token_urlsafe(32)
    with transaction() as db:
        db.execute('DELETE FROM meta_data_sessions WHERE expires_at<?',(int(time.time()),))
        db.execute('INSERT INTO meta_data_sessions(state_hash,tenant_id,user_id,session_hash,kind,app_id,config_id,api_version,expires_at) VALUES(?,?,?,?,?,?,?,?,?)',(digest(state),u['tenant_id'],u['id'],digest(request.cookies.get('wasla_session','')),kind,c['META_APP_ID'],c['config_id'],c['META_GRAPH_VERSION'],int(time.time())+600))
    return {'state':state,'app_id':c['META_APP_ID'],'config_id':c['config_id'],'api_version':c['META_GRAPH_VERSION']}

@router.post('/api/v2/meta/data/exchange')
def exchange(data:Exchange,request:Request,u=Depends(user)):
    permit(u,'settings');throttle(u['tenant_id'])
    if not 1<=len(data.code.get_secret_value())<=4096:raise HTTPException(422,'invalid_authorization_code')
    with transaction() as db:
        s,c=session(db,data.state,request,u,'started');db.execute("UPDATE meta_data_sessions SET stage='consumed' WHERE state_hash=?",(digest(data.state),))
    tid=u['tenant_id'];v=c['META_GRAPH_VERSION'];kind=s['kind']
    token=graph(tid,v,kind+'_exchange','oauth/access_token',params={'client_id':c['META_APP_ID'],'client_secret':c['META_APP_SECRET'],'code':data.code.get_secret_value()}).get('access_token')
    if not isinstance(token,str) or not token:raise HTTPException(502,'meta_exchange_missing_token')
    info=graph(tid,v,kind+'_inspect','debug_token',token=c['META_SYSTEM_USER_TOKEN'],params={'input_token':token}).get('data',{})
    if not isinstance(info,dict) or info.get('is_valid') is not True or str(info.get('app_id'))!=c['META_APP_ID'] or not re.fullmatch('[0-9]{1,40}',str(info.get('user_id',''))):raise HTTPException(409,'meta_invalid_grant')
    scopes=info.get('scopes',[])
    if not isinstance(scopes,list) or not SPECS[kind]['scopes'].issubset({x for x in scopes if isinstance(x,str)}):raise HTTPException(409,'meta_required_permissions_missing')
    try:expiries=[int(info[k]) for k in ('expires_at','data_access_expires_at') if info.get(k)]
    except (ValueError,TypeError):raise HTTPException(502,'meta_invalid_expiry')
    expires=min(expiries) if expiries else None
    if expires and expires<=int(time.time()):raise HTTPException(409,'meta_expired_grant')
    if kind=='marketing':assets=collection(tid,v,'adaccounts_discovery','me/adaccounts',token,{'fields':'id,name,account_status,currency'})
    elif kind=='leads':assets=[x for x in collection(tid,v,'lead_pages_discovery','me/accounts',token,{'fields':'id,name,access_token,tasks'}) if 'ADVERTISE' in x.get('tasks',[]) or 'MANAGE' in x.get('tasks',[])]
    else:
        businesses=collection(tid,v,'business_discovery','me/businesses',token,{'fields':'id,name'});assets=[]
        for business in businesses[:20]:
            if not re.fullmatch('[0-9]{1,40}',str(business.get('id',''))):raise HTTPException(502,'invalid_meta_business')
            for edge in ['owned_product_catalogs','client_product_catalogs']:
                assets+=collection(tid,v,'catalog_discovery',str(business['id'])+'/'+edge,token,{'fields':'id,name'})
        if len(businesses)>20:raise HTTPException(409,'meta_discovery_too_large_narrow_grant')
    selected={}
    for asset in assets:
        ident=str(asset.get('id',''))
        if not re.fullmatch(r'(act_)?[0-9]{1,40}',ident):raise HTTPException(502,'invalid_meta_asset')
        credential=asset.get('access_token') if kind=='leads' else token
        if not isinstance(credential,str) or not credential:continue
        selected[ident]={'id':ident,'name':str(asset.get('name',ident))[:120],'token':credential,'expires_at':expires,'scopes':sorted(SPECS[kind]['scopes'])}
    if not selected:raise HTTPException(409,'no_authorized_assets')
    with transaction() as db:
        session(db,data.state,request,u,'consumed')
        db.execute("UPDATE meta_data_sessions SET stage='select',ciphertext=?,subject_hash=? WHERE state_hash=?",(cipher().encrypt(json.dumps(list(selected.values())).encode()).decode(),digest(str(info['user_id'])),digest(data.state)))
    return {'assets':[{'id':a['id'],'name':a['name']} for a in selected.values()]}

@router.post('/api/v2/meta/data/connect')
def connect(data:Selection,request:Request,u=Depends(user)):
    permit(u,'settings');throttle(u['tenant_id']);tid=u['tenant_id']
    with transaction() as db:
        s,c=session(db,data.state,request,u,'select');assets=json.loads(unseal(s['ciphertext']))
        asset=next((x for x in assets if x['id']==data.asset_id),None)
        if not asset:raise HTTPException(403,'asset_not_granted')
        from .entitlements import capacity
        from .saas import lock_tenant
        lock_tenant(db,tid)
        if s['kind']=='leads':
            from .entitlements import page_ownership
            page_ownership(db,tid,asset['id'])
        previous=one(db,'SELECT * FROM meta_data_connections WHERE kind=? AND provider_id=?',(s['kind'],asset['id']))
        if previous and previous['tenant_id']!=tid:raise HTTPException(409,'meta_asset_unavailable')
        if not previous or previous['state']=='disconnected':capacity(db,tid,'data')
        ident=previous['id'] if previous else uid()
        db.execute('''INSERT INTO meta_data_connections(id,tenant_id,kind,provider_id,label,api_version,ciphertext,app_id,subject_hash,scopes,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET ciphertext=excluded.ciphertext,scopes=excluded.scopes,expires_at=excluded.expires_at,state='connected',error='',app_id=excluded.app_id,subject_hash=excluded.subject_hash,api_version=excluded.api_version''',(ident,tid,s['kind'],asset['id'],asset['name'],c['META_GRAPH_VERSION'],cipher().encrypt(asset['token'].encode()).decode(),c['META_APP_ID'],s['subject_hash'],json.dumps(asset['scopes']),asset['expires_at']))
        db.execute("UPDATE meta_data_sessions SET stage='connected',ciphertext='' WHERE state_hash=?",(digest(data.state),));audit(db,u,'meta.data.connected',ident,s['kind'])
    return {'id':ident,'live_verified':False}

def connection(ident,u):
    permit(u,'settings')
    with transaction() as db:a=get(db,'meta_data_connections',ident,u)
    config(a['kind'],u)
    if a['state']!='connected' or not a['ciphertext']:raise HTTPException(409,'meta_reauthorization_required')
    if a['expires_at'] and a['expires_at']<=int(time.time()):raise HTTPException(409,'meta_token_expired')
    return a,unseal(a['ciphertext'])

def store_records(a,kind,data):
    with transaction() as db:
        current=one(db,'SELECT ciphertext,state FROM meta_data_connections WHERE id=?'+(' FOR UPDATE' if postgres() else ''),(a['id'],))
        if not current or current['state']!='connected' or current['ciphertext']!=a['ciphertext']:raise HTTPException(409,'meta_connection_changed')
        for record in data:
            if kind!='insights' and not record.get('id'):raise HTTPException(502,'invalid_meta_record')
            if kind=='insights' and (str(record.get('account_id',''))!=a['provider_id'].removeprefix('act_') or any(not re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(record.get(k,''))) for k in ('date_start','date_stop'))):raise HTTPException(502,'invalid_meta_insights_record')
            ident=str(record.get('id') or hashlib.sha256(json.dumps([record.get(k) for k in ('account_id','date_start','date_stop')]).encode()).hexdigest())
            if len(ident)>200:raise HTTPException(502,'invalid_meta_record')
            db.execute('INSERT INTO meta_data_records VALUES(?,?,?,?,?,?) ON CONFLICT(connection_id,kind,record_id) DO UPDATE SET ciphertext=excluded.ciphertext,synced_at=excluded.synced_at',(a['id'],a['tenant_id'],ident,kind,cipher().encrypt(json.dumps(record).encode()).decode(),int(time.time())))
        db.execute("UPDATE meta_data_connections SET last_sync=?,error='' WHERE id=?",(int(time.time()),a['id']))

def record_error(ident,error,snapshot=None):
    with transaction() as db:
        current=one(db,'SELECT ciphertext FROM meta_data_connections WHERE id=?'+(' FOR UPDATE' if postgres() else ''),(ident,))
        if not current or (snapshot is not None and current['ciphertext']!=snapshot):return
        db.execute('UPDATE meta_data_connections SET error=? WHERE id=?',(str(error.detail)[:120],ident))
        if error.detail=='meta_190':db.execute("UPDATE meta_data_connections SET state='reauthorize',ciphertext='' WHERE id=?",(ident,))

@router.post('/api/v2/meta/data/{ident}/sync')
def sync(ident:str,data:Sync,u=Depends(user)):
    a,token=connection(ident,u);throttle(u['tenant_id'])
    try:
        if a['kind']=='catalogs':records,cursor=page(a['tenant_id'],a['api_version'],'products_sync',a['provider_id']+'/products',token,{'fields':'id,name,retailer_id,price,currency,availability'},data.cursor);kind='products'
        elif a['kind']=='marketing':records,cursor=page(a['tenant_id'],a['api_version'],'insights_sync',a['provider_id']+'/insights',token,{'fields':'account_id,account_name,date_start,date_stop,impressions,clicks,spend,account_currency','date_preset':data.date_preset,'level':'account'},data.cursor);kind='insights'
        else:records,cursor=page(a['tenant_id'],a['api_version'],'lead_forms_sync',a['provider_id']+'/leadgen_forms',token,{'fields':'id,name,status'},data.cursor);kind='forms'
        allowed={'products':{'id','name','retailer_id','price','currency','availability'},'insights':{'account_id','account_name','date_start','date_stop','impressions','clicks','spend','account_currency'},'forms':{'id','name','status'}}[kind]
        records=[{k:v for k,v in r.items() if k in allowed} for r in records]
        store_records(a,kind,records)
        return {'synced':len(records),'next_cursor':cursor,'complete':not bool(cursor),'kind':kind,'source':'meta_api'}
    except HTTPException as e:record_error(ident,e,a['ciphertext']);raise

@router.get('/api/v2/meta/data/{ident}/records')
def records(ident:str,kind:str='',offset:int=0,u=Depends(user)):
    permit(u,'settings')
    if kind not in {'','products','insights','forms','leads'} or not 0<=offset<=1000000:raise HTTPException(422,'invalid_record_page')
    with transaction() as db:
        a=get(db,'meta_data_connections',ident,u)
        data=rows(db,'SELECT kind,ciphertext,synced_at FROM meta_data_records WHERE connection_id=? AND (?=\'\' OR kind=?) ORDER BY synced_at,record_id LIMIT 100 OFFSET ?',(ident,kind,kind,offset))
    return {'data':[{'kind':r['kind'],'value':json.loads(unseal(r['ciphertext'])),'synced_at':r['synced_at']} for r in data],'next_offset':offset+100 if len(data)==100 else None,'missing_metrics':'not_reported_by_meta','last_sync':a['last_sync']}

@router.post('/api/v2/meta/data/{ident}/disconnect')
def disconnect(ident:str,u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:a=get(db,'meta_data_connections',ident,u)
    remote='not_applicable'
    if a['kind']=='leads' and a['ciphertext']:
        remote='not_confirmed'
        try:
            from .meta_pages import unsubscribe_fields
            result=unsubscribe_fields(a['tenant_id'],a['api_version'],a['provider_id'],unseal(a['ciphertext']),a['app_id'],{'leadgen'})
            if result.get('success') is True:remote='confirmed'
        except HTTPException:pass
    with transaction() as db:
        current=get(db,'meta_data_connections',ident,u,True)
        if current['ciphertext']!=a['ciphertext']:raise HTTPException(409,'meta_connection_changed')
        db.execute("UPDATE meta_data_connections SET state='disconnected',ciphertext='' WHERE id=?",(ident,));audit(db,u,'meta.data.disconnected',ident,'remote_subscription_'+remote)
    return {'local':'disconnected','remote_unsubscribe':remote,'remote_user_permissions':'Manage in Meta Business Integrations; grant may cover other assets.'}

@router.post('/api/v2/meta/data/{ident}/leads/subscribe')
def subscribe_leads(ident:str,u=Depends(user)):
    a,token=connection(ident,u)
    if a['kind']!='leads':raise HTTPException(409,'lead_page_required')
    from .meta_pages import subscribe_fields
    result=subscribe_fields(a['tenant_id'],a['api_version'],a['provider_id'],token,{'leadgen'})
    if result.get('success') not in (True,'true'):raise HTTPException(502,'lead_subscription_not_confirmed')
    return {'subscribed':True,'live_delivery_verified':False}

@router.post('/api/v2/meta/data/{ident}/forms/{form_id}/sync')
def sync_form(ident:str,form_id:str,data:Sync,u=Depends(user)):
    a,token=connection(ident,u)
    if a['kind']!='leads' or not re.fullmatch('[0-9]{1,40}',form_id):raise HTTPException(422,'invalid_lead_form')
    # Form must have been discovered from the authorized Page, never accept arbitrary form IDs.
    with transaction() as db:found=one(db,"SELECT record_id FROM meta_data_records WHERE connection_id=? AND kind='forms' AND record_id=?",(ident,form_id))
    if not found:raise HTTPException(403,'form_not_authorized')
    data_,cursor=page(a['tenant_id'],a['api_version'],'leads_sync',form_id+'/leads',token,{'fields':'id,created_time,field_data,form_id,ad_id'},data.cursor)
    store_records(a,'leads',[{k:v for k,v in r.items() if k in {'id','created_time','field_data','form_id','ad_id'}} for r in data_])
    return {'synced':len(data_),'next_cursor':cursor,'complete':not bool(cursor)}

def ingest_leads(payload):
    if payload.get('object')!='page':raise HTTPException(400,'invalid_lead_object')
    count=0
    with transaction() as db:
        for entry in payload.get('entry',[]):
            for change in entry.get('changes',[]):
                if change.get('field')!='leadgen':continue
                e=change['value'];pageid=str(e['page_id']);leadid=str(e['leadgen_id'])
                if not pageid.isdigit() or not leadid.isdigit() or len(leadid)>40 or str(entry['id'])!=pageid:raise HTTPException(400,'invalid_lead_event')
                a=one(db,"SELECT c.* FROM meta_data_connections c JOIN tenants t ON t.id=c.tenant_id WHERE c.kind='leads' AND c.provider_id=? AND c.state='connected' AND t.status!='suspended'",(pageid,))
                if not a:continue # Unsubscribed assets cannot allocate or fetch tenant data.
                db.execute('INSERT INTO meta_lead_events(id,connection_id,tenant_id,lead_id,created_at) VALUES(?,?,?,?,?) ON CONFLICT(connection_id,lead_id) DO NOTHING',(uid(),a['id'],a['tenant_id'],leadid,int(time.time())));count+=1
                if count>500:raise HTTPException(413,'too_many_events')
    return count

@router.post('/api/webhooks/meta/leads')
async def receive_leads(request:Request):
    from .service import valid_signature
    raw=await request.body()
    if not valid_signature(raw,request.headers.get('X-Hub-Signature-256',''),value('META_APP_SECRET')):raise HTTPException(401,'invalid_signature')
    try:return {'received':True,'events':ingest_leads(json.loads(raw))}
    except (ValueError,TypeError,KeyError,AttributeError):raise HTTPException(400,'invalid_lead_payload')

@router.get('/api/webhooks/meta/leads')
def verify_leads(request:Request):
    from .service_api import verify
    return verify('messenger',request)

def process_lead():
    clock=int(time.time())
    with transaction() as db:
        db.execute("UPDATE meta_lead_events SET state='retry' WHERE state='processing' AND next_attempt<?",(clock,))
        job=one(db,"SELECT * FROM meta_lead_events WHERE state IN ('pending','retry') AND next_attempt<=? LIMIT 1"+(' FOR UPDATE SKIP LOCKED' if postgres() else ''),(clock,))
        if not job:return False
        db.execute("UPDATE meta_lead_events SET state='processing',attempts=attempts+1,next_attempt=? WHERE id=?",(clock+120,job['id']))
    try:
        a,token=connection(job['connection_id'],{'role':'admin','tenant_id':job['tenant_id']})
        result=graph(a['tenant_id'],a['api_version'],'lead_retrieve',job['lead_id'],token=token,params={'fields':'id,created_time,field_data,form_id,ad_id'})
        if str(result.get('id'))!=job['lead_id']:raise HTTPException(502,'lead_identity_mismatch')
        store_records(a,'leads',[{k:v for k,v in result.items() if k in {'id','created_time','field_data','form_id','ad_id'}}])
        with transaction() as db:db.execute("UPDATE meta_lead_events SET state='done',error='' WHERE id=?",(job['id'],))
    except HTTPException as e:
        record_error(job['connection_id'],e,a['ciphertext'] if 'a' in locals() else None)
        with transaction() as db:db.execute("UPDATE meta_lead_events SET state=?,error=?,next_attempt=? WHERE id=?",('failed' if job['attempts']>=4 else 'retry',str(e.detail)[:120],clock+min(3600,30*2**job['attempts']),job['id']))
    except Exception:
        with transaction() as db:db.execute("UPDATE meta_lead_events SET state=?,error='lead_processing_failed',next_attempt=? WHERE id=?",('failed' if job['attempts']>=4 else 'retry',clock+60,job['id']))
    return True

@router.post('/api/v2/meta/data/{ident}/token-health')
def token_health(ident:str,u=Depends(user)):
    a,token=connection(ident,u);c=config(a['kind'],u);throttle(u['tenant_id'])
    info=graph(u['tenant_id'],a['api_version'],'data_token_health','debug_token',token=c['META_SYSTEM_USER_TOKEN'],params={'input_token':token}).get('data')
    if not isinstance(info,dict) or not isinstance(info.get('scopes',[]),list):raise HTTPException(502,'meta_invalid_token_inspection')
    try:expiries=[int(info[k]) for k in ('expires_at','data_access_expires_at') if info.get(k)]
    except (ValueError,TypeError):raise HTTPException(502,'meta_invalid_expiry')
    expires=min(expiries) if expiries else None
    valid=info.get('is_valid') is True and str(info.get('app_id'))==a['app_id'] and SPECS[a['kind']]['scopes'].issubset({s for s in info.get('scopes',[]) if isinstance(s,str)}) and (not expires or expires>int(time.time()))
    with transaction() as db:
        current=get(db,'meta_data_connections',ident,u,True)
        if current['ciphertext']!=a['ciphertext'] or current['state']!='connected':raise HTTPException(409,'meta_connection_changed')
        db.execute('UPDATE meta_data_connections SET state=?,ciphertext=?,expires_at=?,error=? WHERE id=?',('connected' if valid else 'reauthorize',a['ciphertext'] if valid else '',expires,'' if valid else 'meta_grant_invalid',ident));audit(db,u,'meta.data.token_checked',ident,'valid' if valid else 'reauthorize')
    return {'health':'valid' if valid else 'reauthorize','expires_at':expires,'refresh_supported':False,'next_action':'none' if valid else 'reconnect'}

@router.get('/api/v2/meta/data/{ident}/events')
def lead_events(ident:str,u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:
        get(db,'meta_data_connections',ident,u)
        return rows(db,'SELECT id,lead_id,state,attempts,error FROM meta_lead_events WHERE connection_id=? ORDER BY next_attempt DESC LIMIT 100',(ident,))

@router.post('/api/v2/meta/data/{ident}/events/{event}/retry')
def retry_lead(ident:str,event:str,u=Depends(user)):
    connection(ident,u);throttle(u['tenant_id'])
    with transaction() as db:
        if db.execute("UPDATE meta_lead_events SET state='pending',next_attempt=0,attempts=0 WHERE id=? AND connection_id=? AND state='failed'",(event,ident)).rowcount!=1:raise HTTPException(409,'failed_lead_event_required')
        audit(db,u,'meta.lead.retry',event)
    return {'state':'pending'}

# Explicit provider delegation shares the customer services and tenant checks.
def delegate(tenant_id,u):
    from .provider_console import scoped_provider
    return scoped_provider(tenant_id,u)

@router.get('/api/admin/workspaces/{tenant_id}/meta-data')
def provider_listing(tenant_id:str,u=Depends(user)):return listing(delegate(tenant_id,u))

@router.post('/api/admin/workspaces/{tenant_id}/meta-data/{kind}/start')
def provider_start(tenant_id:str,kind:str,request:Request,u=Depends(user)):return start(kind,request,delegate(tenant_id,u))

@router.post('/api/admin/workspaces/{tenant_id}/meta-data/exchange')
def provider_exchange(tenant_id:str,data:Exchange,request:Request,u=Depends(user)):return exchange(data,request,delegate(tenant_id,u))

@router.post('/api/admin/workspaces/{tenant_id}/meta-data/connect')
def provider_connect(tenant_id:str,data:Selection,request:Request,u=Depends(user)):return connect(data,request,delegate(tenant_id,u))

@router.post('/api/admin/workspaces/{tenant_id}/meta-data/{ident}/sync')
def provider_sync(tenant_id:str,ident:str,data:Sync,u=Depends(user)):return sync(ident,data,delegate(tenant_id,u))

@router.get('/api/admin/workspaces/{tenant_id}/meta-data/{ident}/records')
def provider_records(tenant_id:str,ident:str,kind:str='',offset:int=0,u=Depends(user)):return records(ident,kind,offset,delegate(tenant_id,u))

@router.post('/api/admin/workspaces/{tenant_id}/meta-data/{ident}/disconnect')
def provider_disconnect(tenant_id:str,ident:str,u=Depends(user)):return disconnect(ident,delegate(tenant_id,u))

@router.post('/api/admin/workspaces/{tenant_id}/meta-data/{ident}/token-health')
def provider_health(tenant_id:str,ident:str,u=Depends(user)):return token_health(ident,delegate(tenant_id,u))

@router.post('/api/admin/workspaces/{tenant_id}/meta-data/{ident}/leads/subscribe')
def provider_subscribe(tenant_id:str,ident:str,u=Depends(user)):return subscribe_leads(ident,delegate(tenant_id,u))

@router.post('/api/admin/workspaces/{tenant_id}/meta-data/{ident}/forms/{form_id}/sync')
def provider_form(tenant_id:str,ident:str,form_id:str,data:Sync,u=Depends(user)):return sync_form(ident,form_id,data,delegate(tenant_id,u))

@router.get('/api/admin/workspaces/{tenant_id}/meta-data/{ident}/events')
def provider_events(tenant_id:str,ident:str,u=Depends(user)):return lead_events(ident,delegate(tenant_id,u))

@router.post('/api/admin/workspaces/{tenant_id}/meta-data/{ident}/events/{event}/retry')
def provider_retry(tenant_id:str,ident:str,event:str,u=Depends(user)):return retry_lead(ident,event,delegate(tenant_id,u))
