# راهنمای ربات تلگرام مدیریتی

<div dir="rtl">

ربات مدیریتی (`admin-bot`) یک سرویس اختیاری برای مشاهده و عملیات کنترل‌شدهٔ CentralPay Bridge است. این سرویس در مسیر correctness پرداخت مشتری قرار ندارد؛ قطع Telegram نباید ایجاد/تأیید/اعلان پرداخت را متوقف یا rollback کند.

**مدل دسترسی فعلی:** تقریباً همهٔ دستورها read-only هستند. تنها عملیات mutating تلگرام در main فعلی، `/resend_failed confirm` است که با شرط‌های سخت‌گیرانه و فقط در notification mode `idempotent` اجازهٔ requeue delivery-failureهای از قبل gateway-verified را می‌دهد.

## احراز هویت مدیر

مجوزدهی فقط بر اساس Telegram **numeric user ID** انجام می‌شود.

قواعد:

- username ملاک مجوز نیست
- فقط private chat پذیرفته می‌شود
- user ID باید در `ADMIN_TELEGRAM_IDS` باشد
- درخواست غیرمجاز پاسخ عمومی/غیرحساس می‌گیرد و audit می‌شود

پیکربندی نمونه در `/etc/centralpay-bridge/centralpay.env`:

<div dir="ltr">

```env
ADMIN_BOT_ENABLED=true
ADMIN_BOT_TOKEN=...
ADMIN_TELEGRAM_IDS=123456789,987654321
```

</div>

## عملیات host برای خود Admin Bot

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

`admin-bot` با Compose profile فعال می‌شود و public port ندارد.

## فهرست دستورهای تلگرام

دستورهای ثبت‌شده در main فعلی:

| دستور | نوع | کاربرد |
| --- | --- | --- |
| `/start` | read-only | معرفی و شروع |
| `/help` | read-only | راهنمای دستورها |
| `/status` | read-only | خلاصهٔ وضعیت عملیاتی |
| `/health` | read-only | سلامت API/DB/worker/queue |
| `/monitor` | read-only | گزارش کامل پایش سیستم (همان checkهای `app.monitor`/`centralpay monitor check`) |
| `/recent [n]` | read-only | آخرین پرداخت‌ها |
| `/stuck` | read-only | delivery failureهای نیازمند توجه |
| `/waiting [n]` | read-only | paymentهای منتظر تأیید درگاه |
| `/expired [n]` | read-only | لینک‌های منقضی/aged |
| `/manual_review` | read-only | reviewهای باز/عملیاتی |
| `/resolved_reviews [n]` | read-only | reviewهای resolveشده |
| `/errors` | read-only | خطاهای عملیاتی اخیر |
| `/payment ORDER_ID` | read-only | جزئیات یک payment و audit history |
| `/retry_queue` | read-only | صف و زمان‌بندی notification |
| `/backup_status` | read-only | وضعیت backup |
| `/version` | read-only | نسخه/مهاجرت |
| `/fee` | read-only | وضعیت fee policy |
| `/resend_failed` | preview | پیش‌نمایش requeue گروهی |
| `/resend_failed confirm` | **mutating / gated** | requeue گروهی موارد واجد شرایط |

محدودهٔ `[n]` در commandهای لیستی به‌صورت bounded اعتبارسنجی می‌شود و ورودی نامعتبر silently clamp نمی‌شود.

## `/status` و `/health`

این commandها برای دید سریع هستند، نه جایگزین `centralpay db-check` یا بررسی payment audit.

Health فعلی می‌تواند مواردی مثل این‌ها را بسنجد:

- API readiness
- database connectivity
- worker heartbeat freshness
- retry queue stall
- backup freshness از دید alert history
- stuck admin-alert delivery queue

Health monitor موجود (همین `/health`، بخشی از خود admin-bot) از consecutive-failure / recovery threshold استفاده می‌کند.

