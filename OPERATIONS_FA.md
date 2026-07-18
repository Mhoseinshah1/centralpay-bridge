# راهنمای بهره‌برداری (فارسی)

<div dir="rtl">

همهٔ دستورها با فرمان سراسری `centralpay` اجرا می‌شوند.

## وضعیت و لاگ‌ها

<div dir="ltr">

```bash
centralpay status          # وضعیت کانتینرها، سلامت، صف‌ها، بکاپ، دیسک
centralpay logs            # لاگ زندهٔ همهٔ سرویس‌ها
centralpay logs api        # فقط یک سرویس: api | worker | db | caddy
centralpay logs-errors     # فقط خطاها و هشدارها
centralpay diagnose        # گزارش کامل عیب‌یابی (بدون هیچ مقدار محرمانه)
centralpay version         # نسخهٔ برنامه و اجزا
```

</div>

لاگ‌ها ساخت‌یافته (JSON) هستند با فیلدهای `ts`، `level`، `logger`،
`event`، `request_id` و بسته به رویداد `payment_id`، `bot_order_id`،
`gateway_order_id`، `attempt`، `reason_code`، `http_status` و
`duration_ms`. هیچ کلید، توکن، امضا یا شمارهٔ کارت کاملی در لاگ ثبت
نمی‌شود.

## تضمین‌های ورکر اعلان (از ممیزی کد)

- هیچ وضعیتی در حافظهٔ پردازه نگه‌داری نمی‌شود؛ صف، زمان‌بندی تلاش
  مجدد و تاریخچهٔ تلاش‌ها همه در پایگاه‌داده‌اند و راه‌اندازی مجدد
  ورکر (خاموشی، کرش، ری‌استارت کانتینر) هیچ پرداختی را گم نمی‌کند.
- ادعاهای رهاشده (ورکری که وسط تلاش مرده) در هر دور با سقف تعداد
  بازیابی می‌شوند؛ در حالت `safe` به بررسی دستی می‌روند چون نتیجهٔ
  تلاش قطع‌شده نامعلوم است.
- تلاش مجدد در همهٔ مسیرها محدود است: تلاش‌های قطع‌شده هم مثل
  تلاش‌های ناموفق در سقف `BOT_NOTIFY_MAX_ATTEMPTS` حساب می‌شوند و
  رسیدن به سقف، پرداخت را با دلیل `retry_limit_reached` به بررسی دستی
  می‌برد — هیچ چیز برای همیشه تکرار نمی‌شود و هیچ چیز بی‌صدا حذف
  نمی‌شود.
- پرداختِ در بررسی دستی برای ورکر پایانی است؛ نه ورکر، نه کال‌بک و نه
  درخواست ساخت تکراری آن را بازنشانی نمی‌کنند.
- اجرای چند ورکر هم‌زمان امن است (`SKIP LOCKED`): هر پرداخت حداکثر
  توسط یک ورکر ادعا می‌شود.

## چرخهٔ سرویس

<div dir="ltr">

```bash
centralpay restart | stop | start
```

</div>

## بازبینی پرداخت‌ها

<div dir="ltr">

```bash
centralpay recent               # آخرین پرداخت‌ها
centralpay payment ORDER_ID     # یک پرداخت + تاریخچهٔ کامل حسابرسی
centralpay retry-queue          # صف اعلان به ربات با زمان تلاش بعدی
centralpay manual-review        # پرداخت‌های نیازمند بررسی مدیر
```

</div>

**معنی manual_review:** سامانه نتوانسته بدون ریسک نتیجه را تعیین کند
(مغایرت تأیید، تحویل مبهم به ربات، یا پایان سقف تلاش). پرداخت منجمد
می‌شود و تاریخچهٔ آن حفظ است؛ تصمیم نهایی با مدیر است. حالت `safe`
(پیش‌فرض) هرگز تحویل مبهم را خودکار تکرار نمی‌کند تا اعتبار دوباره واریز
نشود.

### رسیدگی به بررسی دستی (از 0.5.0-rc1)

<div dir="ltr">

```bash
centralpay review list                # موارد باز
centralpay review show ORDER_ID       # جزئیات کامل + دلیل
centralpay review acknowledge ORDER_ID --note "در حال پیگیری"
centralpay review resolve ORDER_ID --resolution RESOLUTION --note "توضیح"
```

</div>

مقادیر مجاز `RESOLUTION` (همگی غیرمالی؛ هیچ‌کدام مبلغ یا وضعیت تأیید را
تغییر نمی‌دهند): `confirmed_by_bot_operator`،
`duplicate_notification_confirmed_safe`، `bot_not_credited`،
`refund_required`، `false_positive`، `configuration_fixed`.

