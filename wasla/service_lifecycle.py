"""Tenant-scoped content retention with holds, preview, and durable tombstones."""
import json
import time
import os
from fastapi import Depends, HTTPException
from .db import transaction, one, rows, postgres, audit, now
from .main import user, text
from .security import permit
from .service_api import router, integer

def candidates(db,tid,days):
    return rows(db,"""SELECT t.* FROM service_tickets t WHERE t.tenant_id=? AND t.status IN ('closed','resolved') AND t.updated_at<?
        AND NOT EXISTS (SELECT 1 FROM service_redactions r WHERE r.ticket_id=t.id)
        AND NOT EXISTS (SELECT 1 FROM retention_holds h WHERE (h.entity_id=t.id OR h.entity_id=t.identity_id OR h.entity_id=t.tenant_id) AND (h.held_until IS NULL OR h.held_until>?))
        AND NOT EXISTS (SELECT 1 FROM service_messages m JOIN service_outbox o ON o.message_id=m.id WHERE m.ticket_id=t.id AND o.state IN ('queued','sending','retry','unknown'))
        ORDER BY t.updated_at LIMIT 100""",(tid,int(time.time())-days*86400,now()))

@router.get('/api/v2/retention')
def retention_preview(u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:
        p=one(db,'SELECT * FROM service_retention_policy WHERE tenant_id=?',(u['tenant_id'],))
        data=candidates(db,u['tenant_id'],p['days']) if p else []
    return {'policy':p,'eligible_ticket_ids':[r['id'] for r in data],'limit':100,'scope':'Closed ticket content and attachments; identity/consent records and protected audit retained. Backups expire under their separate retention policy.'}

@router.put('/api/v2/retention')
def retention_policy(data:dict,u=Depends(user)):
    permit(u,'settings');days=integer(data,'days',30,3650);evidence=text(data,'evidence',10,1000)
    if type(data.get('enabled')) is not bool:raise HTTPException(422,'enabled_boolean_required')
    with transaction() as db:
        db.execute('INSERT INTO service_retention_policy VALUES(?,?,?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET days=excluded.days,enabled=excluded.enabled,evidence=excluded.evidence,updated_at=excluded.updated_at',(u['tenant_id'],days,int(data['enabled']),evidence,now()))
        audit(db,u,'service.retention.policy',u['tenant_id'],str(days))
    return {'ok':True}

def redact_one(u):
    with transaction() as db:
        p=one(db,'SELECT * FROM service_retention_policy WHERE tenant_id=?',(u['tenant_id'],))
        if not p or not p['enabled']:return False
        eligible=candidates(db,u['tenant_id'],p['days'])
        if not eligible:return False
        tid=eligible[0]['id']
        db.execute('SELECT id FROM service_tickets WHERE id=?'+(' FOR UPDATE' if postgres() else ''),(tid,))
        if tid not in {t['id'] for t in candidates(db,u['tenant_id'],p['days'])}:return False
        attachments=rows(db,'SELECT * FROM service_media WHERE ticket_id=?',(tid,))
        if attachments:
            from .service_media import client
            storage=client()
            # Block downloads before deletion. Any failed object deletion rolls back DB redaction.
            for m in attachments:storage.delete_object(Bucket=os.environ['WASLA_MEDIA_BUCKET'],Key=m['object_key'])
        messages=rows(db,'SELECT * FROM service_messages WHERE ticket_id=?',(tid,))
        for m in messages:
            db.execute("UPDATE service_outbox SET payload='{}' WHERE message_id=?",(m['id'],))
            db.execute("UPDATE service_media_jobs SET descriptor='{}',state='redacted',error='' WHERE message_id=?",(m['id'],))
            if m['provider_id']:
                db.execute("UPDATE service_events SET payload='{}' WHERE account_id=? AND event_key=? AND state='processed'",(m['account_id'],'message:'+m['provider_id']))
        db.execute("UPDATE service_messages SET body='[content removed under retention policy]' WHERE ticket_id=?",(tid,))
        db.execute('DELETE FROM service_media WHERE ticket_id=?',(tid,))
        db.execute("UPDATE service_tickets SET summary='',version=version+1 WHERE id=?",(tid,))
        db.execute("UPDATE service_ratings SET source='[retained score; source removed]' WHERE ticket_id=?",(tid,))
        db.execute('INSERT INTO service_redactions VALUES(?,?,?)',(tid,u['tenant_id'],int(time.time())))
        audit(db,u,'service.retention.redacted',tid,'content_and_private_objects; backup_expiry_separate')
    return True

@router.post('/api/v2/retention/run')
def run_retention(u=Depends(user)):
    permit(u,'settings')
    return {'redacted_tickets':int(redact_one(u)),'batch_limit':1}

@router.post('/api/v2/tickets/{ident}/hold')
def hold(ident:str,data:dict,u=Depends(user)):
    from .service_api import get
    permit(u,'settings');reason=text(data,'reason',10,1000)
    with transaction() as db:
        get(db,'service_tickets',ident,u,True)
        if data.get('release') is True:db.execute("DELETE FROM retention_holds WHERE entity_type='service_ticket' AND entity_id=?",(ident,))
        else:db.execute("INSERT INTO retention_holds VALUES('service_ticket',?,?,NULL,?) ON CONFLICT(entity_type,entity_id) DO UPDATE SET reason=excluded.reason,held_until=NULL",(ident,reason,now()))
        audit(db,u,'service.retention.hold_released' if data.get('release') else 'service.retention.held',ident,reason)
    return {'ok':True}
