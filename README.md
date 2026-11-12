# Wasla | وصلة — V1

منصة عربية لتأسيس تشغيل المبيعات: موقع الخدمات، طلبات العروض، مشروعات التنفيذ، مساحات الشركات والعملاء والفرص والمهام. مبنية من الصفر وفق خريطة البناء المرفقة بتاريخ 12 نوفمبر 2026.

**الحالة: إصدار تأسيسي قابل للتشغيل والاختبار. ليس الإطلاق الكامل للمنصة المذكورة في الوثيقة.** لا توجد بيانات أو كلمات مرور افتراضية. يجري تجهيز إصدار Vercel Serverless مع PostgreSQL مستقلة؛ لا يوجد اعتماد إنتاجي أو ربط حقيقي بواتساب حتى الآن.

## ما يعمل الآن

- موقع RTL متجاوب وكتالوج الخدمات الإحدى عشرة وصفحة تفصيل لكل خدمة، طلب عرض وصفحات المنصة وطريقة العمل والأسئلة والسياسات الأولية.
- حفظ الطلب والتحقق منه ورقم مرجعي وموافقتان منفصلتان ومنع التكرار وتنبيه داخلي وفرصة لمقدم الخدمة.
- إدارة الطلب والعرض ومرجع التحصيل، وإنشاء مشروع واحد تلقائيًا عند القبول.
- مشروعات بمراحل ومخرجات نصية وملاحظات ومراجعة واعتماد وربط اختياري بشركة.
- شركات مستقلة وسبعة أدوار وإدارة فريق وجلسات دخول آمنة وسجل تدقيق وتعليق المساحة.
- CRM: جهات اتصال ووسوم وموافقات وعمليات انسحاب وتصدير CSV مقيد؛ فرص بمراحل وقيم وعملات ومالك ومتابعة؛ مهام شخصية.
- صندوق محادثات للاستقبال النصي عبر Webhook موقع: تخزين دائم قبل الإقرار، عامل معالجة، منع التكرار، إسناد وإغلاق وإعادة فتح، وملاحظات داخلية.
- تقارير تراكمية تفصل العملات وقيم الفرص عن الإيراد. مراقبة حالات أحداث الاستقبال وإعادة معالجة الأخطاء المستنفدة.
- إعدادات محتوى أساسية، ملفات Docker، ترحيل قاعدة البيانات، نسخ احتياطي، اختبارات قبول وGitHub Actions.

## الحدود الصريحة

**الإرسال الخارجي لواتساب غير منفذ في V1**؛ endpoint يرفضه ولا يسجل نجاحًا وهميًا. الاستقبال منفذ ومختبر بأحداث اصطناعية، لكنه يحتاج حساب Meta وإعدادًا واختبارًا حقيقيًا. القناة تظل `unverified`.

المساعد الذكي، محرر البوت، القوالب والحملات، الدفع والفواتير والاشتراكات التفصيلية، الدعوات البريدية، MFA، رفع المرفقات/المخرجات، استيراد CSV ودمج العملاء وتكاملات سلة وزد وCRM غير مشمولة في V1. واجهة الإنجليزية غير منفذة. راجع [مصفوفة النطاق](docs/V1_SCOPE_AR.md) قبل تقييم المطابقة للوثيقة الكاملة.

## تشغيل سريع — Python 3.12+

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m wasla.cli init
python -m wasla.cli bootstrap
python -m uvicorn wasla.main:app --host 127.0.0.1 --port 8000
```

افتح `http://localhost:8000`؛ الدخول في `/login`. يطلب bootstrap بيانات المدير تفاعليًا دون كتابتها في ملفات أو arguments. يحتاج النموذج حساب مقدم الخدمة النشط ليقبل الطلبات.

في نافذة أخرى ومع نفس متغير `WASLA_DB`:

```bash
python -m wasla.worker
```

للتطوير عبر أدوات المعاينة: `npm run dev` (لا توجد اعتماديات npm؛ ينفذ Python). هذا الأمر يعمل افتراضيًا على `127.0.0.1:8000`؛ إذا استخدمت `127.0.0.1` بدل `localhost` عيّن `WASLA_ORIGIN=http://127.0.0.1:8000` قبل التشغيل. مقارنة Origin مقصودة لحماية الطلبات.

## تشغيل Docker

```bash
cp .env.example .env
# عدّل البيئة والنطاق في .env قبل الإتاحة العامة
# بناء وتشغيل الخادم والعامل على وحدة تخزين دائمة
docker compose up --build -d
docker compose exec web python -m wasla.cli bootstrap
```

المنفذ مكشوف على localhost فقط. ضع TLS reverse proxy أمامه، وحدد `WASLA_ENV=production` و`WASLA_ORIGIN=https://your-actual-domain` للاستخدام خارج الجهاز. عند النشر على Vercel استخدم PostgreSQL على Neon؛ ملفات SQLite المحلية لا تصلح على القرص المؤقت. تعليمات Docker خاصة بخادم ذي قرص دائم. راجع [دليل التشغيل](docs/OPERATIONS_AR.md).

## الاختبارات

```bash
python -m unittest discover -s tests -v
python -m compileall -q wasla scripts
node --check wasla/static/app.js
```

كل اختبار يستخدم قاعدة مؤقتة مع بيانات اصطناعية. لا يرسل رسائل أو بريدًا ولا يتصل بمزود. [نتائج التحقق وحدودها](docs/VERIFICATION_AR.md).

## التوثيق

- [نطاق V1 ومطابقة الوثيقة](docs/V1_SCOPE_AR.md)
- [البنية التقنية والقرارات](docs/ARCHITECTURE_AR.md)
- [دليل المدير والموظف](docs/USER_GUIDE_AR.md)
- [النشر والنسخ الاحتياطي والتراجع](docs/OPERATIONS_AR.md)
- [الهوية البصرية القادمة](docs/BRANDING_AR.md)
- [تحليل النتائج](docs/RESULTS_AR.md)

لا يستخدم المشروع اسم المرجع أو شعاراته أو شهاداته أو إحصاءاته. الشعار النصي والألوان الحالية مؤقتة وقابلة للاستبدال. لا تخزن أسرارًا أو بيانات حقيقية داخل Git.


## Vercel + Neon deployment

- Deployment entrypoint: index.py, Python 3.12, FastAPI.
- Runtime DATABASE_URL is the **Neon pooled** connection; use DATABASE_URL_UNPOOLED only in an operator terminal for migrations (python -m wasla.cli init).
- Separate Neon production and preview branches; never share customer data with previews.
- Set WASLA_ENV=production, WASLA_ORIGIN=https://your-domain and encrypted WASLA_WORKER_SECRET (at least 32 characters). Vercel uses the request host for same-origin checks on its own domains.
- For the initial admin, execute python -m wasla.cli bootstrap from a trusted operator terminal with a direct database connection. The email and password are interactive, not API parameters. There are no default credentials or public setup endpoints.
- WhatsApp events are persisted before acknowledgment. A Neon scheduled Function must invoke POST /api/internal/drain-webhooks with Authorization: Bearer <secret> every minute. Processing uses PostgreSQL row locks, retries with backoff, and a dead-letter status after five failures. Neither WhatsApp sending nor delivery verification is implemented in V1.
- Verify SQLite and PostgreSQL CI acceptance, preview HTTP smoke tests and tenant-boundary checks before production promotion.
- This repository contains no real SQLite database to migrate: the schema is migrated, not customer data. Importing customer data requires an authorized SQLite source backup and a separate controlled process.
