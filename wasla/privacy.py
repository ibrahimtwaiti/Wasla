"""Durable erasure with identity checks, legal holds and private-object cleanup.

Jobs/tombstones contain identifiers and operational results only. No fake remote
revocation is reported. Immutable backups expire under the separate 30-day policy.
"""
import json,os,secrets,time
from urllib.parse import parse_qs,urlparse
import httpx
from fastapi import Depends,HTTPException,Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel,SecretStr,Field,ConfigDict
from .db import transaction,one,rows,uid,now,postgres
from .main import user,provider
from .security import permit,verify_password,limit,digest
from .service_api import router,get
from .service_oauth import cipher,unseal
from .runtime_configuration import value

class DeleteRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    scope:str=Field(pattern=r'^(user|tenant|account)$')
    account_id:str=Field(default='',max_length=80)
    password:SecretStr
    confirm:str=Field(pattern=r'^DELETE$')

def marks(ids):return ','.join('?' for _ in ids)

def remove(db,table,column,ids):
    if ids:db.execute('DELETE FROM '+table+' WHERE '+column+' IN ('+marks(ids)+')',tuple(ids))

def identifiers(db,table,column,ids,field='id'):
    return [r[field] for r in rows(db,'SELECT '+field+' FROM '+table+' WHERE '+column+' IN ('+marks(ids)+')',tuple(ids))] if ids else []

def queue(db,tid,requester,scope,accounts=None,data_accounts=None,source=None):
    if source:
        old=one(db,'SELECT id FROM deletion_jobs WHERE source_key=?',(source,))
        if old:return old['id']
    if scope=='tenant':
        from .saas import lock_tenant
        lock_tenant(db,tid)
        accounts=identifiers(db,'service_accounts','tenant_id',[tid]);data_accounts=identifiers(db,'meta_data_connections','tenant_id',[tid])
        db.execute("UPDATE tenants SET status='suspended' WHERE id=?",(tid,))
    accounts=accounts or [];data_accounts=data_accounts or []
    userids=identifiers(db,'users','tenant_id',[tid]) if scope=='tenant' else [requester] if scope=='user' else []
    for user_id in userids:
        db.execute('UPDATE users SET active=0 WHERE id=?',(user_id,));remove(db,'sessions','user_id',[user_id])
    local_replay=bool(source and source.startswith('restore:'))
    remote=[];objects=[]
    for aid in accounts:
        a=one(db,'SELECT * FROM service_accounts WHERE id=?',(aid,));saved=one(db,'SELECT ciphertext FROM service_secrets WHERE account_id=?',(aid,))
        if not a:continue
        if saved and not local_replay:
            target=a['provider_id']+'/subscribed_apps';host='graph.facebook.com';eligible=a['channel']=='messenger'
            if a['channel']=='messenger':
                peer=one(db,"SELECT id FROM meta_data_connections WHERE kind='leads' AND provider_id=? AND state!='disconnected'",(a['provider_id'],))
                if peer and peer['id'] not in data_accounts:eligible=False
            if a['channel']=='whatsapp':
                m=one(db,'SELECT business_account_id FROM meta_connections WHERE account_id=?',(aid,))
                if m:
                    peers=identifiers(db,'meta_connections','business_account_id',[m['business_account_id']],field='account_id')
                    eligible=set(peers).issubset(accounts);target=m['business_account_id']+'/subscribed_apps'
            if a['channel']=='instagram':host='graph.instagram.com';target='me/permissions';eligible=True
            remote.append({'host':host,'target':target,'version':a['api_version'],'token':unseal(saved['ciphertext']),'eligible':eligible})
        if not local_replay:
            identities=identifiers(db,'service_identities','account_id',[aid]);tickets=identifiers(db,'service_tickets','identity_id',identities)
            objects+=identifiers(db,'service_media','ticket_id',tickets,field='object_key')
        from .meta_lifecycle import disconnect_local
        disconnect_local(db,aid,'privacy_deletion')
        db.execute("UPDATE service_outbox SET state='blocked',error='privacy_deletion' WHERE state='retry' AND message_id IN (SELECT id FROM service_messages WHERE account_id=?)",(aid,))
    if data_accounts:
        for did in data_accounts:
            d=one(db,'SELECT * FROM meta_data_connections WHERE id=?',(did,))
            if not local_replay and d and d['kind']=='leads' and d['ciphertext']:
                remote.append({'host':'graph.facebook.com','target':d['provider_id']+'/subscribed_apps','version':d['api_version'],'token':unseal(d['ciphertext']),'eligible':True,'remove_fields':['leadgen'],'app_id':d['app_id']})
        db.execute("UPDATE meta_data_connections SET ciphertext='',state='disconnected' WHERE id IN ("+marks(data_accounts)+')',tuple(data_accounts))
    if scope=='tenant':
        db.execute("UPDATE channels SET status='disconnected',token_env='' WHERE tenant_id=?",(tid,))
        # External storage may include attachments not currently attached to an active account.
        if not local_replay:objects+=identifiers(db,'service_media','tenant_id',[tid],field='object_key')
    ident=uid();targets={'accounts':accounts,'data_accounts':data_accounts,'users':userids}
    db.execute('INSERT INTO deletion_jobs(id,tenant_id,requester_id,scope,targets,remote_ciphertext,remote_status,created_at,source_key) VALUES(?,?,?,?,?,?,?,?,?)',(ident,tid,requester,scope,json.dumps(targets),cipher().encrypt(json.dumps(remote).encode()).decode() if remote else '', 'restore_local_only' if local_replay else 'requires_meta_review' if data_accounts else 'pending',int(time.time()),source))
    for key in set(objects):db.execute('INSERT INTO deletion_objects VALUES(?,?)',(ident,key))
    return ident