ارسال مجدد اعلان به ربات فقط وقتی مجاز است که حالت ربات `idempotent`
باشد و پرداخت توسط درگاه تأیید شده باشد، و هر دو پرچم صریح داده شود:

<div dir="ltr">

```bash
centralpay review resend ORDER_ID --confirm-idempotent-bot --yes
```

</div>

## به‌روزرسانی

<div dir="ltr">

```bash
centralpay update --check    # فقط نمایش نسخهٔ فعلی و هدف
centralpay update            # به‌روزرسانی با تأیید checksum
centralpay rollback          # بازگشت برنامه به نسخهٔ قبلی
```

</div>

پیش از به‌روزرسانی بکاپ می‌گیرد، مرجع تنظیم‌شده (`CENTRALPAY_UPDATE_REF`)
را دریافت می‌کند، ایمیج‌ها را می‌سازد، مهاجرت پایگاه‌داده را اجرا و
سرویس‌ها را با بررسی سلامت راه‌اندازی می‌کند.

از 0.5.0-rc1: وقتی `CENTRALPAY_UPDATE_REF` یک برچسب انتشار باشد
(پیش‌فرض)، فایل `SHA256SUMS` منتشرشده دانلود و checksum تأیید می‌شود و
در صورت عدم تطابق به‌روزرسانی متوقف می‌گردد. مرجع شاخه‌ای (مثل `main`)
حالت توسعه است و بدون تأیید checksum اجرا می‌شود.

`centralpay rollback` فقط **برنامه** را به نسخهٔ قبلی برمی‌گرداند و
هرگز اسکیمای پایگاه‌داده را پایین نمی‌آورد (مهاجرت‌ها فقط رو به جلو
هستند). پیش از بازگشت، بکاپ گرفته می‌شود و تأیید تایپی `ROLLBACK`
لازم است.

**سلامت ماشین‌خوان:** `GET /health/details` (فقط از داخل سرور؛ از طریق
Caddy منتشر نمی‌شود) نسخه، نسخهٔ مهاجرت، سن ضربان worker، طول صف‌ها و
زمان آخرین بکاپ را به‌صورت JSON برمی‌گرداند.

## بررسی یکپارچگی پایگاه‌داده

<div dir="ltr">

```bash
centralpay db-check                      # فقط‌خواندنی؛ خروجی JSON
centralpay db-check --repair-sequences   # جبران عقب‌ماندگی sequence
```

</div>

پس از هر بازیابی به‌صورت خودکار اجرا می‌شود؛ به‌صورت دستی هم برای
اطمینان از سلامت (یکتایی شناسه‌ها، رویدادهای بی‌صاحب، وضعیت‌های
نامعتبر، موقعیت sequenceها) قابل اجراست.

## مهاجرت پایگاه‌داده

<div dir="ltr">

```bash
centralpay migrate            # alembic upgrade head
centralpay migrate current    # نسخهٔ فعلی
centralpay migrate history    # تاریخچه
```

</div>

سرویس‌های api و worker هرگز پیش از موفقیت مهاجرت بالا نمی‌آیند.

## گواهی TLS

<div dir="ltr">

```bash
centralpay ssl
```

</div>

## حذف

<div dir="ltr">

```bash
centralpay uninstall
```

</div>

به‌صورت پیش‌فرض دادهٔ PostgreSQL، بکاپ‌ها و فایل‌های اعتبار حفظ می‌شوند؛
حذف هر کدام تأیید جداگانه می‌خواهد. سوابق پرداخت هرگز بی‌سروصدا حذف
نمی‌شوند.

## ربات تلگرام مدیریتی

<div dir="ltr">

```bash
centralpay admin-bot status | logs | restart | enable | disable | test-alert
```

</div>

سرویس اختیاری و فقط‌خواندنی برای دیدبانی و هشدار. راهنمای کامل
(راه‌اندازی با BotFather، مرجع دستورها و هشدارها، رفع اشکال):
[ADMIN_BOT_FA.md](ADMIN_BOT_FA.md)

## رفع اشکال سریع

- سرویس بالا نمی‌آید: `centralpay diagnose` سپس `centralpay logs-errors`
- HTTPS فعال نیست: [INSTALL_FA.md](INSTALL_FA.md) بخش SSL
- بکاپ/بازیابی: [BACKUP_RESTORE_FA.md](BACKUP_RESTORE_FA.md)
- ربات مدیریتی: [ADMIN_BOT_FA.md](ADMIN_BOT_FA.md)

</div>
