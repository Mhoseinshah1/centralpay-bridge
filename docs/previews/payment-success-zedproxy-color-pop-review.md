# ZedProxy — "Color Pop Receipt" payment-success preview · design review

Preview file: `docs/previews/payment-success-zedproxy-color-pop-fa.html`
Scope: **visual preview only.** No production template, route, payment/callback,
delivery, Docker, Caddy, env, or deploy file was touched.

---

## Refinement rounds

### Round 1
- **Score:** 7.8 / 10
- **Weakness:** too celebratory and game-like.
- **Correction:** reduced decorative objects and strengthened financial trust cues.

### Round 2
- **Score:** 8.6 / 10
- **Weakness:** excessive glass effects and weak order-ID hierarchy.
- **Correction:** increased receipt focus, simplified surfaces, and created a dedicated order panel.

### Round 3
- **Score:** 9.7 / 10
- **Weakness:** minor mobile crowding and unnecessary continuous motion.
- **Correction:** reduced mobile ornaments, improved spacing, and changed animations to short one-time sequences.

---

## Final selected direction

A dimensional payment receipt with a large emerald confirmation sticker,
supported by a small blue payment card, blue heart, and limited colorful
sparkles in a blue-violet ZedProxy environment.

---

## Verification

Layout checked by measurement at 360×800, 390×844, and 1440×900:
no horizontal scroll, hero scene sits fully above the headline (no sticker
overlaps text), the order ID never wraps or overflows, and the brand pill fits.
Render sampling confirms the colorful background, blue-violet receipt header,
white receipt body, saturated emerald check, and blue-violet payment card.
Reduced-motion collapses all animation to the final settled state.

## Accuracy note for a future production port

The bottom-strip item **سفارش ثبت شد** is stronger than what the production page
in `app/api/pages.py` is permitted to assert. There, the bridge only knows the
payment was verified and that the customer bot **accepted** the order-processing
request (HTTP 2xx = acceptance only) — never that the order was
registered/completed. The label is used here per the explicit preview brief; if
this design is ported to production, reconcile that wording with the contract in
`app/api/pages.py`.

## Files

- `docs/previews/payment-success-zedproxy-color-pop-fa.html`
- `docs/previews/payment-success-zedproxy-color-pop-review.md`
- `docs/previews/screenshots/payment-success-color-pop-mobile.png`
- `docs/previews/screenshots/payment-success-color-pop-desktop.png`

## Typography update (this revision)

The visual composition, colors, sticker scene, spacing, animations, order
panel, and responsive behavior are unchanged from the approved preview. Only
Persian typography was adjusted:

- Primary UI stack is now `Vazirmatn, Tahoma, "Segoe UI", Arial, sans-serif`.
  No Vazirmatn font file exists in this repository, so there is no
  `@font-face`, no bundled binary, and no external font request — Vazirmatn
  renders only where the viewer's system provides it; otherwise the stack
  falls back safely.
- Weights normalized: heading 800, body copy 400/500, labels (order label,
  status items, copied note) 600, the «زدپروکسی» highlight stays 700.
- Body copy line-height set to 1.8 with no artificial letter spacing on
  Persian text.
- The order ID keeps its LTR `ui-monospace` stack and isolation.
