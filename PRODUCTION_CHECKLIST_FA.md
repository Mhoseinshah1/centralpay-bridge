# چک‌لیست Production — CentralPay Bridge 0.6.0-rc4

<div dir="rtl">

این فایل یک **چک‌لیست عملیاتی جاری** است؛ auditهای قدیمی ممکن است blockerهایی را نشان دهند که فقط مربوط به snapshot همان زمان بوده‌اند. برای تاریخچهٔ ریسک‌ها به `RELEASE_RISK_REGISTER.md` و برای دسته‌بندی مستندات به [DOCUMENTATION.md](DOCUMENTATION.md) مراجعه کنید.

نسخهٔ `0.6.0-rc4` هنوز Release Candidate است؛ قبل از هر deployment یا تغییر مهم، وضعیت واقعی source/CI/سرور را بررسی کنید.

## ۱) نسخه و update source

- [ ] `centralpay version` نسخهٔ مورد انتظار را نشان می‌دهد.
- [ ] `CENTRALPAY_UPDATE_REF` روی release tag معتبر (`vX.Y.Z` یا `vX.Y.Z-rcN`) است.
- [ ] `CENTRALPAY_UPDATE_ALLOW_DEV_REF` روی production فعال نیست.
- [ ] `CENTRALPAY_UPDATE_ALLOW_UNVERIFIED` در عملیات عادی production فعال نیست.
- [ ] پیش از update از `centralpay update --check` استفاده شده است.
- [ ] release artifact / `SOURCE_COMMIT` / `SHA256SUMS` مسیر normal update هستند.

## ۲) container و network

- [ ] `centralpay status` همهٔ سرویس‌های لازم را healthy نشان می‌دهد.
- [ ] فقط Caddy پورت‌های host را publish می‌کند.
- [ ] PostgreSQL public port ندارد.
- [ ] API public port مستقیم ندارد.
- [ ] worker public port ندارد.
- [ ] admin-bot در صورت فعال بودن public port ندارد.
- [ ] Caddy فقط روی edge network است و route مستقیم به DB ندارد.
- [ ] API روی edge + internal است؛ DB/worker/admin-bot روی internal هستند.

## ۳) TLS و دامنه

- [ ] DNS دامنه به سرور درست اشاره می‌کند.
- [ ] پورت‌های 80/443 از اینترنت قابل دسترسی‌اند.
- [ ] `centralpay ssl` خطای unresolved ندارد.
- [ ] `https://DOMAIN/health/ready` پاسخ HTTP 200 می‌دهد.
- [ ] Caddy admin API public نشده است.

## ۴) secretها و permissionها

- [ ] secret واقعی داخل git نیست.
- [ ] `/etc/centralpay-bridge/` فقط برای root قابل دسترسی است.
- [ ] فایل‌های secret/config permission محدود دارند.
- [ ] CentralPay key، inbound API key، callback secret، bot Token و DB password مقدار sample/default ندارند.
- [ ] containerها فقط secretهای لازم برای نقش خودشان را می‌گیرند.

## ۵) دیتابیس و migration

- [ ] `centralpay migrate current` revision مورد انتظار را نشان می‌دهد.
- [ ] برای main فعلی، Alembic head برابر **0012** است.
- [ ] `centralpay db-check --details --json` با `status: ok` و `failures: []` تمام می‌شود.
- [ ] هیچ sequence مهمی `behind: true` نیست.
- [ ] هیچ duplicate bot/gateway/reference ID گزارش نمی‌شود.
- [ ] هیچ payable/fee invariant failure گزارش نمی‌شود.
- [ ] هیچ orphan audit row گزارش نمی‌شود.

بعد از هر release که migration جدید دارد، عدد head این چک‌لیست را با source همان release تطبیق دهید؛ به عدد 0010 برای همیشه تکیه نکنید.

## ۶) بکاپ

- [ ] `centralpay backups` حداقل یک backup معتبر نشان می‌دهد.
- [ ] backup جدید manifest/checksum دارد.
- [ ] backup file غیرصفر و `pg_restore --list`-valid است.
- [ ] backup timer فعال است.
- [ ] فضای دیسک برای DB + backup + build کافی است.
- [ ] یک برنامهٔ **off-site** برای backup وجود دارد؛ backup روی همان host به‌تنهایی DR نیست.
- [ ] restore drill دوره‌ای روی محیط/سرور جدا انجام می‌شود.

