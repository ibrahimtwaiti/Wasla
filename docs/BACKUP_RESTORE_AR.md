# النسخ المشفّر والاستعادة إلى Google Cloud Storage — Wasla V1

تحديث: 13 نوفمبر 2026. اختار المالك GCS بدل Amazon S3. الشفرة معدلة، ولا يوجد ادعاء بأن الحاوية أو WIF أو أول نسخة قد شُغلت.

## الحالة الفعلية
المسار البرمجي جاهز للمراجعة والتشغيل، لكنه **غير مفعّل على بيانات الإنتاج** حتى توفير الوجهة والأسرار والتحقق من استعادة ملفها. لا تعني إضافة workflow أن النسخ تم. Neon يحتفظ حاليًا بتاريخ 6 ساعات؛ هذا ليس نسخة يومية خارجية. محاولة إنشاء snapshot بتاريخ 12 نوفمبر 2026 رُفضت بسبب حد النسخ، وجدولة Neon رُفضت لأنها غير مفعلة للمشروع. لم تُحذف النسخة القديمة.

## الإعداد المطلوب مرة واحدة

### أ. مشروع Google Cloud والحاوية
1. استخدم مشروعًا تملكه مع Billing مفعّل. يحدد المالك Project ID ومنطقة التخزين؛ لا نفترض السوق أو ننشئ موارد مدفوعة قبل تحديد المشروع.
2. أنشئ حاوية مخصصة للنسخ، مثال اسم فقط: `wasla-backups-<unique-id>`، وفئة Standard كبداية.
3. فعّل Uniform bucket-level access وPublic access prevention. لا تمنح allUsers أو allAuthenticatedUsers أي دور.
4. فعّل Object Versioning، واعتمد Lifecycle للنسخ الحالية وغير الحالية وسياسة Soft Delete معًا. الاحتفاظ 30 يومًا اقتراح ينتظر الموافقة؛ الاحتفاظ بالإصدارات وSoft Delete يؤثر في المدة والتكلفة الفعلية.
5. التشفير قبل الرفع بـage؛ وتشفير Google الافتراضي في التخزين طبقة إضافية. لا نقفل Retention Policy قفلًا غير قابل للعكس أثناء التجهيز.

### ب. هوية النسخ وWIF
- أنشئ Service Account مخصصًا مثل `wasla-backup-writer`.
- امنحه `roles/storage.objectCreator` على الحاوية المخصصة فقط. هذا المسار ينشئ ملفات فريدة ويستخدم `--if-generation-match=0`؛ لا يحتاج حذف النسخ. إذا ظهرت مشكلة صلاحية، شخّص الإذن المحدد ولا تمنحه Storage Admin.
- حساب الاستعادة مستقل بصلاحية `roles/storage.objectViewer`؛ لا تمنح مهمة النسخ مفتاح فك التشفير.
- فعّل IAM وIAM Service Account Credentials وSecurity Token Service وCloud Storage APIs.
- أنشئ Workload Identity Pool وOIDC Provider للمصدر `https://token.actions.githubusercontent.com`.
- استخدم `google.subject=assertion.sub` و`attribute.repository_id=assertion.repository_id`.
- قيّد Provider بالشرط التالي، مع مراجعة IDs إذا تغير المستودع أو مالكه:

```text
assertion.repository_owner_id == '139933002' &&
assertion.repository_id == '1455204015' &&
assertion.ref == 'refs/heads/main' &&
assertion.environment == 'production-backup' &&
assertion.workflow_ref == 'ibrahimtwaiti/Wasla/.github/workflows/backup.yml@refs/heads/main'
```

- اربط principalSet الخاص بهذا pool وattribute.repository_id=1455204015 بدور `roles/iam.workloadIdentityUser` على حساب الخدمة فقط. لا تستخدم جميع هويات pool دون شرط.
- بيانات مزود WIF تستخدم **Project Number** في المسار؛ لا تخلطه مع Project ID.
- لا تنشئ Service Account JSON key. google-github-actions/auth يولد ملف اعتماد مؤقتًا، وهو مستبعد من git ويُحذف في خطوة تنظيف action.

### ج. بيئة GitHub production-backup
قيّد Deployment branches بـmain. ضع القيم التالية من Settings → Environments → production-backup:

| الاسم | النوع | القيمة |
|---|---|---|
| BACKUP_DATABASE_URL | Secret | اتصال Neon مباشر وليس pooler، TLS، لحساب القراءة اللازم للنسخ |
| BACKUP_AGE_RECIPIENT | Variable | المفتاح العام age1… |
| BACKUP_GCS_PREFIX | Variable | gs://BUCKET/production/postgres |
| GCP_PROJECT_ID | Variable | معرف مشروع Google Cloud |
| GCP_WORKLOAD_IDENTITY_PROVIDER | Variable | projects/NUMBER/locations/global/workloadIdentityPools/POOL/providers/PROVIDER |
| GCP_BACKUP_SERVICE_ACCOUNT | Variable | wasla-backup-writer@PROJECT_ID.iam.gserviceaccount.com |

