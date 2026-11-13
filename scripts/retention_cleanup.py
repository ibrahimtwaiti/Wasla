"""Conservative Wasla V1 retention job.

Only delete well-defined categories whose reference graph can be checked safely.
Complex CRM, closed projects and terminated tenant exports require verified
case-by-case offboarding (and any statutory holds), not blanket SQL cascades.
Never print contact information, payloads or record identifiers.
"""
import argparse
import calendar
import os
from datetime import datetime, timezone, timedelta
from .backup_postgres import database_env
from wasla.db import transaction, postgres

SOURCE_HOST = 'ep-dawn-mode-basxmcbr.c-8.eu-central-1.aws.neon.tech'
SOURCE_DATABASE = 'wasla'


def subtract_months(at, months):
    total = at.year * 12 + at.month - 1 - months
    year, month = divmod(total, 12)
    return at.replace(year=year, month=month+1, day=min(at.day, calendar.monthrange(year, month+1)[1]))


def expires(at):
    return {
        'processed_webhooks': (at - timedelta(days=30)).isoformat(),
        'audit_logs': (at - timedelta(days=180)).isoformat(),
        'unconverted_leads': subtract_months(at, 6).isoformat(),
    }


def eligible(db, name, cutoff, nowstamp, limit=250):
    holds = {
        'processed_webhooks': ("webhook_events", "w", "w.status='processed'", "webhook"),
        'audit_logs': ("audit", "w", "1=1", "audit"),
        'unconverted_leads': ("requests", "w", "w.status IN ('new','rejected') AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.request_id=w.id)", "request"),
    }
    table, alias, extra, entity_type = holds[name]
    date_column = "w.updated_at" if name == "unconverted_leads" else "w.created_at"
    sql = f"""SELECT w.id FROM {table} w WHERE {extra} AND {date_column} < ?
        AND NOT EXISTS (SELECT 1 FROM retention_holds h
            WHERE h.entity_type=? AND h.entity_id=w.id
            AND (h.held_until IS NULL OR h.held_until > ?))
        ORDER BY {date_column} ASC LIMIT ?"""
    return [row[0] for row in db.execute(sql,(cutoff,entity_type,nowstamp,limit)).fetchall()]


def execute(db, when, apply=False, limit=250):
    cutoffs = expires(when)
    stamp = when.isoformat()
    result = {'mode': 'APPLY' if apply else 'DRY_RUN', 'counts': {}, 'bounded_batch': limit}
    for name in ('processed_webhooks','audit_logs','unconverted_leads'):
        selected=eligible(db,name,cutoffs[name],stamp,limit)
        result['counts'][name]=len(selected)
        if not apply: continue
        for ident in selected:
            if name=='processed_webhooks':
                db.execute("DELETE FROM webhook_events WHERE id=? AND status='processed'",(ident,))
            elif name=='audit_logs':
                db.execute("DELETE FROM audit WHERE id=?",(ident,))
            else:
                # Provider opportunity and notification references are removed
                # before the unconverted request. All steps share one transaction.
                db.execute('DELETE FROM provider_opportunities WHERE request_id=?',(ident,))
                db.execute('DELETE FROM notifications WHERE entity_id=?',(ident,))
                db.execute("DELETE FROM requests WHERE id=? AND status IN ('new','rejected') AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.request_id=requests.id)",(ident,))
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if not postgres():
        raise SystemExit('This automated job requires the dedicated PostgreSQL production environment')
    url = os.environ.get('DATABASE_URL','')
    env=database_env(url)
    if env['PGHOST']!=SOURCE_HOST or env['PGDATABASE']!=SOURCE_DATABASE:
        raise SystemExit('Refusing retention outside the approved Wasla production source')
    if args.apply and os.environ.get('WASLA_RETENTION_APPLY')!='YES':
        raise SystemExit('Set WASLA_RETENTION_APPLY=YES to authorize retention mutation')
    with transaction() as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtext('wasla-retention-v1'))")
        report=execute(db,datetime.now(timezone.utc),apply=args.apply)
    print('Wasla retention mode:',report['mode'])
    for key,count in report['counts'].items():
        print(key+':', count, 'candidate rows within bounded batch')
    print('No customer content was logged. Closed projects and tenant offboarding still require separate review.')


if __name__=='__main__':
    main()