@router.post('/api/v2/privacy/deletions',status_code=202)
def request_delete(data:DeleteRequest,u=Depends(user)):
    if not u['tenant_id']:raise HTTPException(403,'customer_workspace_required')
    with transaction() as db:limit(db,'erase:'+u['id'],5,3600)
    if not verify_password(data.password.get_secret_value(),u['password_hash']):raise HTTPException(403,'password_confirmation_failed')
    with transaction() as db:
        from .saas import lock_tenant
        lock_tenant(db,u['tenant_id'])
        accounts=[];data_accounts=[]
        if data.scope=='account':
            permit(u,'settings')
            if one(db,'SELECT id FROM service_accounts WHERE id=? AND tenant_id=?',(data.account_id,u['tenant_id'])):accounts=[get(db,'service_accounts',data.account_id,u,True)['id']]
            else:data_accounts=[get(db,'meta_data_connections',data.account_id,u,True)['id']]
        if data.scope=='tenant':permit(u,'settings')
        if data.scope=='user' and u['role']=='admin' and db.execute("SELECT count(*) FROM users WHERE tenant_id=? AND role='admin' AND active=1",(u['tenant_id'],)).fetchone()[0]<=1:raise HTTPException(409,'transfer_last_admin_or_delete_workspace')
        ident=queue(db,u['tenant_id'],u['id'],data.scope,accounts,data_accounts)
    return {'id':ident,'status':'pending','status_url':'/deletion-status?request='+ident,'backup_retention_days':30}

@router.get('/api/privacy/deletions/{ident}')
def deletion_status(ident:str):
    with transaction() as db:r=one(db,'SELECT state,phase,remote_status,error,completed_at FROM deletion_jobs WHERE id=?',(ident,))
    if not r:raise HTTPException(404,'deletion_request_not_found')
    return {**r,'backup_retention_days':30,'retained':'Minimal pseudonymous tombstone; legal holds remain blocked until reviewed.'}

@router.get('/api/admin/privacy/deletions')
def admin_deletions(u=Depends(user)):
    provider(u)
    with transaction() as db:return rows(db,'SELECT id,tenant_id,scope,state,phase,error,remote_status,created_at,completed_at FROM deletion_jobs ORDER BY created_at DESC LIMIT 200')

@router.post('/api/admin/privacy/deletions/{ident}/retry')
def retry(ident:str,u=Depends(user)):
    provider(u)
    with transaction() as db:
        db.execute("UPDATE deletion_jobs SET state='pending',next_attempt=0 WHERE id=? AND state!='completed'",(ident,))
    return {'ok':True}

