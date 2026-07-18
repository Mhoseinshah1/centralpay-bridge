# Admin Telegram bot validation — 0.6.0-rc1

**Status: NOT PERFORMED against real Telegram — RELEASE BLOCKER B3 for
enabling the admin bot in production.** The payment path does not depend
on the admin bot (it is optional and disabled by default), so B3 blocks
admin-bot enablement, not the payment bridge itself.

No Telegram bot token was available or requested in the development
environment; this document must never contain a real token or real
admin IDs.

## Validation matrix

| Area | Real | Mocked | Not tested |
|---|---|---|---|
| Numeric-ID-only authorization, generic denial, private-chat-only | | ✔ (fake updates) | real Telegram updates |
| All 12 read-only commands | | ✔ | real rendering |
| Persian/HTML formatting + escaping | | ✔ (string assertions) | real client rendering |
| Alert outbox durability (Telegram down ≠ payments blocked) | ✔ (real PG, fake transport) | ✔ | real outage |
| Alert dedup + never-dedup for financial alerts | | ✔ | — |
| Daily report scheduling (Asia/Tehran, restart-safe) | | ✔ (clock control) | real long-run |
| Long-polling behavior, real 429/backoff | | ✔ (simulated 429) | real API limits |
| Hardened compose service (masked secrets, profile gating) | config validated | | runtime on real host |

## Required procedure (to close B3)

On a staging host with a throwaway BotFather token and a test admin ID:

1. Enable via installer or `centralpay admin-bot enable`; confirm the
   container starts only with the `admin-bot` profile and that masked
   env vars hide payment secrets (`docker compose exec admin-bot env`).
2. From the admin account: run every command; confirm output rendering
   (Persian, HTML escaping, message-length splitting).
3. From a non-admin account and from a group chat: confirm generic
   denial and `admin_bot_unauthorized_access` audit events.
4. Stop the bot container; generate alerts (e.g. a manual-review
   payment); confirm payments continue unaffected and queued alerts
   deliver after restart (duplicates possible, never lost).
5. Trigger a real 429 (burst) and confirm backoff.
6. Leave running across a daily-report boundary; confirm exactly one
   report.
7. Record results here (dates, bot username, redacted logs).

## Results

_None recorded. Blocker open._

## Dynamic fee additions (feat/dynamic-payment-fee)

When the live-Telegram validation (blocker B3) is performed, also verify:

- `/fee` shows the current fee, the policy id, and any scheduled change,
  and states that changes affect NEW orders only.
- `/fee` is strictly read-only: no argument to it (or any other command)
  can create, change, schedule, or cancel a fee policy from Telegram —
  mutations exist only in the root host CLI (`centralpay fee`).
- An unauthorized Telegram account receives the generic denial for
  `/fee`, like every other command.
- `/payment ORDER_ID` for a fee-bearing payment clearly separates the
  original bot invoice, the fee, and the amount paid through the gateway
  (the gateway figure is never labelled as the bot invoice).
- The daily report separates original-invoice, fee, and
  collected-via-gateway totals.

_Not performed yet — same blocker, no live Telegram run._
