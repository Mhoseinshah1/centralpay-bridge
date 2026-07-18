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

## به‌روزرسانی

<div dir="ltr">

```bash
centralpay update
```

</div>

پیش از به‌روزرسانی بکاپ می‌گیرد، مرجع تنظیم‌شده (`CENTRALPAY_UPDATE_REF`)
را دریافت می‌کند، ایمیج‌ها را می‌سازد، مهاجرت پایگاه‌داده را اجرا و
سرویس‌ها را با بررسی سلامت راه‌اندازی می‌کند. اگر سلامت برنگردد،
دستورهای بازگشت (rollback) چاپ می‌شود. تا انتشار اولین نسخهٔ پایدار،
مرجع `main` حالت توسعه است.

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