@router.post('/api/webhooks/instagram/data-deletion')
@router.post('/api/webhooks/meta/data-deletion')
async def meta_delete(request:Request):
    from .meta_lifecycle import verify_signed_request
    # The privacy status page can be served on a separate public callback origin.
    origin=(os.getenv('WASLA_DELETION_STATUS_ORIGIN') or value('WASLA_OAUTH_ORIGIN')).rstrip('/');p=urlparse(origin)
    if p.scheme!='https' or not p.hostname or p.path or p.query or p.fragment:raise HTTPException(503,'privacy_https_origin_required')
    if request.headers.get('content-type','').split(';')[0]!='application/x-www-form-urlencoded':raise HTTPException(415,'form_required')
    raw=await request.body()
    if len(raw)>16384:raise HTTPException(413,'callback_too_large')
    try:
        form=parse_qs(raw.decode('ascii'),strict_parsing=True)
        if len(form.get('signed_request',[]))!=1:raise ValueError()
        subject,event=verify_signed_request(form['signed_request'][0],'INSTAGRAM_APP_SECRET' if '/instagram/' in request.url.path else 'META_APP_SECRET')
    except (ValueError,UnicodeError):raise HTTPException(400,'invalid_callback_form')
    appid=value('INSTAGRAM_APP_ID' if '/instagram/' in request.url.path else 'META_APP_ID')
    if not appid:raise HTTPException(503,'meta_callback_not_configured')
    with transaction() as db:
        a=identifiers(db,'meta_grants','subject_hash',[digest(subject)],field='account_id')
        a=[x for x in a if one(db,'SELECT app_id FROM meta_grants WHERE account_id=?',(x,))['app_id']==appid]
        data=rows(db,'SELECT id FROM meta_data_connections WHERE app_id=? AND subject_hash=?',(appid,digest(subject)))
        ident=queue(db,None,None,'accounts',a,[x['id'] for x in data],source='meta:'+appid+':'+event)
    return {'url':origin+'/deletion-status?request='+ident,'confirmation_code':ident}

def purge_accounts(db,accounts,data):
    identities=identifiers(db,'service_identities','account_id',accounts);tickets=identifiers(db,'service_tickets','identity_id',identities)
    business_ids=identifiers(db,'meta_connections','account_id',accounts,field='business_account_id')
    messages=identifiers(db,'service_messages','account_id',accounts)
    # Active holds were checked before this transaction; erase expired reasons as well.
    remove(db,'retention_holds','entity_id',accounts+data+identities+tickets)
    for table,column,ids in [('service_outbox','message_id',messages),('service_media_jobs','account_id',accounts),('service_media','ticket_id',tickets),('service_redactions','ticket_id',tickets),('service_ratings','ticket_id',tickets),('service_consents','identity_id',identities),('service_identity_links','left_id',identities),('service_identity_links','right_id',identities),('service_messages','account_id',accounts),('service_tickets','id',tickets),('service_identities','account_id',accounts)]:remove(db,table,column,ids)
    for table in ['meta_page_posts','service_events','service_status_history','service_templates','service_secrets','service_oauth_states','service_channel_snapshots','meta_connections','meta_page_connections','meta_grants','meta_read_watermarks']:remove(db,table,'account_id',accounts)
    remove(db,'service_accounts','id',accounts)
    for business_id in set(business_ids):
        if not one(db,'SELECT account_id FROM meta_connections WHERE business_account_id=?',(business_id,)):
            remove(db,'meta_business_accounts','id',[business_id])
    for table in ['meta_lead_events','meta_data_records']:remove(db,table,'connection_id',data)
    remove(db,'meta_data_connections','id',data)

