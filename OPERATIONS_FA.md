# راهنمای بهره‌برداری CentralPay Bridge

<div dir="rtl">

این فایل runbook عملیاتی فعلی پروژه است. برای قوانین مهندسی/مالی به [AGENTS.md](AGENTS.md) و برای نقشهٔ مستندات به [DOCUMENTATION.md](DOCUMENTATION.md) مراجعه کنید.

## وضعیت سریع

<div dir="ltr">

```bash
centralpay status
```

</div>

خروجی وضعیت شامل containerها، health، public readiness، صف notification، شمارندهٔ `manual_review`، آخرین backup، disk و نسخهٔ برنامه است.

**نکتهٔ مهم:** شمارندهٔ `manual review` در `centralpay status` یک شمارندهٔ سطح پایین بر اساس `payments.status='manual_review'` است و می‌تواند ردیف‌های تاریخیِ resolveشده را هم در خود داشته باشد. برای فهرست واقعی موارد عملیاتیِ باز از این استفاده کنید:

<div dir="ltr">

```bash
centralpay review list
```

</div>

## لاگ‌ها

<div dir="ltr">

```bash
centralpay logs
centralpay logs api
centralpay logs worker
centralpay logs db
centralpay logs caddy
centralpay logs-errors
centralpay logs-errors worker
```

</div>

لاگ‌های برنامه structured JSON هستند. secretها، callback token/signature، full card data و full redirect URL نباید در خروجی ظاهر شوند.

Caddy باید `ct` و `sig` را هم از callback URI و هم از `Referer` درخواست‌های بعدی static asset به `REDACTED` تبدیل کند.

برای خروج از `docker compose logs -f` معمولاً `Ctrl+C` کافی است. اگر خروجی داخل pager مثل `less` باز شده باشد، کلید `q` را بزنید.

## عیب‌یابی کلی

<div dir="ltr">

```bash
centralpay diagnose
centralpay version
centralpay db-check --details --json
```

</div>

در بررسی مشکل مالی، ابتدا `centralpay payment ORDER_ID` و audit history همان پرداخت را ببینید؛ از تغییر مستقیم DB خودداری کنید.

## lifecycle سرویس

<div dir="ltr">

```bash
centralpay restart
centralpay stop
centralpay start
```

</div>

این فرمان‌ها را فقط وقتی استفاده کنید که اثرشان بر پرداخت‌های در حال انجام را می‌دانید. وضعیت مالی در PostgreSQL پایدار است؛ worker state صرفاً memory queue نیست.

## مشاهدهٔ پرداخت‌ها

<div dir="ltr">

```bash
centralpay recent
centralpay payment ORDER_ID
centralpay stuck
centralpay stuck --json
centralpay retry-queue
centralpay manual-review
```

</div>

`centralpay stuck` پرداخت‌های نیازمند توجه را دسته‌بندی می‌کند و برای بررسی عملیاتی بهتر از grep خام روی log است.

## Manual review

معنی `manual_review`: سامانه به نقطه‌ای رسیده که ادامهٔ automation بدون تصمیم انسانی می‌تواند مبهم یا خطرناک باشد.

<div dir="ltr">

```bash
centralpay review list
centralpay review list --all
centralpay review show ORDER_ID
centralpay review acknowledge ORDER_ID --note "..."
centralpay review resolve ORDER_ID --resolution RESOLUTION --note "..."
```

</div>

resolutionهای allowlistشده:

- `confirmed_by_bot_operator`
- `duplicate_notification_confirmed_safe`
- `bot_not_credited`
- `refund_required`
- `false_positive`
- `configuration_fixed`

Resolve کردن review یک تصمیم عملیاتی را ثبت می‌کند و نباید amount، fee snapshot، `gateway_verified_at` یا `reference_id` را جعل/بازنویسی کند.

### وقتی اپراتور می‌داند ربات فروش واقعاً شارژ کرده است

اگر پرداخت در `manual_review` است و اپراتور مستقل تأیید کرده ربات فروش آن order را پردازش کرده، معمولاً resolution مناسب:

<div dir="ltr">

```bash
centralpay review resolve ORDER_ID \
  --resolution confirmed_by_bot_operator \
  --note "Confirmed credited by downstream bot operator"
```

</div>

فرمان confirmation تعاملی خود CLI را دنبال کنید.

## `notification accept`

این دستور با `review resolve` فرق دارد.

فقط برای پرداختی است که هنوز `bot_notify_pending` است، gateway-verified است و اپراتور مستقل تأیید کرده ربات فروش آن را قبلاً پردازش کرده است:

<div dir="ltr">

```bash
centralpay notification accept ORDER_ID --note "..." --yes
```

</div>

این فرمان:

