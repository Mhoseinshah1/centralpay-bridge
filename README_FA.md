# پل پرداخت CentralPay

<div dir="rtl">

CentralPay Bridge یک پل پرداخت بین «درگاه سفارشی» ربات فروش و CentralPay است.
نسخهٔ فعلی برنامه **0.6.0-rc1** و head فعلی Alembic در این شاخه **0012** است.

اولویت‌های پروژه به‌ترتیب: **صحت مالی، امنیت، پایداری، قابلیت بازیابی، مشاهده‌پذیری، سپس دسترس‌پذیری**. هرجا سامانه نتواند با اطمینان مالی تصمیم بگیرد، به‌جای حدس‌زدن fail-closed می‌کند یا پرداخت را به `manual_review` می‌برد.

قرارداد فنی مرجع: [AGENTS.md](AGENTS.md)  
فهرست و وضعیت همهٔ مستندات: [DOCUMENTATION.md](DOCUMENTATION.md)

## این سامانه چه کاری انجام می‌دهد؟

1. ربات فروش `POST /api/custom-payment` را با `api_key`، مبلغ و `order_id` صدا می‌زند.
2. پل درخواست را اعتبارسنجی می‌کند، کارمزد فعال را snapshot می‌گیرد، ردیف پرداخت idempotent می‌سازد و از CentralPay لینک می‌گیرد.
3. CentralPay پرداخت‌کننده را به callback امضاشده برمی‌گرداند.
4. پل امضا و توکن یک‌بارمصرف callback را بررسی می‌کند، `verify` درگاه را صدا می‌زند و مبلغ، payer identity و `referenceId` را تطبیق می‌دهد.
5. فقط بعد از تأیید قطعی درگاه، اعلان برای ربات فروش در صف قرار می‌گیرد.
6. صف اعلان، تلاش‌های مجدد، reconciliation، بررسی دستی، هشدارهای مدیریتی و تاریخچهٔ مالی در PostgreSQL پایدار می‌مانند.

پیام نهایی به ربات فروش عمداً هیچ مبلغ یا فیلد کارمزدی ندارد؛ ربات فروش همان فاکتور اصلی خودش را شارژ می‌کند.

## مسیرهای عمومی

| متد | مسیر | کاربرد |
| --- | --- | --- |
| `POST` | `/api/custom-payment` | ساخت یا replay امن لینک پرداخت |
| `GET` | `/api/centralpay/callback` | callback امضاشده CentralPay |
| `GET` | `/health/live` | liveness |
| `GET` | `/health/ready` | readiness + بررسی واقعی DB |
| `GET` | `/static/*` | فایل‌های صفحهٔ نتیجه |

`/health/details` داخل برنامه وجود دارد اما عمداً توسط Caddy عمومی route نمی‌شود.

## معماری فعلی

<div dir="ltr">

```text
Internet
   |
   v
Caddy :80/:443
   |
   v
API :8000 --------> PostgreSQL 16
                       ^
                       |
          +------------+----+----------+
          |                 |          |
        Worker            Admin bot   Monitor (اختیاری)
   notification +         Telegram    بررسی سلامت/incident،
   reconciliation         ops         هشدار Telegram
```

</div>

فقط Caddy روی host پورت منتشر می‌کند. PostgreSQL فقط روی شبکهٔ داخلی Docker است و Caddy هیچ route مستقیمی به DB ندارد. سرویس‌های برنامه non-root، با root filesystem فقط‌خواندنی، `cap_drop: ALL` و `no-new-privileges` اجرا می‌شوند و هر سرویس فقط secretهای موردنیاز نقش خودش را می‌بیند.

ربات مدیریتی اختیاری است. تقریباً همهٔ فرمان‌های تلگرام فقط‌خواندنی‌اند. تنها عملیات mutating فعلی `/resend_failed confirm` است که شدیداً محدود شده و فقط برای پرداخت‌های از قبل تأییدشدهٔ درگاه و فقط در `BOT_NOTIFY_RETRY_MODE=idempotent` می‌تواند اعلان‌های واجدشرایط را دوباره در صف بگذارد.

