"""Observed Messenger receipts, not estimates of Meta-wide audience or reach."""
import time
from fastapi import Depends
from .db import one,rows,transaction
from .main import user
from .security import permit
from .service_api import router

def apply_watermark(db,aid,peer,watermark):
    db.execute('INSERT INTO meta_read_watermarks VALUES(?,?,?) ON CONFLICT(account_id,peer_id) DO UPDATE SET watermark_ms=CASE WHEN excluded.watermark_ms>meta_read_watermarks.watermark_ms THEN excluded.watermark_ms ELSE meta_read_watermarks.watermark_ms END',(aid,peer,watermark))
    stamp=one(db,'SELECT watermark_ms FROM meta_read_watermarks WHERE account_id=? AND peer_id=?',(aid,peer))['watermark_ms']
    # Stored dispatch times have second precision: require the entire second to be
    # covered. A boundary-second message is not guessed read.
    messages=rows(db,"""SELECT m.provider_id FROM service_messages m JOIN service_tickets t ON t.id=m.ticket_id JOIN service_identities i ON i.id=t.identity_id
        WHERE m.account_id=? AND i.account_id=? AND i.provider_user_id=? AND m.direction='out' AND m.provider_id IS NOT NULL
        AND m.status IN ('accepted','sent','delivered') AND (m.occurred_at+1)*1000<=?""",(aid,aid,peer,stamp))
    from .service import apply_status
    for m in messages:apply_status(db,aid,m['provider_id'],'read',stamp//1000)

@router.get('/api/v2/meta/messenger/metrics')
def metrics(u=Depends(user)):
    permit(u,'reports')
    with transaction() as db:
        data=rows(db,"""SELECT a.id,a.label,count(m.id) outbound,
            SUM(CASE WHEN m.status IN ('accepted','sent','delivered','read') THEN 1 ELSE 0 END) accepted,
            SUM(CASE WHEN m.status IN ('delivered','read') THEN 1 ELSE 0 END) delivered,
            SUM(CASE WHEN m.status='read' THEN 1 ELSE 0 END) read
            FROM service_accounts a LEFT JOIN service_messages m ON m.account_id=a.id AND m.direction='out' AND m.created_at>=?
            WHERE a.tenant_id=? AND a.channel='messenger' GROUP BY a.id,a.label""",(int(time.time())-30*86400,u['tenant_id']))
    for item in data:item['observed_read_rate']=item['read']/item['accepted'] if item['accepted'] else None
    return {'window_days':30,'source':'signed_webhooks_for_locally_recorded_messages','accounts':data,'unavailable':{'unique_audience':None,'global_reach':None,'historical_reads_before_connection':None},'limitation':'Receipt absence is not proof of unread. Boundary-second messages remain unconfirmed.'}
