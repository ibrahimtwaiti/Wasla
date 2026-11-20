'use strict';
// No access tokens, SDK responses or authorization codes enter storage or logs.
let metaSdkPromise;
function metaSdk(appId,version){
 if(!metaSdkPromise)metaSdkPromise=new Promise((resolve,reject)=>{
  window.fbAsyncInit=()=>{FB.init({appId,version,autoLogAppEvents:false,xfbml:false});resolve()};
  const script=document.createElement('script');script.src='https://connect.facebook.net/en_US/sdk.js';script.async=true;
  script.onerror=()=>{metaSdkPromise=null;reject(new Error(sv('تعذر تحميل نافذة Meta. أعد المحاولة.','Could not load Meta. Please retry.')))};
  document.head.appendChild(script);
 });
 return metaSdkPromise;
}
const originalChannelsView=channelsView;
channelsView=async function(main){
 await originalChannelsView(main);
 const [r,usage]=await Promise.all([api('/v2/meta/readiness'),api('/v2/meta/usage')]);
 const section=document.createElement('section');section.className='card';section.id='meta-integration-center';
 section.innerHTML=`<h2>${sv('ربط WhatsApp عبر Meta','Connect WhatsApp through Meta')}</h2><p>${sv('الربط متاح حاليًا لشركات الاختبار المعتمدة داخل وصلة. نجاح التفويض لا يثبت اعتماد Meta أو نجاح المراسلة الحية.','Onboarding is currently limited to designated test workspaces. Authorization does not prove Meta approval or live messaging.')}</p><button id="meta-prepare" ${r.signup_available?'':'disabled'}>${sv('تجهيز الربط الآمن','Prepare secure connection')}</button><button id="meta-launch" hidden>${sv('متابعة إلى Meta','Continue to Meta')}</button><p id="meta-status" role="status" aria-live="polite">${r.signup_available?'':sv('يلزم أن يضبط مدير مقدم الخدمة تطبيق Meta ويحدد مساحة الاختبار أولًا.','The provider administrator must configure Meta and designate this test workspace first.')}</p>`+
 (r.connections.length?table([sv('الحساب','Account'),sv('حساب الأعمال / المراسلة','Business / messaging account'),sv('التسجيل والأحداث','Registration and events'),sv('الإجراءات','Actions')],r.connections.map(a=>`<tr><td>${esc(a.label)}<small>${esc(a.phone_number_id)}</small></td><td>${esc(a.business_account_id)}</td><td>${esc(a.registration_state)} / ${esc(a.subscription_state)}<small>${esc(a.token_health||"unchecked")}</small></td><td><button class="light small" data-meta-health="${esc(a.account_id)}">${sv("فحص التفويض","Check authorization")}</button> <button class="light small" data-meta-register="${esc(a.account_id)}">${sv('تسجيل الرقم','Register number')}</button> <button class="light small" data-meta-subscribe="${esc(a.account_id)}">${sv('اشتراك الأحداث','Subscribe webhooks')}</button></td></tr>`)):'')+
 `<h3>${sv('استخدام بوابة Meta خلال 30 يومًا','Meta gateway usage over 30 days')}</h3><p class="micro">${sv('تشمل هذه الأعداد الربط وطلبات بوابة الرسائل والأصول. لا تمثل كشف رسوم Meta.','Counts cover onboarding and the messaging/asset gateway. They are not a Meta billing statement.')}</p>`+
 (usage.totals.length?table([sv('العملية','Operation'),sv('الطلبات','Requests'),sv('نجاح','Successful'),sv('فشل','Failed')],usage.totals.map(x=>`<tr><td>${esc(x.operation)}</td><td>${x.requests}</td><td>${x.successful}</td><td>${x.failed}</td></tr>`)):empty(sv('لا توجد عمليات ربط مسجلة','No onboarding operations recorded')));
 main.prepend(section);
 const status=section.querySelector('#meta-status'),prepare=section.querySelector('#meta-prepare'),launch=section.querySelector('#meta-launch');
 const add=document.querySelector('#service-add');if(add)add.onclick=()=>section.scrollIntoView({behavior:'smooth'});
 let pending=null,listener=null,timer=null;
 const cleanup=()=>{if(listener)window.removeEventListener('message',listener);listener=null;clearTimeout(timer);pending=null;launch.hidden=true;prepare.disabled=!r.signup_available};
 prepare.onclick=async()=>{const documentPath=new URL(performance.getEntriesByType('navigation')[0]?.name||location.href).pathname;if(documentPath!=='/app'&&!documentPath.startsWith('/app/')){location.assign(location.href);return}prepare.disabled=true;try{pending=await send('/v2/meta/whatsapp/start','POST',{});await metaSdk(pending.app_id,pending.api_version);launch.hidden=false;status.textContent=sv('اضغط متابعة لفتح نافذة Meta الرسمية.','Continue to open the official Meta window.')}catch(e){cleanup();status.textContent=e.message}};
 launch.onclick=()=>{
  if(!pending)return;launch.disabled=true;let code=null,asset=null,submitting=false;
  const finish=async()=>{if(!code||!asset||submitting)return;submitting=true;
   try{await send('/v2/meta/whatsapp/complete','POST',{state:pending.state,code,business_account_id:asset.messaging_account_ids[0],phone_number_id:asset.phone_number_id});cleanup();toast(sv('تم التحقق من التفويض. أكمل تسجيل الرقم واشتراك الأحداث ثم اختبار الوصول.','Authorization verified. Complete number registration, webhook subscription and access testing.'));await channelsView(main)}catch(e){cleanup();status.textContent=e.message}finally{code=null;asset=null;launch.disabled=false}
  };
  listener=event=>{
   if(!['https://www.facebook.com','https://web.facebook.com'].includes(event.origin))return;
   let data;try{data=typeof event.data==='string'?JSON.parse(event.data):event.data}catch{return}
   if(!data||data.type!=='WA_EMBEDDED_SIGNUP')return;
   if(data.event==='CANCEL'||data.event==='ERROR'){cleanup();launch.disabled=false;status.textContent=sv('لم يكتمل التفويض. يمكنك بدء محاولة جديدة.','Authorization was not completed. You can start again.');return}
   if(data.event!=='FINISH'||data.data?.is_on_biz_app!==false)return;
   if(!Array.isArray(data.data.messaging_account_ids)||data.data.messaging_account_ids.length!==1||!/^\d{1,40}$/.test(data.data.messaging_account_ids[0])||!/^\d{1,40}$/.test(data.data.phone_number_id))return;
   asset=data.data;finish();
  };
  window.addEventListener('message',listener);
  timer=setTimeout(()=>{cleanup();launch.disabled=false;status.textContent=sv('انتهت مهلة الربط. ابدأ من جديد.','Connection timed out. Start again.')},600000);
  FB.login(response=>{if(!pending)return;if(response.authResponse?.code){code=response.authResponse.code;finish()}else{cleanup();launch.disabled=false;status.textContent=sv('أُلغي الربط.','Connection cancelled.')}},{config_id:pending.config_id,response_type:'code',override_default_response_type:true,extras:pending.extras});
 };
 section.querySelectorAll('[data-meta-health]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await send('/v2/meta/accounts/'+b.dataset.metaHealth+'/token-health','POST',{});await channelsView(main)}catch(e){toast(e.message);b.disabled=false}});
 section.querySelectorAll('[data-meta-register]').forEach(b=>b.onclick=()=>modal(sv('تسجيل رقم Cloud API','Register Cloud API number'),`<p class="full">${sv('للرقم الجديد في Cloud API فقط. احفظ PIN المكون من ستة أرقام بأمان؛ لا يحفظه وصلة.','For a standard Cloud API number only. Keep your six-digit PIN securely; Wasla does not store it.')}</p>`+secretField('pin',sv('رمز PIN من ستة أرقام','Six-digit PIN')),async d=>{await send('/v2/meta/accounts/'+b.dataset.metaRegister+'/register','POST',d)}));
 section.querySelectorAll('[data-meta-subscribe]').forEach(b=>b.onclick=async()=>{try{await send('/v2/meta/accounts/'+b.dataset.metaSubscribe+'/subscribe','POST',{});await channelsView(main)}catch(e){toast(e.message)}});
};
const whatsappChannelsView=channelsView;
channelsView=async function(main){
 await whatsappChannelsView(main);
 const r=await api('/v2/meta/pages');const block=document.createElement('section');block.className='card';
 block.innerHTML=`<h2>Facebook Pages / Messenger</h2><p>${sv('ربط تجريبي بصفحة مؤهلة. يدعم الملف التعريفي والرسائل النصية ضمن نافذة 24 ساعة. يلزم اعتماد Meta واختبار حي قبل الإطلاق.','Test connection to an eligible Page. Supports profile and text replies within 24 hours. Meta approval and live testing are required before launch.')}</p><button id="facebook-prepare" ${r.signup_available?'':'disabled'}>${sv('تجهيز ربط الصفحة','Prepare Page connection')}</button> <button id="facebook-launch" hidden>${sv('متابعة إلى Meta','Continue to Meta')}</button><p role="status" id="facebook-status"></p><div id="facebook-pages"></div>${table([sv('الصفحة','Page'),sv('الحالة','Status'),sv('الأحداث','Webhooks')],r.accounts.map(a=>`<tr><td>${esc(a.label)}<small>${esc(a.provider_id)}</small></td><td>${esc(a.state)}</td><td>${esc(a.subscription_state)} <button data-page-posts="${esc(a.id)}">${sv("منشورات الصفحة","Page posts")}</button> <button data-page-profile="${esc(a.id)}">${sv("بيانات الصفحة","Page profile")}</button> <button data-page-disconnect="${esc(a.id)}">${sv("فصل الصفحة","Disconnect Page")}</button> <button data-page-subscribe="${esc(a.id)}">${sv('اشتراك الأحداث','Subscribe webhooks')}</button></td></tr>`))}`;main.append(block);
 let state;const status=block.querySelector('#facebook-status'),launch=block.querySelector('#facebook-launch');
 block.querySelector('#facebook-prepare').onclick=async()=>{try{const initial=new URL(performance.getEntriesByType('navigation')[0]?.name||location.href).pathname;if(!initial.startsWith('/app')){location.assign(location.href);return}state=await send('/v2/meta/pages/start','POST',{});await metaSdk(state.app_id,state.api_version);launch.hidden=false}catch(e){status.textContent=e.message}};
 launch.onclick=()=>{launch.disabled=true;FB.login(async response=>{try{if(!response.authResponse?.code)throw new Error(sv('أُلغي التفويض','Authorization cancelled'));const result=await send('/v2/meta/pages/exchange','POST',{state:state.state,code:response.authResponse.code});block.querySelector('#facebook-pages').innerHTML=result.pages.map(p=>`<button class="light" data-page="${esc(p.id)}">${esc(p.name)}</button>`).join('');block.querySelectorAll('[data-page]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await send('/v2/meta/pages/connect','POST',{state:state.state,page_id:b.dataset.page});state=null;await channelsView(main)}catch(e){status.textContent=e.message;b.disabled=false}})}catch(e){status.textContent=e.message}finally{launch.hidden=true;launch.disabled=false}},{config_id:state.config_id,response_type:'code',override_default_response_type:true});};
 block.querySelectorAll('[data-page-posts]').forEach(b=>b.onclick=async()=>{try{
  const result=await api('/v2/meta/pages/'+b.dataset.pagePosts+'/posts');let box=block.querySelector('#page-posts');if(!box){box=document.createElement('section');box.id='page-posts';box.className='card';block.append(box)}
  box.innerHTML=`<h3>${sv('منشورات الصفحة','Page posts')}</h3>${table([sv('النص','Text'),sv('الحالة','State'),'Meta ID'],result.posts.map(p=>`<tr><td>${esc(p.body)}</td><td>${ss(p.state)}<small>${esc(p.error)}</small></td><td>${esc(p.remote_id||'—')}</td></tr>`))}${result.available?`<form id="page-publish">${errorBox}${area('body',sv('نص المنشور','Post text'))}<p>${sv('هذا ينشر على الصفحة المحددة عبر Meta عند تنفيذ العامل. لا ينشئ إعلانًا. اكتب PUBLISH للتأكيد.','This publishes to the selected Page through Meta when the worker runs. It creates no ad. Type PUBLISH to confirm.')}</p>${field('confirmation',sv('تأكيد النشر','Publishing confirmation'))}<button type="submit">${sv('إرسال المنشور إلى طابور النشر','Queue Page post')}</button></form>`:`<p>${esc(result.reason)}</p>`}`;
  if(result.available){const key=crypto.randomUUID();bindForm(box.querySelector('form'),async d=>{await send('/v2/meta/pages/'+b.dataset.pagePosts+'/posts','POST',d,{'Idempotency-Key':key});b.click()})}
 }catch(e){toast(e.message)}});
 block.querySelectorAll('[data-page-profile]').forEach(b=>b.onclick=async()=>{try{const p=await api('/v2/meta/pages/'+b.dataset.pageProfile+'/profile');status.textContent=[p.name,p.category,p.id].filter(Boolean).join(' · ')}catch(e){status.textContent=e.message}});
 block.querySelectorAll('[data-page-disconnect]').forEach(b=>b.onclick=async()=>{if(!confirm(sv('فصل الصفحة وإيقاف اشتراك التطبيق في أحداثها؟','Disconnect this Page and unsubscribe this app from its webhooks?')))return;try{const r=await send('/v2/meta/pages/'+b.dataset.pageDisconnect+'/disconnect-remote','POST',{});toast(r.remote_unsubscribe==='confirmed'?sv('تم الفصل','Disconnected'):sv('تم الفصل محليًا. راجع إلغاء الاشتراك من حساب Meta.','Disconnected locally. Review remote subscription in Meta.'));await channelsView(main)}catch(e){status.textContent=e.message}});
 block.querySelectorAll('[data-page-subscribe]').forEach(b=>b.onclick=async()=>{try{await send('/v2/meta/pages/'+b.dataset.pageSubscribe+'/subscribe','POST',{});await channelsView(main)}catch(e){status.textContent=e.message}});
};

const channelsBeforeInstagramDiscovery=channelsView;
channelsView=async function(main){
 await channelsBeforeInstagramDiscovery(main);const r=await api('/v2/meta/instagram');const section=document.createElement('section');section.className='card';section.innerHTML=`<h2>Instagram</h2><p>${sv('ربط الحساب الاحترافي عبر نافذة Instagram الرسمية دون إدخال مفتاح أو معرّف الحساب. يلزم اعتماد التطبيق واختبار حي.','Connect a professional account through official Instagram authorization without entering a token or account ID. App approval and live verification are required.')}</p><button id="instagram-connect" ${r.signup_available?'':'disabled'}>${sv('ربط Instagram','Connect Instagram')}</button>${r.signup_available?'':`<p>${sv('أكمل إعداد تطبيق Instagram وعنوان العودة ومساحة الاختبار أولًا.','Configure the Instagram app, callback origin and test workspace first.')}</p>`}`;main.append(section);
 section.querySelector('button').onclick=async()=>{try{const r=await send('/v2/meta/instagram/start','POST',{});location.assign(r.authorization_url)}catch(e){toast(e.message)}};
};
