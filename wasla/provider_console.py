"""Provider-only administration of integrations, scoped explicitly to a customer workspace."""
import json
import re
import time
from urllib.parse import urlparse
from fastapi import Depends, HTTPException, Request
from .db import transaction, one, rows, audit, now
from .main import user, provider, text
from .service_api import router, get
from . import service_api as api, service as s
from .service_oauth import cipher
from .runtime_configuration import value

FIELDS={
 'META_APP_SECRET':('WhatsApp app secret','secret'),
 'META_VERIFY_TOKEN':('Webhook verification token','secret'),
 'INSTAGRAM_APP_ID':('Instagram app ID','text'),
 'INSTAGRAM_APP_SECRET':('Instagram app secret','secret'),
 'WASLA_OAUTH_ORIGIN':('OAuth callback origin','url'),
 'WASLA_MEDIA_ENDPOINT':('Private R2 endpoint','url'),
 'WASLA_MEDIA_BUCKET':('Private media bucket','text'),
 'WASLA_MEDIA_ACCESS_KEY_ID':('Media access key','secret'),
 'WASLA_MEDIA_SECRET_ACCESS_KEY':('Media secret key','secret'),
 'WASLA_MEDIA_SCANNER_URL':('File scanner HTTPS URL','url'),
 'WASLA_MEDIA_SCANNER_TOKEN':('File scanner token','secret'),
}

def scoped_provider(tenant_id,u):
    provider(u)
    with transaction() as db:t=one(db,'SELECT * FROM tenants WHERE id=?',(tenant_id,))
    if not t:raise HTTPException(404,'workspace_not_found')
    # Explicit provider delegation only; never accept role/user/tenant objects from request JSON.
    return {**u,'role':'admin','tenant_id':tenant_id}

@router.get('/api/admin/integrations')
def overview(u=Depends(user)):
    provider(u)
    try:cipher();vault=True
    except HTTPException:vault=False
    with transaction() as db:
        accounts=rows(db,'SELECT a.*,t.name workspace_name,t.status workspace_status FROM service_accounts a JOIN tenants t ON t.id=a.tenant_id ORDER BY a.created_at DESC')
        events=rows(db,"SELECT action,entity_id,detail,created_at FROM audit WHERE action LIKE 'provider.integration.%' ORDER BY created_at DESC LIMIT 50")
    for a in accounts:
        a['capabilities']=s.capabilities(a['channel'])
        try:a['credentials_configured']=bool(s.token(a))
        except HTTPException:a['credentials_configured']=False
        a.pop('token_ref',None)
    configs=[]
    for k,(label,kind) in FIELDS.items():
        try:configured=bool(value(k))
        except HTTPException:configured=False
        configs.append({'key':k,'label':label,'type':kind,'configured':configured,'value':value(k) if kind!='secret' and configured else ''})
    return {'accounts':accounts,'configuration':configs,'vault_ready':vault,'audit':events}

@router.put('/api/admin/integrations/configuration')
def configure(data:dict,u=Depends(user)):
    provider(u);fields=data.get('values')
    if not isinstance(fields,dict) or not fields or set(fields)-set(FIELDS):raise HTTPException(422,'invalid_configuration_fields')
    encrypted={};crypt=cipher()
    for k,v in fields.items():
        if not isinstance(v,str) or len(v)>8192:raise HTTPException(422,'invalid_configuration_value')
        if not v:continue # Empty secret fields preserve current values.
        if FIELDS[k][1]=='url':
            parsed=urlparse(v)
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:raise HTTPException(422,'https_url_required')
            if k=='WASLA_OAUTH_ORIGIN' and (parsed.path not in ('','/') or parsed.query):raise HTTPException(422,'origin_only_required')
            if k=='WASLA_MEDIA_ENDPOINT' and not parsed.hostname.endswith('.r2.cloudflarestorage.com'):raise HTTPException(422,'r2_endpoint_required')
        encrypted[k]=crypt.encrypt(v.encode()).decode()
    with transaction() as db:
        for k,v in encrypted.items():db.execute('INSERT INTO provider_configuration VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET ciphertext=excluded.ciphertext,updated_at=excluded.updated_at',(k,v,now()))
        audit(db,u,'provider.integration.configuration_saved','platform',','.join(sorted(encrypted)))
    return {'ok':True,'updated':list(encrypted)}

@router.post('/api/admin/workspaces/{tenant_id}/integrations',status_code=201)
def create(tenant_id:str,data:dict,u=Depends(user)):
    delegate=scoped_provider(tenant_id,u);r=api.create_account(data,delegate)
    with transaction() as db:audit(db,u,'provider.integration.created',r['id'],tenant_id)
    return {'id':r['id']}