## ۷) selling-bot notification mode

- [ ] اگر `BOT_NOTIFY_RETRY_MODE=safe` است، تیم عملیات می‌داند ambiguous delivery خودکار resend نمی‌شود.
- [ ] اگر `BOT_NOTIFY_RETRY_MODE=idempotent` است، downstream bot واقعاً duplicate `order_id` را idempotent پردازش می‌کند.
- [ ] mode فقط برای راحتی عملیات تغییر نکرده است.
- [ ] `centralpay retry-queue` backlog غیرعادی ندارد.

## ۸) Manual review

- [ ] `centralpay review list` بررسی شده است.
- [ ] reviewهای unresolved دلیل قابل فهم دارند.
- [ ] برای reviewهای resolveشده از history استفاده می‌شود و با SQL دستی status پاک نمی‌شود.
- [ ] اپراتورها تفاوت `notification accept` با `review resolve` را می‌دانند.

**یادآوری:** عدد `manual review` در `centralpay status` می‌تواند historical resolved rows را هم دربر بگیرد. برای actionable unresolved items از `centralpay review list` استفاده کنید.

## ۹) Reconciliation

- [ ] `centralpay reconciliation status` runtime/config را سالم نشان می‌دهد.
- [ ] worker heartbeat تازه است.
- [ ] backlog eligible قدیمی غیرعادی وجود ندارد.
- [ ] `reconciliation_exhausted`/aged-out موارد ناشناخته بررسی شده‌اند.
- [ ] `gateway_not_paid` معمولی به‌اشتباه outage تلقی نمی‌شود.
- [ ] `recover-aged-out` فقط برای یک payment مشخص و با preview/confirm استفاده می‌شود.

## ۱۰) Fee policy

- [ ] `centralpay fee status` rate مورد انتظار را نشان می‌دهد.
- [ ] هر تغییر fee note معنادار دارد.
- [ ] fee policy فقط روی paymentهای جدید اثر می‌گذارد.
- [ ] سقف `MAX_PAYMENT_AMOUNT_TOMAN` با final payable amount سازگار است.
- [ ] روند خرید downstream قبل از هدایت به درگاه مبلغ نهایی/کارمزد را به مشتری شفاف نشان می‌دهد (مسئولیت محصول/اپراتور).

## ۱۱) Callback و logging security

- [ ] callback URL با HMAC/token فعلی ساخته می‌شود.
- [ ] Uvicorn access logging query-bearing ناامن در production فعال نیست.
- [ ] Caddy access log مقدار واقعی `ct`/`sig` را در request URI نشان نمی‌دهد.
- [ ] Caddy access log مقدار واقعی `ct`/`sig` را داخل `Referer` static requests نشان نمی‌دهد.
- [ ] log sharing/support قبل از ارسال از نظر secret بررسی می‌شود.

## ۱۲) Admin Bot

در صورت فعال بودن:

- [ ] `centralpay admin-bot status` healthy است.
- [ ] `centralpay admin-bot test-alert` به مدیر مجاز می‌رسد.
- [ ] `ADMIN_TELEGRAM_IDS` فقط numeric IDs مجاز است.
- [ ] `/status`, `/health`, `/version`, `/fee` کار می‌کنند.
- [ ] اپراتورها می‌دانند بیشتر commandها read-only هستند اما `/resend_failed confirm` mutating/gated است.
- [ ] `/resend_failed` در safe mode عمداً رد می‌شود.

## ۱۳) اولین/نمونه پرداخت بعد از تغییر مهم

پس از نصب جدید، تغییر gateway integration، تغییر payer identity، تغییر fee یا release مهم:

- [ ] یک payment کم‌مبلغ کنترل‌شده ساخته شد.
- [ ] amount اصلی با invoice downstream bot برابر است.
- [ ] fee snapshot صحیح است.
- [ ] payable amount صفحهٔ درگاه با `amount + fee_amount` برابر است.
- [ ] `gateway_user_id` همان payer identity مورد انتظار است.
- [ ] verify reference ID معتبر ثبت شده است.
- [ ] payment فقط بعد از verify به notification flow رفته است.
- [ ] downstream bot همان order را یک بار منطقی پردازش کرده است.
- [ ] audit history payment قابل توضیح است.

## ۱۴) Rate limiting / abuse protection