def purge(db,job,targets):
    purge_accounts(db,targets['accounts'],targets['data_accounts'])
    ids=targets['users'];tid=job['tenant_id']
    for table in ['email_jobs','email_verifications','service_registrations','sessions','notifications','meta_signup_sessions','meta_page_sessions','meta_data_sessions','service_oauth_states']:remove(db,table,'user_id',ids)
    if job['scope']=='user':
        remove(db,'retention_holds','entity_id',ids)
        db.execute("UPDATE users SET email=?,name='Deleted user',password_hash='',active=0 WHERE id=?",('deleted-'+uid()+'@invalid.example',job['requester_id']))
        db.execute("UPDATE audit SET actor_id=NULL,detail='' WHERE actor_id=?",(job['requester_id'],));return
    if job['scope']!='tenant':return
    projectids=identifiers(db,'projects','tenant_id',[tid]);requests=identifiers(db,'projects','tenant_id',[tid],field='request_id')
    remove(db,'milestones','project_id',projectids);remove(db,'projects','id',projectids)
    remove(db,'provider_opportunities','request_id',requests);remove(db,'requests','id',requests)
    # Legacy events are shared envelopes: remove only entries belonging to this workspace.
    phoneids=identifiers(db,'channels','tenant_id',[tid],field='phone_id')
    for e in rows(db,'SELECT id,payload FROM webhook_events'):
        payload=json.loads(e['payload']);entries=[];changed=False
        for entry in payload.get('entry',[]):
            changes=[c for c in entry.get('changes',[]) if str(c.get('value',{}).get('metadata',{}).get('phone_number_id','')) not in phoneids]
            if len(changes)!=len(entry.get('changes',[])):changed=True
            if changes:entries.append({**entry,'changes':changes})
        if not changed:continue
        if entries:db.execute('UPDATE webhook_events SET payload=? WHERE id=?',(json.dumps({**payload,'entry':entries}),e['id']))
        else:remove(db,'webhook_events','id',[e['id']])
    for table in ['messages','conversations','opportunities','tasks','contacts','channels','service_replies','service_settings','service_retention_policy','service_sandbox_invoices','service_usage_counters','service_subscriptions','service_registrations','meta_api_usage','meta_signup_sessions','meta_page_sessions','meta_data_sessions','meta_business_accounts']:
        remove(db,table,'tenant_id',[tid])
    remove(db,'service_oauth_states','user_id',ids)
    db.execute('UPDATE audit SET actor_id=NULL,detail=? WHERE actor_id IN ('+marks(ids)+')',('',*ids)) if ids else None
    remove(db,'audit','tenant_id',[tid]);remove(db,'retention_holds','entity_id',[tid]+ids)
    remove(db,'users','id',ids);remove(db,'tenants','id',[tid])

