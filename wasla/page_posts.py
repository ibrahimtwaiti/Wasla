"""Explicit Page text publishing, no advertising or automatic ambiguous retries."""
import json,time
from fastapi import Depends,HTTPException,Header
from pydantic import BaseModel,Field,ConfigDict
from .db import transaction,one,rows,uid,audit,postgres
from .main import user
from .security import permit,digest
from .service_api import router,get
from .meta_pages import config
from .meta_onboarding import graph,throttle
from .service import token

class Post(BaseModel):
    model_config=ConfigDict(extra='forbid')
    body:str=Field(min_length=1,max_length=4000)
    confirmation:str=Field(pattern='^PUBLISH$')

def eligible(a):
    if a['channel']!='messenger' or a['state']!='verified':raise HTTPException(409,'verified_page_required')
    if not {'pages_manage_posts','pages_read_engagement','pages_show_list'}.issubset(json.loads(a['scopes'])):raise HTTPException(409,'pages_manage_posts_permission_required')
    if a['expires_at'] and a['expires_at']<=int(time.time()):raise HTTPException(409,'meta_reauthorization_required')

@router.get('/api/v2/meta/pages/{ident}/posts')
def listing(ident:str,u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:
        a=get(db,'service_accounts',ident,u)
        try:eligible(a);config(u);available=True;reason=''
        except HTTPException as e:available=False;reason=e.detail
        return {'available':available,'reason':reason,'posts':rows(db,'SELECT id,body,state,remote_id,error,created_at FROM meta_page_posts WHERE account_id=? ORDER BY created_at DESC LIMIT 100',(ident,))}

@router.post('/api/v2/meta/pages/{ident}/posts',status_code=202)
def publish(ident:str,data:Post,idempotency_key:str=Header(alias='Idempotency-Key'),u=Depends(user)):
    config(u);throttle(u['tenant_id'])
    if not 8<=len(idempotency_key)<=128 or not data.body.strip():raise HTTPException(422,'invalid_page_post')
    with transaction() as db:
        from .saas import lock_tenant
        lock_tenant(db,u['tenant_id']);a=get(db,'service_accounts',ident,u,True);eligible(a)
        hashed=digest(ident+'\n'+data.body)
        existing=one(db,'SELECT id,payload_hash FROM meta_page_posts WHERE tenant_id=? AND request_key=?',(u['tenant_id'],idempotency_key))
        if existing:
            if existing['payload_hash']!=hashed:raise HTTPException(409,'idempotency_payload_changed')
            return {'id':existing['id']}
        ident_=uid();db.execute('INSERT INTO meta_page_posts(id,tenant_id,account_id,author_id,request_key,payload_hash,body,created_at) VALUES(?,?,?,?,?,?,?,?)',(ident_,u['tenant_id'],ident,u['id'],idempotency_key,hashed,data.body,int(time.time())))
        audit(db,u,'meta.page.post.queued',ident_)
    return {'id':ident_}

def process_post():
    clock=int(time.time())
    with transaction() as db:
        db.execute("UPDATE meta_page_posts SET state='unknown',error='dispatch_outcome_unknown' WHERE state='sending' AND claimed_at<?",(clock-120,))
        p=one(db,"SELECT * FROM meta_page_posts WHERE state IN ('queued','retry') AND next_attempt<=? LIMIT 1"+(' FOR UPDATE SKIP LOCKED' if postgres() else ''),(clock,))
        if not p:return False
        a=one(db,"SELECT a.* FROM service_accounts a JOIN tenants t ON t.id=a.tenant_id WHERE a.id=? AND t.status!='suspended'",(p['account_id'],))
        author=one(db,"SELECT * FROM users WHERE id=? AND active=1 AND tenant_id=? AND role='admin'",(p['author_id'],p['tenant_id']))
        try:
            if not a or not author:raise HTTPException(409,'page_authorization_changed')
            eligible(a);config(author)
            credential=token(a)
            if not credential:raise HTTPException(409,'page_token_required')
        except HTTPException as e:
            db.execute("UPDATE meta_page_posts SET state='blocked',error=? WHERE id=?",(str(e.detail),p['id']));return True
        db.execute("UPDATE meta_page_posts SET state='sending',claimed_at=?,attempts=attempts+1 WHERE id=?",(clock,p['id']))
    state='unknown';error='dispatch_outcome_unknown';remote=None
    try:
        result=graph(p['tenant_id'],a['api_version'],'page_publish',a['provider_id']+'/feed',token=credential,method='POST',payload={'message':p['body']})
        if isinstance(result.get('id'),str) and result['id']:state='published';remote=result['id'];error=''
    except HTTPException as e:
        error=str(e.detail)
        if e.status_code==429:state='retry' if p['attempts']<4 else 'failed'
        elif e.status_code in (403,409) or error in {'meta_10','meta_100','meta_190','meta_200'}:state='blocked'
    except Exception:pass # Ambiguous dispatch is never automatically sent again.
    with transaction() as db:db.execute('UPDATE meta_page_posts SET state=?,error=?,remote_id=?,next_attempt=? WHERE id=?',(state,error,remote,clock+min(3600,60*2**p['attempts']),p['id']))
    return True
