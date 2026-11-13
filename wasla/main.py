import html
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import psycopg
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from .db import initialize, transaction, one, rows, audit, now, uid, postgres
from .security import hash_password, verify_password, digest, permit, limit, ROLES
from .catalog import SERVICES, CONSENT
from .catalog_en import SERVICES_EN, CONSENT_EN
from . import brand

STATIC=Path(__file__).parent/'static'
REQUEST_STATES={'new','review','information','quoted','accepted','rejected','closed'}
STAGES={'new','qualified','meeting','proposal','negotiation','won','lost'}

@asynccontextmanager
async def lifespan(app):
    if os.getenv('WASLA_ENV')=='production' and not os.getenv('WASLA_ORIGIN','').startswith('https://'):
        raise RuntimeError('Production requires an explicit HTTPS WASLA_ORIGIN')
    initialize()
    yield

app=FastAPI(title='Wasla API',version='1.0.0',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

@app.middleware('http')
async def protect(request, call_next):
    if request.method in {'POST','PATCH','PUT','DELETE'}:
        if len(await request.body())>(2200000 if request.url.path=='/api/admin/brand' else 65536):
            return JSONResponse({'detail':'حجم الطلب أكبر من المسموح'},413)
        if not (request.url.path.startswith('/api/webhooks/') or request.url.path == '/api/internal/drain-webhooks'):
            origin=('https://' + request.headers.get('host','') if os.getenv('VERCEL_ENV') in ('preview','production') else os.getenv('WASLA_ORIGIN','http://localhost:8000'))
            if request.headers.get('origin')!=origin:
                return JSONResponse({'detail':'مصدر الطلب غير مسموح'},403)
            if 'application/json' not in request.headers.get('content-type',''):
                return JSONResponse({'detail':'يجب إرسال JSON'},415)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='same-origin'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control']='no-store'
    if os.getenv('WASLA_ENV')=='production': response.headers['Strict-Transport-Security']='max-age=31536000'
    return response

@app.exception_handler(psycopg.IntegrityError)
@app.exception_handler(sqlite3.IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse({'detail':'السجل مكرر أو مرتبط ببيانات غير صالحة'},409)

def text(data, key, minimum=1, maximum=200):
    value=data.get(key,'')
    if not isinstance(value,str) or not minimum<=len(value.strip())<=maximum:
        raise HTTPException(422,f'يرجى التحقق من الحقل: {key}')
    return value.strip()

def choice(data,key,values):
    value=text(data,key)
    if value not in values: raise HTTPException(422,f'قيمة غير صالحة: {key}')
    return value

def phone(value):
    value=value.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩','0123456789'))
    value=re.sub(r'[\s()\-]','',value)
    if value.startswith('00'): value='+'+value[2:]
    if not re.fullmatch(r'\+[1-9]\d{7,14}',value): raise HTTPException(422,'أدخل رقمًا دوليًا يبدأ بـ + ورمز الدولة')
    return value

def email(value):
    if value and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value): raise HTTPException(422,'البريد الإلكتروني غير صحيح')
    return value.lower()

def operation(request):
    value=request.headers.get('Idempotency-Key','')
    if not re.fullmatch('[a-zA-Z0-9_-]{16,80}',value): raise HTTPException(422,'معرف العملية مطلوب')
    return value

SESSION_IDLE_SECONDS=30*60
SESSION_ABSOLUTE_SECONDS=12*60*60

async def user(request: Request):
    token=request.cookies.get('wasla_session','')
    token_hash=digest(token)
    timestamp=int(time.time())
    with transaction() as db:
        record=one(db,'''SELECT u.*,s.csrf,a.issued_at,a.last_seen_at FROM sessions s
            JOIN session_activity a ON a.token_hash=s.token_hash
            JOIN users u ON s.user_id=u.id
            WHERE s.token_hash=? AND s.expires_at>? AND u.active=1''',(token_hash,timestamp))
        if not record: raise HTTPException(401,'يرجى تسجيل الدخول')
        if (timestamp-record['last_seen_at']>=SESSION_IDLE_SECONDS
                or timestamp-record['issued_at']>=SESSION_ABSOLUTE_SECONDS):
            db.execute('DELETE FROM sessions WHERE token_hash=?',(token_hash,))
            raise HTTPException(401,'انتهت جلسة الدخول؛ سجّل الدخول مجددًا')
        if record['tenant_id']:
            tenant=one(db,'SELECT status FROM tenants WHERE id=?',(record['tenant_id'],))
            if not tenant or tenant['status']=='suspended': raise HTTPException(403,'اشتراك الشركة معلق؛ تواصل مع الإدارة')
        if request.method not in {'GET','HEAD'} and not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),record['csrf']):
            raise HTTPException(403,'انتهت صلاحية النموذج؛ حدّث الصفحة')
        if timestamp-record['last_seen_at']>=60:
            db.execute('UPDATE session_activity SET last_seen_at=? WHERE token_hash=?',(timestamp,token_hash))
        return record

