"""Normalized reason codes for bot notification outcomes.

Reason codes are machine-readable and stored separately from human-readable
error text. There is no generic unexplained "stuck" state: every non-success
state carries one of these explicit codes.
"""

import enum


class ReasonCode(enum.StrEnum):
    BOT_DNS_FAILED = "bot_dns_failed"
    BOT_CONNECTION_REFUSED = "bot_connection_refused"
    BOT_CONNECTION_FAILED = "bot_connection_failed"
    BOT_TIMEOUT_AMBIGUOUS = "bot_timeout_ambiguous"
    BOT_HTTP_400 = "bot_http_400"
    BOT_HTTP_401 = "bot_http_401"
    BOT_HTTP_403 = "bot_http_403"
    BOT_HTTP_404 = "bot_http_404"
    BOT_HTTP_409 = "bot_http_409"
    BOT_HTTP_422 = "bot_http_422"
    BOT_HTTP_429 = "bot_http_429"
    BOT_HTTP_500 = "bot_http_500"
    BOT_HTTP_502 = "bot_http_502"
    BOT_HTTP_503 = "bot_http_503"
    BOT_HTTP_504 = "bot_http_504"
    BOT_HTTP_OTHER = "bot_http_other"
    BOT_INVALID_CONFIGURATION = "bot_invalid_configuration"
    BOT_NOTIFY_ACCEPTED = "bot_notify_accepted"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    RETRY_LIMIT_REACHED = "retry_limit_reached"
