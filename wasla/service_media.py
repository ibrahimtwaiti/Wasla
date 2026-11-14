"""Private attachment storage. No attachment is released without a scanner verdict."""
import base64
import binascii
import hashlib
import os
import re
import time
from urllib.parse import urlparse
import httpx
from fastapi import Depends, HTTPException
from .db import transaction, audit, uid
from .main import user, text
from .security import permit
from .service_api import router, get

MAX_BYTES=2*1024*1024
MIME={'image/png':b'\x89PNG\r\n\x1a\n','image/jpeg':b'\xff\xd8\xff','application/pdf':b'%PDF-'}

def client():
    import boto3
    names=['WASLA_MEDIA_ENDPOINT','WASLA_MEDIA_BUCKET','WASLA_MEDIA_ACCESS_KEY_ID','WASLA_MEDIA_SECRET_ACCESS_KEY']
    if any(not os.getenv(n) for n in names): raise HTTPException(503,'private_media_storage_not_configured')
    endpoint=os.environ[names[0]];host=urlparse(endpoint)
    if host.scheme!='https' or not host.hostname or not host.hostname.endswith('.r2.cloudflarestorage.com') or host.username or host.password:
        raise HTTPException(503,'invalid_private_media_endpoint')
    return boto3.client('s3',endpoint_url=endpoint,region_name='auto',aws_access_key_id=os.environ[names[2]],aws_secret_access_key=os.environ[names[3]])

def validate(raw,mime):
    if not raw or len(raw)>MAX_BYTES: raise HTTPException(413,'attachment_size_limit_2mb')
    if mime not in MIME or not raw.startswith(MIME[mime]): raise HTTPException(422,'unsupported_or_mismatched_attachment_type')

def scan(raw,mime):
    validate(raw,mime);url=os.getenv('WASLA_MEDIA_SCANNER_URL','');secret=os.getenv('WASLA_MEDIA_SCANNER_TOKEN','')
    if not url.startswith('https://') or not secret:raise HTTPException(503,'attachment_scanner_not_configured')
    checksum=hashlib.sha256(raw).hexdigest()
    try:
        response=httpx.post(url,content=raw,headers={'Authorization':'Bearer '+secret,'Content-Type':mime,'X-Content-SHA256':checksum},timeout=20,follow_redirects=False)
        response.raise_for_status(); verdict=response.json()
        if verdict.get('sha256')!=checksum or verdict.get('verdict')!='clean':raise HTTPException(422,'attachment_rejected_by_scanner')
    except HTTPException:raise
    except Exception:raise HTTPException(503,'attachment_scanner_unavailable')
    return checksum

def media_url(record):
    if record['scan_state']!='clean':raise HTTPException(409,'attachment_not_clean')
    return client().generate_presigned_url('get_object',Params={'Bucket':os.environ['WASLA_MEDIA_BUCKET'],'Key':record['object_key'],'ResponseContentType':record['mime_type'],'ResponseContentDisposition':'attachment'},ExpiresIn=120)

@router.post('/api/v2/tickets/{ident}/attachments',status_code=201)
def upload(ident:str,data:dict,u=Depends(user)):
    permit(u,'inbox');mime=text(data,'mime_type',1,80)
    # Scope before decoding or scanning; attacker cannot use another tenant's attachment path.
    with transaction() as db:get(db,'service_tickets',ident,u)
    encoded=data.get('base64')
    if not isinstance(encoded,str) or len(encoded)>MAX_BYTES*4//3+4:raise HTTPException(413,'attachment_size_limit_2mb')
    try:raw=base64.b64decode(encoded,validate=True)
    except (binascii.Error,ValueError):raise HTTPException(422,'invalid_base64_attachment')
    validate(raw,mime);storage=client();checksum=scan(raw,mime);mid=uid();key='tenants/'+u['tenant_id']+'/attachments/'+mid
    try:storage.put_object(Bucket=os.environ['WASLA_MEDIA_BUCKET'],Key=key,Body=raw,ContentType=mime,Metadata={'sha256':checksum,'scan':'clean'})
    except Exception:raise HTTPException(503,'attachment_storage_failed')
    try:
        with transaction() as db:
            get(db,'service_tickets',ident,u)
            db.execute('INSERT INTO service_media VALUES(?,?,?,?,?,?,?,?,?)',(mid,u['tenant_id'],ident,key,mime,len(raw),checksum,'clean',int(time.time())))
            audit(db,u,'service.attachment.uploaded',mid)
    except Exception:
        # Compensate only the object created by this request.
        storage.delete_object(Bucket=os.environ['WASLA_MEDIA_BUCKET'],Key=key)
        raise
    return {'id':mid,'mime_type':mime,'size_bytes':len(raw),'scan_state':'clean'}

@router.get('/api/v2/attachments/{ident}/download')
def download(ident:str,u=Depends(user)):
    permit(u,'inbox')
    with transaction() as db:
        m=get(db,'service_media',ident,u);get(db,'service_tickets',m['ticket_id'],u);audit(db,u,'service.attachment.downloaded',ident)
    return {'url':media_url(m),'expires_in':120}

