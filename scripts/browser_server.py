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
    uvicorn.run('wasla.main:app',host='127.0.0.1',port=8000)
