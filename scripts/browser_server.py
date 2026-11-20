"""Disposable UI-test server. Never imports or modifies production data."""
import os
import tempfile
import uvicorn
from cryptography.fernet import Fernet
os.environ.pop('DATABASE_URL',None)
os.environ.pop('WASLA_DATABASE_URL',None)
port=int(os.getenv('WASLA_BROWSER_PORT','8000'))
os.environ.pop('DATABASE_URL_UNPOOLED',None)
with tempfile.TemporaryDirectory(prefix='wasla-browser-') as folder:
    os.environ.update(WASLA_DB=folder+'/test.sqlite3',WASLA_ENV='development',WASLA_ORIGIN='http://127.0.0.1:'+str(port),WASLA_EMAIL_MODE='local',WASLA_SETUP_TOKEN='b'*48,WASLA_SETUP_EXPIRES='4102444800')
    os.environ['WASLA_TOKEN_ENCRYPTION_KEY']=Fernet.generate_key().decode()
    from wasla.db import initialize,transaction,now
    from wasla.security import hash_password
    initialize()
    with transaction() as db:
        db.execute('INSERT INTO users VALUES(?,NULL,?,?,?,?,1,?)',('ui-provider','ui@example.test','UI Provider',hash_password('ui-only-long-password'),'provider',now()))
        db.execute('INSERT INTO users VALUES(?,NULL,?,?,?,?,1,?)',('ui-commerce','commerce@example.test','Commerce QA',hash_password('ui-only-long-password'),'provider',now()))
    from wasla import service
    import time
    with transaction() as db:
        db.execute('INSERT INTO tenants(id,name,status,created_at) VALUES(?,?,?,?)',('ui-service','Service QA','active',now()))
        db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',('ui-agent','ui-service','service@example.test','Service QA',hash_password('service-test-password'),'admin',now()))
        db.execute('INSERT INTO service_accounts(id,tenant_id,channel,provider_id,label,api_version,state,created_at) VALUES(?,?,?,?,?,?,?,?)',('ui-whatsapp','ui-service','whatsapp','123456','QA WhatsApp','v27.0','verified',now()))
        db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',('ui-meta','ui-service','meta@example.test','Meta QA',hash_password('service-test-password'),'admin',now()))
    service.persist_events('whatsapp',{'object':'whatsapp_business_account','entry':[{'changes':[{'value':{'metadata':{'phone_number_id':'123456'},'messages':[{'id':'qa-inbound','from':'966500000001','timestamp':str(int(time.time())),'type':'text','text':{'body':'Hello, I need an appointment'}}]}}]}]})
    service.process_event()
    # These routes exist only in this disposable test process, never in wasla.main.
    from wasla.main import app,user,provider
    from wasla.db import one
    from fastapi import Depends,HTTPException
    from wasla import email_verification,privacy
    from wasla.service_oauth import cipher
    import json
    @app.post('/api/__test__/verification')
    def test_verification(data:dict,u=Depends(user)):
        provider(u);email_verification.process_email()
        with transaction() as db:
            r=one(db,"SELECT j.ciphertext FROM email_jobs j JOIN users u ON u.id=j.user_id WHERE u.email=? AND j.state='local'",(data['email'],))
        if not r:raise HTTPException(404,'test_email_not_ready')
        return json.loads(cipher().decrypt(r['ciphertext'].encode()))
    @app.post('/api/__test__/drain-deletion')
    def test_deletion(u=Depends(user)):
        provider(u)
        for _ in range(8):privacy.process_deletion()
        return {'ok':True}
    uvicorn.run(app,host='127.0.0.1',port=port)
