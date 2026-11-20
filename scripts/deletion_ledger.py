"""Export deletion tombstones or apply them locally to an isolated restore."""
import argparse
import json
import os
from pathlib import Path

from wasla.db import one, rows, transaction
from wasla.privacy import process_deletion, queue


def export_ledger():
    with transaction() as db:
        data = rows(db, """SELECT id,tenant_id,requester_id,scope,targets,completed_at
            FROM deletion_jobs WHERE state='completed'
            AND (source_key IS NULL OR source_key NOT LIKE 'restore:%')""")
    return {'format': 'wasla-erasure-ledger-v1', 'deletions': data}


def replay_ledger(ledger, apply=False):
    if ledger.get('format') != 'wasla-erasure-ledger-v1' or not isinstance(ledger.get('deletions'), list):
        raise ValueError('invalid_erasure_ledger')
    mode = os.getenv('WASLA_ENV')
    if mode in {'staging', 'production'} or os.getenv('VERCEL_ENV') or (apply and mode != 'restore'):
        raise ValueError('replay_requires_isolated_restore_environment')
    jobs = []
    with transaction() as db:
        for row in ledger['deletions']:
            scope, tid, requester = row['scope'], row['tenant_id'], row['requester_id']
            if scope not in {'tenant', 'user', 'account', 'accounts'}:
                raise ValueError('invalid_deletion_scope')
            targets = json.loads(row['targets'])
            original = one(db, 'SELECT * FROM deletion_jobs WHERE id=?', (row['id'],))
            if original and (original['tenant_id'], original['requester_id'], original['scope']) != (tid, requester, scope):
                raise ValueError('restore_target_mismatch')
            if original and original['state'] != 'completed':
                # The backup captured the original job mid-flight. Retain its idempotency key.
                jobs.append(original['id'])
                if apply:
                    db.execute("""UPDATE deletion_jobs SET remote_ciphertext='',remote_status='restore_local_only',
                        next_attempt=0 WHERE id=?""", (original['id'],))
                    db.execute('DELETE FROM deletion_objects WHERE job_id=?', (original['id'],))
                continue
            if scope == 'tenant' and not one(db, 'SELECT id FROM tenants WHERE id=?', (tid,)):
                continue
            if scope == 'user' and not one(db, "SELECT id FROM users WHERE id=? AND password_hash<>''", (requester,)):
                continue
            if apply:
                jobs.append(queue(db, tid, requester, scope, targets['accounts'], targets['data_accounts'], source='restore:'+row['id']))
            else:
                jobs.append(row['id'])
    return {'apply': apply, 'jobs': jobs}


def drain_replay(jobs):
    """Run only selected replay jobs. Holds and retry states keep the restore closed."""
    pending = 0
    for ident in set(jobs):
        with transaction() as db:
            item = one(db,'SELECT remote_status,source_key FROM deletion_jobs WHERE id=?',(ident,))
        if not item or not ((item['source_key'] or '').startswith('restore:') or item['remote_status']=='restore_local_only'):
            raise ValueError('not_a_restore_job')
        process_deletion(ident)
        with transaction() as db:
            if one(db,'SELECT state FROM deletion_jobs WHERE id=?',(ident,))['state'] != 'completed':
                pending += 1
    return pending


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['export', 'replay'])
    parser.add_argument('file', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.action == 'export':
        descriptor = os.open(args.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(export_ledger(), stream)
        print('Private erasure ledger exported.')
    else:
        result = replay_ledger(json.loads(args.file.read_text()), args.apply)
        pending = drain_replay(result['jobs']) if args.apply else None
        print(json.dumps({'apply': args.apply, 'jobs': len(result['jobs']), 'pending': pending}))
        if pending:
            raise SystemExit('Restore remains closed while erasure jobs are pending.')


if __name__ == '__main__':
    main()
