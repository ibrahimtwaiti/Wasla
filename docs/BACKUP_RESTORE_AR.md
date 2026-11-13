# نسخ PostgreSQL إلى Cloudflare R2 واستعادته — Wasla V1

تحديث 13 نوفمبر 2026. **قرار المالك: رفع نسخ PostgreSQL إلى R2 دون تشفير age أو أي تشفير يتحكم المالك بمفتاحه.** تستخدم R2 التشفير على مستوى التخزين لدى Cloudflare، ويستخدم النقل TLS/HTTPS. هذا **ليس** تشفيرًا من طرف إلى طرف: أي شخص لديه صلاحية قراءة R2 يمكنه تنزيل أرشيف قاعدة البيانات وقراءة بياناته. لا يناسب هذا القرار أي سياسة تلزم بتشفير النسخ بمفتاح مستقل.

## الحالة الحالية — أول نسخ واستعادة ناجحان ومتحقق منهما
- حاوية R2 الخاصة `wasla-production-backups` موجودة بفئة Standard، jurisdiction=default وموقع أُبلغ عنه APAC. الوصول العام r2.dev معطّل ولا توجد نطاقات عامة.
- رمزان مقيدان بالحاوية ظاهران Active في لوحة Cloudflare: `wasla-production-backup-writer` بصلاحية Object Read & Write و`wasla-production-restore-reader` بصلاحية Object Read.
- تظهر أسماء أسرار GitHub `BACKUP_DATABASE_URL` و`R2_ACCESS_KEY_ID` و`R2_SECRET_ACCESS_KEY` والمتغيرات `R2_ENDPOINT_URL` و`R2_BUCKET` و`R2_PREFIX` في بيئة `production-backup`؛ لا يمكن التحقق من قيم الأسرار من صور الشاشة.
- دُمجت حزمة إغلاق V1 عبر PR #7 في `main`. بعد تصحيح المالك مفاتيح البيئة، نجحت إعادة تشغيل GitHub Actions رقم `43426119356` (المحاولة الثانية) بتاريخ **13 نوفمبر 2026 الساعة 12:53 UTC**: الرفع والتنزيل والتحقق من SHA-256 واستعادة 19 جدولًا على PostgreSQL 17 معزولة نجحت. تحقّقنا من وجود أرشيف `.dump` بحجم **79,987 بايت** مباشرة في R2. لم تُمس قاعدة Neon الإنتاجية أو snapshot القائمة.
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
1. المهمة موجودة في `main`، وتعمل يوميًا وتدويًا عبر GitHub → Actions → `PostgreSQL database backup to R2` → Run workflow على `main`، كما تُشغّل عند تحديث ملف المهمة أو `scripts/backup_postgres.py`. ثبت نجاح التشغيل الحي رقم `43426119356`. عند تدوير المفاتيح أو تغيير المهمة أعد Run workflow يدويًا، ولا تدع نجاح CI يحل محل اختبار النسخ والاستعادة الفعلي.
2. يتحقق مسبقًا من اتصال Neon الإنتاجي المباشر، ثم يختبر R2 عبر ListObjectsV2 ورفع ملف تشخيصي صغير وفحصه وحذفه؛ يرفض أخطاء المصادقة قبل تنفيذ `pg_dump`. يثبت PostgreSQL client 17 من PGDG الرسمي، ويرفض الاتصال pooled أو غير TLS.
3. ينشئ `pg_dump` أرشيفًا مخصصًا `.dump` داخل دليل مؤقت خاص وملف mode 0600. عند فشل dump أو عدم صحة بصمة `PGDMP`، لا يبدأ الرفع. تزال الملفات المؤقتة بعد انتهاء المهمة. تبقى البيانات **غير مشفرة بمفتاح العميل** أثناء وجودها على القرص المؤقت.
4. يرفع إلى `production/postgres/wasla-<timestamp>-<uuid>.dump` بشرط `If-None-Match: *` لمنع استبدال كائن موجود. اسم الملف فريد. يرفض الأرشيف الفارغ أو الأكبر من 5 GiB (يتطلب الأكبر Multipart بدعم عدم الاستبدال قبل الاعتماد).
5. بعد الرفع، يفحص HeadObject الحجم وmetadata SHA256 ويفشل المهمة عند الاختلاف. هذا ليس بديلًا عن التنزيل والاستعادة؛ metadata وحدها ليست إثباتًا ضد تعديل مهاجم يملك صلاحيات كتابة.
6. بعد رفع الملف، تُنزّل المهمة **نفس كائن R2** تلقائيًا في Runner منفصل عن قاعدة Neon وتتحقق من SHA256 ثم تستعيده داخل حاوية PostgreSQL 17 مؤقتة؛ تفحص وجود 19 جدولًا على الأقل. لا تعلن نجاح النسخ والاستعادة إلا إذا نجحت خطوة الاستعادة كاملة. احتفظ باسم الكائن، توقيت النسخ، SHA256، ورابط التشغيل. لا ترفع dump أو سجلًا محتويًا على بيانات حساسة كـ artifact.

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
- المسؤول المعين عن النسخ والاستعادة والتنبيهات والاستجابة هو **مالك Wasla وحده** حسب قراره بتاريخ 13 نوفمبر 2026. لا يوجد مسؤول احتياطي معتمد.\n- نُشرت مهمة `Wasla operational alerts` على `main` في PR #5 لتفتح Issue مسندًا إلى المالك عند فشل النسخ، وترصد غياب نجاح أحدث من **26 ساعة** مع فحص كل 6 ساعات (هامش لتأخر GitHub Schedule). لا ترسل بريدًا مباشرة؛ **اعتمد المالك عنوان Gmail بديلًا جديدًا بدل العنوان السابق**، وأكّد المالك وصول إشعار الاختبار إلى بريده، وأُغلقت Issue #6، وتم تأكيد إخطار فشل مراقبة اصطناعي في Issue #8.
- أهداف مقترحة: RPO ≤24h وRTO ≤4h بعد قياسهما عمليًا.
- أُنجزت بوابة أول رفع حي واستعادة معزولة ناجحة، وتجربة إخطار المالك الأساسية. لا يُمنح Final Approved حتى تُعتمد مدة الاحتفاظ والضوابط الإدارية والبيانات والسياسات المؤجلة. لا تغيّر snapshots Neon أو فرع الإنتاج لهذا الاختبار.\n- اعتمادات بيانات الجهة والسوق والسياسات ومدة الاحتفاظ وضوابط الإدارة مؤجلة بطلب المالك، ولم يُقرَّر إعفاؤها من بوابة الاعتماد النهائي.
- خيار عدم تشفير age يسهّل الاستعادة، **لكنه يقلّل العزل الأمني**؛ أعد تقييمه قبل تخزين بيانات عملاء حقيقية أو معلومات حساسة.

