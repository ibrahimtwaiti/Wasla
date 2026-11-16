"""Tenant-scoped queue health; only aggregate counts leave this module."""
import time
from .db import transaction, rows
from .service import settings


def queue_health(tenant_id, clock=None, max_age=300):
    clock=int(time.time()) if clock is None else clock
    if max_age<1:raise ValueError('invalid queue age threshold')
    with transaction() as db:
        paused=settings(db,tenant_id)['sending_paused']
        inbound=rows(db,"SELECT e.state,e.received_at created_at,e.next_attempt FROM service_events e JOIN service_accounts a ON a.id=e.account_id WHERE a.tenant_id=? AND e.state IN ('pending','dead')",(tenant_id,))
        outbound=rows(db,"SELECT state,created_at,next_attempt,claimed_at FROM service_outbox WHERE tenant_id=? AND state IN ('queued','retry','sending','unknown','dead','failed','blocked')",(tenant_id,))
        media=rows(db,"SELECT j.state,m.created_at,j.next_attempt FROM service_media_jobs j JOIN service_messages m ON m.id=j.message_id AND m.tenant_id=j.tenant_id WHERE j.tenant_id=? AND j.state IN ('pending','dead','rejected')",(tenant_id,))
    result={}
    for name,items,waiting in [('inbound',inbound,{'pending'}),('outbound',outbound,{'queued','retry'}),('media',media,{'pending'})]:
        pending=[x for x in items if x['state'] in waiting]
        # A future retry is deliberately deferred, not evidence that the worker stopped.
        due=[x for x in pending if x['next_attempt']<=clock]
        age=max([max(0,clock-max(x['created_at'],x['next_attempt'])) for x in due],default=0)
        review=sum(x['state'] in {'dead','unknown','failed','blocked','rejected'} for x in items)
        stuck=sum(x['state']=='sending' and (x['claimed_at'] is None or clock-x['claimed_at']>300) for x in items)
        is_paused=name=='outbound' and paused
        result[name]={'pending':len(pending),'due':len(due),'oldest_due_seconds':age,'needs_review':review,'stuck_sending':stuck,'paused':is_paused,'healthy':review==0 and stuck==0 and (is_paused or age<=max_age)}
    return {'queues':result,'healthy':all(q['healthy'] for q in result.values()),'threshold_seconds':max_age,'measured_at':clock,'note':'Queue state is not a worker heartbeat or proof of platform delivery.'}