- هیچ HTTP به ربات فروش نمی‌فرستد
- CentralPay را صدا نمی‌زند
- amount/fee/reference/verification را تغییر نمی‌دهد
- retry خودکار همان notification را متوقف می‌کند
- audit event جداگانه ثبت می‌کند

اگر status پرداخت `manual_review` است، این دستور عمداً رد می‌شود؛ در آن حالت از `centralpay review ...` استفاده کنید.

## Resend تک‌سفارش

فقط وقتی downstream bot دریافت duplicate `order_id` را idempotent تضمین کرده و runtime در idempotent mode است:

<div dir="ltr">

```bash
centralpay review resend ORDER_ID --confirm-idempotent-bot --yes
```

</div>

این عملیات فقط برای payment gateway-verified مجاز است و نباید verification facts را تغییر دهد.

## Resend گروهی از Admin Bot

ربات مدیریتی یک مسیر gated برای requeue گروهی delivery failureهای واجد شرایط دارد:

<div dir="ltr">

```text
/resend_failed
/resend_failed confirm
```

</div>

- preview و confirm در `safe` mode رد می‌شوند
- فقط paymentهای gateway-verified واجد شرایط انتخاب می‌شوند
- فقط delivery-failure reasonهای allowlistشده مثل `retry_limit_reached` / `bot_timeout_ambiguous`
- financial mismatchها انتخاب نمی‌شوند
- عملیات مستقیم HTTP نمی‌زند؛ فقط worker queue را دوباره فعال می‌کند
- attempt history reset نمی‌شود

جزئیات: [ADMIN_BOT_FA.md](ADMIN_BOT_FA.md)

## Reconciliation

Reconciliation برای paymentهای `link_created` است که ممکن است پرداخت در CentralPay انجام شده باشد ولی browser callback به پل نرسیده باشد.

<div dir="ltr">

```bash
centralpay reconciliation status
centralpay reconciliation status --json
centralpay reconcile ORDER_ID
centralpay reconcile ORDER_ID --json
```

</div>

`reconcile ORDER_ID` به‌صورت پیش‌فرض local/read-only inspection است و gateway call نمی‌زند.

Diagnostic verify یک شبکه‌خوانی واقعی به CentralPay است و به‌صورت پیش‌فرض gated/disabled است، چون تکرار `verify` برای هر وضعیت gateway نباید بدون قرارداد روشن انجام شود.

### recovery یک aged-out payment

<div dir="ltr">

```bash
centralpay recover-aged-out ORDER_ID
centralpay recover-aged-out ORDER_ID --confirm
```

</div>

بدون `--confirm` فقط preview است. با confirm، ردیف lock و دوباره eligibility check می‌شود و در صورت واجدشرایط بودن فقط یک بار مسیر canonical `verify_and_settle()` اجرا می‌شود. این فرمان automatic reconciliation آن payment را دوباره نامحدود فعال نمی‌کند.

### لاگ‌های معمول reconciliation

این‌ها به‌تنهایی incident نیستند:

```text
centralpay_verify_not_paid
reconciliation_gateway_not_paid
reconciliation_retry_scheduled
```

این pattern معمولاً یعنی لینک هنوز پرداخت نشده و worker طبق schedule دوباره بررسی می‌کند.

مواردی مثل exhausted/aged-out/stale worker/backlog غیرعادی نیازمند بررسی‌اند.

## مدیریت کارمزد

<div dir="ltr">

```bash
centralpay fee status
centralpay fee set 10 --note "..."
centralpay fee schedule 2.5 --at 2026-08-21T00:00:00+03:30 --note "..."
centralpay fee history
centralpay fee cancel POLICY_ID --note "..."
```

</div>

- mutationهای fee host/root-only هستند
- history append-only است
- policy جدید فقط روی paymentهای جدید اثر دارد
- snapshot payment قبلی هرگز با policy جدید تغییر نمی‌کند
- `MAX_PAYMENT_AMOUNT_TOMAN` روی final payable amount اعمال می‌شود

## بکاپ و DB integrity

<div dir="ltr">

```bash
centralpay backup
centralpay backups
centralpay db-check
centralpay db-check --details --json
```

</div>

`db-check` باید منبع canonical بررسی integrity باشد. اگر `status != ok` یا `failures` خالی نیست، قبل از هر اقدام مالی علت را بررسی کنید.

`--repair-sequences` فقط برای repair sequenceهای عقب‌مانده است و با `--details` همزمان استفاده نمی‌شود.

راهنمای restore: [BACKUP_RESTORE_FA.md](BACKUP_RESTORE_FA.md)

## پایش (Monitoring)

