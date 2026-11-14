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

MAX_BYTES=5*1024*1024
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
    if not raw or len(raw)>MAX_BYTES: raise HTTPException(413,'attachment_size_limit_5mb')
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
    if not isinstance(encoded,str) or len(encoded)>MAX_BYTES*4//3+4:raise HTTPException(413,'attachment_size_limit_5mb')
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