def provider(u):
    if u['role']!='provider': raise HTTPException(403,'هذه العملية لإدارة مقدم الخدمة')

def scoped(db,table,ident,u):
    # table is always an internal constant, never user input.
    r=one(db,f'SELECT * FROM {table} WHERE id=? AND tenant_id=?',(ident,u['tenant_id']))
    if not r: raise HTTPException(404,'السجل غير موجود')
    return r

def owned(r,u):
    if u['role']=='sales' and r['owner_id']!=u['id']: raise HTTPException(403,'السجل غير مسند إليك')

def assignee(db,ident,u):
    r=one(db,'SELECT id FROM users WHERE id=? AND tenant_id=? AND active=1',(ident,u['tenant_id']))
    if not r: raise HTTPException(422,'المسؤول غير موجود في الشركة')

def valid_date(value):
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None: raise ValueError()
    except ValueError: raise HTTPException(422,'التاريخ غير صالح')
    return value

@app.get('/api/health')
def health():
    with transaction() as db: db.execute('SELECT 1')
    return {'status':'ok','version':'1.0.0'}

@app.get('/api/catalog')
def catalog(lang: str = "ar"):
    with transaction() as db:
        content={r['key']:r['value'] for r in db.execute("SELECT * FROM content WHERE key != 'brand'")}
        identity=brand.read(db)
        ready=bool(one(db,"SELECT id FROM users WHERE role='provider' AND active=1 LIMIT 1"))
    original_content=content.copy()
    if lang=='en':
        content={**content,'about':content.get('about_en',''),'working_hours':content.get('working_hours_en',''),'privacy':content.get('privacy_en',''),'terms':content.get('terms_en','')}
    return {'services':SERVICES_EN if lang=='en' else SERVICES,'consent':CONSENT_EN if lang=='en' else CONSENT,'content':content,'content_original':original_content,'brand':identity,'request_ready':ready}

@app.get('/api/brand.css')
def brand_styles():
    with transaction() as db: identity=brand.read(db)
    return Response(brand.css(identity),media_type='text/css',headers={'Cache-Control':'no-store'})

@app.put('/api/admin/brand')
def brand_update(data:dict,u=Depends(user)):
    provider(u)
    identity=brand.validate(data)
    with transaction() as db:
        db.execute('INSERT INTO content VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',('brand',json.dumps(identity),now()))
        audit(db,u,'brand.updated','brand')
    return {'ok':True,'brand':identity}

