// Trigger-only entry point. Neon strips forged x-neon-* headers at its edge.
import { parseTriggerDelivery } from '@neon/functions/triggers';
export default {
  async fetch(request) {
    if (request.method !== 'POST') return new Response('method not allowed', {status:405});
    const parsed=await parseTriggerDelivery(request);
    if(!parsed.ok)return new Response(parsed.error,{status:parsed.error==='invalid_body'?400:401});
    if(parsed.invocation.type!=='schedule')return new Response('schedule required',{status:400});
    const url=process.env.WASLA_WORKER_URL,secret=process.env.WASLA_WORKER_SECRET;
    if(!url?.startsWith('https://')||!secret||secret.length<32)return new Response('worker configuration incomplete',{status:503});
    const headers={'authorization':'Bearer '+secret,'content-type':'application/json'};
    if(process.env.VERCEL_AUTOMATION_BYPASS_SECRET)headers['x-vercel-protection-bypass']=process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
    try{
      const result=await fetch(url,{method:'POST',headers,body:'{}',redirect:'error',signal:AbortSignal.timeout(25000)});
      if(!result.ok){console.error('worker status',result.status);return new Response('worker unavailable',{status:502})}
      const data=await result.json();
      console.info('wasla worker tick',JSON.stringify({ok:true,processed:data.processed??0}));
      return Response.json({ok:true});
    }catch{console.error('worker unavailable');return new Response('pending events remain queued',{status:502})}
  }
};
