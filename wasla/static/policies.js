'use strict';
function policyPage(path){
 const en=LANG==='en',privacy=path==='/privacy',c=catalog.content;
 const title=privacy?(en?'Privacy and your data':'الخصوصية وبياناتك'):(en?'Service terms':'شروط الخدمة');
 const sections=privacy?(en?[
 ['Data and purpose','We store the contact details, company information and requirements you submit to review and follow up your service request. Marketing consent is separate and optional. Do not submit sensitive information in the request form.'],
 ['Access and hosting','Authorized team members access records according to their roles. Wasla uses Vercel for application hosting and Neon for its PostgreSQL database. Hosting providers process data to operate the service.'],
 ['Your choices','Contact the operating team using the email below to request access, correction, deletion or withdrawal of marketing consent. Include your request reference where available. Requests are reviewed against applicable retention obligations and operational needs.'],
 ['Browser storage','The site stores a language preference cookie. Signing in creates a secure session cookie. An unfinished service request is temporarily saved in your tab’s session storage and cleared after successful submission. No third-party advertising trackers are included in this release.']
 ]:[
 ['البيانات والغرض','نحفظ بيانات التواصل والشركة ووصف الاحتياج الذي ترسله لمراجعة طلب الخدمة ومتابعته. موافقة التسويق منفصلة واختيارية. يرجى عدم إدخال معلومات حساسة في نموذج الطلب.'],
 ['الوصول والاستضافة','يصل الفريق المخول إلى السجلات بحسب صلاحياته. يستخدم الموقع Vercel لاستضافة التطبيق وNeon لقاعدة PostgreSQL. يعالج مزودو الاستضافة البيانات اللازمة لتشغيل الخدمة.'],
 ['خياراتك','تواصل مع الجهة المشغلة عبر البريد أدناه لطلب الوصول إلى بياناتك أو تصحيحها أو حذفها أو سحب موافقة التسويق، مع رقم الطلب إن توفر. تُراجع الطلبات وفق التزامات الاحتفاظ المنطبقة واحتياجات تشغيل الخدمة.'],
 ['تخزين المتصفح','نحفظ تفضيل اللغة في ملف ارتباط، ونستخدم ملف جلسة آمنًا عند تسجيل الدخول. يُحفظ الطلب غير المكتمل مؤقتًا داخل تبويب المتصفح ويُمسح بعد الإرسال الناجح. لا يتضمن هذا الإصدار أدوات تتبع إعلانية خارجية.']
 ]):(en?[
 ['Requests and agreements','Sending a request does not create a paid subscription. Services, deliverables, fees, timing and responsibilities are agreed in a separate proposal before delivery.'],
 ['External providers','WhatsApp, store and external CRM integrations require provider accounts, approvals and independent testing. Provider approval and specific revenue outcomes are not guaranteed.'],
 ['Responsible use','Use accounts only within your authorized company workspace. Keep credentials private and provide accurate information that you are authorized to share. External WhatsApp sending is not activated in V1.'],
 ['Support','Contact the operating team below about your request or service scope. Changes to an agreed service follow that service agreement.']
 ]:[
 ['الطلبات والاتفاق','إرسال الطلب لا ينشئ اشتراكًا مدفوعًا. تُحدد الخدمات والمخرجات والرسوم والمواعيد والمسؤوليات في عرض مستقل يُعتمد قبل التنفيذ.'],
 ['المزودون الخارجيون','يتطلب ربط واتساب والمتاجر وأنظمة CRM الخارجية حسابات وموافقات واختبارات مستقلة. لا نضمن قبول المزود الخارجي أو تحقيق عائد مالي محدد.'],
 ['الاستخدام المسؤول','استخدم حسابك ضمن مساحة الشركة وصلاحياتك، واحفظ بيانات الدخول، وقدّم معلومات صحيحة مخولًا بمشاركتها. الإرسال الخارجي لواتساب غير مفعل في V1.'],
 ['الدعم','تواصل مع الجهة المشغلة أدناه بشأن طلبك أو نطاق الخدمة. تخضع تغييرات الخدمات المتفق عليها لاتفاق الخدمة.']
 ]);
 const key=privacy?(en?'privacy_en':'privacy'):(en?'terms_en':'terms');
 const custom=c[privacy?'privacy':'terms'];
 const original=(catalog.content_original||{})[key];
 const isDraft=!String(original||'').trim();
 const body=custom?String(custom).split(/\n+/).filter(Boolean).map(line=>`<p class="policy-custom">${esc(line)}</p>`).join(''):
    sections.map(([h,p])=>`<h2>${h}</h2><p>${p}</p>`).join('');
 const warning=isDraft?`<div class="notice" role="status">${en?
    'Operational public notice. The registered operator identity and independent legal review still require verification before binding commercial use.':
    'إشعار تشغيلي: لم يُتحقق بعد من الاسم القانوني المسجل للجهة والمراجعة القانونية المستقلة قبل التعاقد التجاري الملزم.'}</div>`:'';
 publicShell(intro(en?'Wasla information':'معلومات وصلة',title,'')+warning+`<article class="card policy">${body}<hr><h2>${en?'Operating entity and contact':'الجهة المشغلة والتواصل'}</h2><p>${esc(c.legal_entity|| (en?'Legal registration is pending verification.':'بيانات التسجيل القانوني قيد التحقق.'))}${isDraft?(en?' (registration not verified)':' (لم يُتحقق من التسجيل)'):''}</p>${c.contact_email?`<a href="mailto:${esc(c.contact_email)}">${esc(c.contact_email)}</a>`:''}</article>`);
}