سرویس پایش (`monitor`) نیز اختیاری است (`MONITOR_ENABLED=false` پیش‌فرض). checkهای آن فقط‌خواندنی هستند و هیچ‌گاه ردیف پرداخت نمی‌نویسند؛ اما incident lifecycle دائمی آن یک جدول جداگانه و غیرمالی (`monitor_incidents`) و یک ردیف alert-outbox به ازای هر گذار open/escalate/resolve می‌نویسد و از همان مسیر Telegram admin-bot ارسال می‌کند. جزئیات در بخش «پایش» پایین‌تر.

## تضمین‌های مالی مهم

- هیچ پرداختی قبل از موفقیت `verify` درگاه verified نمی‌شود.
- مبلغ گزارش‌شدهٔ درگاه باید دقیقاً با `payable_amount` snapshotشده برابر باشد.
- `userId` درگاه باید با payer identity همان پرداخت تطابق داشته باشد.
- `referenceId` قبل از ذخیره از نظر نوع/طول/کنترل‌کاراکتر بررسی می‌شود و در صورت وجود باید unique باشد.
- `bot_order_id` و `gateway_order_id` در DB unique هستند.
- callback تکراری، پرداخت already-verified را دوباره verify نمی‌کند.
- HTTP 2xx ربات فروش فقط به معنی `bot_notify_accepted` است، نه اثبات شارژ حساب مشتری.
- در حالت `safe`، نتیجهٔ مبهم ارسال هرگز خودکار دوباره ارسال نمی‌شود.
- `manual_review` با callback یا درخواست تکراری دور زده نمی‌شود.
- همهٔ transitionهای مالی در `payment_events` ماندگار ثبت می‌شوند.
- کارمزد با محاسبات integer و snapshot ثابت هر پرداخت نگه‌داری می‌شود.
- reconciliation از مسیر canonical verify/settle استفاده می‌کند و retry نامحدود ندارد.

برای جزئیات: [FINANCIAL_INVARIANTS.md](FINANCIAL_INVARIANTS.md) و [FINANCIAL_TEST_MATRIX.md](FINANCIAL_TEST_MATRIX.md).

## قرارداد ساخت پرداخت

فرمت مرجع:

<div dir="ltr">

```json
{
  "api_key": "...",
  "amount": 100000,
  "order_id": "opaque-string"
}
```

</div>

- `amount` به تومان است.
- JSON integer فرمت اصلی است.
- برای compatibility، رشتهٔ ASCII که دقیقاً با `[0-9]+` جور باشد نیز قبل از validation به integer تبدیل می‌شود.
- float، bool، عدد علامت‌دار، فاصله‌دار، جداکننده‌دار، exponent و رقم فارسی/عربی رد می‌شوند.
- `order_id` opaque است؛ trim، lower-case یا Unicode normalization روی آن انجام نمی‌شود.

برای کلاینت‌های قدیمی، `application/x-www-form-urlencoded`، `text/plain` و یک لایه JSON-string wrapper به‌صورت bounded normalize می‌شوند؛ ولی هیچ‌کدام authentication، amount rules، idempotency، fee یا gateway verification را سست نمی‌کنند.

## محدودسازی نرخ

سامانه sliding-window limiter در حافظه دارد:

- ساخت پرداخت: per-IP + global
- امضای callback نامعتبر: per-IP + global
- API key نامعتبر: global

Caddy مقدار `X-Forwarded-For` را صریحاً با peer واقعی خودش overwrite می‌کند و برنامه فقط یک IP معتبر را برای limiter identity می‌پذیرد.

Replay یک لینک موجود فقط وقتی از create limiter معاف می‌شود که واقعاً work-free باشد: status لینک ساخته‌شده، amount یکسان و payer-identity shape دقیقاً مطابق ردیف ذخیره‌شده باشد. هر حالتی که می‌تواند write، gateway call یا conflict ایجاد کند budget مصرف می‌کند.

معماری کامل: [RATE_LIMITING_ARCHITECTURE.md](RATE_LIMITING_ARCHITECTURE.md)

