> سجل مرحلة سابقة: الحالة الحالية بعد استكمال التطوير في [تدقيق الإكمال](../../DEVELOPMENT_COMPLETION_AUDIT.md) و[دليل التشغيل الحالي](COMPLETION_OPERATIONS_AR.md). النواقص المذكورة أدناه تصف وقت التقرير ولا تمثل الحالة الحالية.

# API والتشغيل والاختبار

## المصادقة والعقود

كل `/api/v2/meta/*` يتطلب جلسة وصلة سارية وصلاحية `settings`. الكتابة تتطلب Origin مطابقًا و`X-CSRF-Token`. لا تقبل المسارات tenant_id من JSON؛ تُؤخذ الشركة من الجلسة. مخطط OpenAPI يصف أجسام CompleteSignup/RegisterPhone، ويُولد من مسارات الخادم في `/api/v2/meta/openapi.json` للمستخدم المخول. Cookie/CSRF موثقان في securitySchemes أيضًا؛ لا يُصدر مفتاح API جديدًا.

| الطريقة والمسار | المدخل/المخرج | الأثر |
|---|---|---|
| GET /api/v2/meta/readiness | وضع الاختبار، قائمة الاتصالات وحالاتها والخدمات غير المنفذة | قراءة للشركة فقط |
| POST /api/v2/meta/whatsapp/start | body فارغ؛ state وapp_id وconfig_id وapi_version وextras | ينشئ جلسة قصيرة؛ لا يخرج secret |
| POST /api/v2/meta/whatsapp/complete | state، code، business_account_id، phone_number_id | يفحص التفويض والرقم ويحفظ الرمز مشفرًا؛ لا يعلن نجاح الرسائل |
| POST /api/v2/meta/accounts/{id}/subscribe | body فارغ | طلب Meta للاشتراك، نجاح صريح فقط يحدّث الحالة |
| POST /api/v2/meta/accounts/{id}/register | pin من ستة أرقام | تسجيل رقم Cloud API قياسي؛ لا يحفظ PIN |
| GET /api/v2/meta/usage | لا معاملات | ملخص آخر 30 يومًا لعمليات onboarding فقط |
| GET /api/v2/meta/openapi.json | لا معاملات | عقد OpenAPI محمي |

المسارات السابقة أعادت استخدام `POST /api/v2/accounts/{id}/check` و`/disconnect` وإرسال `/api/v2/tickets/{id}/messages` ومزامنة القوالب واستقبال `/api/webhooks/v2/whatsapp`. الفصل محلي ولا يسحب موافقة Meta. المراسلة الجديدة لا تجتاز سياسة الإرسال قبل التسجيل والاشتراك وفحص الحساب.

الأخطاء: 401 جلسة مطلوبة؛ 403 صلاحيات/CSRF/state؛ 404 أصل خارج مساحة الشركة أو غير موجود؛ 409 ربط مغلق أو أصل غير مثبت أو تعارض ملكية؛ 422 طلب غير صالح دون إعادة عرض أسراره؛ 429 حد التشغيل؛ 502 فشل Meta برمز آمن؛ 503 إعداد أو خزنة غير جاهزة. لا تعرض استجابة خطأ Meta الخام. تستخدم الشبكة host ثابتًا وtimeout، ولا تتبع redirect أو pagination URL صادرًا من مزود؛ يُستخدم cursor على المسار المعتمد.

## إعداد المعاينة

1. قاعدة مستقلة عن الإنتاج. نسخة احتياطية قابلة للاستعادة قبل migration. تشغيل `python -m wasla.cli init` ببيئة المعاينة؛ لا ترحيل PostgreSQL أثناء HTTP request.
2. مفتاح `WASLA_TOKEN_ENCRYPTION_KEY` آمن ومتوافق مع الرموز الموجودة. لا تغييره دون إعادة تشفير واستعادة مختبرتين.
3. من لوحة المدير: META_APP_ID، META_APP_SECRET، META_SIGNUP_CONFIG_ID، META_GRAPH_VERSION (`v25.0` أو `v26.0`)، META_SYSTEM_USER_TOKEN للفحص، META_VERIFY_TOKEN، WASLA_OAUTH_ORIGIN.
4. META_SIGNUP_MODE=`test` وMETA_TEST_TENANTS قائمة معرفات شركات اختبار مفصولة بفواصل. لا تستخدم production؛ API يرفضه عمدًا حتى إغلاق إطلاق الخدمة.
5. فتح `/app/channels` بمدير الشركة: تجهيز الربط، ثم متابعة إلى Meta من نقرة مباشرة لتجنب حظر popup. لا إدخال token يدوي للعميل.
6. بعد التفويض: تسجيل الرقم القياسي واشتراك الأحداث، ثم فحص الحساب ومزامنة القوالب واختبار الوارد والصادر بأهداف مأذونة.
7. نجاح HTTP لعملية subscribe لا يثبت وصول webhook؛ نجاح إرسال accepted لا يثبت delivered/read. تحفظ الأدلة منفصلة.

## خطة تحقق حي — لم تنفذ

- قبول/رفض/إلغاء التفويض، صلاحيات جزئية، جلسة منتهية، إعادة ربط وفتح أكثر من نافذة.
- تأكيد ملكية WABA والرقم، ظهور expiry، حدود التطبيق والتوثيق.
- GET verification، توقيع صحيح/خاطئ، إعادة تسليم نفس الحدث، عامل يعمل مستقلًا.
- رسالة واردة تفتح نافذة خدمة؛ رد نصي؛ رسالة خارج النافذة مرفوضة إلا بقالب حديث مع موافقة.
- مزامنة قالب ومتابعة حالته؛ وسائط سليمة وضارة وفق الفاحص الحي عندما يُجهز.
- تعطيل الرمز وإعادة التفويض؛ محاكاة 429/timeout ومراجعة unknown دون إعادة إرسال عمياء.
- فصل محلي ثم إلغاء صلاحيات حقيقي وإثبات توقف الوصول بعد تنفيذ مساره الكامل.
- مرور PostgreSQL CI واختبار استعادة كل الجداول الجديدة وقياس أداء في بيئة مماثلة للإنتاج.

