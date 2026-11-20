> تحديث الإكمال 18 نوفمبر 2026: انظر [التدقيق الحالي](../../DEVELOPMENT_COMPLETION_AUDIT.md) و[الصلاحيات ومسارات الخدمات الجديدة](COMPLETION_OPERATIONS_AR.md). لا شراكة أو موافقات رسمية مثبتة.

# جاهزية Meta ومتطلبات تدخل المالك

آخر تحقق وثائقي: 18 نوفمبر 2026. الحساب المطور منشأ بحسب المالك؛ لا يوجد في هذا التسليم دليل على اكتمال تحقق النشاط أو App Review أو صفة شراكة.

## التصنيفات منفصلة

- حساب Meta Developer يتيح تطوير التطبيقات؛ إنشاؤه ليس اعتمادًا لتقديم الخدمة لشركات أخرى.
- Tech Provider يتعلق بتقديم خدمات عبر بيانات وأصول شركات العملاء، ويتطلب إجراءات Meta المناسبة. Access Verification لا يغني عن App Review وAdvanced Access.
- Tech Partner مسار ترقية/شراكة مستقل عن مجرد حساب المطور أو جاهزية الكود.
- Solution Partner مسار مختلف، ومنه امتياز مشاركة خط ائتمان وفق الأهلية؛ لم تنفذه وصلة ولم تفترض امتلاكه.

