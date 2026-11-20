"""Sandbox commercial model. No payment gateway, tax invoice or Meta reselling."""
import json
from datetime import datetime, timezone
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from .db import transaction, one, rows, uid, now, audit, postgres
from .main import user, provider, email
from .security import permit, limit, digest, hash_password
from .runtime_configuration import value
from .service_api import router

class Limits(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    seats:int=Field(default=25,ge=1,le=10000)
    channels:int=Field(default=10,ge=0,le=10000)
    meta_accounts:int=Field(default=10,ge=0,le=10000)
    api_requests:int=Field(default=10000,ge=0,le=100000000)

class Plan(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    name:str=Field(min_length=2,max_length=100)
    currency:str=Field(pattern=r'^[A-Z]{3}$')
    monthly_amount_minor:int=Field(ge=0,le=100000000)
    message_limit:int=Field(ge=0,le=10000000)
    limits:Limits=Field(default_factory=Limits)

class Subscription(BaseModel):
    model_config=ConfigDict(extra='forbid')
    plan_id:str=Field(min_length=1,max_length=80)
    status:str=Field(pattern=r'^(trial|paused)$')

class Registration(BaseModel):
    model_config=ConfigDict(extra='forbid')
    company:str=Field(min_length=2,max_length=120)
    name:str=Field(min_length=2,max_length=120)
    email:str=Field(max_length=254)
    password:SecretStr
    terms_version:str=Field(pattern=r'^[A-Za-z0-9._-]{1,80}$')
    plan_id:str=Field(default='',max_length=80)

def period():return datetime.now(timezone.utc).strftime('%Y-%m')

def lock_tenant(db,tid):
    t=one(db,'SELECT id FROM tenants WHERE id=?'+(' FOR UPDATE' if postgres() else ''),(tid,))
    if not t:raise HTTPException(404,'workspace_not_found')

def consume_message(db,tid):
    # Same transaction as outbox insertion. Retries/idempotent requests are not billed twice.
    lock_tenant(db,tid)
    subscription=one(db,'SELECT * FROM service_subscriptions WHERE tenant_id=?',(tid,))
    if not subscription:return # Existing contracts are unchanged until explicitly assigned.
    if subscription['status']!='trial':raise HTTPException(409,'subscription_paused')
    ceiling=json.loads(subscription['snapshot'])['message_limit'];month=period()
    db.execute("INSERT INTO service_usage_counters VALUES(?,?,'outbound_messages',0) ON CONFLICT(tenant_id,period,metric) DO NOTHING",(tid,month))
    if db.execute("UPDATE service_usage_counters SET used=used+1 WHERE tenant_id=? AND period=? AND metric='outbound_messages' AND used<?",(tid,month,ceiling)).rowcount!=1:
        raise HTTPException(429,'monthly_message_quota_exceeded')

@router.get('/api/admin/commerce')
def administration(u=Depends(user)):
    provider(u)
    with transaction() as db:return {'mode':'sandbox','plans':plan_list(db,True),
        'subscriptions':rows(db,'SELECT s.*,t.name FROM service_subscriptions s JOIN tenants t ON t.id=s.tenant_id'),
        'registrations':rows(db,'SELECT r.*,t.name,u.email FROM service_registrations r JOIN tenants t ON t.id=r.tenant_id JOIN users u ON u.id=r.user_id')}

@router.post('/api/admin/commerce/plans',status_code=201)
def create_plan(data:Plan,u=Depends(user)):
    provider(u);ident=uid()
    with transaction() as db:
        db.execute('INSERT INTO service_plans VALUES(?,?,?,?,?,1,?)',(ident,data.name,data.currency,data.monthly_amount_minor,data.message_limit,now()))
        db.execute('INSERT INTO service_plan_limits VALUES(?,?)',(ident,json.dumps(data.limits.model_dump())))
        audit(db,u,'commerce.plan.created',ident,'sandbox')
    return {'id':ident,'mode':'sandbox'}

@router.put('/api/admin/commerce/subscriptions/{tenant_id}')
def assign(tenant_id:str,data:Subscription,u=Depends(user)):
    provider(u)
    with transaction() as db:
        lock_tenant(db,tenant_id)
        plan=one(db,'SELECT * FROM service_plans WHERE id=? AND active=1',(data.plan_id,))
        if not plan:raise HTTPException(404,'plan_not_found')
        limit_row=one(db,'SELECT limits_json FROM service_plan_limits WHERE plan_id=?',(data.plan_id,))
        from .entitlements import DEFAULT,counts
        plan['limits']=json.loads(limit_row['limits_json']) if limit_row else DEFAULT
        if any(n>plan['limits'][k] for k,n in counts(db,tenant_id).items()):raise HTTPException(409,'plan_below_current_usage')
        db.execute('INSERT INTO service_subscriptions VALUES(?,?,?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET plan_id=excluded.plan_id,snapshot=excluded.snapshot,status=excluded.status,updated_at=excluded.updated_at',(tenant_id,data.plan_id,json.dumps(plan),data.status,now()))
        audit(db,u,'commerce.subscription.assigned',tenant_id,data.status)
    return {'mode':'sandbox','status':data.status}

@router.get('/api/v2/billing')
def billing(u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:
        sub=one(db,'SELECT * FROM service_subscriptions WHERE tenant_id=?',(u['tenant_id'],))
        if sub:sub['snapshot']=json.loads(sub['snapshot'])
        invoices=rows(db,'SELECT * FROM service_sandbox_invoices WHERE tenant_id=? ORDER BY created_at DESC LIMIT 100',(u['tenant_id'],))
        for invoice in invoices:invoice['snapshot']=json.loads(invoice['snapshot'])
        from .entitlements import DEFAULT,counts
        limits={**DEFAULT,**(sub['snapshot'].get('limits',{}) if sub else {})}
        return {'limits':limits,'counts':counts(db,u['tenant_id']),'mode':'sandbox','payment_collection_enabled':False,'meta_charges_included':False,'subscription':sub,'period':period(),
            'usage':rows(db,'SELECT metric,used FROM service_usage_counters WHERE tenant_id=? AND period=?',(u['tenant_id'],period())),'invoices':invoices}

@router.post('/api/v2/billing/invoice')
def invoice(u=Depends(user)):
    permit(u,'settings');tid=u['tenant_id'];month=period()
    with transaction() as db:
        lock_tenant(db,tid)
        existing=one(db,'SELECT id FROM service_sandbox_invoices WHERE tenant_id=? AND period=?',(tid,month))
        if existing:return {**existing,'mode':'sandbox'}
        sub=one(db,'SELECT * FROM service_subscriptions WHERE tenant_id=?',(tid,))
        if not sub:raise HTTPException(409,'subscription_required')
        ident=uid();snapshot={'plan':json.loads(sub['snapshot']),'status':'draft','tax_document':False,'meta_charges_included':False}
        db.execute('INSERT INTO service_sandbox_invoices VALUES(?,?,?,?,?)',(ident,tid,month,json.dumps(snapshot),now()))
        audit(db,u,'commerce.invoice.drafted',ident,'sandbox_not_payable')
    return {'id':ident,'mode':'sandbox'}

@router.post('/api/auth/register',status_code=202)
def register(data:Registration,request:Request):
    from .launch import signup_policy
    policy=signup_policy()
    if not policy['signup_enabled']:raise HTTPException(409,'self_signup_not_enabled')
    if data.terms_version!=policy['terms_version']:raise HTTPException(409,'signup_terms_changed_reload')
    with transaction() as db:limit(db,'signup:'+digest(request.client.host),5,3600)
    from .email_verification import configuration,enqueue
    configuration()
    address=email(data.email);hashed=hash_password(data.password.get_secret_value());tid=uid();ident=uid()
    with transaction() as db:
        plan=one(db,'SELECT * FROM service_plans WHERE id=? AND active=1',(data.plan_id,)) if data.plan_id else one(db,'SELECT * FROM service_plans WHERE active=1 ORDER BY created_at LIMIT 1')
        if not plan:raise HTTPException(409,'no_signup_plan_available')
        limitrow=one(db,'SELECT limits_json FROM service_plan_limits WHERE plan_id=?',(plan['id'],))
        from .entitlements import DEFAULT
        plan['limits']=json.loads(limitrow['limits_json']) if limitrow else DEFAULT
        # Uniform result for existing addresses, no password reset or account takeover.
        if not one(db,'SELECT id FROM users WHERE email=?',(address,)):
            db.execute("INSERT INTO tenants(id,name,status,created_at) VALUES(?,?,'setup',?)",(tid,data.company,now()))
            inserted=db.execute("INSERT INTO users VALUES(?,?,?,?,?,'admin',0,?) ON CONFLICT(email) DO NOTHING",(ident,tid,address,data.name,hashed,now())).rowcount
            if not inserted:
                db.execute("DELETE FROM tenants WHERE id=?",(tid,))
                return {"status":"pending_email_verification","mode":policy['mode']}
            db.execute("INSERT INTO service_registrations VALUES(?,?,'pending',?,'',?)",(tid,ident,data.terms_version,now()))
            db.execute("INSERT INTO service_subscriptions VALUES(?,?,?,'trial',?)",(tid,plan['id'],json.dumps(plan),now()))
            enqueue(db,ident)
    return {'status':'pending_email_verification','mode':policy['mode']}

@router.post('/api/admin/commerce/registrations/{tenant_id}/approve')
def approve(tenant_id:str,data:dict,u=Depends(user)):
    provider(u)
    evidence=data.get('evidence','')
    if not isinstance(evidence,str) or not 10<=len(evidence)<=500:raise HTTPException(422,'verification_evidence_required')
    with transaction() as db:
        lock_tenant(db,tenant_id)
        reg=one(db,'SELECT * FROM service_registrations WHERE tenant_id=?',(tenant_id,))
        if not reg:raise HTTPException(404,'registration_not_found')
        if not one(db,'SELECT tenant_id FROM service_subscriptions WHERE tenant_id=?',(tenant_id,)):raise HTTPException(409,'assign_sandbox_plan_first')
        verified=one(db,'SELECT verified_at FROM email_verifications WHERE user_id=?',(reg['user_id'],))
        if not verified or not verified['verified_at']:raise HTTPException(409,'email_verification_required')
        from .entitlements import capacity
        account=one(db,'SELECT active FROM users WHERE id=?',(reg['user_id'],))
        if not account['active']:capacity(db,tenant_id,'seat')
        db.execute("UPDATE service_registrations SET status='approved',review_evidence=? WHERE tenant_id=?",(evidence,tenant_id))
        db.execute('UPDATE users SET active=1 WHERE id=?',(reg['user_id'],))
        audit(db,u,'commerce.registration.approved',tenant_id,'sandbox')
    return {'status':'approved','mode':'sandbox','production_enabled':False}


@router.get('/api/public/plans')
def public_plans():
    from .launch import signup_policy
    from .email_verification import configuration
    policy=signup_policy()
    with transaction() as db:plans=plan_list(db)
    if policy['signup_enabled'] and plans:
        try:configuration()
        except HTTPException as exc:
            if exc.status_code!=503:raise
            policy['signup_enabled']=False
    else:policy['signup_enabled']=False
    return {**policy,'payment_collection_enabled':False,'plans':plans}

@router.post('/api/v2/billing/plan')
def choose_plan(data:Subscription,u=Depends(user)):
    permit(u,'settings')
    if data.status!='trial':raise HTTPException(422,'self_service_trial_only')
    # Sandbox selection only, with the same backend capacity validation as provider assignment.
    return assign(u['tenant_id'],data,{**u,'role':'provider'})


def plan_list(db,all_plans=False):
    from .entitlements import DEFAULT
    plans=rows(db,'SELECT p.*,l.limits_json FROM service_plans p LEFT JOIN service_plan_limits l ON l.plan_id=p.id '+('' if all_plans else 'WHERE p.active=1 ')+'ORDER BY p.created_at')
    for p in plans:p['limits']=json.loads(p.pop('limits_json') or json.dumps(DEFAULT))
    return plans

@router.patch('/api/admin/commerce/plans/{ident}')
def plan_update(ident:str,data:Plan,u=Depends(user)):
    provider(u)
    with transaction() as db:
        if not one(db,'SELECT id FROM service_plans WHERE id=?',(ident,)):raise HTTPException(404,'plan_not_found')
        db.execute('UPDATE service_plans SET name=?,currency=?,monthly_amount_minor=?,message_limit=? WHERE id=?',(data.name,data.currency,data.monthly_amount_minor,data.message_limit,ident))
        db.execute('INSERT INTO service_plan_limits VALUES(?,?) ON CONFLICT(plan_id) DO UPDATE SET limits_json=excluded.limits_json',(ident,json.dumps(data.limits.model_dump())))
        audit(db,u,'commerce.plan.updated',ident,'existing_subscriptions_unchanged')
    return {'mode':'sandbox','existing_subscriptions':'unchanged_until_reassigned'}
