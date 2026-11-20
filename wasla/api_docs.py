"""OpenAPI generated from executable routes, with the actual auth boundaries."""
from fastapi import Depends
from fastapi.openapi.utils import get_openapi
from .main import app,user
from .security import permit
from .service_api import router

def schema():
    data=app.openapi()
    data['info']['title']='Wasla V2 API'
    data['paths']={k:v for k,v in data['paths'].items() if k.startswith('/api/')}
    data.setdefault('components',{})['securitySchemes']={
        'SessionCookie':{'type':'apiKey','in':'cookie','name':'wasla_session'},
        'CSRF':{'type':'apiKey','in':'header','name':'X-CSRF-Token'},
        'WorkerBearer':{'type':'http','scheme':'bearer'},
        'MetaSignature':{'type':'apiKey','in':'header','name':'X-Hub-Signature-256'}}
    anonymous={'/api/v2/oauth/instagram/callback','/api/auth/login','/api/auth/register','/api/auth/setup','/api/auth/verification/resend','/api/auth/verification/complete','/api/public/plans','/api/privacy/deletions/{ident}','/api/health','/api/catalog','/api/brand','/api/brand.css','/api/requests'}
    for path,methods in data['paths'].items():
        for method,operation in methods.items():
            if method not in {'get','post','patch','put','delete'}:continue
            if path.startswith('/api/webhooks/'):
                operation['security']=[] if method=='get' or path.endswith(('/deauthorize','/data-deletion')) else [{'MetaSignature':[]}]
                operation['description']='GET verifies hub.verify_token. JSON POST verifies raw-body HMAC-SHA256. Form callbacks verify signed_request with the app secret. No cookie or CSRF is required.'
            elif path=='/api/internal/drain-webhooks':operation['security']=[{'WorkerBearer':[]}]
            elif path in anonymous:operation['security']=[]
            else:operation['security']=[{'SessionCookie':[],**({'CSRF':[]} if method!='get' else {})}]
            if method!='get' and not path.startswith('/api/webhooks/') and path!='/api/internal/drain-webhooks':operation['description']=operation.get('description','')+' Same-origin JSON is required. Tenant identity comes from the session; provider delegation requires the provider role.'
            for code,desc in {'401':'Authentication required','403':'Forbidden, invalid state, CSRF or webhook signature','404':'Resource not in authorized workspace','409':'Conflict, quota or unmet prerequisites','422':'Invalid request (credentials are not reflected)','429':'Rate or monthly API limit','502':'Sanitized upstream error','503':'Required configuration unavailable'}.items():operation.setdefault('responses',{}).setdefault(code,{'description':desc})
    return data

@router.get('/api/v2/openapi.json')
def openapi(u=Depends(user)):
    permit(u,'settings');return schema()
