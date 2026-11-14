"""Authenticated V2 service APIs; all IDs are tenant-scoped before use."""
import csv
import hashlib
import io
import json
import os
import re
import time
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from .db import transaction, one, rows, audit, now, uid, postgres
from .main import user, text, choice, operation, assignee
from .security import permit
from . import service as s

router=APIRouter()

def get(db,table,ident,u,lock=False):
    r=one(db,f'SELECT * FROM {table} WHERE id=? AND tenant_id=?'+(' FOR UPDATE' if lock and postgres() else ''),(ident,u['tenant_id']))
    if not r: raise HTTPException(404,'record_not_found')
    if table=='service_tickets' and u['role']=='sales' and r['assignee_id']!=u['id']: raise HTTPException(403,'ticket_not_assigned')
    return r

def version(data,current):
    if type(data.get('version')) is not int or data['version']!=current['version']:
        raise HTTPException(409,'ticket_changed_reload_before_action')

def integer(data,key,lo,hi):
    v=data.get(key)
    if type(v) is not int or not lo<=v<=hi: raise HTTPException(422,'invalid_'+key)
    return v

@router.get('/api/v2/accounts')
def accounts(u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:
        data=rows(db,'SELECT * FROM service_accounts WHERE tenant_id=? ORDER BY created_at',(u['tenant_id'],))
    for a in data:
        a['capabilities']=s.capabilities(a['channel']); a['credentials_configured']=bool(s.token(a))
        a['token_ref']='WASLA_CHANNEL_'+a['id'].upper()+'_TOKEN'
    return {'accounts':data,'capabilities':[s.capabilities(c) for c in sorted(s.CHANNELS)]}

@router.post('/api/v2/accounts',status_code=201)
def create_account(data:dict,u=Depends(user)):
    permit(u,'settings'); channel=choice(data,'channel',s.CHANNELS); pid=text(data,'provider_id',1,120)
    api_version=text(data,'api_version',1,20)
    if not re.fullmatch(r'[A-Za-z0-9_-]+',pid) or not re.fullmatch(r'v\d+\.\d+',api_version): raise HTTPException(422,'invalid_provider_id_or_version')
    aid=uid(); label=text(data,'label',1,120)
    with transaction() as db:
        db.execute('INSERT INTO service_accounts(id,tenant_id,channel,provider_id,label,api_version,state,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (aid,u['tenant_id'],channel,pid,label,api_version,'awaiting_approval' if channel=='tiktok' else 'referral_only' if channel=='snapchat' else 'unverified',now()))
        audit(db,u,'service.account.created',aid,channel)
    return {'id':aid,'token_ref':'WASLA_CHANNEL_'+aid.upper()+'_TOKEN'}

@router.post('/api/v2/accounts/{ident}/check')
def check_account(ident:str,u=Depends(user)):
    permit(u,'settings')
    with transaction() as db: a=get(db,'service_accounts',ident,u)
    try:
        response=s.provider_request(a,'GET'); result=response.json()
    except HTTPException: raise
    except Exception: raise HTTPException(502,'connection_check_failed')
    valid=response.status_code==200 and str(result.get('id'))==a['provider_id']
    with transaction() as db:
        db.execute('UPDATE service_accounts SET state=?,checked_at=? WHERE id=?',('verified' if valid else 'reauthorize',now(),ident))
        audit(db,u,'service.account.checked',ident,'verified_asset' if valid else 'failed')
    if not valid: raise HTTPException(409,'asset_verification_failed')
    return {'asset_verified':True,'live_messaging_verified':bool(a['last_inbound_at'] and a['last_outbound_at'])}

@router.post('/api/v2/accounts/{ident}/disconnect')
def disconnect(ident:str,u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:
        get(db,'service_accounts',ident,u); db.execute("UPDATE service_accounts SET state='disconnected',token_ref='' WHERE id=?",(ident,))
        db.execute('DELETE FROM service_secrets WHERE account_id=?',(ident,))
        db.execute('DELETE FROM service_oauth_states WHERE account_id=?',(ident,))
        audit(db,u,'service.account.disconnected',ident)
    return {'ok':True}

@router.get('/api/webhooks/v2/{channel}')
def verify(channel:str,request:Request):
    if channel not in {'whatsapp','instagram'}: raise HTTPException(409,'unsupported_webhook')
    secret=os.getenv('META_VERIFY_TOKEN',''); q=request.query_params
    import hmac
    if secret and q.get('hub.mode')=='subscribe' and hmac.compare_digest(q.get('hub.verify_token','').encode(),secret.encode()): return PlainTextResponse(q.get('hub.challenge',''))
    raise HTTPException(403,'verification_failed')

@router.post('/api/webhooks/v2/{channel}')
async def receive(channel:str,request:Request):
    if channel not in {'whatsapp','instagram'}: raise HTTPException(409,'unsupported_webhook')
    secret=os.getenv('META_APP_SECRET','') if channel=='whatsapp' else os.getenv('INSTAGRAM_APP_SECRET','')
    if not secret: raise HTTPException(503,'app_secret_missing')
    raw=await request.body()
    if not s.valid_signature(raw,request.headers.get('X-Hub-Signature-256',''),secret): raise HTTPException(401,'invalid_signature')
    try: count=s.persist_events(channel,json.loads(raw))
    except (ValueError,KeyError,TypeError,AttributeError): raise HTTPException(400,'invalid_payload')
    return {'received':True,'events':count}

@router.get('/api/v2/tickets')
def tickets(q:str='',status:str='',channel:str='',u=Depends(user)):
    permit(u,'inbox')
    sql='''SELECT t.*,i.name,i.provider_user_id,a.channel,a.label account_label FROM service_tickets t
        JOIN service_identities i ON i.id=t.identity_id JOIN service_accounts a ON a.id=i.account_id WHERE t.tenant_id=?'''; args=[u['tenant_id']]
    if u['role']=='sales': sql+=' AND t.assignee_id=?'; args.append(u['id'])
    if status: sql+=' AND t.status=?'; args.append(status)
    if channel: sql+=' AND a.channel=?'; args.append(channel)
    sql+=' AND (i.name LIKE ? OR i.provider_user_id LIKE ?) ORDER BY t.updated_at DESC LIMIT 200'; args+=['%'+q[:100]+'%']*2
    with transaction() as db: return rows(db,sql,args)

@router.get('/api/v2/tickets/{ident}')
def ticket(ident:str,u=Depends(user)):
    permit(u,'inbox')
    with transaction() as db:
        t=get(db,'service_tickets',ident,u)
        t['messages']=rows(db,'SELECT * FROM service_messages WHERE ticket_id=? AND tenant_id=? ORDER BY created_at,id LIMIT 1000',(ident,u['tenant_id']))
        t['identity']=get(db,'service_identities',t['identity_id'],u)
        a=get(db,'service_accounts',t['identity']['account_id'],u)
        t['account']={k:a[k] for k in ('id','channel','label','state')}
        t['reply_window_open']=int(time.time())-t['last_customer_at']<86400
        t['attachments']=rows(db,'SELECT id,mime_type,size_bytes,scan_state FROM service_media WHERE ticket_id=? AND tenant_id=?',(ident,u['tenant_id']))
        t['consents']=rows(db,'SELECT * FROM service_consents WHERE identity_id=? AND tenant_id=?',(t['identity_id'],u['tenant_id']))
        return t

@router.patch('/api/v2/tickets/{ident}')
def update_ticket(ident:str,data:dict,u=Depends(user)):
    permit(u,'inbox')
    with transaction() as db:
        t=get(db,'service_tickets',ident,u,True); version(data,t)
        status=choice(data,'status',s.STATES); owner=text(data,'assignee_id',0,120) or None
        if owner:
            assignee(db,owner,u)
            agent=one(db,"SELECT role FROM users WHERE id=?",(owner,))
            if agent['role'] not in {'admin','supervisor','sales'}: raise HTTPException(422,'assignee_requires_inbox_access')
        if u['role']=='sales' and owner!=t['assignee_id']: raise HTTPException(403,'supervisor_assignment_required')
        config=s.settings(db,u['tenant_id'])
        if owner and owner!=t['assignee_id']:
            if postgres(): db.execute('SELECT id FROM users WHERE id=? FOR UPDATE',(owner,))
            load=db.execute("SELECT count(*) FROM service_tickets WHERE assignee_id=? AND status NOT IN ('closed','resolved')",(owner,)).fetchone()[0]
            if load>=config['max_active']: raise HTTPException(409,'assignee_at_capacity')
        resolution=text(data,'resolution_code',0,120)
        if status in {'resolved','closed'} and not resolution: raise HTTPException(422,'resolution_code_required')
        stamp=int(time.time()); resolved=stamp if status in {'resolved','closed'} else None
        if t['resolved_at'] and resolved: resolved=t['resolved_at']
        db.execute('''UPDATE service_tickets SET status=?,assignee_id=?,priority=?,category=?,team=?,branch=?,language=?,summary=?,resolution_code=?,
            automation_paused=?,resolved_at=?,first_resolved_at=COALESCE(first_resolved_at,?),version=version+1,updated_at=? WHERE id=?''',
            (status,owner,choice(data,'priority',{'low','normal','high','urgent'}),text(data,'category',1,80),text(data,'team',1,80),text(data,'branch',0,80),
             choice(data,'language',{'ar','en'}),text(data,'summary',0,2000),resolution,1 if owner else t['automation_paused'],resolved,resolved,stamp,ident))
        audit(db,u,'service.ticket.updated',ident,status)
    return {'ok':True,'version':t['version']+1}

@router.post('/api/v2/tickets/{ident}/messages',status_code=201)
def enqueue(ident:str,data:dict,request:Request,u=Depends(user)):
    permit(u,'inbox'); key=operation(request); direction=choice(data,'direction',{'out','note'}); body=text(data,'body',1,4000)
    if '{{' in body or '}}' in body: raise HTTPException(422,'complete_saved_reply_fields')
    p={'kind':choice({**data,'kind':data.get('kind','text')},'kind',{'text','template','image','document','video'}),'template':text(data,'template',0,120),'language':text(data,'language',0,20),'attachment_id':text(data,'attachment_id',0,120)}
    fingerprint=hashlib.sha256(json.dumps({'ticket':ident,'body':body,'direction':direction,**p},sort_keys=True).encode()).hexdigest()
    with transaction() as db:
        t=get(db,'service_tickets',ident,u,True)
        old=one(db,'SELECT * FROM service_outbox WHERE tenant_id=? AND request_key=?',(u['tenant_id'],key))
        if old:
            if old['payload_hash']!=fingerprint: raise HTTPException(409,'idempotency_key_reused')
            return {'id':old['message_id'],'status':old['state']}
        version(data,t)
        identity=get(db,'service_identities',t['identity_id'],u); a=get(db,'service_accounts',identity['account_id'],u)
        if direction=='out': s.policy(db,a,t,p)
        mid=uid(); stamp=int(time.time()); state='queued' if direction=='out' else 'internal'
        db.execute('INSERT INTO service_messages(id,tenant_id,ticket_id,account_id,direction,body,status,author_id,occurred_at,created_at,kind) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (mid,u['tenant_id'],ident,a['id'],direction,body,state,u['id'],stamp,stamp,p['kind']))
        db.execute('INSERT INTO service_outbox(id,tenant_id,message_id,request_key,payload_hash,payload,state,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (uid(),u['tenant_id'],mid,key,fingerprint,json.dumps(p),'queued' if direction=='out' else 'internal',stamp))
        db.execute('UPDATE service_tickets SET version=version+1,automation_paused=1,updated_at=? WHERE id=?',(stamp,ident))
        audit(db,u,'service.message.'+direction,mid)
    return {'id':mid,'status':state}

@router.post('/api/v2/identities/{ident}/consents')
def consent(ident:str,data:dict,u=Depends(user)):
    permit(u,'inbox'); purpose=choice(data,'purpose',{'template_messages','marketing','identity_link'}); source=text(data,'source',3,500)
    with transaction() as db:
        get(db,'service_identities',ident,u)
        if u['role']=='sales' and not one(db,'SELECT id FROM service_tickets WHERE identity_id=? AND assignee_id=?',(ident,u['id'])): raise HTTPException(403,'identity_not_assigned')
        if data.get('revoke') is True:
            db.execute('UPDATE service_consents SET revoked_at=? WHERE identity_id=? AND purpose=? AND revoked_at IS NULL',(int(time.time()),ident,purpose))
        else: db.execute('INSERT INTO service_consents VALUES(?,?,?,?,?,?,NULL)',(uid(),u['tenant_id'],ident,purpose,source,int(time.time())))
        audit(db,u,'service.consent.revoked' if data.get('revoke') else 'service.consent.recorded',ident,purpose)
    return {'ok':True}

@router.post('/api/v2/identity-links')
def identity_link(data:dict,u=Depends(user)):
    permit(u,'settings'); left=text(data,'left_id'); right=text(data,'right_id'); evidence=text(data,'evidence',10,1000)
    if left==right: raise HTTPException(422,'different_identities_required')
    with transaction() as db:
        get(db,'service_identities',left,u); get(db,'service_identities',right,u)
        ident=uid(); db.execute('INSERT INTO service_identity_links VALUES(?,?,?,?,?,?,?,NULL)',(ident,u['tenant_id'],left,right,evidence,u['id'],int(time.time())))
        audit(db,u,'service.identity.linked',ident)
    return {'id':ident}

@router.delete('/api/v2/identity-links/{ident}')
def unlink(ident:str,u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:
        get(db,'service_identity_links',ident,u); db.execute('UPDATE service_identity_links SET revoked_at=? WHERE id=?',(int(time.time()),ident)); audit(db,u,'service.identity.unlinked',ident)
    return {'ok':True}

@router.get('/api/v2/settings')
def get_settings(u=Depends(user)):
    permit(u,'settings')
    with transaction() as db: return s.settings(db,u['tenant_id'])

@router.put('/api/v2/settings')
def put_settings(data:dict,u=Depends(user)):
    permit(u,'settings'); config={**s.DEFAULT_SETTINGS}
    for k,lo,hi in [('start_hour',0,23),('end_hour',1,24),('sla_minutes',1,1440),('max_active',1,200)]: config[k]=integer(data,k,lo,hi)
    if config['end_hour']<=config['start_hour']: raise HTTPException(422,'invalid_working_hours')
    days=data.get('workdays'); holidays=data.get('holidays',[]); agents=data.get('available_agents',[])
    if not isinstance(days,list) or not days or any(type(x) is not int or not 0<=x<=6 for x in days): raise HTTPException(422,'invalid_workdays')
    if not isinstance(holidays,list) or len(holidays)>366 or any(not isinstance(d,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',d) for d in holidays): raise HTTPException(422,'invalid_holidays')
    if not isinstance(agents,list) or len(agents)>200 or any(not isinstance(a,str) for a in agents): raise HTTPException(422,'invalid_agents')
    config.update(workdays=days,holidays=holidays,available_agents=agents,sending_paused=data.get('sending_paused') is True)
    with transaction() as db:
        for aid in agents: assignee(db,aid,u)
        db.execute('INSERT INTO service_settings VALUES(?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET settings=excluded.settings,updated_at=excluded.updated_at',(u['tenant_id'],json.dumps(config),now()))
        audit(db,u,'service.settings.updated',u['tenant_id'])
    return config

@router.get('/api/v2/replies')
def replies(u=Depends(user)):
    permit(u,'inbox')
    with transaction() as db: return rows(db,'SELECT * FROM service_replies WHERE tenant_id=? ORDER BY title',(u['tenant_id'],))

@router.post('/api/v2/replies',status_code=201)
def create_reply(data:dict,u=Depends(user)):
    permit(u,'inbox')
    if u['role']=='sales': raise HTTPException(403,'supervisor_required')
    with transaction() as db:
        ident=uid(); db.execute('INSERT INTO service_replies VALUES(?,?,?,?,?)',(ident,u['tenant_id'],text(data,'title',1,120),text(data,'body',1,4000),int(time.time()))); audit(db,u,'service.reply.created',ident)
    return {'id':ident}

@router.get('/api/v2/templates')
def templates(u=Depends(user)):
    permit(u,'inbox')
    with transaction() as db: return rows(db,'SELECT * FROM service_templates WHERE tenant_id=?',(u['tenant_id'],))

@router.post('/api/v2/accounts/{ident}/templates/sync')
def sync_templates(ident:str,data:dict,u=Depends(user)):
    permit(u,'settings'); waba=text(data,'business_account_id',1,120)
    if not waba.isdigit(): raise HTTPException(422,'numeric_business_account_id_required')
    with transaction() as db: a=get(db,'service_accounts',ident,u)
    if a['channel']!='whatsapp': raise HTTPException(409,'whatsapp_only')
    # Prove the business-account relationship before accepting its templates.
    try:
        business={**a,'provider_id':waba}
        phones=s.provider_request(business,'GET','/phone_numbers?limit=100').json().get('data',[])
        if not any(str(p.get('id'))==a['provider_id'] for p in phones): raise HTTPException(409,'business_account_relationship_not_verified')
        response=s.provider_request(business,'GET','/message_templates?limit=100')
        if response.status_code!=200: raise HTTPException(409,'template_sync_failed')
        result=response.json()
    except HTTPException: raise
    except Exception: raise HTTPException(502,'template_sync_failed')
    with transaction() as db:
        db.execute("UPDATE service_templates SET status='STALE' WHERE account_id=?",(ident,))
        for t in result.get('data',[]):
            db.execute('''INSERT INTO service_templates VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account_id,name,language)
                DO UPDATE SET status=excluded.status,components=excluded.components,checked_at=excluded.checked_at''',
                (uid(),u['tenant_id'],ident,t['name'],t['language'],t['status'],json.dumps(t.get('components',[])),int(time.time())))
        audit(db,u,'service.templates.synced',ident)
    return {'count':len(result.get('data',[])),'partial':bool(result.get('paging',{}).get('next'))}

@router.get('/api/v2/operations')
def operations(u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:
        return {'inbound':rows(db,'SELECT e.state,count(*) count FROM service_events e JOIN service_accounts a ON a.id=e.account_id WHERE a.tenant_id=? GROUP BY e.state',(u['tenant_id'],)),
            'outbound':rows(db,'SELECT state,count(*) count FROM service_outbox WHERE tenant_id=? GROUP BY state',(u['tenant_id'],)),
            'review':rows(db,"SELECT id,state,error,attempts,created_at FROM service_outbox WHERE tenant_id=? AND state IN ('unknown','blocked','dead','failed') ORDER BY created_at DESC LIMIT 100",(u['tenant_id'],)),
            'dead_events':rows(db,"SELECT e.id,e.error,e.attempts FROM service_events e JOIN service_accounts a ON a.id=e.account_id WHERE a.tenant_id=? AND e.state='dead'",(u['tenant_id'],))}

@router.post('/api/v2/events/{ident}/retry')
def retry(ident:str,u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:
        e=one(db,"SELECT e.id FROM service_events e JOIN service_accounts a ON a.id=e.account_id WHERE e.id=? AND a.tenant_id=? AND e.state='dead'",(ident,u['tenant_id']))
        if not e: raise HTTPException(404,'dead_event_not_found')
        db.execute("UPDATE service_events SET state='pending',attempts=0,next_attempt=0 WHERE id=?",(ident,)); audit(db,u,'service.event.retried',ident)
    return {'ok':True}

@router.post('/api/v2/outbox/{ident}/reconcile')
def reconcile(ident:str,data:dict,u=Depends(user)):
    permit(u,'settings'); evidence=text(data,'evidence',10,1000); action=choice(data,'action',{'cancel','accepted'})
    with transaction() as db:
        o=get(db,'service_outbox',ident,u,True)
        if o['state'] not in {'unknown','blocked','dead','failed'}: raise HTTPException(409,'not_reconcilable')
        pid=text(data,'provider_id',1,300) if action=='accepted' else None
        db.execute('UPDATE service_outbox SET state=?,error=? WHERE id=?',(action,'manually_reconciled',ident))
        db.execute('UPDATE service_messages SET status=?,provider_id=? WHERE id=?',(action,pid,o['message_id']))
        audit(db,u,'service.outbox.reconciled',ident,evidence)
    return {'ok':True}

@router.post('/api/v2/tickets/{ident}/rating')
def rating(ident:str,data:dict,u=Depends(user)):
    permit(u,'inbox'); score=integer(data,'score',1,5); source=text(data,'source',5,500)
    with transaction() as db:
        get(db,'service_tickets',ident,u)
        db.execute('INSERT INTO service_ratings VALUES(?,?,?,?,?,?)',(uid(),u['tenant_id'],ident,score,source,int(time.time())))
        audit(db,u,'service.rating.recorded',ident)
    return {'ok':True}

@router.get('/api/v2/reports')
def reports(start:int=0,end:int=0,channel:str='',team:str='',branch:str='',agent:str='',category:str='',priority:str='',u=Depends(user)):
    permit(u,'reports'); end=end or int(time.time()); start=start or end-30*86400
    if start<0 or end<=start or end-start>366*86400: raise HTTPException(422,'report_range_maximum_366_days')
    sql='SELECT t.* FROM service_tickets t JOIN service_identities i ON i.id=t.identity_id JOIN service_accounts a ON a.id=i.account_id WHERE t.tenant_id=? AND t.created_at>=? AND t.created_at<=?'; args=[u['tenant_id'],start,end]
    for value,column in [(channel,'a.channel'),(team,'t.team'),(branch,'t.branch'),(agent,'t.assignee_id'),(category,'t.category'),(priority,'t.priority')]:
        if value: sql+=' AND '+column+'=?'; args.append(value)
    with transaction() as db:
        data=rows(db,sql,args); config=s.settings(db,u['tenant_id']); ids={t['id'] for t in data}
        outgoing=[m for m in rows(db,"SELECT m.* FROM service_messages m JOIN service_accounts a ON a.id=m.account_id WHERE m.tenant_id=? AND m.direction='out' AND a.channel='whatsapp' AND m.created_at>=? AND m.created_at<=?",(u['tenant_id'],start,end)) if m['ticket_id'] in ids]
        ratings=[r for r in rows(db,'SELECT * FROM service_ratings WHERE tenant_id=?',(u['tenant_id'],)) if r['ticket_id'] in ids]
        coverage=one(db,'SELECT min(created_at) start FROM service_tickets WHERE tenant_id=?',(u['tenant_id'],))['start']
    response=[t['first_human_at']-t['created_at'] for t in data if t['first_human_at'] is not None]
    resolved=[t['resolved_at']-t['created_at'] for t in data if t['resolved_at'] is not None]
    business=[s.business_seconds(t['created_at'],t['first_human_at'],config) for t in data if t['first_human_at'] is not None]
    eligible=[t for t in data if t['first_human_at'] is not None or s.business_seconds(t['created_at'],end,config)>config['sla_minutes']*60]
    within=sum(s.business_seconds(t['created_at'],t['first_human_at'],config)<=config['sla_minutes']*60 for t in eligible if t['first_human_at'] is not None)
    accepted=[m for m in outgoing if m['provider_id']]
    return {'timezone':'Asia/Riyadh','start':start,'end':end,'updated_at':now(),'coverage_start':coverage,'tickets':len(data),
        'backlog':sum(t['status'] not in {'closed','resolved'} for t in data),'first_human_median_seconds':s.percentile(response,.5),'first_human_p90_seconds':s.percentile(response,.9),
        'business_response_median_seconds':s.percentile(business,.5),'resolution_median_seconds':s.percentile(resolved,.5),
        'sla_eligible':len(eligible),'sla_percent':round(within/len(eligible)*100,2) if eligible else None,
        'delivery_eligible':len(accepted),'delivery_percent':round(sum(m['status'] in {'delivered','read'} for m in accepted)/len(accepted)*100,2) if accepted else None,
        'pending_delivery':sum(m['status'] not in {'delivered','read','failed'} for m in accepted),
        'csat_responses':len(ratings),'csat_percent':round(sum(r['score']>=4 for r in ratings)/len(ratings)*100,2) if ratings else None,
        'csat_response_percent':round(len(ratings)/len(data)*100,2) if data else None,
        'booking_conversion_percent':None,
        'definitions':{'response':'First accepted human reply; automated greetings excluded.', 'resolution':'Final resolution; first resolution retained separately. Reopen same ticket.',
        'cohort':'Tickets created in period; backlog is current state of that cohort, not a historical snapshot.', 'sla':'Current configured Riyadh working hours; unanswered overdue tickets included.',
        'delivery':'Provider-accepted messages in the ticket cohort; unknown reading is not unread.', 'csat':'Manual evidenced 1–5 scores, positive >=4; no automated survey is sent.',
        'booking':'Unavailable until a verified booking provider is connected.'}}

@router.get('/api/v2/export')
def export(u=Depends(user)):
    permit(u,'export'); output=io.StringIO(); writer=csv.writer(output); writer.writerow(['ticket_id','status','category','priority','created_at','resolved_at'])
    with transaction() as db:
        for t in rows(db,'SELECT * FROM service_tickets WHERE tenant_id=? ORDER BY created_at',(u['tenant_id'],)):
            writer.writerow([("'"+str(t[k])) if str(t[k]).startswith(('=','+','-','@')) else t[k] for k in ['id','status','category','priority','created_at','resolved_at']])
        audit(db,u,'service.exported',u['tenant_id'])
    return PlainTextResponse('\ufeff'+output.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=wasla-service.csv'})

@router.get('/api/v2/tickets/{ident}/stream')
async def ticket_stream(ident:str,request:Request,u=Depends(user)):
    import asyncio
    from fastapi.responses import StreamingResponse
    permit(u,'inbox')
    with transaction() as db:get(db,'service_tickets',ident,u)
    async def events():
        for _ in range(5):
            if await request.is_disconnected():break
            with transaction() as db:
                active=one(db,"SELECT u.id FROM users u JOIN tenants t ON t.id=u.tenant_id WHERE u.id=? AND u.active=1 AND t.status!='suspended'",(u['id'],))
                if not active:break
                try:t=get(db,'service_tickets',ident,u)
                except HTTPException:break
                # Include delivery status changes, which do not alter the ticket's edit version.
                messages=rows(db,'SELECT id,status FROM service_messages WHERE ticket_id=? AND tenant_id=? ORDER BY id',(ident,u['tenant_id']))
            fingerprint=hashlib.sha256(json.dumps(messages,sort_keys=True).encode()).hexdigest()
            yield 'event: update\ndata: '+json.dumps({'version':t['version'],'messages':fingerprint})+'\n\n'
            await asyncio.sleep(3)
    return StreamingResponse(events(),media_type='text/event-stream',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})
