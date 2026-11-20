"""Single-use email verification with a durable encrypted SMTP outbox.

The local transport is an explicit development-only sink in the database. No
verification tokens are returned by public APIs or logged.
"""
import json,os,secrets,smtplib,ssl,time
from email.message import EmailMessage
from urllib.parse import urlparse
from fastapi import Depends,HTTPException,Request
from pydantic import BaseModel,SecretStr,Field,ConfigDict
from .db import transaction,one,rows,uid,now,postgres
from .main import user,email
from .security import digest,limit
from .service_api import router
from .service_oauth import cipher
from .runtime_configuration import value

class Verify(BaseModel):
    model_config=ConfigDict(extra='forbid')
    token:SecretStr
class Resend(BaseModel):
    model_config=ConfigDict(extra='forbid')
    email:str=Field(min_length=3,max_length=254)

def configuration():
    mode=value('WASLA_EMAIL_MODE','disabled');origin=value('WASLA_EMAIL_ORIGIN',os.getenv('WASLA_ORIGIN','')).rstrip('/')
    p=urlparse(origin)
    local=mode=='local' and os.getenv('WASLA_ENV')=='development' and not os.getenv('VERCEL_ENV') and p.hostname in {'localhost','127.0.0.1'}
    if mode not in {'smtp','local'} or (mode=='local' and not local):raise HTTPException(503,'email_provider_not_configured')
    if (p.scheme not in {'http','https'} or (not local and p.scheme!='https') or not p.hostname or p.username or p.password or p.path or p.query or p.fragment):raise HTTPException(503,'email_https_origin_required')
    if mode=='smtp' and not all(value(k) for k in ('WASLA_SMTP_HOST','WASLA_SMTP_FROM','WASLA_SMTP_USER','WASLA_SMTP_PASSWORD')):raise HTTPException(503,'email_provider_not_configured')
    cipher();return mode,origin

def enqueue(db,uid_):
    _,origin=configuration();token=secrets.token_urlsafe(32);clock=int(time.time())
    account=one(db,'SELECT email FROM users WHERE id=?',(uid_,))
    db.execute("UPDATE email_jobs SET state='superseded',ciphertext='' WHERE user_id=? AND state IN ('pending','retry','local')",(uid_,))
    db.execute('INSERT INTO email_verifications VALUES(?,?,?,NULL) ON CONFLICT(user_id) DO UPDATE SET token_hash=excluded.token_hash,expires_at=excluded.expires_at,verified_at=NULL',(uid_,digest(token),clock+3600))
    message={'to':account['email'],'url':origin+'/verify-email#'+token}
    db.execute('INSERT INTO email_jobs(id,user_id,ciphertext,created_at) VALUES(?,?,?,?)',(uid(),uid_,cipher().encrypt(json.dumps(message).encode()).decode(),clock))

@router.post('/api/auth/verification/resend',status_code=202)
def resend(data:Resend,request:Request):
    configuration();address=email(data.email)
    with transaction() as db:
        limit(db,'verify-ip:'+digest(request.client.host),10,3600)
        limit(db,'verify-address:'+digest(address),3,3600)
    with transaction() as db:
        u=one(db,'SELECT u.id,u.tenant_id,e.verified_at FROM users u LEFT JOIN email_verifications e ON e.user_id=u.id WHERE email=?',(address,))
        if u and u['tenant_id'] and not u['verified_at']:
            from .saas import lock_tenant
            lock_tenant(db,u['tenant_id'])
            verified=one(db,'SELECT verified_at FROM email_verifications WHERE user_id=?',(u['id'],))
            if verified and not verified['verified_at'] and one(db,'SELECT tenant_id FROM service_registrations WHERE user_id=?',(u['id'],)):enqueue(db,u['id'])
    return {'status':'verification_requested_if_eligible'}

@router.post('/api/auth/verification/complete')
def complete(data:Verify,request:Request):
    token=data.token.get_secret_value()
    if not 32<=len(token)<=128:raise HTTPException(422,'invalid_verification_token')
    with transaction() as db:limit(db,'verify-complete:'+digest(request.client.host),30,900)
    with transaction() as db:
        r=one(db,'SELECT e.*,u.tenant_id,r.terms_version FROM email_verifications e JOIN users u ON u.id=e.user_id JOIN service_registrations r ON r.user_id=u.id AND r.tenant_id=u.tenant_id WHERE e.token_hash=?',(digest(token),))
        if not r or r['verified_at'] or r['expires_at']<=int(time.time()):raise HTTPException(409,'verification_expired_or_used')
        from .entitlements import capacity
        capacity(db,r['tenant_id'],'seat')
        if db.execute('UPDATE email_verifications SET verified_at=?,token_hash=? WHERE user_id=? AND verified_at IS NULL AND token_hash=? AND expires_at>?',(int(time.time()),'',r['user_id'],digest(token),int(time.time()))).rowcount!=1:raise HTTPException(409,'verification_expired_or_used')
        db.execute("UPDATE tenants SET status='active' WHERE id=? AND status='setup'",(r['tenant_id'],))
        db.execute('UPDATE users SET active=1 WHERE id=?',(r['user_id'],))
        db.execute("UPDATE service_registrations SET status='approved',review_evidence='email_verified' WHERE user_id=?",(r['user_id'],))
        db.execute("UPDATE email_jobs SET ciphertext='',state='verified' WHERE user_id=?",(r['user_id'],))
    return {'status':'verified','next':'/login','mode':'sandbox' if r['terms_version']=='sandbox-v1' else 'trial'}