## مراجع
- https://developers.cloudflare.com/r2/how-r2-works/
- https://developers.cloudflare.com/r2/api/tokens/
- https://developers.cloudflare.com/r2/buckets/bucket-locks/
- https://developers.cloudflare.com/r2/api/s3/api/


## سجل تاريخي: إصلاح رفض توقيع R2 عند أول تشغيل — تم حله
- فشل سابق تم حله: `R2_UPLOAD_R2_SECRET_MISMATCH` من تشغيل GitHub رقم `43396303322`؛ نجاح تصدير Neon مثبت قبله. هذا قد يكون عدم تطابق زوج مفاتيح S3 أو خللًا في توقيع الطلب، وليس إذنًا بتغيير قاعدة الإنتاج.
- افتح Cloudflare → R2 → Manage R2 API Tokens، وتحقق أن رمز الكاتب بصلاحية Object Read & Write ومقيد بحاوية `wasla-production-backups` فقط. إن فقدت Secret Access Key الأصلي، أنشئ رمز كاتب جديدًا واحتفظ **بـ Access Key ID وSecret Access Key اللذين ظهرا معًا**.
- داخل GitHub → Wasla → Settings → Environments → production-backup → Environment secrets، استبدل **كلا** السرّين `R2_ACCESS_KEY_ID` و`R2_SECRET_ACCESS_KEY` بزوج الرمز نفسه. لا تخلط رمز الاستعادة `Object Read` مع رمز النسخ. لا تنشر القيم أو الصور التي تعرضها.
- اترك `R2_ENDPOINT_URL` و`R2_BUCKET` و`R2_PREFIX` كما هي. اختبر `HeadBucket` بالاعتمادات الجديدة ثم `Run workflow` يدويًا من `main`. تحقق من `R2_S3_AUTH_CHECK_OK` ثم `R2_UPLOAD_METADATA_OK` ثم `Isolated PostgreSQL 17 restore passed`.
- لا تحذف الرموز القديمة حتى تتأكد من نجاح الإصدار البديل؛ بعدها دوّر القديم بحذر وحافظ على رمز الاستعادة المستقل.

