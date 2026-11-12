# النسخ المشفّر إلى Cloudflare R2 والاستعادة — Wasla V1

تحديث 13 نوفمبر 2026. اختار المالك R2 بدل GCS. الشفرة والتوثيق جاهزان؛ **لم تُنشأ حاوية أو مفاتيح ولم يُنفذ نسخ أو استعادة إنتاجية بعد**. نسخة Neon الحالية snap-divine-unit-bay7yrah لم تُحذف أو تُعدّل.

## التصميم المعتمد للتجهيز
PostgreSQL المباشر → pg_dump 17 → تشفير age → R2 خاص → تحقق metadata → تنزيل وفك تشفير واستعادة معزولة.
نستخدم AWS CLI بوصفه عميلًا لبروتوكول S3 المتوافق مع R2؛ التخزين والفوترة لدى Cloudflare، ولا يلزم حساب Amazon أو Google. لا يستخدم المسار WIF/OIDC أو مفاتيح Google.

## 1. إنشاء التخزين في لوحة Cloudflare
1. افتح R2 Object Storage في حساب تملكه، وفعّل الخدمة والفوترة المطلوبة إن لم تكن مفعلة.
2. أنشئ حاوية مخصصة باسم مثل wasla-production-backups، واختر Standard.
3. أبقِ Public Development URL / r2.dev معطلًا، ولا تربط Custom Domain عامًا. لا تحتاج CORS للنسخ من GitHub.
4. اختر Location/Jurisdiction بعد اعتماد مكان حفظ البيانات؛ location hint ليس ضمانًا لإقامة البيانات في دولة بعينها. انسخ S3 API endpoint من لوحة الحاوية بما فيه jurisdiction إن وجد.
5. اقترح Bucket Lock لمدة 30 يومًا على production/postgres/، وLifecycle لحذف النسخ بعد مدة معتمدة لا تقل عن مدة القفل. لم يُفعّل القفل أو الحذف تلقائيًا. Bucket Lock له أولوية على Lifecycle.
6. لا ننقل إعداد Versioning أو Soft Delete الخاص بـGCS إلى R2؛ كل نسخة لها اسم زمني وUUID ورفع شرطي يمنع استبدال كائن قائم. القفل هو طبقة الحماية المقترحة من الحذف أثناء الاحتفاظ.

## 2. إنشاء مفاتيح R2 بصلاحيات محددة
من R2 → Manage R2 API Tokens، أنشئ رمزًا مخصصًا لمهمة النسخ:
- Object Read & Write، مع Apply to specific buckets only واختيار حاوية النسخ فقط. لا تمنح Admin Read & Write.
- اقرأ حدود الدور: هو ليس Write-only، وقد يتيح حذف الكائنات؛ لذلك لا ندّعي منع الحذف عبر الدور وحده. استخدم Bucket Lock وحسابًا إداريًا منفصلًا وفق سياسة الاحتفاظ المعتمدة.
- انسخ Access Key ID وSecret Access Key مباشرة إلى GitHub Secrets. الـAPI Token الإداري العام ليس بديلًا لهذين الحقلين.
- أنشئ رمز استعادة مستقلًا بصلاحية Object Read فقط للحاوية نفسها؛ احتفظ به لدى المشغل ولا تضعه في مهمة النسخ.
- احتفظ بسجل مالك الرمز وتاريخ تدويره/انتهائه، واختبر البديل قبل إلغاء القديم. لا ترسل الأسرار في المحادثة.

## 3. إعداد GitHub Environment
في المستودع: Settings → Environments → production-backup. قيّد Deployment branches إلى main.

| الاسم | النوع | القيمة |
|---|---|---|
| BACKUP_DATABASE_URL | Secret | اتصال Neon مباشر مع TLS؛ حساب مخصص بصلاحيات القراءة اللازمة لجميع جداول النسخ |
| R2_ACCESS_KEY_ID | Secret | Access Key ID من رمز R2 |
| R2_SECRET_ACCESS_KEY | Secret | Secret Access Key من رمز R2 |
| BACKUP_AGE_RECIPIENT | Variable | مفتاح age العام age1… |
| R2_ENDPOINT_URL | Variable | https://ACCOUNT_ID.r2.cloudflarestorage.com أو endpoint jurisdiction المنسوخ من Cloudflare |
| R2_BUCKET | Variable | wasla-production-backups أو اسم الحاوية الفعلي |
| R2_PREFIX | Variable | production/postgres |

لا تحتاج GCP_PROJECT_ID أو WIF أو BACKUP_GCS_PREFIX. لا تحذف إعدادات مشتركة مع مشاريع أخرى. صلاحيات workflow هي contents:read فقط، ولا يحتاج id-token:write.
إذا فُرضت موافقة بشرية لكل تشغيل للبيئة، سيتوقف النسخ المجدول حتى الموافقة؛ قرر ضوابط البيئة بما يناسب التشغيل الآلي.

### مفتاح التشفير
على جهاز المشغل الموثوق: age-keygen -o wasla-backup-identity.txt.
احفظ المفتاح الخاص في مدير أسرار ونسخة استرداد مستقلة، وضع المفتاح العام فقط في BACKUP_AGE_RECIPIENT. لا تحفظ المفتاح الخاص في GitHub أو الحاوية مع النسخ. لا يعني وجود سر الاتصال أن المفتاح الخاص متاح للاستعادة تلقائيًا.