## شروط النشر

التغيير فرع تطوير فوق V2، وليس ترقية main أو Production. يلزم إكمال اختبارات CI على commit النهائي، معاينة مع schema 6، تحقق UI والمتصفحات، ثم موافقة تغييرات النشر الحساسة. لا نحذف migration أو بيانات العملاء عند التراجع؛ نوقف onboarding والإرسال ونرجع إلى إصدار متوافق مع schema أو نستعيد في قاعدة مستقلة بعد قرار مشغل مخول.

## تحديث التنفيذ — 18 نوفمبر 2026

- `POST /api/v2/meta/accounts/{id}/token-health`: يعيد فحص app/scopes/asset/expiry. الطلب غير الصالح يزيل الرمز ويحجب الرسائل المنتظرة. فشل المزود المؤقت لا يلغي التفويض. لا يفترض تجديدًا تلقائيًا لرمز WhatsApp.
- `POST /api/webhooks/meta/deauthorize`: نموذج `signed_request` موقّع HMAC-SHA256؛ ربط المستخدم بمعرف التطبيق، ومنع إعادة المعالجة. يلزم تسجيل عنوان HTTPS في إعدادات التطبيق. الربط السابق الذي لم يحفظ subject لا يمكن اكتشافه بهذا المسار قبل إعادة التفويض.
- `GET /api/v2/meta/pages`؛ `POST /pages/start`, `/pages/exchange`, `/pages/connect`: حالة مرتبطة بالجلسة والشركة لمدة عشر دقائق، تبادل الكود خلفيًا، اختيار صفحة صريح من قائمة الأصول الممنوحة. تخزين رموز الاختيار مشفر ومؤقت.
- `GET /pages/{id}/profile` و`POST /pages/{id}/subscribe`: ملف الصفحة واشتراك messages/message_deliveries. صلاحيات Facebook Login for Business: pages_show_list, pages_read_engagement, pages_manage_metadata, pages_messaging. حساب Facebook ذو مهمة MESSAGING على الصفحة.
- `POST /pages/{id}/disconnect-remote`: إلغاء اشتراك التطبيق في الصفحة ثم إزالة الرمز محليًا؛ تعاد نتيجة الإلغاء البعيد بوضوح. لا يلغي صلاحيات مستخدم Facebook الشاملة لأنها قد تخدم صفحات أخرى.
- Messenger يستخدم مسارات المحادثات والطابور القائمة؛ نصوص ضمن 24 ساعة مع `messaging_type=RESPONSE`. استقبال موقّع عبر `/api/webhooks/v2/messenger`، مع منع تكرار الرسائل وتحديث التسليم. مؤشرات read watermark والنشر على الصفحة غير منفذة.
- `GET /api/v2/meta/usage`: عمليات الربط وبوابة الرسائل/الأصول؛ ليست فاتورة Meta، ولا تشمل كل مصادر اتصال المنصة القديمة.

## الخدمات التجارية التجريبية

- `GET /api/admin/commerce`, `POST /api/admin/commerce/plans`، `PUT /api/admin/commerce/subscriptions/{tenant}`: مدير مقدم الخدمة فقط؛ أسعار بوحدات العملة الصغرى، نسخة ثابتة للخطة عند الإسناد، حالة trial/paused.
- `GET /api/v2/billing`, `POST /api/v2/billing/invoice`: مدير الشركة فقط؛ مسودة غير قابلة للدفع وغير ضريبية، واحدة لكل شهر UTC، بلا رسوم Meta.
- حصة outbound_messages تحجز داخل معاملة إنشاء الرسالة. إعادة نفس Idempotency-Key لا تحجز مرتين. لا يُعاد احتساب المحاولات. لا تخصم الملاحظات الداخلية. التراجع عن المعاملة يعيد الحصة. الرسائل المقبولة في الطابور تستهلك الحصة ولو فشلت لاحقًا؛ هذا حد تشغيل وليس رسمًا ماليًا.
- العملاء الحاليون دون اشتراك يحتفظون بسلوكهم الحالي؛ يجب إسناد خطة لتطبيق حدودها. حدود المقاعد/الحسابات ومحاسبة التكلفة الفعلية غير منفذة.
- `POST /api/auth/register`: مغلق افتراضيًا؛ إعداد WASLA_SELF_SIGNUP_MODE=test يتيح طلبًا مع حساب معطل حتى المراجعة. `POST /api/admin/commerce/registrations/{tenant}/approve` يتطلب مرجع تحقق خارجي وإسناد خطة. لا يوجد إرسال بريد تحقق تلقائي.
- مسارات الواجهة: `/signup`, `/app/billing`, `/app/commerce`. جميع عمليات الإدارة تستخدم جلسة وCSRF.

ترحيلات 008–010 إضافية، تتطلب `python -m wasla.cli init` في بيئة اختبار أولًا. لا تنفذ أثناء الطلبات على PostgreSQL. يعاد بناء قيد قنوات SQLite داخل معاملة، مع فحص سلامة العلاقات والحفاظ على البيانات. لا تغيير لبيانات الإنتاج في هذا العمل.

المراجع الرسمية: https://developers.facebook.com/documentation/business-messaging/messenger-platform/send-messages وhttps://developers.facebook.com/docs/pages-api/webhooks-for-pages/ وhttps://developers.facebook.com/documentation/facebook-login/guides/advanced/manual-flow/ .