سرویس اختیاری و جداگانهٔ پایش (`MONITOR_ENABLED`، پیش‌فرض `false`) readiness عمومی، دیتابیس، heartbeat workerها، backlog اعلان/manual-review، سلامت reconciliation، تازگی/اعتبار manifest بکاپ، فضای دیسک، یکپارچگی دیتابیس و burst شکست gateway/bot را بررسی می‌کند. incident state آن در جدول دائمی نگه‌داری می‌شود (restart آن را پاک نمی‌کند) و alertها از همان مسیر Telegram admin-bot ارسال می‌شوند.

<div dir="ltr">

```bash
centralpay monitor enable
centralpay monitor check --json
centralpay monitor incidents
centralpay monitor status
centralpay monitor logs
centralpay monitor restart
centralpay monitor disable
```

</div>

در Telegram، دستور `/monitor` همین snapshot را به‌صورت read-only نمایش می‌دهد.

اگر PostgreSQL کاملاً در دسترس نباشد: checkهای مستقل از دیتابیس (readiness، backup، disk space) همچنان کار می‌کنند و checkهای وابسته به دیتابیس به‌جای crash، نتیجهٔ `database_unavailable` برمی‌گردانند — به شرطی که container مربوط به `monitor` از قبل در حال اجرا بوده باشد (چون Compose آن را در حین outage دیتابیس (re)start نمی‌کند). ثبت دائمی خود incident «دیتابیس در دسترس نیست» و ارسال alert آن نیز به همان دیتابیس غیرقابل‌دسترس نیاز دارد و در این حالت ممکن نیست؛ برای آن پنجرهٔ زمانی به healthcheck داخلی Docker یا پایش host-level تکیه کنید. جزئیات کامل و محدودیت‌ها: [MONITORING.md](MONITORING.md).

## Update production

Production باید به release tag اشاره کند:

<div dir="ltr">

```env
CENTRALPAY_UPDATE_REF=v0.6.0-rc1
```

```bash
centralpay update --check
centralpay update
```

</div>

Updater برای release tag، artifact + `SOURCE_COMMIT` + `SHA256SUMS` را verify می‌کند و tag commit باید دقیقاً با `SOURCE_COMMIT` تأییدشده برابر باشد.

branchهایی مثل `main` به‌صورت پیش‌فرض رد می‌شوند. `CENTRALPAY_UPDATE_ALLOW_DEV_REF=true` فقط برای development است.

بعد از update:

<div dir="ltr">

```bash
centralpay status
centralpay db-check --details --json
```

</div>

اگر migration جدید اعمال شده، انتظار revision جدید را با release docs تطبیق دهید.

## Rollback

<div dir="ltr">

```bash
centralpay rollback
```

</div>

Rollback application-level است. Alembic schema خودکار downgrade نمی‌شود. اگر نسخهٔ قدیمی با schema جدید ناسازگار باشد، roll-forward یا restore بکاپ پیش از update راه امن است.

راهنما: [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md)

## Admin Bot host operations

<div dir="ltr">

```bash
centralpay admin-bot status
centralpay admin-bot logs
centralpay admin-bot restart
centralpay admin-bot enable
centralpay admin-bot disable
centralpay admin-bot test-alert
```

</div>

Admin bot جزء correctness مسیر پرداخت نیست؛ outage تلگرام نباید payment processing را block کند.

## SSL / Caddy

<div dir="ltr">

```bash
centralpay ssl
centralpay logs caddy
```

</div>

فقط Caddy پورت 80/443 را publish می‌کند. API/DB/worker/admin-bot نباید public port داشته باشند.

## بررسی Caddy secret redaction

در access log callback باید `ct` و `sig` به‌صورت `REDACTED` دیده شوند. همچنین `Referer` درخواست‌های static asset نباید secret واقعی callback را در لاگ نشان دهد.

در صورت مشاهدهٔ secret واقعی در log، آن را security incident تلقی کنید و قبل از share کردن log، secretها را redact کنید.

## صف Notification

`pending notifications` بالا یا payment قدیمی در queue می‌تواند مشکل downstream bot یا شبکه باشد. یک pending کوتاه‌مدت به‌تنهایی incident نیست.

برای جزئیات:

<div dir="ltr">

```bash
centralpay retry-queue
centralpay payment ORDER_ID
centralpay logs worker
```

</div>

## قواعد عملیاتی مهم

- برای تصمیم مالی از grep خام روی log به‌تنهایی استفاده نکنید؛ DB + audit history مرجع‌اند.
- statusهای HTTP ربات فروش را با «شارژ قطعی» یکی ندانید.
- `gateway_not_paid` معمولاً به معنی پرداخت‌نشدهٔ همان لحظه است، نه خرابی درگاه.
- manual review را با SQL دستی پاک نکنید؛ CLI audited استفاده کنید.
- migrationهای applied را ویرایش نکنید.
- روی production برای update از `git pull` به‌جای release updater استفاده نکنید.
- backup محلی را off-site DR فرض نکنید.
- secret یا callback URL کامل را در تیکت/چت paste نکنید.

</div>
