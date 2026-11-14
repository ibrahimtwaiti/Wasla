"""V2 service domain. Provider I/O is separate from durable queue transactions."""
import hashlib
import hmac
import json
import math
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import httpx
from fastapi import HTTPException
from .db import transaction, one, rows, uid, now, postgres

CHANNELS = {'whatsapp', 'instagram', 'tiktok', 'snapchat'}
STATES = {'new', 'in_progress', 'waiting_customer', 'waiting_internal', 'resolved', 'closed'}
RANK = {'queued': 0, 'accepted': 1, 'sent': 2, 'failed': 2, 'delivered': 3, 'read': 4}
DEFAULT_SETTINGS = {'timezone': 'Asia/Riyadh', 'workdays': [0,1,2,3,6], 'start_hour': 9, 'end_hour': 17,
                    'holidays': [], 'sla_minutes': 30, 'max_active': 20, 'available_agents': [], 'sending_paused': False}

def settings(db, tid):
    r = one(db, 'SELECT settings FROM service_settings WHERE tenant_id=?', (tid,))
    return {**DEFAULT_SETTINGS, **(json.loads(r['settings']) if r else {})}

def capabilities(channel):
    return {'channel': channel, 'messaging': channel in {'whatsapp','instagram'},
            'types': ['text','template','image','document'] if channel=='whatsapp' else ['text'] if channel=='instagram' else [],
            'reply_window_hours': 24 if channel in {'whatsapp','instagram'} else 48 if channel=='tiktok' else None,
            'delivery_events': channel=='whatsapp', 'media_download': False,
            'reason': 'requires_business_messaging_approval_and_provider_contract' if channel=='tiktok' else
            'public_profile_or_referral_only_not_customer_dm' if channel=='snapchat' else 'requires_verified_account'}

def valid_signature(raw, header, secret):
    expected = 'sha256='+hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return bool(secret) and hmac.compare_digest(expected.encode(), header.encode('utf-8'))

def token(account):
    # Account mapping is provisioned by an operator, never an arbitrary env name from a tenant.
    if account.get('token_ref','').startswith('vault:'):
        from .service_oauth import read_token
        return read_token(account)
    return os.getenv('WASLA_CHANNEL_'+account['id'].upper()+'_TOKEN', '')

def provider_request(account, method, suffix='', payload=None):
    if account['channel'] not in {'whatsapp','instagram'}:
        raise HTTPException(409, 'unsupported_channel')
    credential = token(account)
    if not credential: raise HTTPException(409, 'channel_credentials_missing')
    host = 'graph.facebook.com' if account['channel']=='whatsapp' else 'graph.instagram.com'
    url = f"https://{host}/{account['api_version']}/{account['provider_id']}{suffix}"
    return httpx.request(method, url, headers={'Authorization': 'Bearer '+credential}, json=payload,
                         timeout=httpx.Timeout(15, connect=5), follow_redirects=False)

def normalize(channel, payload):
    """Yield (asset id, semantic key, normalized event); never trust a supplied tenant."""
    if channel=='whatsapp':
        if payload.get('object')!='whatsapp_business_account': raise ValueError('wrong_object')
        for entry in payload['entry']:
            for change in entry.get('changes', []):
                value=change.get('value', {}); asset=str(value.get('metadata', {}).get('phone_number_id',''))
                for msg in value.get('messages', []):
                    mid=str(msg['id']); stamp=int(msg['timestamp']); kind=msg.get('type','unsupported')
                    yield asset, 'message:'+mid, {'type':'message','id':mid,'user':str(msg['from']),
                        'at':stamp,'kind':kind,'body':msg.get('text',{}).get('body','') if kind=='text' else '[مرفق أو نوع غير مدعوم؛ لم يُنزّل الملف]', 'direction':'in'}
                for status in value.get('statuses', []):
                    yield asset, f"status:{status['id']}:{status['status']}:{status['timestamp']}", {
                        'type':'status','id':str(status['id']),'status':status['status'],'at':int(status['timestamp'])}
    elif channel=='instagram':
        if payload.get('object')!='instagram': raise ValueError('wrong_object')
        for entry in payload['entry']:
            asset=str(entry['id'])
            for item in entry.get('messaging', []):
                msg=item.get('message')
                if not msg: continue
                mid=str(msg['mid']); echo=bool(msg.get('is_echo'))
                yield asset, 'message:'+mid, {'type':'message','id':mid,'user':str(item['recipient' if echo else 'sender']['id']),
                    'at':int(item['timestamp'])//1000,'kind':'text' if 'text' in msg else 'unsupported',
                    'body':msg.get('text','[مرفق أو نوع غير مدعوم؛ لم يُنزّل الملف]'),'direction':'out' if echo else 'in'}
    else: raise ValueError('unsupported_channel')

