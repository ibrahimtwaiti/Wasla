"""Read-only Snap profile capability and explicit release evidence per account."""
import json
import httpx
from fastapi import Depends, HTTPException
from .db import transaction, one, audit, now
from .main import user, text
from .security import permit
from .service_api import router, get
from . import service as s

@router.put('/api/v2/accounts/{ident}/approval')
def approval(ident:str,data:dict,u=Depends(user)):
    permit(u,'settings');reference=text(data,'reference',10,1000)
    with transaction() as db:
        get(db,'service_accounts',ident,u)
        db.execute('UPDATE service_accounts SET approval_reference=? WHERE id=?',(reference,ident));audit(db,u,'service.account.approval_recorded',ident)
    return {'ok':True,'note':'Evidence alone does not enable unsupported capabilities or prove a live test.'}

@router.post('/api/v2/accounts/{ident}/snap/sync')
def snap_sync(ident:str,u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:a=get(db,'service_accounts',ident,u)
    if a['channel']!='snapchat':raise HTTPException(409,'snapchat_only')
    if not a['approval_reference']:raise HTTPException(409,'snap_allowlist_and_asset_evidence_required')
    token=s.token(a)
    if not token:raise HTTPException(409,'channel_credentials_missing')
    base='https://businessapi.snapchat.com/v1/public_profiles/'+a['provider_id'];results=[]
    for suffix in ('','/stats?assetType=PROFILE&granularity=LIFETIME'):
        try:r=httpx.get(base+suffix,headers={'Authorization':'Bearer '+token},timeout=15,follow_redirects=False);data=r.json()
        except Exception:raise HTTPException(502,'snap_profile_unavailable')
        if r.status_code!=200 or data.get('request_status')!='SUCCESS':raise HTTPException(409,'snap_profile_access_not_verified')
        results.append(data)
    profiles=results[0].get('public_profiles',[])
    if not any(str(p.get('public_profile',{}).get('id'))==a['provider_id'] and p.get('sub_request_status')=='SUCCESS' for p in profiles):raise HTTPException(409,'snap_profile_id_mismatch')
    with transaction() as db:
        db.execute('INSERT INTO service_channel_snapshots VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET profile=excluded.profile,metrics=excluded.metrics,fetched_at=excluded.fetched_at',(ident,u['tenant_id'],json.dumps(results[0]),json.dumps(results[1]),now()))
        audit(db,u,'service.snap.profile_synced',ident)
    return {'ok':True,'customer_messaging':False}

@router.get('/api/v2/accounts/{ident}/snap')
def snap_read(ident:str,u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:
        get(db,'service_accounts',ident,u)
        r=one(db,'SELECT * FROM service_channel_snapshots WHERE account_id=? AND tenant_id=?',(ident,u['tenant_id']))
    return {'profile':json.loads(r['profile']) if r else None,'metrics':json.loads(r['metrics']) if r else None,'fetched_at':r['fetched_at'] if r else None,'customer_messaging':False}

@router.get('/api/v2/readiness')
def readiness(u=Depends(user)):
    import os
    from .db import rows
    permit(u,'settings')
    with transaction() as db:accounts=rows(db,'SELECT * FROM service_accounts WHERE tenant_id=?',(u['tenant_id'],))
    return {'accounts':[{'id':a['id'],'channel':a['channel'],'approval_documented':bool(a['approval_reference']),'credentials_configured':bool(s.token(a)),'inbound_observed':bool(a['last_inbound_at']),'outbound_accepted':bool(a['last_outbound_at']),'expires_at':a['expires_at'],'state':a['state']} for a in accounts],
        'media_configuration':{k:bool(os.getenv(k)) for k in ['WASLA_MEDIA_ENDPOINT','WASLA_MEDIA_BUCKET','WASLA_MEDIA_ACCESS_KEY_ID','WASLA_MEDIA_SECRET_ACCESS_KEY','WASLA_MEDIA_SCANNER_URL','WASLA_MEDIA_SCANNER_TOKEN']},
        'tiktok':'blocked_pending_approved_business_messaging_contract','production_approved':False}

@router.post('/api/v2/identities/{ident}/crm')
def link_crm(ident:str,data:dict,u=Depends(user)):
    from .main import phone
    from .db import uid
    permit(u,'settings');evidence=text(data,'evidence',10,1000)
    with transaction() as db:
        identity=get(db,'service_identities',ident,u,True)
        cid=text(data,'customer_id',0,120)
        if cid:
            contact=one(db,'SELECT id FROM contacts WHERE id=? AND tenant_id=?',(cid,u['tenant_id']))
            if not contact:raise HTTPException(404,'customer_not_found')
        else:
            number=phone(text(data,'phone'));name=text(data,'name',1,200);cid=uid()
            # Explicit identity evidence, never merge by name or profile image.
            db.execute("INSERT INTO contacts(id,tenant_id,name,phone,tags,marketing_consent,consent_source,created_at) VALUES(?,?,?,?,?,0,'',?) ON CONFLICT(tenant_id,phone) DO NOTHING",(cid,u['tenant_id'],name,number,'service-v2',now()))
            cid=one(db,'SELECT id FROM contacts WHERE tenant_id=? AND phone=?',(u['tenant_id'],number))['id']
        if identity['customer_id'] and identity['customer_id']!=cid:raise HTTPException(409,'unlink_existing_customer_first')
        db.execute('UPDATE service_identities SET customer_id=?,verified_at=? WHERE id=?',(cid,now(),ident));audit(db,u,'service.identity.crm_linked',ident,evidence)
    return {'customer_id':cid}

@router.delete('/api/v2/identities/{ident}/crm')
def unlink_crm(ident:str,u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:
        get(db,'service_identities',ident,u,True);db.execute('UPDATE service_identities SET customer_id=NULL,verified_at=NULL WHERE id=?',(ident,));audit(db,u,'service.identity.crm_unlinked',ident)
    return {'ok':True}

@router.get('/api/v2/tickets/{ident}/suggestions')
def suggestions(ident:str,u=Depends(user)):
    """Local retrieval from supervisor-approved replies only; never executes tools."""
    import re
    from .db import rows
    permit(u,'inbox')
    with transaction() as db:
        t=get(db,'service_tickets',ident,u)
        m=one(db,"SELECT body FROM service_messages WHERE ticket_id=? AND direction='in' ORDER BY created_at DESC LIMIT 1",(ident,))
        replies=rows(db,'SELECT * FROM service_replies WHERE tenant_id=?',(u['tenant_id'],))
    words=set(re.findall(r'\w{3,}',m['body'].lower() if m else ''))
    ranked=[]
    for reply in replies:
        overlap=words & set(re.findall(r'\w{3,}',(reply['title']+' '+reply['body']).lower()))
        if overlap:ranked.append({'id':reply['id'],'title':reply['title'],'body':reply['body'],'matched_terms':len(overlap)})
    ranked.sort(key=lambda r:(-r['matched_terms'],r['id']))
    return {'suggestions':ranked[:3],'requires_human_review':True,'automatic_send':False,'source':'supervisor_saved_replies','automation_paused':bool(t['automation_paused'])}