## نصب

سیستم‌های پشتیبانی‌شده:

- Ubuntu 22.04 / 24.04 / 26.04
- amd64 / arm64
- Docker Engine + Docker Compose plugin

<div dir="ltr">

```bash
curl -fsSL https://raw.githubusercontent.com/Mhoseinshah1/centralpay-bridge/main/install.sh | sudo bash
```

</div>

secretها خارج از git در `/etc/centralpay-bridge/` نگه‌داری می‌شوند. مسیر پیش‌فرض بکاپ `/var/backups/centralpay-bridge/` است.

راهنماها:

- [INSTALL_FA.md](INSTALL_FA.md) — نصب
- [OPERATIONS_FA.md](OPERATIONS_FA.md) — بهره‌برداری
- [BACKUP_RESTORE_FA.md](BACKUP_RESTORE_FA.md) — بکاپ/restore
- [ADMIN_BOT_FA.md](ADMIN_BOT_FA.md) — ربات مدیریتی
- [PRODUCTION_CHECKLIST_FA.md](PRODUCTION_CHECKLIST_FA.md) — چک‌لیست عملیاتی تولید

## دستورهای مهم سرور

<div dir="ltr">

```text
centralpay status
centralpay logs [api|worker|db|caddy]
centralpay logs-errors [COMPONENT]
centralpay diagnose
centralpay version

centralpay payment ORDER_ID
centralpay recent
centralpay stuck
centralpay retry-queue
centralpay manual-review

centralpay review list
centralpay review show ORDER_ID
centralpay review acknowledge ORDER_ID --note TEXT
centralpay review resolve ORDER_ID --resolution VALUE --note TEXT
centralpay review resend ORDER_ID --confirm-idempotent-bot --yes
centralpay notification accept ORDER_ID --note TEXT --yes

centralpay reconciliation status
centralpay reconcile ORDER_ID
centralpay recover-aged-out ORDER_ID

centralpay fee status
centralpay fee set RATE --note TEXT
centralpay fee schedule RATE --at ISO --note TEXT
centralpay fee history
centralpay fee cancel POLICY_ID --note TEXT

centralpay monitor enable
centralpay monitor disable
centralpay monitor check --json
centralpay monitor incidents
centralpay monitor status
centralpay monitor logs
centralpay monitor restart

centralpay backup
centralpay backups
centralpay restore FILE
centralpay db-check --details --json

centralpay update --check
centralpay update
centralpay rollback
```

</div>

## سیاست update در production

در production، `CENTRALPAY_UPDATE_REF` باید به‌طور عادی release tag معتبر مثل `v0.6.0-rc1` اشاره کند.

برای release tag، updater این موارد را بررسی می‌کند:

1. artifact نسخه
2. `SOURCE_COMMIT`
3. `SHA256SUMS`
4. تطابق commit واقعی tag با `SOURCE_COMMIT` تأییدشده

اگر تطابق برقرار نباشد، قبل از checkout/deploy/migration/restart عملیات متوقف می‌شود.

branchهایی مثل `main` به‌صورت پیش‌فرض رد می‌شوند. فقط برای محیط توسعه می‌توان صریحاً تنظیم کرد:

<div dir="ltr">

```env
CENTRALPAY_UPDATE_ALLOW_DEV_REF=true
```

</div>

این flag برای production نیست. `CENTRALPAY_UPDATE_ALLOW_UNVERIFIED=true` هم escape hatch جداگانه‌ای برای شرایط اضطراری release assets است و مسیر عادی production محسوب نمی‌شود.

## بکاپ و بازیابی

بکاپ‌ها `pg_dump --format=custom` هستند، با `pg_restore --list` اعتبارسنجی می‌شوند و manifest شامل SHA-256 دارند. restore قبل از تغییر DB فایل را بررسی می‌کند، بکاپ پیشابازیابی می‌سازد، writerها را متوقف می‌کند، با `--exit-on-error` restore می‌کند، migration و `db-check` را اجرا می‌کند و فقط بعد از سلامت کامل سرویس‌ها را بالا می‌آورد.

