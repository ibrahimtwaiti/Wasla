"""Closed-by-default launch gates; owner evidence is not a Meta approval claim."""
import re
from fastapi import HTTPException
from .runtime_configuration import value

META_SERVICES={'whatsapp','messenger','instagram','catalogs','leads','marketing'}

def meta_gate(tenant_id,service):
    mode=value('META_SIGNUP_MODE','disabled')
    if mode=='test' and tenant_id in {x.strip() for x in value('META_TEST_TENANTS').split(',') if x.strip()}:return 'test'
    approved={x.strip() for x in value('META_APPROVED_SERVICES').split(',') if x.strip()}
    if mode=='enabled' and tenant_id and service in META_SERVICES and service in approved and len(value('META_APPROVAL_REFERENCE').strip())>=10:return 'enabled'
    raise HTTPException(409,'meta_signup_pending_provider_configuration_and_approval')

def signup_policy():
    mode=value('WASLA_SELF_SIGNUP_MODE','disabled')
    terms=value('WASLA_SIGNUP_TERMS_VERSION') if mode=='enabled' else 'sandbox-v1'
    enabled=mode=='test' or (mode=='enabled' and len(value('WASLA_SIGNUP_APPROVAL_REFERENCE').strip())>=10 and terms!='sandbox-v1' and bool(re.fullmatch(r'[A-Za-z0-9._-]{1,80}',terms)))
    return {'signup_enabled':bool(enabled),'mode':'trial' if mode=='enabled' else 'sandbox','terms_version':terms}
