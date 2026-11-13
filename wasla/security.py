import hashlib
import hmac
import secrets
import time
from fastapi import HTTPException

ROLES = {'admin','supervisor','sales','marketing','services','reader'}
PERMISSIONS = {
 'admin': {'contacts','crm','inbox','team','settings','reports','projects','export'},
 'supervisor': {'contacts','crm','inbox','reports'},
 'sales': {'contacts','crm','inbox'},
 'marketing': {'contacts'},
 'services': {'projects'},
 'reader': {'reports'},
}

def hash_password(password):
    if len(password) < 16 or len(password) > 128:
        raise HTTPException(422, 'كلمة المرور يجب أن تكون بين 16 و128 حرفًا')
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(),salt=salt.encode(),n=16384,r=8,p=1).hex()
    return f'scrypt${salt}${digest}'

def verify_password(password, encoded):
    try:
        _,salt,expected=encoded.split('$')
        actual=hashlib.scrypt(password.encode(),salt=salt.encode(),n=16384,r=8,p=1).hex()
        return hmac.compare_digest(actual,expected)
    except (ValueError, TypeError):
        return False

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def permit(user, permission):
    if permission not in PERMISSIONS.get(user['role'],set()):
        raise HTTPException(403,'ليس لديك صلاحية لهذه العملية')

def limit(db, key, maximum=10, seconds=600):
    current=int(time.time())
    db.execute('DELETE FROM rate_limits WHERE expires_at < ?', (current,))
    record=db.execute('SELECT count FROM rate_limits WHERE key=?',(key,)).fetchone()
    if record and record[0]>=maximum:
        raise HTTPException(429,'طلبات كثيرة؛ يرجى المحاولة لاحقًا')
    db.execute('INSERT INTO rate_limits VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=rate_limits.count+excluded.count', (key,current+seconds))
