# راهنمای نصب (فارسی)

<div dir="rtl">

## پیش‌نیازها

- سرور اوبونتو ۲۲.۰۴، ۲۴.۰۴ یا ۲۶.۰۴ (معماری amd64 یا arm64)
- حداقل ۱ گیگابایت رم (۲ گیگابایت پیشنهاد می‌شود) و ۵ گیگابایت فضای آزاد
- دسترسی root
- یک دامنه برای درگاه پرداخت (مثلاً `pay.example.com`)

## ۱) تنظیم DNS

پیش از نصب، در پنل DNS دامنهٔ خود یک رکورد `A` بسازید:

- نام: `pay` (یا هر زیردامنهٔ دلخواه)
- مقدار: آدرس IP سرور

اگر DNS هنوز آماده نباشد نصب ادامه می‌یابد ولی گواهی HTTPS صادر نمی‌شود؛
بعد از اصلاح DNS کافی است `centralpay ssl` را اجرا کنید.

## ۲) اجرای نصب یک‌خطی

<div dir="ltr">

```bash
curl -fsSL https://raw.githubusercontent.com/Mhoseinshah1/centralpay-bridge/main/install.sh | sudo bash
```

</div>

## ۳) پرسش‌های نصاب

1. **Payment domain** — دامنهٔ درگاه پرداخت، مثل `pay.example.com`
2. **Bot API base domain or URL** — دامنه یا آدرس API ربات، مثل
   `https://bot.example.com` (نصاب خودش `/api/payment` را اضافه می‌کند)
3. **CentralPay getLink API key** — کلید ساخت لینک پرداخت CentralPay
4. **CentralPay verify API key** — کلید تأیید پرداخت CentralPay
   (این دو کلید متفاوت‌اند؛ هر دو را از پنل CentralPay بگیرید)
5. **Bot /token2 value** — توکن ربات که با دستور `/token2` می‌گیرید.
   این با توکن BotFather فرق دارد و فقط برای اعلام پرداخت به API ربات است.
6. **Telegram bot username** — اختیاری؛ برای لینک «بازگشت به ربات»
7. **Email for TLS** — ایمیل برای گواهی Let's Encrypt
8. **Minimum payment amount** — حداقل مبلغ به تومان (پیش‌فرض ۱۰۰۰)
9. **Maximum payment amount** — حداکثر مبلغ به تومان (پیش‌فرض ۱۰۰٬۰۰۰٬۰۰۰)
10. **Retry mode** — حالت تلاش مجدد اعلان (`safe` پیش‌فرض و توصیه‌شده؛
    `idempotent` فقط با تأیید صریح توسعه‌دهندهٔ ربات)

مقادیر محرمانه هنگام تایپ نمایش داده نمی‌شوند.

## ۴) خروجی پایان نصب

در پایان، نصاب این‌ها را چاپ می‌کند:

- **آدرس API درگاه:** `https://YOUR_DOMAIN/api/custom-payment` —
  این آدرس را در تنظیمات «درگاه سفارشی» ربات تلگرام وارد کنید.
- **توکن API تولیدشده (inbound API key):** این مقدار را به‌عنوان
  `api_key` درگاه سفارشی در ربات وارد کنید.
- آدرس کال‌بک، آدرس سلامت، و محل فایل خلاصهٔ نصب
  (`/etc/centralpay-bridge/credentials.txt`؛ با `centralpay credentials`
  دوباره قابل مشاهده است).

کلیدهای CentralPay و token2 هرگز در خروجی چاپ نمی‌شوند.

## ۵) بررسی سلامت

<div dir="ltr">

```bash
centralpay status
curl https://YOUR_DOMAIN/health/ready
```

</div>

## رفع اشکال SSL

- `centralpay ssl` — بررسی DNS و تلاش دوباره برای صدور گواهی
- `centralpay logs caddy` — مشاهدهٔ خطاهای صدور گواهی
- مطمئن شوید پورت‌های ۸۰ و ۴۴۳ باز و DNS به همین سرور اشاره می‌کند.

## رفع اشکال Docker

- `docker ps` — وضعیت کانتینرها
- `centralpay diagnose` — گزارش کامل
- `systemctl status docker` — وضعیت سرویس Docker
- نصب مجدد امن است: نصاب قابل اجرا مجدد است و تنظیمات موجود را حفظ می‌کند.

## رفع اشکال PostgreSQL

- `centralpay logs db`
- `centralpay diagnose` بخش Database
- دادهٔ پایگاه در volume داکری `db_data` نگه‌داری می‌شود و با حذف
  کانتینرها از بین نمی‌رود.

</div>
