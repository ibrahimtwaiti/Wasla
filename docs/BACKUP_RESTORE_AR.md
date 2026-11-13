# نسخ PostgreSQL إلى Cloudflare R2 واستعادته — Wasla V1

تحديث 13 نوفمبر 2026. **قرار المالك: رفع نسخ PostgreSQL إلى R2 دون تشفير age أو أي تشفير يتحكم المالك بمفتاحه.** تستخدم R2 التشفير على مستوى التخزين لدى Cloudflare، ويستخدم النقل TLS/HTTPS. هذا **ليس** تشفيرًا من طرف إلى طرف: أي شخص لديه صلاحية قراءة R2 يمكنه تنزيل أرشيف قاعدة البيانات وقراءة بياناته. لا يناسب هذا القرار أي سياسة تلزم بتشفير النسخ بمفتاح مستقل.

## الحالة الحالية (لا تدّعِ نجاح تشغيل حي)
- حاوية R2 الخاصة `wasla-production-backups` موجودة بفئة Standard، jurisdiction=default وموقع أُبلغ عنه APAC. الوصول العام r2.dev معطّل ولا توجد نطاقات عامة.
- رمزان مقيدان بالحاوية ظاهران Active في لوحة Cloudflare: `wasla-production-backup-writer` بصلاحية Object Read & Write و`wasla-production-restore-reader` بصلاحية Object Read.
- تظهر أسماء أسرار GitHub `BACKUP_DATABASE_URL` و`R2_ACCESS_KEY_ID` و`R2_SECRET_ACCESS_KEY` والمتغيرات `R2_ENDPOINT_URL` و`R2_BUCKET` و`R2_PREFIX` في بيئة `production-backup`؛ لا يمكن التحقق من قيم الأسرار من صور الشاشة.
- لم يُدمج PR #4 في `main` بعد. **لم تُرفع نسخة حقيقية ولم تنفذ استعادة R2 حتى الآن.** نسخة Neon الحالية والفرع الإنتاجي لم تُمس.
- لا توجد قاعدة Bucket Lock مفعّلة حتى آخر فحص. يحتاج المالك اعتماد مدة الاحتفاظ ومكان الإقامة/الولاية القضائية قبل فرض القواعد.

## البنية
Neon PostgreSQL 17 production (اتصال Direct) → `pg_dump --format=custom` → ملف مؤقت .dump بصلاحية 0600 على GitHub-hosted runner → Cloudflare R2 خاص عبر S3-compatible API/HTTPS → `HeadObject` وفحص الحجم وSHA256 metadata → استعادة منفصلة معزولة.

تستخدم مهمة النسخ `AWS CLI` كبروتوكول فقط؛ التخزين لدى Cloudflare وليس AWS. لا تحتاج GCS أو Google WIF أو age. الأرشيف غير مشفر بمفتاح المستخدم، وتحميه أثناء وجوده محليًا صلاحيات الملف والحاوية المؤقتة والتنظيف بعد انتهاء المهمة. قد يظل معرضًا لمن يحصل على وصول إلى بيئة التشغيل أو حساب Cloudflare: هذا خطر مقبول فقط بقرار المالك.

## 1. إعداد الحاوية والمفاتيح
1. bucket: `wasla-production-backups`، التخزين Standard، وصول عام معطّل دون custom domain.
2. حساب الكتابة: R2 Account API token `Object Read & Write` مقيد بالحاوية فقط. **ليس write-only**؛ لذا ينبغي حماية الاحتفاظ بـ Bucket Lock عند اعتماد السياسة.
3. حساب الاستعادة: `Object Read` مقيد بالحاوية نفسها، ومفاتيحه خارج GitHub Actions للنسخ.
4. المقترح (غير مفعل): Bucket Lock لمدة 30 يومًا على `production/postgres/`. لا تضف Lifecycle Delete قبل اعتماد سياسة الاحتفاظ. ادرس إقامة البيانات لأن default/APAC ليست ضمانًا للإقامة في بلد بعينه.
5. تجنب مشاركة أي Secret Access Key أو PostgreSQL URL في Git أو المحادثات أو السجلات. راجع صلاحية الرمز ودوّره بجدول موثق.

## 2. GitHub Environment: production-backup
قيد `Deployment branches and tags` إلى `main` فقط. لا تفرض required reviewers على مهمة مجدولة دون آلية موافقة تشغيلية.

| الاسم | النوع | الاستخدام |
| --- | --- | --- |
| BACKUP_DATABASE_URL | Secret | اتصال Neon PostgreSQL 17 مباشر مع TLS وقراءة كافة البيانات المطلوبة؛ يفضل مستخدم نسخ محدود |
| R2_ACCESS_KEY_ID | Secret | Access Key ID للرمز writer |
| R2_SECRET_ACCESS_KEY | Secret | Secret Access Key للرمز writer |
| R2_ENDPOINT_URL | Variable | https://cbd764cddd5790396878771139794161.r2.cloudflarestorage.com |
| R2_BUCKET | Variable | wasla-production-backups |
| R2_PREFIX | Variable | production/postgres |

**لا تضف `BACKUP_AGE_RECIPIENT`.** لا تستعمل مفتاح فك التشفير ولا تحتاج `age-keygen`. إذا أُضيف متغير age قديم يمكن تركه غير مستخدم مؤقتًا أو حذفه بعد التحقق من عدم استخدامه في مهام أخرى.