**محدودیت فعلی main:** counterهای failure/success این health monitor سبک داخلی در حافظهٔ process هستند؛ restart آن‌ها را reset می‌کند. این محدودیت مختص همین چک سبک `/health` است و همچنان برقرار است.

این محدودیت را با نبود پایش جدی اشتباه نگیرید: سرویس اختیاری و جداگانهٔ `app.monitor` (`MONITOR_ENABLED`، پیش‌فرض `false`) مجموعهٔ کامل‌تری از checkها (readiness عمومی، دیتابیس، heartbeat workerها، backlog اعلان/manual-review، سلامت reconciliation، تازگی و اعتبار manifest بکاپ، فضای دیسک، یکپارچگی دیتابیس، و burst شکست gateway/bot) را در background loop خودش اجرا می‌کند. فقط همین background loop نتیجهٔ هر check را در جدول دائمی `monitor_incidents` ثبت/به‌روزرسانی می‌کند و روی گذار open/escalate/resolve یک ردیف در outbox صف هشدارها می‌گذارد — یعنی restart این تاریخچه را پاک نمی‌کند.

دستور `/monitor` بالا و `centralpay monitor check` هر دو یک snapshot زندهٔ لحظه‌ای هستند: همان مجموعهٔ check را در لحظهٔ فراخوانی دوباره اجرا می‌کنند، اما خودشان چیزی در `monitor_incidents` نمی‌نویسند و از مسیر ثبت دائمی incident عبور نمی‌کنند. برای خواندن تاریخچهٔ دائمی incident (بدون اجرای دوبارهٔ checkها) باید از `centralpay monitor incidents` استفاده کرد — این دستور فقط ردیف‌های از قبل ثبت‌شدهٔ همان background loop را می‌خواند. جزئیات کامل در [MONITORING.md](MONITORING.md).

## `/manual_review` و `/resolved_reviews`

`/manual_review` باید برای موارد unresolved/actionable استفاده شود، نه شمارندهٔ خام تاریخی status.

Resolve کردن review از Telegram انجام نمی‌شود؛ مسیر audited host CLI:

<div dir="ltr">

```bash
centralpay review show ORDER_ID
centralpay review acknowledge ORDER_ID --note "..."
centralpay review resolve ORDER_ID --resolution RESOLUTION --note "..."
```

</div>

## `/payment ORDER_ID`

برای بررسی یک تراکنش از این command استفاده کنید تا state و audit history را ببینید. خروجی نباید secret، callback signature/token، full redirect URL، full card number یا raw external error text نشان دهد.

برای تصمیم مالی پیچیده‌تر، `centralpay payment ORDER_ID` روی host مرجع عملیاتی کامل‌تری است.

## `/fee`

`/fee` فقط وضعیت fee policy را نشان می‌دهد. تغییر fee از Telegram انجام نمی‌شود.

Mutationهای fee فقط host CLI:

<div dir="ltr">

```bash
centralpay fee status
centralpay fee set RATE --note "..."
centralpay fee schedule RATE --at ISO --note "..."
centralpay fee history
centralpay fee cancel POLICY_ID --note "..."
```

</div>

## `/resend_failed` — عملیات استثنایی و gated

این command فقط برای delivery failureهایی است که:

- payment واقعاً gateway-verified است
- status/operational review در حالت واجد شرایط است
- review هنوز unresolved است
- worker claim فعال روی آن وجود ندارد
- reason در allowlist delivery ambiguity/failure قرار دارد، از جمله `retry_limit_reached` یا `bot_timeout_ambiguous`

موارد financial mismatch / identity mismatch / invalid reference / configuration failure نباید توسط bulk resend انتخاب شوند.

### Preview

<div dir="ltr">

```text
/resend_failed
```

</div>

هیچ تغییر DB انجام نمی‌دهد و تعداد/خلاصهٔ واجدین شرایط را نشان می‌دهد.

### Confirm

<div dir="ltr">

```text
/resend_failed confirm
```

</div>

