"""Durable inbound webhook worker. No external message sending in V1."""
import json
import time
from .db import transaction, one, uid, now, initialize
from .main import phone

def process_one():
    # One SQLite writer claims + processes atomically. Crashes roll back the event.
    event_id=None
    try:
        with transaction() as db:
            event=one(db,"SELECT * FROM webhook_events WHERE status='pending' ORDER BY created_at LIMIT 1")
            if not event: return False
            event_id=event['id']; payload=json.loads(event['payload'])
            for entry in payload['entry']:
                for change in entry.get('changes',[]):
                    v=change.get('value',{}); pid=v.get('metadata',{}).get('phone_number_id')
                    channel=one(db,"SELECT c.* FROM channels c JOIN tenants t ON c.tenant_id=t.id WHERE c.phone_id=? AND t.status!='suspended'",(pid,))
                    if not channel: raise ValueError('unknown_or_suspended_channel')
                    tid=channel['tenant_id']; names={c['wa_id']:c.get('profile',{}).get('name','عميل واتساب') for c in v.get('contacts',[])}
                    for msg in v.get('messages',[]):
                        if one(db,'SELECT id FROM messages WHERE provider_id=?',(msg['id'],)): continue
                        number=phone('+'+msg['from']); stamp=now()
                        contact=one(db,'SELECT * FROM contacts WHERE tenant_id=? AND phone=?',(tid,number))
                        if not contact:
                            cid=uid(); db.execute('INSERT INTO contacts(id,tenant_id,name,phone,created_at) VALUES(?,?,?,?,?)',(cid,tid,names.get(msg['from'],'عميل واتساب')[:200],number,stamp))
                        else: cid=contact['id']
                        conv=one(db,'SELECT * FROM conversations WHERE channel_id=? AND contact_id=?',(channel['id'],cid))
                        inbound=min(int(msg['timestamp']),int(time.time()))
                        if not conv:
                            vid=uid(); db.execute('INSERT INTO conversations(id,tenant_id,contact_id,channel_id,last_inbound,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(vid,tid,cid,channel['id'],inbound,stamp,stamp))
                        else:
                            vid=conv['id']; db.execute("UPDATE conversations SET status='open',close_reason='',last_inbound=max(last_inbound,?),updated_at=? WHERE id=?",(inbound,stamp,vid))
                        body=msg.get('text',{}).get('body') if msg.get('type')=='text' else '[رسالة غير نصية — المرفقات غير مدعومة في V1]'
                        if not isinstance(body,str): raise ValueError('invalid_message')
                        db.execute('INSERT INTO messages(id,tenant_id,conversation_id,provider_id,direction,body,status,created_at) VALUES(?,?,?,?,?,?,?,?)',(uid(),tid,vid,msg['id'],'in',body[:4000],'received',stamp))
                    # Inbound proof does not prove send permissions or full connection health.
            db.execute("UPDATE webhook_events SET status='processed',error='' WHERE id=?",(event_id,))
        return True
    except Exception:
        if event_id:
            with transaction() as db:
                db.execute("UPDATE webhook_events SET attempts=attempts+1,error='processing_failed',status=CASE WHEN attempts>=4 THEN 'dead' ELSE 'pending' END WHERE id=?",(event_id,))
        return False

if __name__=='__main__':
    initialize()
    while True:
        if not process_one(): time.sleep(2)