def persist_events(channel, payload):
    events=list(normalize(channel,payload))
    if len(events)>500: raise HTTPException(413,'too_many_events')
    with transaction() as db:
        for asset,key,event in events:
            account=one(db,"SELECT a.* FROM service_accounts a JOIN tenants t ON t.id=a.tenant_id WHERE a.channel=? AND a.provider_id=? AND t.status!='suspended' AND a.state!='disconnected'",(channel,asset))
            if not account: raise HTTPException(403,'unknown_or_disconnected_account')
            if not event['id'] or len(event['id'])>300: raise HTTPException(400,'invalid_message_id')
            db.execute('INSERT INTO service_events(id,account_id,event_key,payload,received_at) VALUES(?,?,?,?,?) ON CONFLICT(account_id,event_key) DO NOTHING',
                       (uid(),account['id'],key,json.dumps(event),int(time.time())))
    return len(events)

def choose_agent(db, tid, config):
    available=config['available_agents']
    if not available: return None
    agents=rows(db,"SELECT u.id,COUNT(t.id) load FROM users u LEFT JOIN service_tickets t ON t.assignee_id=u.id AND t.status NOT IN ('closed','resolved') WHERE u.tenant_id=? AND u.active=1 AND u.role IN ('sales','admin','supervisor') GROUP BY u.id ORDER BY load,u.id",(tid,))
    return next((a['id'] for a in agents if a['id'] in available and a['load']<config['max_active']), None)

def apply_status(db, aid, pid, status, stamp):
    if status not in RANK: return
    db.execute('INSERT INTO service_status_history VALUES(?,?,?,?,?) ON CONFLICT(account_id,provider_id,status,occurred_at) DO NOTHING',(uid(),aid,pid,status,stamp))
    message=one(db,'SELECT * FROM service_messages WHERE account_id=? AND provider_id=? AND direction=\'out\'',(aid,pid))
    if message and RANK.get(message['status'],-1)<RANK[status]:
        db.execute('UPDATE service_messages SET status=? WHERE id=?',(status,message['id']))