@app.post('/api/requests',status_code=201)
def submit_request(data:dict,request:Request):
    key=operation(request)
    normalized={k:text(data,k,1,n) for k,n in [('name',100),('company',160),('country',80),('activity',160),('need',2000)]}
    normalized.update(phone=phone(text(data,'phone')),email=email(text(data,'email',0,200)),
        service=choice(data,'service',{s['id'] for s in SERVICES}),source=text(data,'source',0,300))
    if data.get('contact_consent') is not True: raise HTTPException(422,'موافقة التواصل بخصوص الخدمة مطلوبة')
    normalized['marketing_consent']=data.get('marketing_consent') is True
    payload_hash=digest(json.dumps(normalized,sort_keys=True))
    with transaction() as db:
        existing=one(db,'SELECT reference,payload_hash FROM requests WHERE operation_key=?',(key,))
        if existing:
            if existing['payload_hash']!=payload_hash: raise HTTPException(409,'استخدم معرف عملية جديدًا للطلب المعدل')
            return {'reference':existing['reference']}
        limit(db,'request:'+digest(request.client.host),5,600)
        owner=one(db,"SELECT id FROM users WHERE role='provider' AND active=1 ORDER BY created_at LIMIT 1")
        if not owner: raise HTTPException(503,'استقبال الطلبات لم يُفعّل بعد؛ يرجى المحاولة لاحقًا')
        ident=uid(); reference='WS-'+secrets.token_hex(5).upper(); stamp=now()
        db.execute('''INSERT INTO requests(id,reference,operation_key,payload_hash,name,company,phone,country,activity,email,service,need,source,contact_consent,marketing_consent,consented_at,owner_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(ident,reference,key,payload_hash,normalized['name'],normalized['company'],normalized['phone'],normalized['country'],normalized['activity'],normalized['email'],normalized['service'],normalized['need'],normalized['source'],CONSENT,int(normalized['marketing_consent']),stamp,owner['id'],stamp,stamp))
        db.execute('INSERT INTO provider_opportunities(id,request_id,owner_id) VALUES(?,?,?)',(uid(),ident,owner['id']))
        db.execute('INSERT INTO notifications VALUES(?,?,?,?,0,?)',(uid(),owner['id'],'طلب خدمة جديد '+reference,ident,stamp))
        db.execute('INSERT INTO audit VALUES(?,NULL,NULL,?,?,?,?)',(uid(),'request.created',ident,'consent:v1',stamp))
    return {'reference':reference}

@app.post('/api/auth/setup',status_code=201)
def first_provider(data:dict,request:Request):
    # Capability is random, expires, and is useless as soon as a provider exists.
    with transaction() as db:
        limit(db,'setup:'+digest(request.client.host),10,900)
    expected=os.getenv('WASLA_SETUP_TOKEN','')
    supplied=data.get('token','')
    try: expires=int(os.getenv('WASLA_SETUP_EXPIRES','0'))
    except ValueError: expires=0
    if len(expected)<40 or not isinstance(supplied,str) or not hmac.compare_digest(supplied,expected) or time.time()>expires:
        raise HTTPException(403,'رابط التفعيل غير صالح أو انتهت صلاحيته')
    address=email(text(data,'email')); name=text(data,'name',2,100)
    encoded=hash_password(text(data,'password',16,128))
    entity=text(data,'legal_entity',2,160)
    contact=email(text(data,'contact_email'))
    with transaction() as db:
        if postgres(): db.execute("SELECT pg_advisory_xact_lock(hashtext('wasla-first-provider'))")
        if one(db,"SELECT id FROM users WHERE role='provider' LIMIT 1"):
            raise HTTPException(409,'تم تفعيل حساب الإدارة مسبقًا')
        ident=uid()
        db.execute('INSERT INTO users VALUES(?,NULL,?,?,?,?,1,?)',(ident,address,name,encoded,'provider',now()))
        audit(db,{'id':ident,'tenant_id':None},'provider.initialized',ident)
        for key,value in [('legal_entity',entity),('contact_email',contact)]:
            db.execute('INSERT INTO content VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',(key,value,now()))
    return {'ok':True}

@app.post('/api/auth/login')
def login(data:dict,request:Request,response:Response):
    address=email(text(data,'email')); password=text(data,'password',1,128)
    with transaction() as db:
        limit(db,'login-ip:'+digest(request.client.host),30,900)
        limit(db,'login-email:'+digest(address),10,900)
    with transaction() as db:
        u=one(db,'SELECT * FROM users WHERE email=? AND active=1',(address,))
        # Run a real password KDF even for unknown identities.
        encoded=u['password_hash'] if u else 'scrypt$invalid-user-salt$'+'0'*128
        ok=verify_password(password,encoded)
        if not ok or not u: raise HTTPException(401,'بيانات الدخول غير صحيحة')
        token=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(24)
        timestamp=int(time.time())
        db.execute('DELETE FROM sessions WHERE expires_at<?',(timestamp,))
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(digest(token),u['id'],csrf,timestamp+SESSION_ABSOLUTE_SECONDS))
        db.execute('INSERT INTO session_activity(token_hash,issued_at,last_seen_at) VALUES(?,?,?)',(digest(token),timestamp,timestamp))
        audit(db,u,'auth.login',u['id'])
    response.set_cookie('wasla_session',token,max_age=SESSION_ABSOLUTE_SECONDS,httponly=True,secure=os.getenv('WASLA_ENV')=='production',samesite='strict',path='/')
    return {'ok':True}

@app.get('/api/me')
def me(u=Depends(user)):
    return {k:u[k] for k in ['id','tenant_id','name','email','role','csrf']}

@app.post('/api/auth/logout')
def logout(request:Request,response:Response,u=Depends(user)):
    with transaction() as db: db.execute('DELETE FROM sessions WHERE token_hash=?',(digest(request.cookies.get('wasla_session','')),))
    response.delete_cookie('wasla_session',path='/'); return {'ok':True}

@app.post('/api/auth/password')
def password(data:dict,u=Depends(user)):
    if not verify_password(text(data,'current_password',1,128),u['password_hash']): raise HTTPException(400,'كلمة المرور الحالية غير صحيحة')
    new_hash=hash_password(text(data,'new_password',16,128))
    with transaction() as db:
        db.execute('UPDATE users SET password_hash=? WHERE id=?',(new_hash,u['id']))
        db.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],)); audit(db,u,'auth.password',u['id'])
    return {'ok':True}

@app.get('/api/admin/requests')
def requests_list(u=Depends(user)):
    provider(u)
    with transaction() as db:
        return rows(db,'''SELECT r.*,u.name owner_name,(SELECT count(*) FROM requests x WHERE x.phone=r.phone)>1 duplicate_phone
           FROM requests r JOIN users u ON r.owner_id=u.id ORDER BY r.created_at DESC LIMIT 500''')

@app.patch('/api/admin/requests/{ident}')
def request_update(ident:str,data:dict,u=Depends(user)):
    provider(u); status=choice(data,'status',REQUEST_STATES)
    quote=text(data,'quote',0,5000); payment=text(data,'payment_reference',0,200)
    if status in {'quoted','accepted'} and not quote: raise HTTPException(422,'أضف تفاصيل عرض السعر قبل الاعتماد')
    with transaction() as db:
        r=one(db,'SELECT * FROM requests WHERE id=?',(ident,))
        if not r: raise HTTPException(404,'الطلب غير موجود')
        db.execute('UPDATE requests SET status=?,quote=?,payment_reference=?,updated_at=? WHERE id=?',(status,quote,payment,now(),ident))
        db.execute('UPDATE provider_opportunities SET stage=? WHERE request_id=?',({'quoted':'proposal','accepted':'won','rejected':'lost'}.get(status,'qualified'),ident))
        if status=='accepted' and not one(db,'SELECT id FROM projects WHERE request_id=?',(ident,)):
            pid=uid(); db.execute('INSERT INTO projects(id,request_id,name,owner_id,created_at) VALUES(?,?,?,?,?)',(pid,ident,r['company']+' — '+r['service'],u['id'],now()))
            for title in ['جمع المتطلبات','اعتماد النطاق','التنفيذ','الاختبار والتسليم']:
                db.execute('INSERT INTO milestones(id,project_id,title,updated_at) VALUES(?,?,?,?)',(uid(),pid,title,now()))
        audit(db,u,'request.status',ident,status)
    return {'ok':True}

@app.get('/api/admin/tenants')
def tenants(u=Depends(user)):
    provider(u)
    with transaction() as db: return rows(db,'SELECT * FROM tenants ORDER BY created_at DESC')

@app.post('/api/admin/tenants',status_code=201)
def tenant_create(data:dict,u=Depends(user)):
    provider(u); name=text(data,'name'); owner=text(data,'owner'); address=email(text(data,'email'))
    password_hash=hash_password(text(data,'password',12,128)); ident=uid()
    with transaction() as db:
        db.execute('INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)',(ident,name,now()))
        db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',(uid(),ident,address,owner,password_hash,'admin',now()))
        audit(db,u,'tenant.created',ident)
    return {'id':ident}

@app.patch('/api/admin/tenants/{ident}')
def tenant_status(ident:str,data:dict,u=Depends(user)):
    provider(u); status=choice(data,'status',{'setup','active','suspended'})
    with transaction() as db:
        if not one(db,'SELECT id FROM tenants WHERE id=?',(ident,)): raise HTTPException(404,'الشركة غير موجودة')
        db.execute('UPDATE tenants SET status=? WHERE id=?',(status,ident)); audit(db,u,'tenant.status',ident,status)
    return {'ok':True}

@app.put('/api/admin/content')
def content_update(data:dict,u=Depends(user)):
    provider(u); key=choice(data,'key',{'about','about_en','contact_email','contact_phone','working_hours','working_hours_en','legal_entity','privacy','privacy_en','terms','terms_en'})
    value=text(data,'value',0,4000)
    if key=='contact_email': value=email(value)
    if key=='contact_phone' and value: value=phone(value)
    with transaction() as db:
        db.execute('INSERT INTO content VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',(key,value,now()))
        audit(db,u,'content.updated',key)
    return {'ok':True}

@app.get('/api/projects')
def project_list(u=Depends(user)):
    if u['role']!='provider': permit(u,'projects')
    with transaction() as db:
        projects=rows(db,'SELECT * FROM projects'+(' WHERE tenant_id=?' if u['tenant_id'] else '')+' ORDER BY created_at DESC', (u['tenant_id'],) if u['tenant_id'] else ())
        for p in projects: p['milestones']=rows(db,'SELECT * FROM milestones WHERE project_id=? ORDER BY updated_at,id',(p['id'],))
        return projects

@app.patch('/api/projects/{ident}')
def project_update(ident:str,data:dict,u=Depends(user)):
    provider(u)
    status=choice(data,'status',{'requirements','setup','testing','ready','active','blocked','complete'})
    tenant=text(data,'tenant_id',0)
    with transaction() as db:
        if not one(db,'SELECT id FROM projects WHERE id=?',(ident,)): raise HTTPException(404,'المشروع غير موجود')
        if tenant and not one(db,'SELECT id FROM tenants WHERE id=?',(tenant,)): raise HTTPException(422,'الشركة غير موجودة')
        db.execute('UPDATE projects SET status=?,tenant_id=? WHERE id=?',(status,tenant or None,ident)); audit(db,u,'project.updated',ident,status)
    return {'ok':True}

@app.patch('/api/milestones/{ident}')
def milestone_update(ident:str,data:dict,u=Depends(user)):
    if u['role']!='provider': permit(u,'projects')
    status=choice(data,'status',{'pending','submitted','approved','revision'})
    deliverable=text(data,'deliverable',0,4000); feedback=text(data,'feedback',0,2000)
    if status in {'submitted','approved'} and not deliverable: raise HTTPException(422,'وصف المخرج مطلوب')
    if status=='revision' and not feedback: raise HTTPException(422,'وضح التعديل المطلوب')
    with transaction() as db:
        r=one(db,'SELECT m.*,p.tenant_id FROM milestones m JOIN projects p ON p.id=m.project_id WHERE m.id=?',(ident,))
        if not r or (u['tenant_id'] and r['tenant_id']!=u['tenant_id']): raise HTTPException(404,'المرحلة غير موجودة')
        if u['role']=='services' and status=='approved': raise HTTPException(403,'اعتماد التسليم لمدير الشركة')
        db.execute('UPDATE milestones SET status=?,deliverable=?,feedback=?,updated_at=? WHERE id=?',(status,deliverable,feedback,now(),ident))
        audit(db,u,'milestone.updated',ident,status)
    return {'ok':True}

@app.get('/api/team')
def team(u=Depends(user)):
    if not u['tenant_id']: raise HTTPException(403,'هذه العملية للشركات')
    with transaction() as db: return rows(db,'SELECT id,name,email,role,active FROM users WHERE tenant_id=?',(u['tenant_id'],))

@app.post('/api/team',status_code=201)
def team_create(data:dict,u=Depends(user)):
    permit(u,'team'); name=text(data,'name'); address=email(text(data,'email')); role=choice(data,'role',ROLES)
    password_hash=hash_password(text(data,'password',12,128)); ident=uid()
    with transaction() as db:
        db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',(ident,u['tenant_id'],address,name,password_hash,role,now())); audit(db,u,'user.created',ident,role)
    return {'id':ident}

@app.patch('/api/team/{ident}')
def team_update(ident:str,data:dict,u=Depends(user)):
    permit(u,'team'); role=choice(data,'role',ROLES); active=data.get('active') is True
    if ident==u['id']: raise HTTPException(422,'لا يمكنك تعطيل حسابك أو تغيير دورك من هنا')
    with transaction() as db:
        target=scoped(db,'users',ident,u)
        db.execute('UPDATE users SET role=?,active=? WHERE id=?',(role,int(active),target['id']))
        db.execute('DELETE FROM sessions WHERE user_id=?',(ident,)); audit(db,u,'user.updated',ident,role)
    return {'ok':True}

@app.get('/api/contacts')
def contacts(q:str='',u=Depends(user)):
    permit(u,'contacts')
    with transaction() as db: return rows(db,'SELECT * FROM contacts WHERE tenant_id=? AND (name LIKE ? OR phone LIKE ?) ORDER BY created_at DESC LIMIT 500',(u['tenant_id'],'%'+q[:100]+'%','%'+q[:100]+'%'))

@app.post('/api/contacts',status_code=201)
def contact_create(data:dict,u=Depends(user)):
    permit(u,'contacts'); name=text(data,'name'); number=phone(text(data,'phone')); tags=text(data,'tags',0,400)
    consent=data.get('marketing_consent') is True; source=text(data,'consent_source',1 if consent else 0,400); ident=uid()
    with transaction() as db:
        db.execute('INSERT INTO contacts VALUES(?,?,?,?,?,?,?,?,?,?)',(ident,u['tenant_id'],name,number,tags,int(consent),source,now() if consent else None,None,now()))
        audit(db,u,'contact.created',ident)
    return {'id':ident}

@app.patch('/api/contacts/{ident}/consent')
def contact_consent(ident:str,data:dict,u=Depends(user)):
    permit(u,'contacts'); consent=data.get('marketing_consent') is True
    source=text(data,'consent_source',1 if consent else 0,400)
    with transaction() as db:
        scoped(db,'contacts',ident,u)
        db.execute('UPDATE contacts SET marketing_consent=?,consent_source=?,consented_at=?,withdrawn_at=? WHERE id=?',(int(consent),source,now() if consent else None,None if consent else now(),ident))
        audit(db,u,'consent.updated',ident,'opt-in' if consent else 'withdrawn')
    return {'ok':True}

@app.get('/api/contacts/export')
def export_contacts(u=Depends(user)):
    permit(u,'export')
    with transaction() as db:
        data=rows(db,'SELECT name,phone,tags,marketing_consent FROM contacts WHERE tenant_id=?',(u['tenant_id'],)); audit(db,u,'contacts.export',u['tenant_id'],str(len(data)))
    output=io.StringIO(); writer=csv.writer(output); writer.writerow(['name','phone','tags','marketing_consent'])
    for r in data: writer.writerow([("'"+str(v)) if str(v).startswith(('=','+','-','@','\t','\r','\n')) else v for v in r.values()])
    return Response('\ufeff'+output.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="contacts.csv"'})

@app.get('/api/opportunities')
def opportunities(u=Depends(user)):
    permit(u,'crm')
    with transaction() as db: return rows(db,'SELECT o.*,c.name contact_name FROM opportunities o JOIN contacts c ON o.contact_id=c.id WHERE o.tenant_id=?'+(' AND o.owner_id=?' if u['role']=='sales' else '')+' ORDER BY o.created_at DESC',(u['tenant_id'],u['id']) if u['role']=='sales' else (u['tenant_id'],))

@app.post('/api/opportunities',status_code=201)
def opportunity_create(data:dict,u=Depends(user)):
    permit(u,'crm'); title=text(data,'title'); contact=text(data,'contact_id'); currency=text(data,'currency',3,3).upper()
    if not re.fullmatch('[A-Z]{3}',currency): raise HTTPException(422,'استخدم رمز العملة من 3 أحرف')
    amount=data.get('value_minor',0)
    if type(amount)!=int or not 0<=amount<=10**12: raise HTTPException(422,'القيمة غير صالحة')
    follow=text(data,'follow_up',0,40)
    if follow: valid_date(follow)
    ident=uid()
    with transaction() as db:
        scoped(db,'contacts',contact,u)
        db.execute('INSERT INTO opportunities(id,tenant_id,contact_id,owner_id,title,value_minor,currency,follow_up,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(ident,u['tenant_id'],contact,u['id'],title,amount,currency,follow,now())); audit(db,u,'opportunity.created',ident)
    return {'id':ident}

@app.patch('/api/opportunities/{ident}')
def opportunity_update(ident:str,data:dict,u=Depends(user)):
    permit(u,'crm'); stage=choice(data,'stage',STAGES); reason=text(data,'reason',0,1000); follow=text(data,'follow_up',0,40)
    if stage in {'won','lost'} and not reason: raise HTTPException(422,'أدخل سبب الخسارة أو مرجع إثبات الفوز')
    if follow: valid_date(follow)
    with transaction() as db:
        r=scoped(db,'opportunities',ident,u); owned(r,u)
        owner=text(data,'owner_id',0) or r['owner_id']; assignee(db,owner,u)
        if u['role']=='sales' and owner!=u['id']: raise HTTPException(403,'نقل الملكية للمشرف')
        db.execute('UPDATE opportunities SET stage=?,reason=?,follow_up=?,owner_id=? WHERE id=?',(stage,reason,follow,owner,ident)); audit(db,u,'opportunity.updated',ident,stage)
    return {'ok':True}

@app.get('/api/tasks')
def tasks(u=Depends(user)):
    permit(u,'crm')
    with transaction() as db: return rows(db,'SELECT * FROM tasks WHERE tenant_id=? AND owner_id=? ORDER BY due_at',(u['tenant_id'],u['id']))

@app.post('/api/tasks')
def task_create(data:dict,u=Depends(user)):
    permit(u,'crm'); title=text(data,'title'); due=valid_date(text(data,'due_at',1,40)); ident=uid()
    with transaction() as db:
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(ident,u['tenant_id'],u['id'],title,due,'open',now())); audit(db,u,'task.created',ident)
    return {'id':ident}

@app.patch('/api/tasks/{ident}')
def task_update(ident:str,data:dict,u=Depends(user)):
    permit(u,'crm'); status=choice(data,'status',{'open','done'})
    with transaction() as db:
        r=scoped(db,'tasks',ident,u)
        if r['owner_id']!=u['id']: raise HTTPException(403,'المهمة لمستخدم آخر')
        db.execute('UPDATE tasks SET status=? WHERE id=?',(status,ident)); audit(db,u,'task.updated',ident,status)
    return {'ok':True}

@app.get('/api/reports')
def reports(u=Depends(user)):
    permit(u,'reports')
    with transaction() as db:
        return {'timezone':'UTC','period':'all-time','source':'Wasla database',
            'contacts':db.execute('SELECT count(*) FROM contacts WHERE tenant_id=?',(u['tenant_id'],)).fetchone()[0],
            'conversations':rows(db,'SELECT status,count(*) count FROM conversations WHERE tenant_id=? GROUP BY status',(u['tenant_id'],)),
            'pipeline':rows(db,'SELECT stage,currency,count(*) count,sum(value_minor) value_minor FROM opportunities WHERE tenant_id=? GROUP BY stage,currency',(u['tenant_id'],)),
            'note':'قيم الفرص ليست إيرادات محصلة. التقارير تراكمية منذ إنشاء المساحة؛ لا يوجد قياس إيرادات أو إسناد تسويقي في V1.'}

@app.get('/api/audit')
def audit_list(u=Depends(user)):
    if u['role'] not in {'admin','provider'}: raise HTTPException(403,'غير مسموح')
    with transaction() as db: return rows(db,'SELECT a.*,u.name actor_name FROM audit a LEFT JOIN users u ON u.id=a.actor_id WHERE a.tenant_id IS ? ORDER BY a.created_at DESC LIMIT 200',(u['tenant_id'],))

@app.get('/api/admin/operations')
def operations(u=Depends(user)):
    provider(u)
    with transaction() as db:
        return {'webhook_counts':rows(db,'SELECT status,count(*) count FROM webhook_events GROUP BY status'),
            'failed_events':rows(db,"SELECT id,status,attempts,error,created_at FROM webhook_events WHERE status='dead' ORDER BY created_at DESC LIMIT 100")}

@app.post('/api/admin/operations/{ident}/retry')
def retry_event(ident:str,u=Depends(user)):
    provider(u)
    with transaction() as db:
        r=one(db,"SELECT id FROM webhook_events WHERE id=? AND status='dead'",(ident,))
        if not r: raise HTTPException(404,'الحدث غير موجود في قائمة المراجعة')
        db.execute("UPDATE webhook_events SET status='pending',attempts=0,error=''"+(",next_attempt_at=0" if postgres() else "")+" WHERE id=?",(ident,))
        audit(db,u,'webhook.retry',ident)
    return {'ok':True}

@app.get('/api/notifications')
def notifications(u=Depends(user)):
    with transaction() as db: return rows(db,'SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 100',(u['id'],))

@app.get('/api/conversations')
def conversations(q:str='',status:str='',u=Depends(user)):
    permit(u,'inbox')
    sql='SELECT v.*,c.name contact_name,c.phone FROM conversations v JOIN contacts c ON c.id=v.contact_id WHERE v.tenant_id=?'
    args=[u['tenant_id']]
    if u['role']=='sales': sql+=' AND v.owner_id=?'; args.append(u['id'])
    if status: sql+=' AND v.status=?'; args.append(status)
    sql+=' AND (c.name LIKE ? OR c.phone LIKE ?) ORDER BY v.updated_at DESC LIMIT 200'; args.extend(['%'+q[:100]+'%']*2)
    with transaction() as db: return rows(db,sql,args)

@app.get('/api/conversations/{ident}/messages')
def messages(ident:str,u=Depends(user)):
    permit(u,'inbox')
    with transaction() as db:
        r=scoped(db,'conversations',ident,u); owned(r,u)
        return rows(db,'SELECT * FROM messages WHERE tenant_id=? AND conversation_id=? ORDER BY created_at LIMIT 1000',(u['tenant_id'],ident))

@app.patch('/api/conversations/{ident}')
def conversation_update(ident:str,data:dict,u=Depends(user)):
    permit(u,'inbox'); status=choice(data,'status',{'open','waiting','closed'}); reason=text(data,'close_reason',0,1000)
    if status=='closed' and not reason: raise HTTPException(422,'سبب الإغلاق مطلوب')
    with transaction() as db:
        r=scoped(db,'conversations',ident,u); owned(r,u); owner=text(data,'owner_id',0) or None
        if owner: assignee(db,owner,u)
        if u['role']=='sales' and owner!=r['owner_id']: raise HTTPException(403,'الإسناد للمشرف')
        db.execute('UPDATE conversations SET status=?,owner_id=?,close_reason=?,updated_at=? WHERE id=?',(status,owner,reason,now(),ident)); audit(db,u,'conversation.updated',ident,status)
    return {'ok':True}

@app.post('/api/conversations/{ident}/messages')
def send_message(ident:str,data:dict,request:Request,u=Depends(user)):
    permit(u,'inbox'); body=text(data,'body',1,4000); direction=choice(data,'direction',{'out','note'}); key=operation(request)
    with transaction() as db:
        conv=scoped(db,'conversations',ident,u); owned(conv,u)
        old=one(db,'SELECT * FROM messages WHERE tenant_id=? AND operation_key=?',(u['tenant_id'],key))
        if old:
            if old['conversation_id']!=ident or old['body']!=body or old['direction']!=direction: raise HTTPException(409,'معرف عملية مستخدم لمحتوى آخر')
            return {'id':old['id'],'status':old['status']}
        if direction=='out':
            raise HTTPException(409,'الإرسال الخارجي غير مفعل في V1؛ يلزم اختبار واعتماد الربط أولًا. يمكنك حفظ ملاحظة داخلية.')
        mid=uid(); db.execute('INSERT INTO messages(id,tenant_id,conversation_id,operation_key,author_id,direction,body,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(mid,u['tenant_id'],ident,key,u['id'],direction,body,'internal',now()))
        db.execute('UPDATE conversations SET updated_at=? WHERE id=?',(now(),ident)); audit(db,u,'message.note',mid)
    return {'id':mid,'status':'internal'}

@app.get('/api/channels')
def channels(u=Depends(user)):
    permit(u,'settings')
    with transaction() as db: return rows(db,'SELECT id,phone_id,status,verified_at FROM channels WHERE tenant_id=?',(u['tenant_id'],))

@app.post('/api/channels')
def channel_create(data:dict,u=Depends(user)):
    permit(u,'settings'); phone_id=text(data,'phone_id'); ident=uid()
    if not phone_id.isdigit(): raise HTTPException(422,'معرف رقم القناة يجب أن يكون رقميًا')
    with transaction() as db:
        db.execute('INSERT INTO channels(id,tenant_id,phone_id,token_env) VALUES(?,?,?,?)',(ident,u['tenant_id'],phone_id,'')); audit(db,u,'channel.registered',ident)
    return {'id':ident,'status':'unverified'}

@app.post('/api/internal/drain-webhooks')
def drain_webhooks(request:Request):
    secret=os.environ.get('WASLA_WORKER_SECRET','')
    if len(secret)<32 or not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+secret):
        raise HTTPException(401,'Unauthorized')
    from .worker import drain
    return {'processed':drain(max_events=20,max_seconds=20)}

@app.get('/api/webhooks/whatsapp')
def verify_webhook(request:Request):
    expected=os.getenv('META_VERIFY_TOKEN',''); q=request.query_params
    if expected and q.get('hub.mode')=='subscribe' and hmac.compare_digest(q.get('hub.verify_token',''),expected): return PlainTextResponse(q.get('hub.challenge',''))
    raise HTTPException(403,'Webhook verification failed')

@app.post('/api/webhooks/whatsapp')
async def receive_webhook(request:Request):
    secret=os.getenv('META_APP_SECRET','')
    if not secret: raise HTTPException(503,'Webhook not configured')
    raw=await request.body(); expected='sha256='+hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.headers.get('X-Hub-Signature-256',''),expected): raise HTTPException(401,'Invalid signature')
    try:
        payload=json.loads(raw)
        if not isinstance(payload,dict) or payload.get('object')!='whatsapp_business_account' or not isinstance(payload.get('entry'),list): raise ValueError()
    except (ValueError,TypeError): raise HTTPException(400,'Invalid payload')
    with transaction() as db:
        db.execute('INSERT INTO webhook_events(id,payload,created_at) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING',(hashlib.sha256(raw).hexdigest(),raw.decode(),now()))
    return {'received':True}

@app.get('/robots.txt',response_class=PlainTextResponse)
def robots():
    origin=os.getenv('WASLA_ORIGIN','').rstrip('/')
    return 'User-agent: *\nDisallow: /app\nDisallow: /api/\nDisallow: /setup\nDisallow: /login\n'+('Sitemap: '+origin+'/sitemap.xml\n' if origin.startswith('https://') else '')

@app.get('/sitemap.xml')
def sitemap():
    origin=os.getenv('WASLA_ORIGIN','').rstrip('/')
    paths=['/','/services','/platform','/how-it-works','/about','/faq','/request','/privacy','/terms']+['/services/'+s['id'] for s in SERVICES]
    urls=''.join('<url><loc>'+html.escape(origin+p+suffix)+'</loc></url>' for p in paths for suffix in ['', '?lang=en'])
    return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+urls+'</urlset>',media_type='application/xml')

app.mount('/static' ,StaticFiles(directory=STATIC),name='static')

@app.get('/{path:path}')
def shell(path:str,request:Request):
    if path.startswith('api/'): raise HTTPException(404,'المسار غير موجود')
    lang=request.query_params.get('lang',request.cookies.get('wasla_lang','ar'))
    en=lang=='en'; source=(STATIC/'index.html').read_text()
    if en:
        source=source.replace('lang="ar" dir="rtl"','lang="en" dir="ltr"').replace('وصلة | Wasla','Wasla | Connected sales and teams').replace('جارٍ تحميل وصلة…','Loading Wasla…').replace('انتقل للمحتوى','Skip to content')
    private=path.startswith('app') or path in {'login','setup'}
    known=path in {'','services','platform','how-it-works','about','faq','pricing','privacy','terms','request','login','setup'} or path.startswith('app/') or path=='app' or path in {'services/'+s['id'] for s in SERVICES}
    if private or not known: source=source.replace('</head>','<meta name="robots" content="noindex,nofollow"></head>')
    return HTMLResponse(source,status_code=200 if known else 404,headers={'Cache-Control':'no-store','Vary':'Cookie'})
