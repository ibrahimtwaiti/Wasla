"""Isolated synthetic SQLite benchmark. Never connects to a configured database or provider."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
from pathlib import Path
import statistics
import sys
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--messages',type=int,default=1000)
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if not 10<=args.messages<=10000 or not 1<=args.workers<=16:parser.error('messages 10–10000; workers 1–16')
    # Clear remote DB selectors before even constructing the disposable test fixture.
    os.environ.pop('DATABASE_URL',None);os.environ.pop('WASLA_DATABASE_URL',None)
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
    from test_acceptance import Acceptance
    from wasla.db import transaction,one
    from wasla import service
    fixture=Acceptance();fixture.setUp()
    try:
        fixture.login()
        r=fixture.client.post('/api/v2/accounts',json={'channel':'whatsapp','provider_id':'benchmark-only','label':'Synthetic benchmark','api_version':'v27.0'})
        r.raise_for_status()
        stamp=int(time.time());latencies=[]
        def submit(batch):
            payload={'object':'whatsapp_business_account','entry':[{'changes':[{'value':{'metadata':{'phone_number_id':'benchmark-only'},'messages':batch}}]}]}
            raw=json.dumps(payload).encode();signature='sha256='+hmac.new(b'test-webhook-secret',raw,hashlib.sha256).hexdigest()
            start=time.perf_counter();reply=fixture.client.post('/api/webhooks/v2/whatsapp',content=raw,headers={'X-Hub-Signature-256':signature,'Content-Type':'application/json'});reply.raise_for_status()
            return (time.perf_counter()-start)*1000
        batches=[]
        for start in range(0,args.messages,50):
            batches.append([{'id':f'synthetic-{i}','from':f'synthetic-user-{i}','timestamp':str(stamp),'type':'text','text':{'body':'Synthetic benchmark request'}} for i in range(start,min(start+50,args.messages))])
        start=time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:latencies=list(pool.map(submit,batches+batches))
        ingest=time.perf_counter()-start
        def drain(_):
            n=0
            while service.process_event():n+=1
            return n
        start=time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:processed=sum(pool.map(drain,range(args.workers)))
        elapsed=time.perf_counter()-start
        with transaction() as db:
            counts={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['service_events','service_messages','service_tickets','service_outbox']}
            failed=db.execute("SELECT count(*) FROM service_events WHERE state!='processed'").fetchone()[0]
        assert processed==args.messages and failed==0 and counts['service_outbox']==0,(processed,failed,counts)
        assert all(counts[t]==args.messages for t in ['service_events','service_messages','service_tickets']),counts
        start=time.perf_counter();report=fixture.client.get('/api/v2/reports').json();report_ms=(time.perf_counter()-start)*1000
        assert report['tickets']==args.messages and report['backlog']==args.messages,report
        fixture.login('admin-b');assert fixture.client.get('/api/v2/reports').json()['tickets']==0
        result={'scope':'Synthetic local SQLite; not Vercel/PostgreSQL production capacity or a live platform test','messages':args.messages,'duplicate_deliveries':args.messages,'workers':args.workers,'webhook_requests':len(latencies),'webhook_p50_ms':round(statistics.median(latencies),2),'webhook_p95_ms':round(sorted(latencies)[int((len(latencies)-1)*.95)],2),'ingest_seconds':round(ingest,3),'processing_seconds':round(elapsed,3),'processed_per_second':round(processed/elapsed,2),'report_ms':round(report_ms,2),'counts':counts,'failed_events':failed,'cross_workspace_tickets':0,'external_sends':0,'integrity_passed':True}
        Path(args.output).write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
    finally:fixture.tearDown()

if __name__=='__main__':main()
