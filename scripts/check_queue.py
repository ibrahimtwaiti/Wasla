"""Read-only queue check; emits aggregate counts only, never event payloads."""
import json
import os
from datetime import datetime, timezone


def evaluate(pending, dead, oldest, now=None, max_age=300):
    if pending < 0 or dead < 0 or max_age <= 0:
        raise ValueError('Invalid queue metrics or threshold')
    age = 0
    if pending:
        if not oldest:
            raise ValueError('Pending events require an oldest timestamp')
        stamp = datetime.fromisoformat(oldest.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('Queue timestamp must have a timezone')
        age = max(0, int(((now or datetime.now(timezone.utc)) - stamp).total_seconds()))
    return {'pending': pending, 'dead': dead, 'oldest_pending_seconds': age,
            'healthy': dead == 0 and age <= max_age}


def main():
    import psycopg
    url = os.environ['MONITOR_DATABASE_URL']
    with psycopg.connect(url, connect_timeout=10) as db:
        db.execute('SET TRANSACTION READ ONLY')
        db.execute("SET LOCAL statement_timeout = '10s'")
        pending, dead, oldest = db.execute("""
            SELECT count(*) FILTER (WHERE status='pending'),
                   count(*) FILTER (WHERE status='dead'),
                   min(created_at) FILTER (WHERE status='pending')
            FROM webhook_events
        """).fetchone()
        result = evaluate(pending, dead, oldest,
                          max_age=int(os.environ.get('QUEUE_MAX_AGE_SECONDS', '300')))
    print(json.dumps(result))
    return 0 if result['healthy'] else 1


if __name__ == '__main__':
    try:
        code = main()
    except Exception:
        raise SystemExit('Queue check failed: check configuration or database access; no success recorded.')
    raise SystemExit(code)