بکاپ روی همان سرور **Disaster Recovery کامل نیست**؛ off-site copy همچنان مسئولیت اپراتور است مگر مکانیزم جداگانه‌ای برای آن اضافه شود.

## پایش

سرویس اختیاری و جداگانهٔ پایش (`MONITOR_ENABLED=false` پیش‌فرض، جدا از worker) این موارد را بررسی می‌کند: readiness عمومی، اتصال دیتابیس، heartbeat workerها، backlog اعلان/manual-review، سلامت reconciliation، تازگی و اعتبار manifest بکاپ، فضای دیسک، یکپارچگی دیتابیس، و burst شکست gateway/bot. incident state آن دائمی است (در جدول `monitor_incidents`، restart آن را پاک نمی‌کند) و هشدارهای open/escalation/recovery با dedupe به‌ازای هر گذار دقیقاً یک‌بار صف می‌شوند و از همان مسیر Telegram admin-bot ارسال می‌شوند؛ خودِ تحویل روی Telegram at-least-once است، نه exactly-once — اگر پاسخ بعد از پذیرش پیام توسط Telegram گم شود، ممکن است پیام عملیاتی تکراری ارسال شود.

<div dir="ltr">

```bash
centralpay monitor enable            # فعال‌سازی سرویس
centralpay monitor check --json      # اجرای فوری همهٔ checkها
centralpay monitor incidents         # incidentهای باز فعلی
```

</div>

در Telegram هم دستور `/monitor` همین snapshot زنده را به‌صورت read-only نشان می‌دهد. burst شکست gateway/bot فقط شکست‌های واقعی transport/protocol را می‌شمارد؛ انصراف یا رهاسازی معمول یک پرداخت توسط کاربر هرگز آن را trigger نمی‌کند. اعتبارسنجی بکاپ فقط metadata فایل manifest را بررسی می‌کند و هیچ‌گاه کل فایل dump را hash نمی‌کند. اگر PostgreSQL کاملاً در دسترس نباشد، checkهای مستقل از دیتابیس همچنان اجرا می‌شوند و checkهای وابسته به دیتابیس به‌جای crash، `database_unavailable` برمی‌گردانند.

جزئیات کامل معماری، جدول threshold، طراحی incident lifecycle، راهنمای عملیاتی و محدودیت‌های شناخته‌شده (ازجمله رفتار در outage کامل PostgreSQL): [MONITORING.md](MONITORING.md).

## امنیت

کنترل‌های اصلی:

- HMAC + callback token یک‌بارمصرف
- compare constant-time برای secret/signature
- parsing سخت‌گیرانهٔ پاسخ درگاه
- HTTPS اجباری برای CentralPay
- اعتبارسنجی redirect/referenceId
- secret isolation بین containerها
- لاگ structured با redaction
- redaction پارامترهای `ct` و `sig` هم در URI و هم در `Referer` لاگ Caddy
- rate limiting
- row lock / unique / CHECK در PostgreSQL
- secret scan و dependency scan در CI

جزئیات: [SECURITY.md](SECURITY.md)

## وضعیت مستندات Audit قدیمی

فایل‌های audit، validation، incident و release-candidate در repo snapshot یک commit یا یک مرحلهٔ مشخص‌اند. **قرار نیست متن آن‌ها بعد از هر PR بازنویسی شود.** اگر در یک audit قدیمی نوشته شده «فلان review هنوز انجام نشده»، آن جمله فقط وضعیت همان snapshot است و نباید به‌عنوان وضعیت main امروز خوانده شود.

[DOCUMENTATION.md](DOCUMENTATION.md) دقیقاً مشخص می‌کند کدام فایل living/authoritative است و کدام فایل historical evidence.

## وضعیت نسخه

`0.6.0-rc1` هنوز pre-release است. برای تصمیم انتشار/استقرار به source فعلی، CI، `RELEASE_RISK_REGISTER.md` و چک‌لیست عملیاتی فعلی مراجعه کنید؛ نه به یک audit قدیمی.

</div>