@router.patch('/api/admin/workspaces/{tenant_id}/integrations/{ident}')
def edit(tenant_id:str,ident:str,data:dict,u=Depends(user)):
    delegate=scoped_provider(tenant_id,u)
    label=text(data,'label',1,120);version=text(data,'api_version',1,20)
    if not re.fullmatch(r'v\d+\.\d+',version):raise HTTPException(422,'invalid_api_version')
    credential=text(data,'access_token',0,8192)
    encrypted=cipher().encrypt(credential.encode()).decode() if credential else None
    with transaction() as db:
        a=get(db,'service_accounts',ident,delegate,True)
        db.execute('UPDATE service_accounts SET label=?,api_version=? WHERE id=?',(label,version,ident))
        if encrypted:
            db.execute('INSERT INTO service_secrets VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE SET ciphertext=excluded.ciphertext,issued_at=excluded.issued_at',(ident,encrypted,int(time.time())))
            db.execute("UPDATE service_accounts SET token_ref=?,state='unverified',checked_at=NULL,expires_at=NULL WHERE id=?",('vault:'+ident,ident))
        elif version!=a['api_version']:db.execute("UPDATE service_accounts SET state='unverified',checked_at=NULL WHERE id=?",(ident,))
        audit(db,u,'provider.integration.updated',ident,'credential_rotated' if encrypted else 'metadata')
    return {'ok':True,'requires_check':bool(encrypted or version!=a['api_version'])}

@router.post('/api/admin/workspaces/{tenant_id}/integrations/{ident}/{action}')
def action(tenant_id:str,ident:str,action:str,data:dict,request:Request,u=Depends(user)):
    delegate=scoped_provider(tenant_id,u)
    with transaction() as db:get(db,'service_accounts',ident,delegate)
    if action=='check':result=api.check_account(ident,delegate)
    elif action=='disconnect':result=api.disconnect(ident,delegate)
    elif action=='templates':result=api.sync_templates(ident,data,delegate)
    elif action=='authorize':
        from .service_oauth import start
        result=start(ident,request,delegate)
    elif action=='refresh':
        from .service_oauth import refresh
        result=refresh(ident,delegate)
    elif action=='approval':
        from .service_channels import approval
        result=approval(ident,data,delegate)
    elif action=='snap-sync':
        from .service_channels import snap_sync
        result=snap_sync(ident,delegate)
    else:raise HTTPException(404,'unsupported_integration_action')
    with transaction() as db:audit(db,u,'provider.integration.'+action,ident,tenant_id)
    return result

@router.get('/api/admin/workspaces/{tenant_id}/integration-operations')
def operations(tenant_id:str,u=Depends(user)):
    delegate=scoped_provider(tenant_id,u)
    from .service_health import queue_health
    return {**api.operations(delegate),'health':queue_health(tenant_id)}

@router.get('/api/admin/workspaces/{tenant_id}/integration-readiness')
def workspace_readiness(tenant_id:str,u=Depends(user)):
    from .service_channels import readiness
    return readiness(scoped_provider(tenant_id,u))

@router.get('/api/admin/workspaces/{tenant_id}/integration-agents')
def workspace_agents(tenant_id:str,u=Depends(user)):
    scoped_provider(tenant_id,u)
    with transaction() as db:
        return rows(db,"SELECT id,name,role FROM users WHERE tenant_id=? AND active=1 AND role IN ('admin','supervisor','sales') ORDER BY name",(tenant_id,))

@router.post('/api/admin/workspaces/{tenant_id}/integration-operations/{kind}/{ident}')
def recover_operation(tenant_id:str,kind:str,ident:str,data:dict,u=Depends(user)):
    delegate=scoped_provider(tenant_id,u)
    if kind=='event-retry':result=api.retry(ident,delegate)
    elif kind=='media-retry':
        from .service_media import retry_media
        result=retry_media(ident,delegate)
    elif kind=='reconcile':result=api.reconcile(ident,data,delegate)
    else:raise HTTPException(404,'unsupported_recovery_action')
    with transaction() as db:audit(db,u,'provider.integration.'+kind,ident,tenant_id)
    return result

@router.get('/api/admin/workspaces/{tenant_id}/integration-settings')
def workspace_settings(tenant_id:str,u=Depends(user)):
    return api.get_settings(scoped_provider(tenant_id,u))

@router.put('/api/admin/workspaces/{tenant_id}/integration-settings')
def save_workspace_settings(tenant_id:str,data:dict,u=Depends(user)):
    result=api.put_settings(data,scoped_provider(tenant_id,u))
    with transaction() as db:audit(db,u,'provider.integration.workspace_settings',tenant_id)
    return result

@router.get('/api/admin/workspaces/{tenant_id}/retention')
def workspace_retention(tenant_id:str,u=Depends(user)):
    from .service_lifecycle import retention_preview
    return retention_preview(scoped_provider(tenant_id,u))

@router.put('/api/admin/workspaces/{tenant_id}/retention')
def save_workspace_retention(tenant_id:str,data:dict,u=Depends(user)):
    from .service_lifecycle import retention_policy
    result=retention_policy(data,scoped_provider(tenant_id,u))
    with transaction() as db:audit(db,u,'provider.integration.retention_policy',tenant_id)
    return result
