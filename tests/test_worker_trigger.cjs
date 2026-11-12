const {test}=require('node:test'),assert=require('node:assert/strict');
test('trigger worker rejects forged or mismatched requests and forwards only validated schedules',async()=>{
 const worker=(await import('../neon/functions/drain/index.mjs')).default;
 assert.equal((await worker.fetch(new Request('https://worker.test/'))).status,405);
 assert.equal((await worker.fetch(new Request('https://worker.test/',{method:'POST',body:'{}'}))).status,401);
 const body={version:1,invocation_id:'local-test',trigger:{type:'schedule',id:'trigger-test',name:'test'},data:{scheduled_at:new Date().toISOString()}};
 const req=id=>new Request('https://worker.test/',{method:'POST',headers:{'x-neon-trigger-invocation-id':id,'content-type':'application/json'},body:JSON.stringify(body)});
 assert.equal((await worker.fetch(req('wrong'))).status,401);
 process.env.WASLA_WORKER_URL='https://wasla.test/api/internal/drain-webhooks';process.env.WASLA_WORKER_SECRET='x'.repeat(48);
 const original=global.fetch;let called=false;
 global.fetch=async(url,options)=>{called=true;assert.equal(url,process.env.WASLA_WORKER_URL);assert.equal(options.redirect,'error');assert.equal(options.headers.authorization,'Bearer '+'x'.repeat(48));return Response.json({processed:1})};
 try{assert.equal((await worker.fetch(req('local-test'))).status,200);assert.equal(called,true)}finally{global.fetch=original}
});