المصدران: [Tech Providers](https://developers.facebook.com/docs/development/release/tech-providers/) و[تصنيفات شركاء WhatsApp](https://whatsappbusiness.com/partners/become-a-partner/).

## إصدارات موثقة، لا نسخ للأمثلة القديمة

جدول [Graph API Versions](https://developers.facebook.com/docs/graph-api/changelog/versions/) الذي فُتح يعرض v26.0 وv25.0 بإتاحة حالية. تستخدم الإضافة allowlist لهذين الإصدارين، وتحتاج اختيار المدير صراحةً؛ لم نغيّر إصدارات حسابات V2 الموجودة. بعض صفحات المرجع تحمل v27.0، لذلك لا نساوي رقم عنوان المرجع بدليل توافق أو نرقّي كل الحسابات تلقائيًا.

[Embedded Signup v5](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/version-5) يغيّر session info إلى `messaging_account_ids` ويُفعّل انضمام WhatsApp Business app افتراضيًا. الإضافة تتعامل مع v5 وتستبعد coexistence صراحةً إلى أن يُنفذ مساره. لا يُستخدم sessionInfoVersion القديم. معرف WABA القديم ما زال ذا صلة بحساب المراسلة وفق [نظرة Embedded Signup](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/overview).

## ما يجب مراجعته داخل Meta

| العنصر | الحالة المثبتة الآن | المطلوب من المالك |
|---|---|---|
| Business Portfolio والكيان القانوني | غير مفحوص | تحديد النشاط الصحيح والجهة المسجلة وربط التطبيق به |
| Business Verification | غير معروف | إتمام المتطلبات الفعلية التي تظهر بالحساب وتقديم المستندات |
| Access Verification | غير معروف | استكمال التحقق للوصول إلى أصول عملاء آخرين |
| نوع التطبيق/use case | غير معروف | اختيار مسار الأعمال المناسب للمنتجات المطلوبة |
| Facebook Login for Business | الكود جاهز للإعداد | إنشاء configuration منفصل لكل من WhatsApp وFacebook Pages/Messenger وتحديد المنتجات والصلاحيات اللازمة فقط |
| App Domains وSDK domains | غير معروفة | إضافة نطاق HTTPS النهائي ونطاق اختبار معزول |
| Redirect URLs | لا عنوان إنتاج معتمد لهذه الإضافة | مطابقة origin وعناوين العودة الفعلية مع إعدادات التطبيق |
| Privacy / Terms | صفحات أولية موجودة بالمشروع | استكمال الهوية القانونية والسياسات والمراجعة النظامية وتثبيت URLs عامة |
| Data deletion | محرك الحذف وcallbacks وصفحة الحالة منفذة؛ الاختبار الحي معلق | اعتماد الإجراء ونطاق الحذف ومدته وتنفيذ callback/صفحة الحالة قبل الإطلاق |
| App Review / Advanced Access | لا دليل | طلب الصلاحيات المستخدمة وإرفاق الأدلة |
| Webhook fields | استقبال messages/status موجود | ضبط callback وverify token واشتراك الحقول بما فيها دورة حياة الحساب |
| Live mode | لم يُفعّل | الانتقال بعد الموافقات واختبارات القبول فقط |

[تعليمات التنفيذ الرسمية](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/implementation) تشرح إعداد Facebook Login for Business والنطاقات وإرجاع الكود والأصول. [إكمال onboarding لمقدم التقنية](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-customers-as-a-tech-provider) يوضح تبادل الكود خادميًا والتسجيل والاشتراك. وجود الزر وحده لا يُكمل هذه الخطوات.

لا ترسل الأسرار في المحادثة. بعد تجهيز معاينة مستقلة تُدخل بيانات التطبيق ورمز فحص التفويض في **مدير مقدم الخدمة ← مركز الربط ← تطبيقات المنصات**. مفتاح Fernet الجذري يبقى في بيئة الاستضافة الآمنة. يحتاج التطوير الحي حساب اختبار مخصصًا ورقمًا وهدف استقبال مأذونًا؛ لا نستخدم عميلًا حقيقيًا لتجربة عشوائية.

## مصفوفة الخدمات

هذه صلاحيات بحسب العمليات المذكورة، وليست طلبًا شاملًا لكل scopes. لا تُفعّل خدمة بمجرد تسجيل اسم صلاحية في DB.

| الخدمة وAPI | الوظائف المقصودة | الصلاحيات الأساسية بحسب المسار | الحساب والمراجعة | القيود | حالة وصلة وتدخل المالك |
|---|---|---|---|---|---|
| WhatsApp Cloud API | رسائل، قوالب، أرقام وWebhooks | whatsapp_business_messaging، whatsapp_business_management؛ business_management فقط إذا احتاجت عملية محددة موثقة | حساب أعمال/مراسلة ورقم مؤهل؛ مراجعة ووصول مناسب لأصول الآخرين | موافقات مراسلة، نافذة الخدمة، قوالب معتمدة، حدود الحساب وطريقة دفع Meta | V2 + onboarding جديد مختبران اصطناعيًا؛ يلزم حساب وتفويض واختبار حي |
| Instagram API with Instagram Login | معلومات احترافية ورسائل مدعومة | instagram_business_basic، instagram_business_manage_messages | حساب احترافي مؤهل ومراجعة الصلاحيات | ليس Facebook Login؛ لا تُخلط scopes المسارين؛ لا نفترض كل أنواع الوسائط | موصل V2 موجود؛ يحتاج موافقات وحساب اختبار |
| Messenger Platform | محادثات صفحة وإرسال/استقبال | pages_messaging؛ pages_manage_metadata للاشتراك؛ pages_read_engagement للعمليات التي تتطلبها؛ pages_show_list لاختيار الصفحات | صفحة ومستخدم ذو مهمة مناسبة، Page token وApp Review | سياسات ونوافذ المنتج وموافقة عمليات التسويق مستقلة | منفذ تجريبيًا للنصوص والاستقبال والتسليم؛ يلزم إعداد الصفحة والمراجعة واختبار حي |
| Pages API | اختيار صفحة وقراءة بياناتها | pages_show_list، pages_read_engagement؛ scopes إضافية فقط للوظائف المختارة | Page access token ومهام الصفحة والوصول الملائم | إدارة المنشورات تحتاج مراجعة منفصلة؛ لا وصول تلقائي لبيانات عامة | اختيار الصفحة وقراءة الملف والاشتراك والفصل منفذة تجريبيًا؛ نشر النصوص منفذ بصلاحية إضافية، وينتظر اختبارًا حيًا |
| Business Management API | إدارة الأصول | business_management وصلاحيات الأصل/العملية عند الحاجة | Business Portfolio وأدوار الأصول المناسبة | لا يخول تلقائيًا كل بيانات العملاء | دراسة فقط؛ تحديد الأصول المأذونة |
| Catalog API | استعراض/إدارة كتالوج | catalog_management بحسب العملية | كتالوج مملوك/مشارك وصلاحيات ومراجعة مناسبة | نشاط التجارة وسياسات المنتج؛ لا نفترض إدارة الطلبات | اكتشاف واختيار ومزامنة منتجات منفذة؛ يلزم كتالوج مأذون واختبار حي |
| Lead Ads | استقبال واسترجاع leads | leads_retrieval، وأذونات الصفحة/الاشتراك المطلوبة للمسار | صلاحية leads وأصول العميل ومراجعة | بيانات شخصية، موافقات واحتفاظ وCRM mapping | OAuth وleadgen واسترجاع leads والمزامنة منفذة؛ يلزم صفحة ونموذج وصلاحيات حية |
| Marketing API / Insights | تقارير إعلانات ثم إدارة اختيارية | ads_read للتقارير؛ ads_management للإدارة؛ business_management حسب عملية الأصل | حساب إعلاني ومهامه ومستوى الوصول المناسب | لا تفعيل حملات أو إنفاق تلقائي؛ حدود مستقلة | OAuth وقراءة Insights منفذان دون إدارة إعلانات؛ يلزم حساب إعلاني واختبار حي |

مراجع المصفوفة: [WhatsApp App Review](https://developers.facebook.com/docs/whatsapp/solution-providers/app-review/sample-submission/)، [Instagram Login](https://developers.facebook.com/docs/instagram/platform/instagram-api/changelog/)، [Messenger conversations](https://developers.facebook.com/docs/graph-api/reference/page/conversations/)، [Pages Webhooks](https://developers.facebook.com/docs/graph-api/webhooks/getting-started/webhooks-for-pages)، [Page reference](https://developers.facebook.com/docs/graph-api/reference/page/)، [Business asset management](https://developers.facebook.com/docs/business-management-apis/business-asset-management/overview)، [System user permissions](https://developers.facebook.com/docs/business-management-apis/system-users/install-apps-and-generate-tokens/). يجب الرجوع لعقد endpoint بعينه قبل كتابة موصل الخدمات المؤجلة.

## حزمة App Review المطلوبة

تجهيز حساب مراجعة مع بيانات اصطناعية وتعليمات دخول، وتوضيح كيف تخدم كل صلاحية عميل الشركة. تسجيل فيديو فعلي يبيّن الدخول، بدء التفويض، اختيار الأصل، العودة، ثم وظيفة الصلاحية: إدارة الأرقام/القوالب وعمليات المراسلة. تضمين خطوات إعادة الاختبار وحالات الفشل وإلغاء الربط والسياسة. لا يُقدّم فيديو mock على أنه اتصال حي ولا حساب مراجعة وهمي. لم تُسجل هذه الأدلة ولم يُرسل طلب مراجعة في هذه الجولة.

## القرارات التالية اللازمة

1. تحديد App ID وBusiness Portfolio والنطاق النهائي وحالة التحقق من داخل حسابك؛ يمكن مشاركة المعرفات والحالات غير السرية فقط.
2. تجهيز configuration ورقم وشركة اختبار، وإدخال الأسرار داخل اللوحة/بيئة المعاينة الآمنة.
3. توفير اتصال **معاينة مستقلة** وصلاحية تشغيلها، ثم ترحيلها إلى schema 10 واختبار استعادتها؛ اختبار PostgreSQL المعزول في CI نُفذ، والإنتاج لم يتغير.
4. تنفيذ التجربة الحية وتوثيق نتائجها، ثم اختبار مسار محو البيانات والتحقق من المتطلبات الرسمية قبل App Review.
5. اعتماد الهوية القانونية ونموذج الاشتراك وحدود الاستخدام لاحقًا؛ لا تحصيل مدفوعات ولا ادعاء شراكة الآن.
