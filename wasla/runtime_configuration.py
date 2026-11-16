"""Encrypted provider-owned settings; secrets are never returned by administration APIs."""
import os
from .db import one, postgres, transaction, path

def value(key,default=''):
    # Independent SQLite reader permits use inside the service's write transaction.
    if postgres():
        with transaction() as db:r=one(db,'SELECT ciphertext FROM provider_configuration WHERE key=?',(key,))
    else:
        import sqlite3
        with sqlite3.connect(path()) as db:
            db.row_factory=sqlite3.Row
            r=one(db,'SELECT ciphertext FROM provider_configuration WHERE key=?',(key,))
    if r:
        from .service_oauth import cipher
        from cryptography.fernet import InvalidToken
        from fastapi import HTTPException
        try:return cipher().decrypt(r['ciphertext'].encode()).decode()
        except InvalidToken:raise HTTPException(503,'configuration_decryption_failed')
    return os.getenv(key,default)