## 4. التشغيل الأول والتحقق
1. بعد نجاح CI ومراجعة ودمج الشفرة، شغّل Encrypted database backup على main يدويًا. الجدولة لا تعمل لمجرد وجود الملف على فرع PR.
2. يفحص workflow الإعدادات وتوفر AWS CLI الداعم لـIfNoneMatch، ويثبت age وpg_dump 17.
3. النسخة تمر مباشرة من pg_dump إلى age؛ لا تُكتب نسخة plaintext على القرص.
4. يُرفع ملف مشفر باسم زمني مع UUID إلى R2 عبر HTTPS. PutObject مع If-None-Match:* يرفض استبدال كائن موجود، وليس مجرد اختبار وجود سابق قابل للتسابق.
5. يفحص HeadObject حجم الملف وmetadata SHA256. أي فشل يفشل المهمة دون رسالة نجاح. هذا ليس تنزيلًا مستقلًا أو تجربة استعادة.
6. احتفظ باسم الملف ووقت النسخ وبصمته ورابط تشغيل Actions. يتطلب قياس RPO تسجيل بداية أخذ النسخة، وليس وقت انتهاء الرفع فقط.

**حد V1:** المسار يستخدم PutObject واحدًا لملفات غير فارغة حتى 5 GiB. يرفض الأكبر صراحة؛ إذا اقترب الحجم من هذا الحد نضيف multipart مع شرط عدم الاستبدال والتحقق المناسب قبل الاعتماد. لا يستخدم أعلام SSE/ACL غير المدعومة في R2؛ age يوفر تشفيرًا قبل الرفع.

## 5. اختبار الاستعادة
يستخدم المشغل رمز R2 للقراءة ومفتاح age الخاص على جهاز موثوق. جميع الأسرار تُحقن في البيئة دون طباعتها أو وضعها في arguments. لا تستخدم بيئة Preview العامة لبيانات الإنتاج.

1. أنشئ قاعدة PostgreSQL 17 فارغة ومعزولة، بلا DNS عام أو مجدولات أو تكاملات إرسال. تحقق يدويًا من هوية الهدف وأنه ليس الإنتاج.
2. افتح مجلدًا خاصًا (umask 077). اضبط AWS_ACCESS_KEY_ID وAWS_SECRET_ACCESS_KEY برمز الاستعادة، AWS_DEFAULT_REGION=auto، وR2_ENDPOINT_URL بالقيمة المعتمدة؛ أزل أي AWS_SESSION_TOKEN/Profile قديم لا يخص هذه المهمة.
3. نزّل الملف المشفر المحدد:

```bash
aws --endpoint-url "$R2_ENDPOINT_URL" --region auto s3api get-object \
  --bucket "$R2_BUCKET" --key "$R2_OBJECT_KEY" backup.dump.age
sha256sum backup.dump.age
age -d -i wasla-backup-identity.txt -o restore.dump backup.dump.age
pg_restore --list restore.dump
```

4. طابق SHA256 مع تشغيل النسخ المحفوظ. metadata وحدها ليست توقيعًا مستقلاً ضد التلاعب. نجاح فك age جزء من تحقق سلامة المحتوى.
5. اضبط PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD/PGSSLMODE للهدف المعزول. PGDATABASE اسم قاعدة وليس URL. ثم:

```bash
pg_restore --exit-on-error --no-owner --no-acl --dbname="$PGDATABASE" restore.dump
```

6. طابق المخطط والترحيلات والقيود والجداول وأعداد السجلات مع نقطة النسخ. العدد السابق 19 جدولًا مرجع تاريخي وليس ثابتًا إذا تغير المخطط. إذا استمرت الكتابة في الإنتاج، لا تقارن بعداد حالي؛ جهز بيانًا متسقًا مع snapshot النسخ أو نافذة توقف كتابة معتمدة.
7. شغّل التطبيق على القاعدة المعزولة واختبر القراءة والأدوار وعزل الشركات ورحلة قبول. لا تشغّل Webhooks أو رسائل خارجية أو مهام موروثة. تطبيق الجلسات المستعادة يجب أن يبقى ضمن البيئة المعزولة؛ تدويرها عند تعافٍ حقيقي إجراء منفصل.
8. سجّل وقت البداية والنهاية وحالة الاختبارات وأي فرق، وامسح plaintext المؤقت بعد الاختبار مع الاحتفاظ بالتقرير والنسخة المشفرة.

## 6. الاعتماد والتشغيل
- الجدول المقترح الموجود: يوميًا 00:17 UTC / 03:17 الرياض. GitHub scheduling best effort.
- عيّن مسؤولًا وتنبيهًا لفشل النسخ ولتجاوز عمر آخر نسخة ناجحة 24 ساعة؛ لا يُعتبر البريد وصل دون اختبار استلام.
- أهداف مقترحة تحتاج قياسًا: RPO ≤24 ساعة، RTO ≤4 ساعات. إعادة اختبار استعادة شهريًا وبعد تغييرات المخطط الجوهرية.
- لا يُغلق المانع قبل إثبات أول رفع وتنزيل وفك تشفير واستعادة وتشغيل معزول وتنبيه.
- نسخة Neon الحالية تبقى كما هي. لا حذف أو تدوير snapshots في هذا المسار.

## التراجع عن التطبيق
سجل آخر نشر سليم. حزمة الإغلاق لا تغير مخطط البيانات. rollback للتطبيق لا يعني استعادة قاعدة الإنتاج. لا تُجرّب الاستعادة فوق الإنتاج.

## المراجع
- https://developers.cloudflare.com/r2/get-started/cli/
- https://developers.cloudflare.com/r2/get-started/s3/
- https://developers.cloudflare.com/r2/api/s3/api/
- https://developers.cloudflare.com/r2/buckets/bucket-locks/

## الحالة الفعلية
تجهيز برمجي واختبارات محاكاة فقط. يلزم حساب Cloudflare وحاوية خاصة ومفتاحا وصول داخل GitHub ومفتاح age محفوظ. لا توجد أداة إدارة R2 متصلة في هذه الجلسة؛ لم ندّعِ إنشاء تخزين أو اختبار استعادة حي.
