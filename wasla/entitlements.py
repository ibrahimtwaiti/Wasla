"""Authoritative tenant quotas, serialized with the tenant row on PostgreSQL."""
import json
from fastapi import HTTPException
from .db import one,postgres
from .saas import period

DEFAULT={'seats':25,'channels':10,'meta_accounts':10,'api_requests':10000}

def subscription(db,tid):
    t=one(db,'SELECT status FROM tenants WHERE id=?'+(' FOR UPDATE' if postgres() else ''),(tid,))
    if not t or t['status']=='suspended':raise HTTPException(403,'workspace_unavailable')
    s=one(db,'SELECT * FROM service_subscriptions WHERE tenant_id=?',(tid,))
    if s and s['status']=='paused':raise HTTPException(409,'subscription_paused')
    return {**DEFAULT,**(json.loads(s['snapshot']).get('limits',{}) if s else {})}

def counts(db,tid):
    count=lambda sql:db.execute(sql,(tid,)).fetchone()[0]
    service=count("SELECT count(*) FROM service_accounts WHERE tenant_id=? AND state!='disconnected'")
    legacy=count("SELECT count(*) FROM channels WHERE tenant_id=? AND status!='disconnected'")
    meta=count("SELECT count(*) FROM service_accounts WHERE tenant_id=? AND state!='disconnected' AND channel IN ('whatsapp','instagram','messenger')")
    data=count("SELECT count(*) FROM meta_data_connections WHERE tenant_id=? AND state!='disconnected'")
    return {'seats':count('SELECT count(*) FROM users WHERE tenant_id=? AND active=1'),'channels':service+legacy,'meta_accounts':meta+legacy+data}

def capacity(db,tid,kind,channel=None):
    limits=subscription(db,tid);used=counts(db,tid)
    keys=['seats'] if kind=='seat' else ['meta_accounts'] if kind=='data' else ['channels']+(['meta_accounts'] if channel in {'whatsapp','instagram','messenger'} else [])
    for key in keys:
        if used[key]>=limits[key]:raise HTTPException(409,'plan_limit_'+key)

def consume_api(db,tid):
    ceiling=subscription(db,tid)['api_requests'];month=period()
    db.execute("INSERT INTO service_usage_counters VALUES(?,?,'api_requests',0) ON CONFLICT(tenant_id,period,metric) DO NOTHING",(tid,month))
    if db.execute("UPDATE service_usage_counters SET used=used+1 WHERE tenant_id=? AND period=? AND metric='api_requests' AND used<?",(tid,month,ceiling)).rowcount!=1:raise HTTPException(429,'plan_limit_api_requests')

def page_ownership(db,tid,page_id):
    # The same physical Page is shared by Messenger and Lead Ads. Serialize
    # claims across both tables so two tenants cannot race different services.
    if postgres():db.execute('SELECT pg_advisory_xact_lock(hashtext(?))',('wasla:meta-page:'+page_id,))
    owners=[r['tenant_id'] for r in db.execute("SELECT tenant_id FROM service_accounts WHERE channel='messenger' AND provider_id=? UNION SELECT tenant_id FROM meta_data_connections WHERE kind='leads' AND provider_id=?",(page_id,page_id)).fetchall()]
    if any(owner!=tid for owner in owners):raise HTTPException(409,'meta_asset_unavailable')
