"""Fair, bounded durable-job ticks for HTTP triggers and the container worker."""
import time
from .db import transaction

def tick(max_jobs=20,max_seconds=18):
    from .worker import process_one
    from .service import process_event,send_outbox
    from .service_media import process_media
    from .email_verification import process_email
    from .privacy import process_deletion
    from .meta_data import process_lead
    from .page_posts import process_post
    tasks=[('inbound',process_event),('outbound',send_outbox),('media',process_media),('legacy',process_one),('email',process_email),('deletions',process_deletion),('leads',process_lead),('page_posts',process_post)]
    cleanup_expired_security_metadata()
    result={name:0 for name,_ in tasks};end=time.monotonic()+max_seconds;empty=0
    for _ in range(max_jobs+len(tasks)):
        # Reserve a complete provider timeout before beginning another operation.
        if sum(result.values()) and (sum(result.values())>=max_jobs or end-time.monotonic()<15):break
        with transaction() as db:
            index=db.execute("INSERT INTO worker_cursors VALUES('durable_jobs',1) ON CONFLICT(name) DO UPDATE SET position=worker_cursors.position+1 RETURNING position").fetchone()[0]-1
        name,fn=tasks[index%len(tasks)]
        if fn():
            result[name]+=1;empty=0
            if name in {'email','deletions','media'}:break # Multipart network work uses its own bounded timeouts.
        else:
            empty+=1
            if empty>=len(tasks):break
    return {'processed':sum(result.values()),'queues':result}

def cleanup_expired_security_metadata():
    clock=int(time.time())
    with transaction() as db:
        for table in ['meta_signup_sessions','meta_page_sessions','meta_data_sessions','service_oauth_states']:
            db.execute('DELETE FROM '+table+' WHERE state_hash IN (SELECT state_hash FROM '+table+' WHERE expires_at<=? LIMIT 500)',(clock,))
        db.execute('DELETE FROM sessions WHERE token_hash IN (SELECT token_hash FROM sessions WHERE expires_at<=? LIMIT 500)',(clock,))
        db.execute('DELETE FROM rate_limits WHERE key IN (SELECT key FROM rate_limits WHERE expires_at<=? LIMIT 500)',(clock,))
        db.execute("UPDATE email_verifications SET token_hash='' WHERE verified_at IS NULL AND expires_at<=?",(clock,))