def fetch_provider_media(account, descriptor):
    """Fetch only a signed-event attachment from provider-controlled HTTPS hosts."""
    import ipaddress
    import socket
    from . import service as s
    headers={}; expected=''; url=descriptor.get('url','')
    if account['channel']=='whatsapp':
        mid=descriptor.get('id','')
        if not re.fullmatch(r'[0-9]+',mid):raise HTTPException(422,'invalid_provider_media_id')
        response=s.provider_request({**account,'provider_id':mid},'GET','?phone_number_id='+account['provider_id'])
        if response.status_code!=200:raise HTTPException(502,'provider_media_metadata_unavailable')
        meta=response.json();url=meta.get('url','');expected=meta.get('sha256','')
        if str(meta.get('id'))!=mid:raise HTTPException(422,'provider_media_id_mismatch')
        if int(meta.get('file_size',0))>MAX_BYTES:raise HTTPException(413,'attachment_size_limit_2mb')
        if descriptor.get('sha256') and expected!=descriptor['sha256']:raise HTTPException(422,'provider_media_checksum_mismatch')
        headers={'Authorization':'Bearer '+s.token(account)}
    host=urlparse(url); name=host.hostname or ''
    allowed=(name=='lookaside.fbsbx.com') if account['channel']=='whatsapp' else any(name.endswith('.'+d) for d in ('cdninstagram.com','fbcdn.net','fbsbx.com'))
    if not allowed or host.scheme!='https' or host.port not in (None,443) or host.username or host.password:
        raise HTTPException(422,'untrusted_provider_media_url')
    addresses=socket.getaddrinfo(name,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise HTTPException(422,'nonpublic_provider_media_host')
    chunks=[];size=0
    with httpx.stream('GET',url,headers=headers,timeout=httpx.Timeout(20,connect=5),follow_redirects=False) as response:
        if response.status_code!=200:raise HTTPException(502,'provider_media_download_failed')
        mime=response.headers.get('content-type','').split(';')[0].lower()
        for chunk in response.iter_bytes():
            size+=len(chunk)
            if size>MAX_BYTES:raise HTTPException(413,'attachment_size_limit_2mb')
            chunks.append(chunk)
    raw=b''.join(chunks);validate(raw,mime)
    digest=hashlib.sha256(raw).digest()
    if expected and expected not in {digest.hex(),base64.b64encode(digest).decode()}:raise HTTPException(422,'provider_media_checksum_mismatch')
    return raw,mime

def process_media():
    from .db import one, postgres
    job=None
    try:
        with transaction() as db:
            job=one(db,"SELECT * FROM service_media_jobs WHERE state='pending' AND next_attempt<=? ORDER BY id LIMIT 1"+(' FOR UPDATE SKIP LOCKED' if postgres() else ''),(int(time.time()),))
            if not job:return False
            m=one(db,'SELECT * FROM service_messages WHERE id=?',(job['message_id'],))
            # Serialize redaction and media downloads on the ticket.
            db.execute('SELECT id FROM service_tickets WHERE id=?'+(' FOR UPDATE' if postgres() else ''),(m['ticket_id'],))
            a=one(db,"SELECT a.* FROM service_accounts a JOIN tenants t ON t.id=a.tenant_id WHERE a.id=? AND a.state!='disconnected' AND t.status!='suspended'",(job['account_id'],))
            if not a:raise HTTPException(409,'account_unavailable')
            storage=client()
            raw,mime=fetch_provider_media(a,__import__('json').loads(job['descriptor']));checksum=scan(raw,mime)
            key='tenants/'+job['tenant_id']+'/attachments/'+job['id']
            storage.put_object(Bucket=os.environ['WASLA_MEDIA_BUCKET'],Key=key,Body=raw,ContentType=mime,Metadata={'sha256':checksum,'scan':'clean'})
            db.execute('INSERT INTO service_media VALUES(?,?,?,?,?,?,?,?,?)',(job['id'],job['tenant_id'],m['ticket_id'],key,mime,len(raw),checksum,'clean',int(time.time())))
            db.execute("UPDATE service_media_jobs SET state='complete',media_id=?,descriptor='{}',error='' WHERE id=?",(job['id'],job['id']))
            db.execute("UPDATE service_messages SET body='[مرفق جاهز للتنزيل الآمن]' WHERE id=?",(m['id'],))
            db.execute('UPDATE service_tickets SET version=version+1 WHERE id=?',(m['ticket_id'],))
        return True
    except Exception as exc:
        if not job:return False
        permanent=isinstance(exc,HTTPException) and exc.status_code in {409,413,422}
        code=str(exc.detail) if isinstance(exc,HTTPException) else 'provider_media_processing_failed'
        with transaction() as db:
            state='rejected' if permanent else 'dead' if job['attempts']>=4 else 'pending'
            db.execute('UPDATE service_media_jobs SET state=?,attempts=attempts+1,next_attempt=?,error=? WHERE id=? AND state=\'pending\'',(state,int(time.time())+min(3600,30*2**job['attempts']),code,job['id']))
            db.execute('UPDATE service_messages SET body=? WHERE id=?',('[تعذر إتاحة المرفق: '+code+']',job['message_id']))
        return True

@router.post('/api/v2/media-jobs/{ident}/retry')
def retry_media(ident:str,u=Depends(user)):
    permit(u,'settings')
    with transaction() as db:
        j=get(db,'service_media_jobs',ident,u)
        if j['state']!='dead':raise HTTPException(409,'media_job_not_retryable')
        db.execute("UPDATE service_media_jobs SET state='pending',attempts=0,next_attempt=0 WHERE id=?",(ident,));audit(db,u,'service.media.retried',ident)
    return {'ok':True}