على جهاز المشغل الموثوق: `age-keygen -o wasla-backup-identity.txt`. احفظ المفتاح الخاص في مدير أسرار مع نسخة استرداد منفصلة؛ ضع المفتاح العام فقط في GitHub.
الأسرار السابقة الخاصة بـAWS غير مستخدمة في هذا المسار؛ لا تحذف أي أسرار مستخدمة في مشاريع أخرى.
عند فرض موافقة بشرية لكل تشغيل للبيئة، سيتوقف الجدول حتى الموافقة؛ اضبط هذا القرار بما يناسب النسخ الآلي.

### د. التشغيل الأول
1. بعد مراجعة CI ودمج الشفرة: شغّل Encrypted database backup يدويًا على main.
2. يتحقق workflow من الإعدادات، ويثبت pg_dump 17 وage، ويحصل على اعتماد Google مؤقت، ثم يشفر ويرفع.
3. لا تُرفع نسخة غير مشفرة، ولا يعلن نجاح الرفع عند فشل dump أو age أو gcloud.
4. يسجّل اسم الملف وبصمة SHA-256 للملف المشفر، وتُرفق البصمة بmetadata الكائن.
5. تحقق بحساب الاستعادة من الملف ووقت إنشائه وgeneration وبصمته. البصمة تتحقق من سلامة الملف وليست توقيعًا مستقلًا.
6. نفّذ تجربة الاستعادة التالية قبل إغلاق المانع. الجدول 00:17 UTC / 03:17 الرياض، ويعمل بعد وجود workflow على الفرع الافتراضي. راقب عمر آخر نسخة ناجحة؛ GitHub scheduling ليس ضمانًا زمنيًا.

## تجربة الاستعادة
- اختر نسخة حديثة، وسجّل وقت البداية ومعرف الملف ووقت أخذ النسخة.
- أنشئ **قاعدة فارغة منفصلة** للاستعادة، بلا domain أو مجدول إرسال. لا تستعد فوق الإنتاج.
- نزّل الملف المشفر من GCS بحساب الاستعادة باستخدام `gcloud storage cp gs://BUCKET/production/postgres/FILE.dump.age backup.dump.age` إلى جهاز موثوق، وطابق `sha256sum backup.dump.age` مع البصمة المسجلة؛ فكّه بـ`age -d -i wasla-backup-identity.txt -o restore.dump backup.dump.age` مع مجلد خاص وصلاحية 0600.
- اضبط متغيرات libpq للقاعدة المعزولة دون وضع كلمة المرور في arguments. نفّذ `pg_restore --exit-on-error --no-owner --no-acl --dbname="$PGDATABASE" restore.dump` بعد التأكد أن PGDATABASE اسم القاعدة التجريبية وليس connection URL.
- طابق عدد الجداول (19 حاليًا)، schema_migrations، وأعداد الشركات والمستخدمين والطلبات والعملاء والفرص والرسائل مع بيان النسخة وقت التقاطها. تحقق من القيود وعينة تقارير ومشروع، ثم شغّل نسخة التطبيق في بيئة معزولة على القاعدة المستعادة.
- إلغاء الجلسات القديمة عند التحويل الفعلي إجراء تشغيلي منفصل يحتاج تسجيلًا؛ لا تعِد تفعيل أي مجدول موروث أو تكامل خارجي في الاختبار.
- سجّل وقت الوصول للخدمة ومقدار فقد البيانات. الهدف المقترح من الوثيقة: RPO ≤24 ساعة وRTO ≤4 ساعات. لا يوجد قياس إنتاجي حديث يثبت تحقيقهما الآن.
- احذف plaintext من منطقة العمل المؤقتة بعد التحقق واحتفظ بالنسخة المشفرة وبيان النتيجة في مساحة تشغيل خاصة.

## التراجع عن التطبيق
احتفظ بمعرف آخر نشر سليم. تغيير V1 الحالي لا يضيف ترحيل بيانات؛ لذلك يمكن إعادة نشر إصدار التطبيق السابق على المخطط نفسه. لا تخلط rollback للتطبيق مع استعادة قاعدة البيانات. عند حادث: أوقف التغييرات التشغيلية، تحقق من النسخ، ارجع النشر السليم من Vercel، شغّل smoke، ثم وثق الأثر. الاستعادة فوق الإنتاج لا تنفذ كتجربة.

## مراجع الإعداد
- https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines
- https://github.com/google-github-actions/auth
- https://docs.cloud.google.com/sdk/gcloud/reference/storage/cp

## حدود التحقق الحالي
اختبارات البرنامج محاكية وليست إثبات رفع إلى GCS أو استعادة من ملف إنتاج. لم نُنشئ bucket أو WIF، ولم نغيّر Neon. يلزم Project ID وموقع الحاوية، ثم صلاحية إعداد Google Cloud وإدخال الأسرار داخل GitHub. لا تُرسل كلمة مرور قاعدة البيانات أو مفتاح age الخاص في المحادثة.

