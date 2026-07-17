# Codex / Cloud Code Prompt — Phase 1

Copy the prompt below into Codex or Cloud Code.

```text
Open repository:

Mhoseinshah1/centralpay-bridge

Read AGENTS.md completely before making any change.
AGENTS.md is the authoritative project contract.
Read GitHub issue #1 for the implementation roadmap.

First inspect the repository and produce a concise implementation plan for Phase 1.
Then implement only Phase 1 in a new branch named:

feat/core-payment-api

Do not implement everything at once.
Do not start Phase 2.

Phase 1 requirements:

1. Create a clean FastAPI application structure.
2. Use Python 3.12, SQLAlchemy 2, Alembic, PostgreSQL, httpx, and Pydantic Settings.
3. Add configuration loaded from environment variables.
4. Add a Payment model with at least:
   - id
   - bot_order_id as unique string
   - gateway_order_id as unique integer
   - gateway_user_id as integer
   - amount in TOMAN
   - status
   - redirect_url
   - reference_id
   - card_last4
   - last_error
   - created_at
   - updated_at
5. Add a permanent payment_events audit model with:
   - id
   - payment_id
   - event_type
   - level
   - request_id
   - data
   - created_at
6. Add Alembic configuration and initial migrations.
7. Implement POST /api/custom-payment.
8. Validate the inbound api_key with constant-time comparison.
9. Make payment creation idempotent by original bot_order_id.
10. Reject the same bot_order_id when the amount differs.
11. Preserve the original string bot order_id.
12. Generate a unique integer gateway_order_id for CentralPay.
13. Integrate CentralPay getLink:
    POST JSON to https://centralapi.org/webservice/basic/getLink.php
    with api_key, type=deposit, amount, userId, orderId, and returnUrl.
14. Return exactly this success shape to the bot:
    {"url":"https://payment-url"}
15. Create the callback URL with orderId and an HMAC signature.
16. Implement GET /api/centralpay/callback.
17. Validate callback HMAC before database or gateway processing.
18. Use a database transaction and row locking during callback processing.
19. Integrate CentralPay verify:
    POST JSON to https://centralapi.org/webservice/basic/verify.php
    with api_key and orderId.
20. On successful verify, validate:
    - amount matches the database
    - userId matches the database
    - referenceId exists
21. Store only the final four card digits, never the full card number.
22. Never call verify again after the payment is successfully verified.
23. Do not notify the Telegram bot in Phase 1.
24. Use structured JSON logs and request IDs.
25. Never log API keys, tokens, callback signatures, request bodies containing secrets, full card numbers, or full redirect URLs.
26. Add GET /health/live.
27. Add GET /health/ready with a real database connectivity check.
28. Add clear typed exceptions and safe external API error handling.
29. Add README development instructions for running Phase 1 locally.
30. Add .env.example with placeholders only and ensure .env is ignored.

Required tests:

- invalid inbound API key
- payment creation success
- duplicate order returns the existing link
- duplicate order with different amount is rejected
- getLink success
- getLink rejected response
- getLink network failure
- invalid callback signature
- callback payment not found
- verify success
- verify amount mismatch
- verify userId mismatch
- verify missing referenceId
- duplicate callback does not call verify again
- audit events are created
- logs do not expose configured secrets
- health/live success
- health/ready success and database failure

Quality requirements:

- pytest passes
- Ruff passes
- type checking passes
- Alembic migration can upgrade an empty PostgreSQL database
- no production secrets are committed

Use small, reviewable commits.
Open a draft pull request to main when Phase 1 is complete.
Include in the PR description:

- architecture summary
- database schema summary
- endpoint summary
- security decisions
- commands run
- test results
- known limitations

Do not merge the pull request.
Do not start Docker deployment, installer, worker, bot notification, retries, or the admin Telegram bot yet.
Stop and wait for review after opening the draft pull request.
```
