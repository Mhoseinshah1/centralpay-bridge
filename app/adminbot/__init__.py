"""Administrator-only Telegram bot (Phase 4).

Operational visibility and best-effort alerts. This package is never part of
the customer-facing payment flow: the API and worker only insert alert
outbox rows; all Telegram traffic happens in the separate admin-bot service.
"""