فقط وقتی `BOT_NOTIFY_RETRY_MODE=idempotent` است مجاز می‌شود. در `safe` mode هم preview و هم confirm باید با پیام ثابت رد شوند تا اپراتور به‌اشتباه تصور نکند re-delivery امن است.

این عملیات:

- CentralPay verification را جعل نمی‌کند
- amount/fee/reference/gateway-verified facts را تغییر نمی‌دهد
- مستقیماً به selling bot HTTP نمی‌زند
- فقط paymentهای واجد شرایط را برای worker requeue می‌کند
- attempt history را reset نمی‌کند
- duplicate delivery را ممکن می‌کند، بنابراین idempotent بودن downstream bot شرط اساسی است
- proof of customer balance credit نیست

انتخاب/requeue با PostgreSQL locking و batchهای bounded انجام می‌شود تا دو مدیر همزمان یک payment را دوبار requeue نکنند.

## معنی `bot_notify_accepted`

`bot_notify_accepted` یعنی downstream bot API پاسخ HTTP 2xx داده است.

این state **به‌تنهایی سند قطعی شارژ حساب مشتری نیست**. اگر برای یک incident نیاز به اثبات balance دارید، باید با خود downstream bot یا اپراتور آن مستقل بررسی شود.

## هشدارها

Admin alert outbox در PostgreSQL نگه‌داری می‌شود تا Telegram delivery از transaction مالی جدا بماند.

دسته‌های alert شامل payment/manual-review/delivery/health/backup/security operational alerts هستند. Payment-success alert می‌تواند جداگانه configurable باشد تا Telegram شلوغ نشود.

Alert creation در مسیر مالی با savepoint ایزوله می‌شود: خرابی ساخت alert نباید transaction اصلی payment را abort کند.

## مالکیت claim هشدار

هنگام delivery admin alert، نتیجهٔ HTTP/Telegram فقط وقتی روی row ثبت می‌شود که claim هنوز متعلق به همان worker/attempt باشد. نتیجهٔ دیررسیدهٔ claim قدیمی discard و audit می‌شود و نباید claim جانشین را overwrite کند.

این مکانیزم DB-result overwrite را جلوگیری می‌کند؛ Telegram delivery ذاتاً می‌تواند at-least-once باشد و در شرایط crash/timeout operational message duplicate ممکن است رخ دهد.

## داده‌هایی که هرگز نباید به Telegram بروند

- CentralPay API key
- inbound API key
- selling-bot Token
- admin bot Token
- DB password
- callback HMAC secret
- callback `ct`
- callback `sig`
- full redirect URL
- full card number
- raw gateway response/error body

Dynamic valueها باید escape شوند.

## Container security

`admin-bot`:

- public port ندارد
- فقط روی internal network است
- CentralPay keys / inbound key / callback secret / selling-bot Token را نیاز ندارد و Compose آن‌ها را mask می‌کند
- root filesystem فقط‌خواندنی دارد
- capabilityها drop شده‌اند
- `no-new-privileges` دارد
- Docker socket ندارد
- از DB و Telegram credential خودش استفاده می‌کند

## تست هشدار

<div dir="ltr">

```bash
centralpay admin-bot test-alert
```

</div>

برای validate کردن setup مفید است، اما تست alert به معنی تست end-to-end payment flow نیست.

## Troubleshooting

اگر command پاسخ نمی‌دهد:

<div dir="ltr">

```bash
centralpay admin-bot status
centralpay admin-bot logs
centralpay status
```

</div>

اگر alert نمی‌رسد ولی payment processing سالم است، ابتدا admin alert delivery را جدا از payment path بررسی کنید؛ Telegram outage نباید payment را fail کند.

اگر `/resend_failed` رد شد، قبل از تغییر config بررسی کنید downstream bot واقعاً duplicate `order_id` را idempotent پردازش می‌کند. فقط برای راحتی عملیات `safe` mode را به `idempotent` تغییر ندهید.

</div>