def process_deletion(job_id=None):
    clock=int(time.time())
    with transaction() as db:
        sql="SELECT * FROM deletion_jobs WHERE state IN ('pending','retry','blocked_hold') AND next_attempt<=?"
        args=(clock,)
        if job_id is not None:
            sql+=' AND id=?'
            args+=(job_id,)
        job=one(db,sql+' ORDER BY created_at LIMIT 1'+(' FOR UPDATE SKIP LOCKED' if postgres() else ''),args)
        if not job:return False
        targets=json.loads(job['targets']);a=targets['accounts'];scopeids=a+targets['data_accounts']+targets['users']+([job['tenant_id']] if job['tenant_id'] else [])
        identities=identifiers(db,'service_identities','account_id',a);scopeids+=identities+identifiers(db,'service_tickets','identity_id',identities)
        scopeids+=identifiers(db,'service_accounts','id',a,field='tenant_id')
        scopeids+=identifiers(db,'meta_data_connections','id',targets['data_accounts'],field='tenant_id')
        # Holding the job row makes each phase exclusive. Operations are idempotent on crash.
        try:
            local_replay=(job['source_key'] or '').startswith('restore:') or job['remote_status']=='restore_local_only'
            if local_replay:
                # Covers replay jobs copied from releases before local-only replay.
                db.execute("UPDATE deletion_jobs SET remote_ciphertext='',remote_status='restore_local_only' WHERE id=?",(job['id'],))
                db.execute('DELETE FROM deletion_objects WHERE job_id=?',(job['id'],))
            remote=json.loads(unseal(job['remote_ciphertext'])) if job['remote_ciphertext'] and not local_replay else []
            if remote:
                target=remote.pop(0);result='requires_meta_review'
                if target['eligible']:
                    try:
                        endpoint='https://'+target['host']+'/'+target['version']+'/'+target['target'];headers={'Authorization':'Bearer '+target['token']}
                        method='DELETE';payload=None
                        if target.get('remove_fields'):
                            from .meta_pages import remaining_fields
                            current=httpx.request('GET',endpoint,headers=headers,timeout=8,follow_redirects=False);current.raise_for_status()
                            fields=remaining_fields(current.json(),target['app_id'],target['remove_fields'])
                            if fields is None:response=httpx.Response(200,json={'success':True})
                            else:
                                method='POST' if fields else 'DELETE';payload={'subscribed_fields':fields} if fields else None
                                response=httpx.request(method,endpoint,headers=headers,json=payload,timeout=8,follow_redirects=False)
                        else:response=httpx.request(method,endpoint,headers=headers,timeout=8,follow_redirects=False)
                        if response.status_code==200 and response.json().get('success') is True:result='confirmed'
                    except Exception:pass
                status='requires_meta_review' if result!='confirmed' or job['remote_status']=='requires_meta_review' else 'confirmed'
                db.execute("UPDATE deletion_jobs SET remote_ciphertext=?,remote_status=? WHERE id=?",(cipher().encrypt(json.dumps(remote).encode()).decode() if remote else '',status,job['id']));return True
            if scopeids and one(db,'SELECT entity_id FROM retention_holds WHERE entity_id IN ('+marks(scopeids)+') AND (held_until IS NULL OR held_until>?)',(*scopeids,now())):
                db.execute("UPDATE deletion_jobs SET state='blocked_hold',error='legal_hold_review_required',next_attempt=? WHERE id=?",(clock+3600,job['id']));return True
            db.execute("UPDATE deletion_jobs SET phase='files' WHERE id=?",(job['id'],))
            obj=one(db,'SELECT object_key FROM deletion_objects WHERE job_id=? LIMIT 1',(job['id'],))
            if obj:
                from .service_media import client
                client().delete_object(Bucket=value('WASLA_MEDIA_BUCKET'),Key=obj['object_key'])
                db.execute('DELETE FROM deletion_objects WHERE job_id=? AND object_key=?',(job['id'],obj['object_key']));return True
            if a and one(db,"SELECT id FROM meta_page_posts WHERE account_id IN ("+marks(a)+") AND state='sending' AND claimed_at>?",(*a,clock-120)):
                db.execute('UPDATE deletion_jobs SET next_attempt=? WHERE id=?',(clock+125,job['id']));return True
            if a and one(db,"SELECT o.id FROM service_outbox o JOIN service_messages m ON m.id=o.message_id WHERE m.account_id IN ("+marks(a)+") AND o.state='sending' AND o.claimed_at>?",(*a,clock-120)):
                db.execute('UPDATE deletion_jobs SET next_attempt=? WHERE id=?',(clock+125,job['id']));return True
            # Savepoint prevents a failed purge from committing a partially deleted relational graph.
            db.execute("UPDATE deletion_jobs SET phase='database' WHERE id=?",(job['id'],))
            db.execute('SAVEPOINT erasure')
            try:purge(db,job,targets)
            except Exception:
                db.execute('ROLLBACK TO SAVEPOINT erasure');raise
            db.execute('RELEASE SAVEPOINT erasure')
            db.execute("UPDATE deletion_jobs SET state='completed',phase='done',remote_ciphertext='',error='',completed_at=?,remote_status=CASE WHEN remote_status='pending' THEN 'not_applicable' ELSE remote_status END WHERE id=?",(clock,job['id']))
        except Exception:
            db.execute("UPDATE deletion_jobs SET state='retry',error='cleanup_failed_retry_required',attempts=attempts+1,next_attempt=? WHERE id=?",(clock+min(3600,30*2**min(job['attempts'],6)),job['id']))
    return True

@router.get('/api/admin/privacy/holds')
def holds(u=Depends(user)):
    provider(u)
    with transaction() as db:return rows(db,'SELECT * FROM retention_holds ORDER BY created_at DESC LIMIT 200')

class Release(BaseModel):
    model_config=ConfigDict(extra='forbid')
    entity_type:str=Field(min_length=1,max_length=80)
    entity_id:str=Field(min_length=1,max_length=80)
    reason:str=Field(min_length=10,max_length=500)

@router.post('/api/admin/privacy/holds/release')
def release_hold(data:Release,u=Depends(user)):
    from .db import audit
    provider(u)
    with transaction() as db:
        if db.execute('DELETE FROM retention_holds WHERE entity_type=? AND entity_id=?',(data.entity_type,data.entity_id)).rowcount!=1:raise HTTPException(404,'hold_not_found')
        audit(db,u,'privacy.hold_released',data.entity_id,data.reason)
    return {'released':True}