## 3. التشغيل والتحقق
1. بعد نجاح فحوص CI ومراجعة ودمج PR #4، شغّل GitHub → Actions → `PostgreSQL database backup to R2` → Run workflow على `main`. لا يتم تشغيل workflow من فرع PR بواسطة إعدادات job الحالية؛ الملف يجب أن يُدمج أولاً.
2. يثبت PostgreSQL client 17. يرفض الاتصالات pooled وغير TLS، ويزيل وراثة مفاتيح AWS أخرى قبل الاتصال بـR2.
3. ينشئ `pg_dump` أرشيفًا مخصصًا `.dump` داخل دليل مؤقت خاص وملف mode 0600. عند فشل dump أو عدم صحة بصمة `PGDMP`، لا يبدأ الرفع. تزال الملفات المؤقتة بعد انتهاء المهمة. تبقى البيانات **غير مشفرة بمفتاح العميل** أثناء وجودها على القرص المؤقت.
4. يرفع إلى `production/postgres/wasla-<timestamp>-<uuid>.dump` بشرط `If-None-Match: *` لمنع استبدال كائن موجود. اسم الملف فريد. يرفض الأرشيف الفارغ أو الأكبر من 5 GiB (يتطلب الأكبر Multipart بدعم عدم الاستبدال قبل الاعتماد).
5. بعد الرفع، يفحص HeadObject الحجم وmetadata SHA256 ويفشل المهمة عند الاختلاف. هذا ليس بديلًا عن التنزيل والاستعادة؛ metadata وحدها ليست إثباتًا ضد تعديل مهاجم يملك صلاحيات كتابة.
6. احفظ اسم الكائن، توقيت بدء النسخ، SHA256، ورابط تشغيل Actions. لا ترفع dump أو سجلًا محتويًا على بيانات حساسة كـ artifact.

## 4. تجربة الاستعادة الإلزامية (ليس على الإنتاج)
1. أنشئ قاعدة PostgreSQL 17 جديدة فارغة ومعزولة كليًا عن الإنتاج، دون webhooks/رسائل/مجدولات أو شبكة عامة. تحقق من وجهة الاتصال قبل أي أمر استعادة.
2. على جهاز مشغل موثوق، ضع `R2_ENDPOINT_URL` و`R2_BUCKET` و`R2_OBJECT_KEY` في متغيرات البيئة، واضبط `AWS_ACCESS_KEY_ID` و`AWS_SECRET_ACCESS_KEY` برمز **الاستعادة Object Read** فقط، و`AWS_DEFAULT_REGION=auto`. لا تستخدم ملف AWS profile مشتركًا.
3. قيّد صلاحيات المجلد المحلي قبل التنزيل، ثم:
```bash
umask 077
aws --endpoint-url "$R2_ENDPOINT_URL" --region auto s3api get-object \
  --bucket "$R2_BUCKET" --key "$R2_OBJECT_KEY" restore.dump
sha256sum restore.dump
pg_restore --list restore.dump
```
4. طابق SHA256 مع القيمة المسجلة في سجل النسخ؛ افحص مخطط الأرشيف. لا تحتاج فك تشفير age.
5. بعد ضبط بيئة اتصال الهدف المعزول فقط (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD, PGSSLMODE):
```bash
pg_restore --exit-on-error --no-owner --no-acl --dbname="$PGDATABASE" restore.dump
```
6. تحقق من المخطط والجداول والقيود وعدد السجلات وعزل الشركات وتشغيل التطبيق المعزول. لا تُنفذ رسائل أو webhooks أو Jobs خارجية. سجّل RPO وRTO وزمن الاستعادة.
7. احذف ملفات الأرشيف المؤقت من بيئة الاستعادة بعد التحقق، واحتفظ بتقرير النتائج لا بكلمة المرور أو بيانات العملاء.

## 5. الاعتماد والمتابعة
- الجدول المقترح يوميًا 00:17 UTC = 03:17 بتوقيت الرياض؛ جدولة GitHub best-effort، وقد تتأخر.
- المسؤول المعين عن النسخ والاستعادة والتنبيهات والاستجابة هو **مالك Wasla وحده** حسب قراره بتاريخ 13 نوفمبر 2026. لا يوجد مسؤول احتياطي معتمد.\n- أُضيفت مهمة `Wasla operational alerts` إلى PR #4 لتفتح Issue مسندًا إلى المالك عند فشل النسخ، وترصد غياب نجاح أحدث من **26 ساعة** مع فحص كل 6 ساعات (هامش لتأخر GitHub Schedule). لا ترسل بريدًا مباشرة؛ **اعتمد المالك عنوان Gmail بديلًا جديدًا بدل العنوان السابق**، لكن تفعيل هذا العنوان والتحقق منه في إعدادات إشعارات GitHub واختبار الاستلام فعليًا ما زال بوابة مفتوحة.
- أهداف مقترحة: RPO ≤24h وRTO ≤4h بعد قياسهما عمليًا.
- لا تغلق بوابة V1 قبل أول رفع حي واستعادة معزولة ناجحة واختبار مراقبة ووصول التنبيه إلى المالك. لا تغيّر snapshots Neon أو فرع الإنتاج لهذا الاختبار.\n- اعتمادات بيانات الجهة والسوق والسياسات ومدة الاحتفاظ وضوابط الإدارة مؤجلة بطلب المالك، ولم يُقرَّر إعفاؤها من بوابة الاعتماد النهائي.
- خيار عدم تشفير age يسهّل الاستعادة، **لكنه يقلّل العزل الأمني**؛ أعد تقييمه قبل تخزين بيانات عملاء حقيقية أو معلومات حساسة.

## مراجع
- https://developers.cloudflare.com/r2/how-r2-works/
- https://developers.cloudflare.com/r2/api/tokens/
- https://developers.cloudflare.com/r2/buckets/bucket-locks/
- https://developers.cloudflare.com/r2/api/s3/api/
