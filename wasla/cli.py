"""Operator commands; secrets are entered interactively, never command arguments."""
import argparse
import getpass
import sqlite3
from .db import initialize, transaction, uid, now, path
from .security import hash_password

p=argparse.ArgumentParser()
sub=p.add_subparsers(dest='command',required=True)
sub.add_parser('init')
sub.add_parser('bootstrap')
b=sub.add_parser('backup'); b.add_argument('destination')
r=sub.add_parser('reset-password'); r.add_argument('email')

def main():
    args=p.parse_args(); initialize()
    if args.command=='init': print('Database migrated.')
    elif args.command=='backup':
        with sqlite3.connect(path()) as src, sqlite3.connect(args.destination) as dst: src.backup(dst)
        print('Consistent backup created. Encrypt and store off-host before retention cleanup.')
    elif args.command=='bootstrap':
        email=input('Provider admin email: ').strip().lower()
        name=input('Display name: ').strip()
        if '@' not in email or not name: raise SystemExit('Valid email and name required')
        password=hash_password(getpass.getpass('Password (12+ characters): '))
        with transaction() as db:
            if db.execute("SELECT 1 FROM users WHERE role='provider'").fetchone(): raise SystemExit('Provider already exists')
            db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',(uid(),None,email,name,password,'provider',now()))
        print('Provider admin created. No default credentials are supplied.')
    elif args.command=='reset-password':
        password=hash_password(getpass.getpass('New password: '))
        with transaction() as db:
            user=db.execute('SELECT id FROM users WHERE email=?',(args.email.lower(),)).fetchone()
            if not user: raise SystemExit('User not found')
            db.execute('UPDATE users SET password_hash=? WHERE id=?',(password,user[0]))
            db.execute('DELETE FROM sessions WHERE user_id=?',(user[0],))
        print('Password changed and sessions revoked.')

if __name__=='__main__': main()
