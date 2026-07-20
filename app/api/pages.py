"""User-facing payment status page for the CentralPay callback.

Every callback outcome that follows successful CentralPay verification —
BOT_ACCEPTED, BOT_PENDING, and UNDER_REVIEW — renders the SAME approved
Persian-only "ZedProxy Color Pop Receipt" page, so the payer experience
does not depend on the asynchronous customer-bot delivery state. The only
interpolated values are the machine-readable status (a fixed enum value on
``data-status``) and the HTML-escaped bot order id. The page is fully
self-contained: original inline SVG/CSS and the bundled Vazirmatn webfont
only — no external image, script, stylesheet, remote font, analytics, or
any other network dependency (the single outbound *navigation* link is the
fixed t.me return-to-bot button). Stack traces, secrets, bot responses,
and raw gateway errors never reach this page.
"""

import html

from app.services.verification import CallbackStatus

# Wording contract (fix/payer-status-page-accuracy): the bridge KNOWS only
# (1) CentralPay verification and, for BOT_ACCEPTED, (2) that the bot API
# accepted the order-processing request (HTTP 2xx = acceptance only —
# see app/services/notification.py). The final business result inside the
# customer bot is never known here, so this page must never claim the
# order was registered/completed or that credit was applied, and must
# never promise near-term application. Because ONE page now serves all
# three verified outcomes, its copy states only what is true in EVERY
# one of them: the payment succeeded (provable via CentralPay verify),
# order processing may take some time, and the order status lives in the
# bot. The visible internal state stays machine-readable on
# ``data-status`` without changing any stored status.
#
# The approved "ZedProxy Color Pop Receipt" design, ported from
# docs/previews/payment-success-zedproxy-color-pop-fa.html. Plain string
# template (NOT an f-string, so the CSS braces stay readable); the ONLY
# substitution points are __STATUS__ (a fixed CallbackStatus enum value)
# and __ORDER_ID__ (the HTML-escaped payer order id).
# Production deltas versus the preview, each required for production use:
#   - data-status carries the REAL callback status (monitoring/tests);
#   - the real order id replaces the preview example, and the id pill can
#     scroll internally so a long (up to 128-char) id never overflows;
#   - the bundled Vazirmatn variable webfont is loaded via a local
#     @font-face (root-relative /static URL; never a remote request);
#   - neutral copy that is true for accepted, pending, AND under-review
#     outcomes (no order-registered / credited claim anywhere);
#   - ONE primary action: a FIXED return-to-bot button whose destination
#     is always https://t.me/zedproxy_bot — no dynamic value alters it.
_SUCCESS_PAGE_TEMPLATE = """<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>پرداخت با موفقیت انجام شد</title>
<style>
  /* Vazirmatn v33.0.3 — repository-local variable webfont (SIL OFL 1.1,
     app/static/fonts/vazirmatn-v33/OFL.txt). Served by this application
     from a root-relative URL: never a Google Fonts / CDN / remote request. */
  @font-face{
    font-family:"Vazirmatn";
    src:url("/static/fonts/vazirmatn-v33/vazirmatn-variable.woff2") format("woff2");
    font-weight:100 900;
    font-style:normal;
    font-display:swap;
  }
  :root{
    --blue:#3977F6; --blue-deep:#2454D8;
    --violet:#7C5CFC; --violet-deep:#5A3FDB;
    --cyan:#35C8E8;
    --emerald:#19C878; --emerald-deep:#0E9F5C;
    --coral:#FF718C; --yellow:#FFD86B;
    --ink:#14213D; --ink-2:#56637A;
    --surface:rgba(255,255,255,.82);
    /* Persian typography: the bundled Vazirmatn variable font above,
       falling back to system fonts while it loads (font-display:swap). */
    --font:"Vazirmatn", Tahoma, "Segoe UI", Arial, sans-serif;
    --mono:ui-monospace, SFMono-Regular, Consolas, monospace;
    /* Hero sticker-scene scale. The composition is authored at 288x298;
       the whole scene scales with this factor (layout height is
       reclaimed via the .hero margin calc) so the page fits ONE viewport
       with no vertical scrolling at every target size. */
    --hs:.78;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{
    margin:0;font-family:var(--font);color:var(--ink);
    min-height:100dvh;position:relative;overflow-x:hidden;
    -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
    background:
      linear-gradient(180deg,#EEF5FF 0%,#F4EEFF 40%,#EAFBFF 74%,#FFF7FB 100%);
  }

  /* ---- ambient background: blobs + grain ---- */
  .bg{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none}
  .blob{position:absolute;border-radius:50%;filter:blur(64px);opacity:.55}
  .blob.blue  {width:30rem;height:30rem;top:-9rem;right:-7rem;background:rgba(57,119,246,.5)}
  .blob.violet{width:27rem;height:27rem;top:-7rem;left:-8rem;background:rgba(124,92,252,.42)}
  .blob.cyan  {width:23rem;height:23rem;bottom:-8rem;left:-6rem;background:rgba(53,200,232,.4)}
  .blob.coral {width:20rem;height:20rem;top:46%;right:-5rem;background:rgba(255,113,140,.34)}
  .grain{position:absolute;inset:0;opacity:.5;
    background-image:radial-gradient(rgba(20,33,61,.05) 1px,transparent 1.4px);
    background-size:22px 22px}

  /* ---- layout ---- */
  .wrap{position:relative;z-index:1;min-height:100dvh;
    display:flex;flex-direction:column;align-items:center;justify-content:center;
    padding:clamp(10px,1.8vh,18px) 16px;
    width:100%;max-width:520px;margin-inline:auto}
  @media (min-width:900px){ .wrap{max-width:620px} }

  main{width:100%;display:flex;flex-direction:column;align-items:center;text-align:center}

  /* ---- brand pill ---- */
  .brand{display:inline-flex;align-items:center;gap:.5rem;
    background:rgba(255,255,255,.72);border:1px solid rgba(255,255,255,.9);
    box-shadow:0 8px 22px rgba(57,119,246,.22);backdrop-filter:blur(10px);
    padding:.4rem .85rem .4rem .55rem;border-radius:999px;
    font-weight:700;color:var(--ink);font-size:.95rem;margin-bottom:clamp(4px,1vh,8px)}
  .brand .z{width:28px;height:28px;display:block;flex:0 0 auto;
    filter:drop-shadow(0 3px 6px rgba(57,119,246,.45))}
  .brand .tw{width:12px;height:12px;display:block;color:var(--yellow)}

  /* ==================  HERO STICKER SCENE  ================== */
  /* The scene keeps its authored 288x298 coordinate space and scales as a
     whole via --hs. transform does not shrink layout height, so the
     margin-bottom calc reclaims exactly the difference — the page column
     sees the VISUAL hero height, never the authored one. */
  .hero{position:relative;width:288px;height:298px;
    margin:clamp(2px,.8vh,8px) auto calc(clamp(4px,1.2vh,12px) + 298px*(var(--hs) - 1));
    transform:scale(var(--hs));transform-origin:top center}
  .hero svg{overflow:visible;display:block}
  @media (min-width:900px){ :root{--hs:.82} }

  .hero > *{position:absolute;filter:drop-shadow(0 12px 18px rgba(28,40,88,.20))}
  .h-shield {left:96px; top:-6px;  width:58px; z-index:1}
  .h-receipt{left:74px; top:6px;   width:150px;height:196px;z-index:2;transform:rotate(-4deg)}
  .h-card   {left:176px;top:18px;  width:80px; z-index:3;transform:rotate(12deg)}
  .h-heart  {left:6px;  top:44px;  width:42px; z-index:3;transform:rotate(-9deg)}
  .h-check  {left:26px; top:166px; width:108px;z-index:4}
  .spark    {filter:none;z-index:5}
  .s-violet {left:150px;top:0;    width:17px}
  .s-cyan   {left:0;    top:150px;width:15px}
  .s-coral  {left:252px;top:150px;width:13px}
  .s-yellow {left:120px;top:150px;width:11px}

  /* green "تأیید شد" badge sitting on the receipt */
  .rc-ok{position:absolute;left:78px;top:150px;z-index:5;
    display:inline-flex;align-items:center;gap:.25rem;
    background:linear-gradient(135deg,#25D68A,var(--emerald-deep));
    color:#fff;font-family:var(--font);font-weight:700;font-size:11px;
    padding:.2rem .45rem;border-radius:999px;transform:rotate(-4deg);
    box-shadow:0 4px 10px rgba(14,159,92,.4),inset 0 1px 0 rgba(255,255,255,.4);
    border:1.5px solid rgba(255,255,255,.9)}
  .rc-ok svg{width:11px;height:11px;display:block}

  /* ==================  TEXT  ================== */
  h1{margin:.1rem 0 .4rem;color:var(--ink);font-weight:800;
    font-size:clamp(22px,5.8vw,28px);line-height:1.4;letter-spacing:0;
    max-width:19rem;text-wrap:balance}
  @media (min-width:900px){ h1{max-width:24rem} }
  .thanks{margin:0 0 .2rem;color:var(--ink);font-weight:500;
    font-size:clamp(14px,3.8vw,15.5px);line-height:1.7;letter-spacing:0}
  .thanks .zx{color:var(--blue);font-weight:700}
  .sub{margin:.1rem 0 0;color:var(--ink-2);font-weight:400;
    font-size:clamp(14px,3.6vw,15.5px);line-height:1.7;letter-spacing:0;max-width:24rem}

  /* ==================  ORDER-ID PANEL  ================== */
  .order{width:100%;max-width:26rem;margin:clamp(10px,2vh,16px) auto 0;
    background:var(--surface);backdrop-filter:blur(12px);
    border:1px solid rgba(124,92,252,.28);border-radius:22px;
    padding:.7rem .9rem .75rem;
    box-shadow:0 14px 30px rgba(57,119,246,.16),0 0 0 4px rgba(124,92,252,.06),
      inset 0 1px 0 rgba(255,255,255,.7)}
  .order .top{display:flex;align-items:center;justify-content:center;gap:.4rem;
    color:var(--ink-2);font-weight:600;font-size:.86rem;margin-bottom:.45rem}
  .order .top svg{width:17px;height:17px;display:block;color:var(--blue-deep)}
  .id-row{display:flex;align-items:center;justify-content:center;gap:.55rem;max-width:100%}
  /* Real order ids can be up to 128 characters: the pill keeps the id on
     one unwrapped LTR line and scrolls internally instead of overflowing
     the viewport. */
  .id{direction:ltr;unicode-bidi:isolate;font-family:var(--mono);
    font-size:1rem;font-weight:700;letter-spacing:.14em;color:var(--ink);
    background:#fff;border:1px solid rgba(124,92,252,.22);border-radius:12px;
    padding:.4rem .8rem;white-space:nowrap;user-select:all;
    display:inline-block;max-width:calc(100% - 3.1rem);overflow-x:auto;
    box-shadow:inset 0 1px 0 rgba(255,255,255,.8),0 2px 6px rgba(57,119,246,.1)}
  .copy{width:40px;height:40px;flex:0 0 auto;border-radius:13px;cursor:pointer;
    display:grid;place-items:center;color:var(--blue-deep);
    background:#fff;border:1px solid rgba(124,92,252,.22);
    box-shadow:0 2px 6px rgba(57,119,246,.12);
    transition:transform .1s,background .15s,border-color .15s}
  .copy:hover{background:rgba(57,119,246,.08)}
  .copy:active{transform:scale(.93)}
  .copy svg{width:18px;height:18px;display:block}
  .copy .ok{display:none}
  .copy.done{color:var(--emerald-deep);background:rgba(25,200,120,.12);border-color:rgba(25,200,120,.45)}
  .copy.done .clip{display:none}.copy.done .ok{display:block}
  .copied-note{min-height:.9rem;margin-top:.3rem;color:var(--emerald-deep);
    font-weight:600;font-size:.8rem;opacity:0;transition:opacity .2s}
  .copied-note.show{opacity:1}

  /* primary return-to-bot action */
  .botlink{display:inline-flex;align-items:center;justify-content:center;gap:.5rem;
    width:min(100%,260px);min-height:44px;margin-top:clamp(12px,2.4vh,20px);
    background:linear-gradient(135deg,var(--blue),var(--violet-deep));color:#fff;
    font-weight:700;font-size:1rem;text-decoration:none;
    padding:.55rem 1.4rem;border-radius:999px;
    box-shadow:0 10px 24px rgba(57,119,246,.35),inset 0 1px 0 rgba(255,255,255,.35);
    transition:filter .15s,transform .1s}
  .botlink:hover{filter:brightness(1.07)}
  .botlink:active{transform:scale(.98)}
  .botlink svg{width:17px;height:17px;display:block;flex:0 0 auto}

  :focus-visible{outline:3px solid var(--violet);outline-offset:3px;border-radius:12px}

  /* ==================  ANIMATION  ================== */
  @media (prefers-reduced-motion:no-preference){
    .h-receipt{animation:enter-rc .55s .05s cubic-bezier(.2,.7,.3,1) both}
    .rc-ok    {animation:fade .4s .5s ease both}
    .h-check  {animation:pop-check .6s .18s cubic-bezier(.22,1.2,.36,1) both}
    .h-ring   {animation:ring 2.2s .4s ease-out forwards}
    .h-card   {animation:enter-side .5s .28s cubic-bezier(.22,1.15,.36,1) both}
    .h-heart  {animation:enter-side .5s .34s cubic-bezier(.22,1.15,.36,1) both}
    .h-shield {animation:fade .5s .3s ease both}
    .spark    {animation:fade .5s ease both}
    .s-violet {animation-delay:.5s}.s-cyan{animation-delay:.62s}
    .s-coral  {animation-delay:.74s}.s-yellow{animation-delay:.86s}
    .blob.blue,.blob.cyan  {animation:drift 46s ease-in-out infinite alternate}
    .blob.violet,.blob.coral{animation:drift 54s ease-in-out infinite alternate-reverse}
  }
  @keyframes enter-rc{from{opacity:0;transform:translateY(20px) rotate(-9deg)}
    to{opacity:1;transform:translateY(0) rotate(-4deg)}}
  @keyframes pop-check{from{opacity:0;transform:scale(.85)}to{opacity:1;transform:scale(1)}}
  @keyframes enter-side{from{opacity:0;transform:translateY(10px) scale(.85) rotate(var(--r,0deg))}
    to{opacity:1;transform:translateY(0) scale(1) rotate(var(--r,0deg))}}
  @keyframes fade{from{opacity:0}to{opacity:1}}
  @keyframes ring{0%{transform:scale(.55);opacity:.8}70%{opacity:.12}100%{transform:scale(1.3);opacity:0}}
  @keyframes drift{from{transform:translate(0,0)}to{transform:translate(24px,-20px)}}
  .h-card{--r:12deg}.h-heart{--r:-9deg}

  /* mobile: thin ornaments, never shrink text too far */
  @media (max-width:360px){
    .s-coral{display:none}
    .blob.coral{opacity:.24}
  }

  /* ---- one-viewport fit on shorter screens (kept LAST so the height
     rules win over the width defaults above). Nothing is hidden: only
     scale, spacing, and type sizes tighten. ---- */
  @media (max-height:850px){
    :root{--hs:.73}
    .wrap{padding-block:10px}
    .brand{margin-bottom:4px}
    h1{font-size:clamp(22px,5.2vw,26px)}
  }
  @media (max-height:780px){
    :root{--hs:.66}
    .wrap{padding-block:8px}
    .brand{margin-bottom:2px}
    h1{font-size:clamp(21px,5vw,25px);margin-bottom:.3rem}
    .thanks{font-size:14px}
    .sub{font-size:14px;line-height:1.65}
    .order{margin-top:8px;padding:.55rem .8rem .6rem}
    .botlink{margin-top:9px}
  }
</style>
</head>
<body>

<div class="bg" aria-hidden="true">
  <span class="blob blue"></span><span class="blob violet"></span>
  <span class="blob cyan"></span><span class="blob coral"></span>
  <span class="grain"></span>
</div>

<div class="wrap">
  <main data-status="__STATUS__">

    <!-- brand pill -->
    <span class="brand">
      <svg class="z" viewBox="0 0 40 40" aria-hidden="true">
        <defs>
          <linearGradient id="zg" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#4B86FF"/><stop offset=".55" stop-color="#3977F6"/>
            <stop offset="1" stop-color="#6B4BF0"/>
          </linearGradient>
          <radialGradient id="zgl" cx=".32" cy=".26" r=".7">
            <stop offset="0" stop-color="#fff" stop-opacity=".85"/><stop offset=".42" stop-color="#fff" stop-opacity="0"/>
          </radialGradient>
        </defs>
        <circle cx="20" cy="20" r="18" fill="#fff"/>
        <circle cx="20" cy="20" r="16.5" fill="url(#zg)"/>
        <path d="M13 14h14l-11 12h11" fill="none" stroke="#fff" stroke-width="3.1" stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="20" cy="20" r="16.5" fill="url(#zgl)"/>
      </svg>
      <span>زدپروکسی</span>
      <svg class="tw" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 0c1 6 4 9 12 12-8 3-11 6-12 12-1-6-4-9-12-12C8 9 11 6 12 0z" fill="currentColor"/></svg>
    </span>

    <!-- ===== HERO (decorative) ===== -->
    <div class="hero" aria-hidden="true">

      <!-- tiny shield behind receipt -->
      <svg class="h-shield" viewBox="0 0 64 72">
        <defs>
          <linearGradient id="shd" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#5B8CFF"/><stop offset="1" stop-color="#2454D8"/></linearGradient>
          <radialGradient id="shg" cx=".3" cy=".22" r=".8"><stop offset="0" stop-color="#fff" stop-opacity=".7"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/></radialGradient>
        </defs>
        <path d="M32 3l26 9v15c0 16-10 27-26 33C16 54 6 43 6 27V12L32 3z" fill="#fff"/>
        <path d="M32 7l22 7.6V27c0 13.6-8.6 23.2-22 28.6C18.6 50.2 10 40.6 10 27V14.6L32 7z" fill="url(#shd)"/>
        <path d="M32 7l22 7.6V27c0 13.6-8.6 23.2-22 28.6C18.6 50.2 10 40.6 10 27V14.6L32 7z" fill="url(#shg)"/>
      </svg>

      <!-- main receipt -->
      <div class="h-receipt">
        <svg viewBox="0 0 150 196" width="150" height="196">
          <defs>
            <linearGradient id="hd" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#4E88FF"/><stop offset=".55" stop-color="#3977F6"/><stop offset="1" stop-color="#6B4BF0"/></linearGradient>
            <linearGradient id="gloss" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".9"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
          </defs>
          <path d="M0 18 Q0 4 14 4 H136 Q150 4 150 18 V172
                   l-12.5 11 -12.5 -11 -12.5 11 -12.5 -11 -12.5 11 -12.5 -11
                   -12.5 11 -12.5 -11 -12.5 11 -12.5 -11 -12.5 11 -12.5 -11 Z"
                fill="#fff"/>
          <path d="M0 18 Q0 4 14 4 H136 Q150 4 150 18 V40 H0 Z" fill="url(#hd)"/>
          <path d="M0 18 Q0 4 14 4 H136 Q150 4 150 18 V24 H0 Z" fill="url(#gloss)" opacity=".7"/>
          <circle cx="128" cy="22" r="11" fill="#fff"/>
          <path d="M123 18h10l-8 8h8" fill="none" stroke="#3977F6" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
          <rect x="20" y="60"  width="110" height="7" rx="3.5" fill="#E7ECF6"/>
          <rect x="20" y="80"  width="88"  height="7" rx="3.5" fill="#EDF0F8"/>
          <rect x="20" y="100" width="104" height="7" rx="3.5" fill="#E7ECF6"/>
          <rect x="20" y="120" width="70"  height="7" rx="3.5" fill="#EDF0F8"/>
        </svg>
        <span class="rc-ok">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7"/></svg>
          تأیید شد
        </span>
      </div>

      <!-- main success check (with expanding ring) -->
      <svg class="h-check" viewBox="0 0 108 108">
        <defs>
          <radialGradient id="cbd" cx=".38" cy=".3" r=".85"><stop offset="0" stop-color="#4CE6A8"/><stop offset=".55" stop-color="#19C878"/><stop offset="1" stop-color="#0C8E52"/></radialGradient>
          <radialGradient id="cgl" cx=".36" cy=".24" r=".55"><stop offset="0" stop-color="#fff" stop-opacity=".92"/><stop offset=".6" stop-color="#fff" stop-opacity="0"/></radialGradient>
          <linearGradient id="crim" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#E9F7EF"/></linearGradient>
        </defs>
        <circle class="h-ring" cx="54" cy="54" r="47" fill="none" stroke="#19C878" stroke-opacity=".35" stroke-width="3"/>
        <circle cx="54" cy="54" r="46" fill="url(#crim)"/>
        <circle cx="54" cy="54" r="39" fill="url(#cbd)"/>
        <circle cx="54" cy="54" r="39" fill="none" stroke="#0C8E52" stroke-opacity=".22" stroke-width="2"/>
        <path d="M36 56l12 12 26-28" fill="none" stroke="#fff" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>
        <ellipse cx="44" cy="38" rx="27" ry="17" fill="url(#cgl)"/>
      </svg>

      <!-- small blue payment card -->
      <svg class="h-card" viewBox="0 0 96 74">
        <defs>
          <linearGradient id="pcd" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#5B8CFF"/><stop offset="1" stop-color="#5A3FDB"/></linearGradient>
          <radialGradient id="pcg" cx=".3" cy=".2" r=".85"><stop offset="0" stop-color="#fff" stop-opacity=".75"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/></radialGradient>
        </defs>
        <rect x="2" y="2" width="92" height="70" rx="15" fill="#fff"/>
        <rect x="5" y="5" width="86" height="64" rx="13" fill="url(#pcd)"/>
        <rect x="14" y="20" width="20" height="15" rx="4" fill="#FFD86B"/>
        <rect x="16" y="25" width="16" height="2.4" rx="1.2" fill="#E4B23F"/>
        <rect x="14" y="48" width="52" height="6" rx="3" fill="#fff" opacity=".9"/>
        <rect x="5" y="5" width="86" height="64" rx="13" fill="url(#pcg)"/>
      </svg>

      <!-- blue heart -->
      <svg class="h-heart" viewBox="0 0 48 44">
        <defs>
          <linearGradient id="htd" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#6FA0FF"/><stop offset="1" stop-color="#2454D8"/></linearGradient>
          <radialGradient id="htg" cx=".32" cy=".24" r=".7"><stop offset="0" stop-color="#fff" stop-opacity=".8"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/></radialGradient>
        </defs>
        <path d="M24 43C10 34 4 26 4 17.5 4 11 9 6 15.3 6c3.7 0 7 1.9 8.7 4.9C25.7 7.9 29 6 32.7 6 39 6 44 11 44 17.5 44 26 38 34 24 43z" fill="#fff"/>
        <path d="M24 40C11.5 31.6 7 24.4 7 17.4 7 12 11.2 8.6 15.9 8.6c3.4 0 6.5 1.9 7.8 4.9l.3.7.3-.7c1.3-3 4.4-4.9 7.8-4.9C44.8 8.6 49 12 49 17.4" fill="url(#htd)" transform="translate(-1)"/>
        <path d="M24 40C11.5 31.6 7 24.4 7 17.4 7 12 11.2 8.6 15.9 8.6c3.4 0 6.5 1.9 7.8 4.9" fill="url(#htg)" transform="translate(-1)"/>
      </svg>

      <!-- sparkles -->
      <svg class="spark s-violet" viewBox="0 0 24 24"><path d="M12 0c1 6 4 9 12 12-8 3-11 6-12 12-1-6-4-9-12-12C8 9 11 6 12 0z" fill="#7C5CFC"/></svg>
      <svg class="spark s-cyan"   viewBox="0 0 24 24"><path d="M12 0c1 6 4 9 12 12-8 3-11 6-12 12-1-6-4-9-12-12C8 9 11 6 12 0z" fill="#35C8E8"/></svg>
      <svg class="spark s-coral"  viewBox="0 0 24 24"><path d="M12 0c1 6 4 9 12 12-8 3-11 6-12 12-1-6-4-9-12-12C8 9 11 6 12 0z" fill="#FF718C"/></svg>
      <svg class="spark s-yellow" viewBox="0 0 24 24"><path d="M12 0c1 6 4 9 12 12-8 3-11 6-12 12-1-6-4-9-12-12C8 9 11 6 12 0z" fill="#FFD86B"/></svg>
    </div>

    <!-- ===== COPY ===== -->
    <h1>پرداخت با موفقیت انجام شد</h1>
    <p class="thanks">از خرید شما از <span class="zx">زدپروکسی</span> سپاسگزاریم <span aria-hidden="true">💙</span></p>
    <p class="sub">پرداخت شما تأیید شد. پردازش سفارش ممکن است چند لحظه زمان ببرد. لطفاً برای مشاهده وضعیت سفارش به ربات بازگردید.</p>

    <!-- ===== ORDER-ID PANEL ===== -->
    <section class="order" aria-label="شماره سفارش">
      <div class="top">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 3h12a1 1 0 0 1 1 1v17l-3.5-2-3.5 2-3.5-2L5 21V4a1 1 0 0 1 1-1z"/><path d="M9 8h6M9 12h6"/></svg>
        شماره سفارش
      </div>
      <div class="id-row">
        <span class="id" id="orderId">__ORDER_ID__</span>
        <button class="copy" id="copyBtn" type="button" aria-label="کپی شمارهٔ سفارش">
          <svg class="clip" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M6 15V6a2 2 0 0 1 2-2h9"/></svg>
          <svg class="ok" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7"/></svg>
        </button>
      </div>
      <p class="copied-note" id="copiedNote" aria-live="polite"></p>
    </section>

    <!-- ===== PRIMARY ACTION: fixed return-to-bot button ===== -->
    <a class="botlink" href="https://t.me/zedproxy_bot"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 4L3 11l6 2 2 6 4-5 6 6z"/></svg>بازگشت به ربات</a>
  </main>
</div>

<script>
  (function(){
    var b=document.getElementById('copyBtn'),
        id=document.getElementById('orderId'),
        note=document.getElementById('copiedNote');
    if(!b) return;
    function done(){
      b.classList.add('done');
      note.textContent='کپی شد'; note.classList.add('show');
      setTimeout(function(){ b.classList.remove('done'); note.classList.remove('show'); note.textContent=''; },1600);
    }
    b.addEventListener('click',function(){
      var t=id.textContent.trim();
      if(navigator.clipboard&&navigator.clipboard.writeText){
        navigator.clipboard.writeText(t).then(done).catch(done);
      }else{
        try{var r=document.createRange();r.selectNode(id);var s=getSelection();
          s.removeAllRanges();s.addRange(r);document.execCommand('copy');s.removeAllRanges();}catch(e){}
        done();
      }
    });
  })();
</script>
</body>
</html>"""


def payment_status_page(
    status: CallbackStatus, bot_order_id: str, *, bot_username: str = ""
) -> str:
    """Render the unified verified-payment page.

    ``status`` fills only the machine-readable ``data-status`` attribute —
    the stored payment/notification state is never altered or promoted by
    rendering. The return-to-bot destination is intentionally FIXED in the
    template (https://t.me/zedproxy_bot); ``bot_username`` is retained for
    call-site compatibility and deliberately unused.
    """
    del bot_username  # fixed destination: no dynamic value may alter the page
    return _SUCCESS_PAGE_TEMPLATE.replace("__STATUS__", status.value).replace(
        "__ORDER_ID__", html.escape(bot_order_id)
    )