def process_event():
    eid=None; attempts=0
    try:
        with transaction() as db:
            event=one(db,"SELECT * FROM service_events WHERE state='pending' AND next_attempt<=? ORDER BY received_at,id LIMIT 1"+(' FOR UPDATE SKIP LOCKED' if postgres() else ''),(int(time.time()),))
            if not event: return False
            eid=event['id']; attempts=event['attempts']; e=json.loads(event['payload'])
            a=one(db,"SELECT a.* FROM service_accounts a JOIN tenants t ON t.id=a.tenant_id WHERE a.id=? AND t.status!='suspended' AND a.state!='disconnected'"+(' FOR UPDATE OF a' if postgres() else ''),(event['account_id'],))
            if not a: raise ValueError('account_unavailable')
            aid=a['id']; tid=a['tenant_id']; clock=int(time.time())
            if e['type']=='status': apply_status(db,aid,e['id'],e['status'],e['at'])
            elif e['direction']=='out':
                # Echoes confirm an existing send only; never create a customer or a ticket.
                apply_status(db,aid,e['id'],'sent',e['at'])
            elif not one(db,'SELECT id FROM service_messages WHERE account_id=? AND provider_id=?',(aid,e['id'])):
                identity=one(db,'SELECT * FROM service_identities WHERE account_id=? AND provider_user_id=?',(aid,e['user']))
                if not identity:
                    ident=uid(); db.execute('INSERT INTO service_identities(id,tenant_id,account_id,provider_user_id,name,created_at) VALUES(?,?,?,?,?,?)',(ident,tid,aid,e['user'],e['user'][:200],now()))
                else: ident=identity['id']
                ticket=one(db,'SELECT * FROM service_tickets WHERE identity_id=? ORDER BY created_at DESC LIMIT 1',(ident,))
                stamp=max(0,min(e['at'],clock)); config=settings(db,tid)
                if not ticket:
                    ticket_id=uid(); category='complaint' if 'شكوى' in e['body'] else 'booking' if any(w in e['body'] for w in ('موعد','حجز')) else 'general'
                    team='supervisors' if category=='complaint' else 'booking' if category=='booking' else 'support'
                    db.execute('INSERT INTO service_tickets(id,tenant_id,identity_id,category,team,source,assignee_id,last_customer_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (ticket_id,tid,ident,category,team,a['channel'],choose_agent(db,tid,config),stamp,stamp,clock))
                else:
                    ticket_id=ticket['id']; new_state='new' if ticket['status'] in {'closed','resolved'} and stamp>ticket['last_customer_at'] else ticket['status']
                    db.execute('UPDATE service_tickets SET last_customer_at=?,status=?,version=version+1,updated_at=?,resolved_at=? WHERE id=?',
                        (max(ticket['last_customer_at'],stamp),new_state,clock,None if new_state=='new' else ticket['resolved_at'],ticket_id))
                db.execute('INSERT INTO service_messages(id,tenant_id,ticket_id,account_id,provider_id,direction,kind,body,status,occurred_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (uid(),tid,ticket_id,aid,e['id'],'in',e['kind'],str(e['body'])[:4000],'received',stamp,clock))
                db.execute('UPDATE service_accounts SET last_inbound_at=? WHERE id=?',(now(),aid))
            db.execute("UPDATE service_events SET state='processed',processed_at=?,error='' WHERE id=?",(clock,eid))
        return True
    except Exception:
        if eid:
            with transaction() as db:
                db.execute("UPDATE service_events SET attempts=attempts+1,state=?,next_attempt=?,error='processing_failed' WHERE id=? AND state='pending'",('dead' if attempts>=4 else 'pending',int(time.time())+min(3600,15*2**attempts),eid))
        return False

def policy(db,a,ticket,payload):
    if not capabilities(a['channel'])['messaging']: raise HTTPException(409,'channel_not_supported_for_customer_messaging')
    if a['state']!='verified': raise HTTPException(409,'channel_requires_connection_check')
    if not token(a): raise HTTPException(409,'channel_credentials_missing')
    if a['expires_at'] and a['expires_at']<=int(time.time()): raise HTTPException(409,'token_expired_reauthorize')
    if settings(db,a['tenant_id'])['sending_paused']: raise HTTPException(409,'sending_paused')
    if ticket['status'] in {'closed','resolved'}: raise HTTPException(409,'reopen_ticket_before_reply')
    kind=payload.get('kind','text')
    if kind not in capabilities(a['channel'])['types']: raise HTTPException(422,'unsupported_message_type')
    if kind=='text':
        if int(time.time())-ticket['last_customer_at']>=86400: raise HTTPException(409,'reply_window_expired_template_required')
    elif kind=='template':
        template=one(db,"SELECT * FROM service_templates WHERE account_id=? AND name=? AND language=? AND status='APPROVED' AND checked_at>?",(a['id'],payload.get('template'),payload.get('language'),int(time.time())-86400))
        if not template: raise HTTPException(409,'approved_recent_template_required')
        if '{{' in template['components'] or any(c.get('type') not in {'BODY','FOOTER'} for c in json.loads(template['components'])):
            raise HTTPException(409,'template_requires_unsupported_parameters')
        if not one(db,'SELECT id FROM service_consents WHERE identity_id=? AND purpose=? AND revoked_at IS NULL',(ticket['identity_id'],'template_messages')):
            raise HTTPException(409,'template_consent_required')
    if kind in {'image','document'}:
        if int(time.time())-ticket['last_customer_at']>=86400: raise HTTPException(409,'reply_window_expired_template_required')
        media=one(db,"SELECT * FROM service_media WHERE id=? AND tenant_id=? AND ticket_id=? AND scan_state='clean'",(payload.get('attachment_id'),a['tenant_id'],ticket['id']))
        if not media: raise HTTPException(422,'clean_attachment_required')
        if (kind=='image') != media['mime_type'].startswith('image/'):raise HTTPException(422,'attachment_kind_mismatch')

def send_outbox():
    clock=int(time.time())
    with transaction() as db:
        # A crash after dispatch may have reached the provider. Never automatically resend.
        stale=rows(db,"SELECT message_id FROM service_outbox WHERE state='sending' AND claimed_at<?",(clock-120,))
        for s in stale: db.execute("UPDATE service_messages SET status='unknown' WHERE id=? AND status='queued'",(s['message_id'],))
        db.execute("UPDATE service_outbox SET state='unknown',error='dispatch_outcome_unknown' WHERE state='sending' AND claimed_at<?",(clock-120,))
        o=one(db,"SELECT * FROM service_outbox WHERE state IN ('queued','retry') AND next_attempt<=? ORDER BY created_at,id LIMIT 1"+(' FOR UPDATE SKIP LOCKED' if postgres() else ''),(clock,))
        if not o: return False
        m=one(db,'SELECT * FROM service_messages WHERE id=?',(o['message_id'],))
        a=one(db,"SELECT a.* FROM service_accounts a JOIN tenants t ON a.tenant_id=t.id WHERE a.id=? AND t.status!='suspended'",(m['account_id'],))
        t=one(db,'SELECT * FROM service_tickets WHERE id=?',(m['ticket_id'],)); p=json.loads(o['payload'])
        try:
            if not a: raise HTTPException(409,'tenant_suspended')
            policy(db,a,t,p)
            actor=one(db,"SELECT * FROM users WHERE id=? AND tenant_id=? AND active=1 AND role IN ('admin','supervisor','sales')",(m['author_id'],m['tenant_id']))
            if not actor or (actor['role']=='sales' and t['assignee_id']!=actor['id']): raise HTTPException(403,'sender_permission_changed')
        except HTTPException as e:
            db.execute("UPDATE service_outbox SET state='blocked',error=? WHERE id=?",(str(e.detail),o['id']))
            db.execute("UPDATE service_messages SET status='blocked' WHERE id=?",(m['id'],)); return True
        identity=one(db,'SELECT provider_user_id FROM service_identities WHERE id=?',(t['identity_id'],))
        if a['channel']=='whatsapp':
            body={'messaging_product':'whatsapp','to':identity['provider_user_id'],'type':p['kind']}
            if p['kind']=='text': body['text']={'body':m['body']}
            elif p['kind']=='template': body['template']={'name':p['template'],'language':{'code':p['language']}}
            else:
                from .service_media import media_url
                media=one(db,'SELECT * FROM service_media WHERE id=?',(p['attachment_id'],))
                body[p['kind']]={'link':media_url(media),'caption':m['body']}

        else: body={'recipient':{'id':identity['provider_user_id']},'message':{'text':m['body']}}
        db.execute("UPDATE service_outbox SET state='sending',claimed_at=?,attempts=attempts+1 WHERE id=?",(clock,o['id']))
    state='unknown'; error='dispatch_outcome_unknown'; pid=None; retry=0; reauth=False
    try:
        response=provider_request(a,'POST','/messages',body)
        if response.status_code==429:
            state='retry' if o['attempts']<4 else 'dead'; error='rate_limited'
            try: retry=min(86400,max(1,int(response.headers.get('Retry-After','60'))))
            except ValueError: retry=60
        elif response.status_code in (401,403): state='blocked'; error='authorization_revoked'; reauth=True
        elif 400<=response.status_code<500: state='failed'; error='provider_rejected'
        elif response.status_code<300:
            result=response.json()
            pid=(result.get('messages') or [{}])[0].get('id') if a['channel']=='whatsapp' else result.get('message_id')
            if pid: state='accepted'; error=''
        # 5xx/timeouts are ambiguous and require reconciliation, not blind retries.
    except httpx.ConnectError:
        state='retry' if o['attempts']<4 else 'dead'; error='connection_failed'; retry=min(3600,15*2**o['attempts'])
    except (httpx.HTTPError,ValueError,HTTPException): pass
    with transaction() as db:
        db.execute('UPDATE service_outbox SET state=?,error=?,next_attempt=? WHERE id=?',(state,error,int(time.time())+retry+random.randint(0,5),o['id']))
        db.execute('UPDATE service_messages SET status=?,provider_id=? WHERE id=?',(state,pid,m['id']))
        if reauth: db.execute("UPDATE service_accounts SET state='reauthorize' WHERE id=?",(a['id'],))
        if pid:
            db.execute('UPDATE service_accounts SET last_outbound_at=? WHERE id=?',(now(),a['id']))
            db.execute('UPDATE service_tickets SET first_human_at=COALESCE(first_human_at,?) WHERE id=?',(int(time.time()),t['id']))
            for event in rows(db,'SELECT * FROM service_status_history WHERE account_id=? AND provider_id=? ORDER BY occurred_at',(a['id'],pid)):
                apply_status(db,a['id'],pid,event['status'],event['occurred_at'])
    return True

def drain(max_events=20, max_seconds=18):
    result={'inbound':0,'outbound':0}; end=time.monotonic()+max_seconds
    for _ in range(max_events):
        if time.monotonic()>=end: break
        worked=process_event()
        if worked: result['inbound']+=1
        if time.monotonic()<end:
            sent=send_outbox()
            if sent: result['outbound']+=1
        else: sent=False
        if not worked and not sent: break
    return result

def business_seconds(start,end,config):
    if end<=start: return 0
    zone=ZoneInfo('Asia/Riyadh'); first=datetime.fromtimestamp(start,zone); last=datetime.fromtimestamp(end,zone)
    day=first.date(); total=0
    while day<=last.date():
        if day.weekday() in config['workdays'] and day.isoformat() not in config['holidays']:
            lo=datetime.combine(day,datetime.min.time(),zone)+timedelta(hours=config['start_hour'])
            hi=datetime.combine(day,datetime.min.time(),zone)+timedelta(hours=config['end_hour'])
            total+=max(0,min(end,hi.timestamp())-max(start,lo.timestamp()))
        day+=timedelta(days=1)
    return int(total)

def percentile(values,p):
    return sorted(values)[max(0,math.ceil(len(values)*p)-1)] if values else None