- [ ] Caddy تنها مسیر public به API است.
- [ ] `X-Forwarded-For` overwrite صریح Caddy حذف نشده است.
- [ ] rate limiter enabled است مگر تصمیم عملیاتی مستندی برای خاموش‌کردن وجود داشته باشد.
- [ ] create per-IP/global limits با traffic واقعی سازگارند.
- [ ] valid signed callback به‌خاطر limiter drop نمی‌شود.

## ۱۵) Load smoke test

Production smoke test فقط controlled و غیرمخرب باشد.

- [ ] `/health/ready` تحت concurrency محدود بدون error غیرعادی پاسخ می‌دهد.
- [ ] CPU/RAM/DB تحت smoke test به saturation نمی‌رسند.
- [ ] بعد از smoke test `centralpay status` همچنان healthy است.
- [ ] برای payment/gateway load سنگین از staging/mock استفاده می‌شود؛ production محل ساخت هزاران payment مصنوعی نیست.

## ۱۶) Monitoring / alerting

main فعلی دو لایهٔ health/alert دارد:

1. health check سبک داخلی admin-bot (`/health`، `/status`) — همیشه همراه admin-bot فعال است؛ counterهای consecutive-failure/recovery آن در حافظهٔ process هستند و restart آن‌ها را reset می‌کند (محدودیت شناخته‌شده و بدون تغییر).
2. سرویس اختیاری و جداگانهٔ پایش (`app.monitor`، `MONITOR_ENABLED`، پیش‌فرض `false`) با مجموعهٔ کامل‌تری از checkها (public readiness، دیتابیس، heartbeat workerها، backlog اعلان/manual-review، سلامت reconciliation، تازگی/اعتبار manifest بکاپ، فضای دیسک، یکپارچگی دیتابیس، burst شکست gateway/bot) که incident state آن در جدول دائمی `monitor_incidents` نگه‌داری می‌شود و **restart آن را پاک نمی‌کند**. جزئیات کامل در [MONITORING.md](MONITORING.md).

- [ ] health alerts در صورت فعال بودن admin bot تست شده‌اند.
- [ ] worker heartbeat قابل مشاهده است.
- [ ] backup failure alert path شناخته شده است.
- [ ] اپراتور می‌داند لایهٔ سبک داخلی admin-bot (`/health`) همچنان process-memory است؛ برای پایش تولیدی جدی‌تر با incident lifecycle دائمی، سرویس اختیاری `app.monitor` باید با `centralpay monitor enable` فعال شود.
- [ ] در صورت فعال بودن `app.monitor`: خروجی `centralpay monitor check --json` و `centralpay monitor incidents` بررسی شده و alert Telegram آن تست شده است.

## ۱۷) Release/CI

قبل از tag/publish:

- [ ] pytest required suites سبز هستند.
- [ ] PostgreSQL integration/concurrency suite سبز است.
- [ ] Ruff سبز است.
- [ ] mypy سبز است.
- [ ] ShellCheck / `bash -n` سبز است.
- [ ] Docker build/compose validation سبز است.
- [ ] secret scan سبز است.
- [ ] dependency scan سبز است.
- [ ] release workflow برای tag موردنظر سبز است.
- [ ] release draft قبل از publish انسانی بررسی شده است.

## ۱۸) بعد از update

<div dir="ltr">

```bash
centralpay status
centralpay db-check --details --json
centralpay reconciliation status
centralpay review list
```

</div>

- [ ] public health 200 است.
- [ ] container restart loop وجود ندارد.
- [ ] migration head مورد انتظار است.
- [ ] integrity failure وجود ندارد.
- [ ] notification backlog قابل توضیح است.
- [ ] manual-review جدید ناشناخته ایجاد نشده است.
- [ ] logهای Caddy/API/worker خطای جدید سیستماتیک ندارند.

## ۱۹) قواعد اضطراری

- [ ] برای incident مالی ابتدا payment row + audit history بررسی می‌شود.
- [ ] verification fact با SQL دستی جعل نمی‌شود.
- [ ] manual review با delete/update مستقیم پاک نمی‌شود.
- [ ] restore وسط failure با روشن‌کردن اجباری writerها دور زده نمی‌شود.
- [ ] برای رفع incident از `CENTRALPAY_UPDATE_ALLOW_UNVERIFIED` بدون تحلیل ریسک استفاده نمی‌شود.
- [ ] secret واقعی داخل chat/ticket عمومی paste نمی‌شود.

</div>
