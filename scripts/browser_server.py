"""Disposable UI-test server. Never imports or modifies production data."""
import os
import tempfile
import uvicorn
os.environ.pop('DATABASE_URL',None)
os.environ.pop('DATABASE_URL_UNPOOLED',None)
with tempfile.TemporaryDirectory(prefix='wasla-browser-') as folder:
    os.environ.update(WASLA_DB=folder+'/test.sqlite3',WASLA_ENV='development',WASLA_ORIGIN='http://127.0.0.1:8000',WASLA_SETUP_TOKEN='b'*48,WASLA_SETUP_EXPIRES='4102444800')
    from wasla.db import initialize,transaction,now
    from wasla.security import hash_password
    initialize()
    with transaction() as db:
        db.execute('INSERT INTO users VALUES(?,NULL,?,?,?,?,1,?)',('ui-provider','ui@example.test','UI Provider',hash_password('ui-only-long-password'),'provider',now()))
    from wasla import service
    import time
    with transaction() as db:
        db.execute('INSERT INTO tenants(id,name,status,created_at) VALUES(?,?,?,?)',('ui-service','Service QA','active',now()))
        db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',('ui-agent','ui-service','service@example.test','Service QA',hash_password('service-test-password'),'admin',now()))
        db.execute('INSERT INTO service_accounts(id,tenant_id,channel,provider_id,label,api_version,created_at) VALUES(?,?,?,?,?,?,?)',('ui-whatsapp','ui-service','whatsapp','123456','QA WhatsApp','v27.0',now()))
    service.persist_events('whatsapp',{'object':'whatsapp_business_account','entry':[{'changes':[{'value':{'metadata':{'phone_number_id':'123456'},'messages':[{'id':'qa-inbound','from':'966500000001','timestamp':str(int(time.time())),'type':'text','text':{'body':'Hello, I need an appointment'}}]}}]}]})
    service.process_event()
    uvicorn.run('wasla.main:app',host='127.0.0.1',port=8000)