@router.get('/api/v2/email-status')
def status(u=Depends(user)):
    with transaction() as db:r=one(db,'SELECT verified_at FROM email_verifications WHERE user_id=?',(u['id'],))
    return {'verified':bool(r and r['verified_at']),'legacy_managed_account':r is None}

def process_email():
    clock=int(time.time())
    with transaction() as db:
        db.execute("UPDATE email_jobs SET state='expired',ciphertext='' WHERE state IN ('local','failed') AND user_id IN (SELECT user_id FROM email_verifications WHERE expires_at<=?)",(clock,))
        db.execute("UPDATE email_jobs SET state='retry',error='worker_interrupted' WHERE state='sending' AND claimed_at<?",(clock-120,))
        job=one(db,"SELECT * FROM email_jobs WHERE state IN ('pending','retry') AND next_attempt<=? ORDER BY created_at LIMIT 1"+(' FOR UPDATE SKIP LOCKED' if postgres() else ''),(clock,))
        if not job:return False
        verification=one(db,'SELECT * FROM email_verifications WHERE user_id=?',(job['user_id'],))
        if not verification or verification['verified_at'] or verification['expires_at']<=clock:
            db.execute("UPDATE email_jobs SET state='expired',ciphertext='' WHERE id=?",(job['id'],));return True
        db.execute("UPDATE email_jobs SET state='sending',claimed_at=?,attempts=attempts+1 WHERE id=?",(clock,job['id']))
    try:
        mode,_=configuration();data=json.loads(cipher().decrypt(job['ciphertext'].encode()))
        if mode=='smtp':
            message=EmailMessage();message['From']=value('WASLA_SMTP_FROM');message['To']=data['to'];message['Subject']='Wasla — تحقق من بريدك / Verify your email'
            message.set_content('Verify your email within one hour. If you did not request this, ignore this message.\nتحقق من بريدك خلال ساعة. تجاهل الرسالة إذا لم تطلبها.\n'+data['url'])
            with smtplib.SMTP(value('WASLA_SMTP_HOST'),int(value('WASLA_SMTP_PORT','587')),timeout=10) as smtp:
                smtp.starttls(context=ssl.create_default_context());smtp.login(value('WASLA_SMTP_USER'),value('WASLA_SMTP_PASSWORD'));smtp.send_message(message)
        with transaction() as db:db.execute("UPDATE email_jobs SET state=?,ciphertext=?,error='' WHERE id=? AND state='sending'",('sent' if mode=='smtp' else 'local','' if mode=='smtp' else job['ciphertext'],job['id']))
    except Exception:
        with transaction() as db:db.execute("UPDATE email_jobs SET state=?,error='email_delivery_failed',next_attempt=? WHERE id=? AND state='sending'",('failed' if job['attempts']>=4 else 'retry',clock+min(3600,30*2**job['attempts']),job['id']))
    return True

@router.get('/api/admin/email/jobs')
def administration(u=Depends(user)):
    from .main import provider
    provider(u)
    with transaction() as db:return rows(db,'SELECT id,user_id,state,attempts,error,created_at FROM email_jobs ORDER BY created_at DESC LIMIT 100')

@router.post('/api/admin/email/jobs/{ident}/retry')
def retry(ident:str,u=Depends(user)):
    from .main import provider
    from .db import audit
    provider(u)
    with transaction() as db:
        job=one(db,"SELECT j.* FROM email_jobs j JOIN email_verifications e ON e.user_id=j.user_id WHERE j.id=? AND j.state='failed' AND e.verified_at IS NULL AND e.expires_at>?",(ident,int(time.time())))
        if not job:raise HTTPException(409,'eligible_failed_email_required')
        db.execute("UPDATE email_jobs SET state='pending',attempts=0,next_attempt=0 WHERE id=?",(ident,));audit(db,u,'email.retry',ident)
    return {'state':'pending'}