## سجل تاريخي: تشخيص توقيع R2 — تحديث 13 نوفمبر 2026
- محاولة سابقة رقم `43396303322` وصلت `NEON_DUMP_COMPLETE` ثم فشلت عند `PutObject` برمز `R2_UPLOAD_R2_SECRET_MISMATCH`. هذا يشير إلى توقيع S3 غير متطابق؛ قد يكون بسبب عدم تطابق زوج المفاتيح **أو أسلوب توقيع/نقل PUT**، ولا يكفي وحده لإثبات أن السر خاطئ.
- يجري فحص R2 مستقل قبل تفريغ Neon: `ListObjectsV2` الموقّع، ثم `PutObject` صغير غير حساس، `HeadObject` و`DeleteObject`، ولا يحتفظ بمفاتيح في السجلات. نجاح القراءة وفشل الكتابة يوجه التحقيق إلى طريقة توقيع PUT بدل افتراض تغيير المفاتيح فورًا.
- إذا فشلت القراءة نفسها بتوقيع غير صحيح بعد تدوير المفاتيح، يجب مراجعة `R2_ACCESS_KEY_ID` و`R2_SECRET_ACCESS_KEY` من **نفس رمز الكاتب** داخل GitHub Environment `production-backup`. لا تعرض أي قيمة حساسة في المحادثة.
- النتيجة التي تحققت بالفعل: `R2_PREFLIGHT_WRITE_READ_CLEANUP_OK` ثم نجاح `pg_dump` ورفع `.dump` واستعادته إلى PostgreSQL 17 مؤقتة، عبر التشغيل `43426119356`.


## محضر أول استعادة خارجية ناجحة — 13 نوفمبر 2026

- **تشغيل GitHub موثق:** [Actions #43426119356](https://github.com/ibrahimtwaiti/Wasla/actions/runs/43426119356)، المحاولة الثانية بعد تصحيح GitHub Environment secrets.
- **اتصال R2:** `R2_PREFLIGHT_LIST_OK` ثم `R2_PREFLIGHT_WRITE_READ_CLEANUP_OK`.
- **المصدر:** Neon Production، قاعدة `wasla`، عبر PostgreSQL 17 Direct و`pg_dump --format=custom`.
- **الرفع:** `NEON_DUMP_COMPLETE` و`R2_UPLOAD_METADATA_OK`؛ أكد Cloudflare وجود كائن `.dump` واحد تحت `production/postgres/` وقت الاختبار، بحجم 79,987 بايت، ووقت الرفع `2026-11-13T12:53:45Z`.
- **التنزيل والسلامة:** `R2 object downloaded; bytes and SHA256 verified`.
- **الاستعادة المعزولة:** `Isolated PostgreSQL 17 restore passed; tables verified: 19`. جرت داخل خدمة PostgreSQL مؤقتة لا تتصل بالتطبيق الإنتاجي.
- **الحفاظ على Neon:** بقيت Snapshot `snap-divine-unit-bay7yrah` موجودة ولم تُحذف.
- **الإغلاق التشغيلي:** أُغلق بلاغ [Issue #18](https://github.com/ibrahimtwaiti/Wasla/issues/18) بعد توثيق النجاح، ولم تُمسح النسخة القديمة الموجودة في Neon.
- **بوابات لا تزال مفتوحة:** اعتماد مدة الاحتفاظ/قواعد Bucket Lock، والتأكيد على الإقامة الجغرافية إن كانت مطلوبة، وبيانات الجهة والسوق والسياسات وضوابط الإدارة التي أجّلها المالك. **مفاتيح الكاتب والقارئ ظهرت في صور المحادثة** ويجب اعتبارها مكشوفة وتدويرها قبل استخدام معلومات عملاء حساسة. مسار النسخ يعمل دون تشفير `age` بقرار المالك.
