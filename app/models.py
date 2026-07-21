"""Database models: payments and the permanent payment_events audit trail."""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# SQLite (unit tests only) needs INTEGER primary keys for autoincrement.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")
JSONColumn = JSON().with_variant(postgresql.JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


# Storage contract for CentralPay reference IDs. The verify parser
# (app/centralpay.py) validates gateway-reported referenceId values against
# this exact limit BEFORE any query, assignment, audit event, or log use —
# the column length and the parser bound must never drift apart.
CENTRALPAY_REFERENCE_ID_MAX_LENGTH = 128


class PaymentStatus(enum.StrEnum):
    CREATED = "created"
    LINK_CREATED = "link_created"
    GETLINK_FAILED = "getlink_failed"
    GATEWAY_VERIFIED = "gateway_verified"
    BOT_NOTIFY_PENDING = "bot_notify_pending"
    BOT_NOTIFY_ACCEPTED = "bot_notify_accepted"
    MANUAL_REVIEW = "manual_review"


class FeePolicy(Base):
    """Append-only, versioned service-fee configuration.

    Financial configuration is never edited or deleted: changing the fee
    creates a NEW row, cancelling a scheduled policy fills the
    cancellation fields on its row. Payments snapshot the policy at
    creation and never depend on later reads. Selection is deterministic:
    highest ``effective_at`` not after now, then highest ``id``, skipping
    cancelled rows.
    """

    __tablename__ = "fee_policies"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    # Fee percentage in basis points: 0 = 0%, 225 = 2.25%, 10000 = 100%.
    rate_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[str | None] = mapped_column(String(128))
    cancellation_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_fee_policies_effective_at", "effective_at"),
        CheckConstraint(
            "rate_bps >= 0 AND rate_bps <= 10000", name="ck_fee_policies_rate_bps_range"
        ),
        CheckConstraint("note <> ''", name="ck_fee_policies_note_not_empty"),
        # Cancellation fields are set together or not at all.
        CheckConstraint(
            "(cancelled_at IS NULL AND cancelled_by IS NULL AND cancellation_note IS NULL)"
            " OR (cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL"
            " AND cancellation_note IS NOT NULL)",
            name="ck_fee_policies_cancellation_consistent",
        ),
    )


class CentralPayPayerIdentity(Base):
    """Stable per-customer CentralPay payer identity (incident 2026-07).

    Maps an upstream customer (by a keyed, non-reversible ``customer_key_hash``
    — the raw customer_id is never stored) to a stable numeric gateway
    ``userId``. Uniqueness on both columns guarantees two different customers
    can never share one gateway payer identity (which would share saved-card
    suggestions). See app/services/payer_identity.py.
    """

    __tablename__ = "centralpay_payer_identities"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    customer_key_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    gateway_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    derivation_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("gateway_user_id > 0", name="ck_payer_identities_gateway_user_id_positive"),
        CheckConstraint(
            "derivation_version >= 1", name="ck_payer_identities_derivation_version_positive"
        ),
    )


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    # Original bot order id (string) — preserved verbatim for bot notification.
    bot_order_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    # Numeric order id generated for CentralPay, which requires an integer orderId.
    gateway_order_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    # Gateway payer identity snapshot. For payments created since the
    # per-customer isolation fix this is the customer-specific derived userId;
    # legacy payments carry the old shared CENTRALPAY_USER_ID and a NULL
    # payer_identity_id (their marker). Verification always compares the
    # gateway's reported userId against THIS snapshot, never a live value.
    gateway_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # NULL == created under the legacy shared payer identity (pre-fix). Set for
    # every payment created with per-customer isolation.
    payer_identity_id: Mapped[int | None] = mapped_column(
        ForeignKey("centralpay_payer_identities.id", ondelete="RESTRICT"), index=True
    )
    payer_derivation_version: Mapped[int | None] = mapped_column(Integer)
    # ORIGINAL bot invoice amount in TOMAN — exactly what the bot requested.
    # Never includes the service fee and is never modified after creation.
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # --- Immutable fee snapshot (dynamic fee feature) -----------------------
    # Captured exactly once at payment creation from the then-effective fee
    # policy; a later policy change NEVER alters an existing payment.
    # fee_amount = (amount * fee_rate_bps + 5000) // 10000  (round half up)
    # payable_amount = amount + fee_amount  (what CentralPay charges).
    fee_policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("fee_policies.id", ondelete="RESTRICT"), nullable=True
    )
    fee_rate_bps: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    fee_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    payable_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PaymentStatus.CREATED.value, index=True
    )
    redirect_url: Mapped[str | None] = mapped_column(Text)
    # Unique across payments: CentralPay must never report one referenceId
    # for two different payments (collision => manual review, never overwrite).
    reference_id: Mapped[str | None] = mapped_column(
        String(CENTRALPAY_REFERENCE_ID_MAX_LENGTH), unique=True
    )
    # SHA-256 hex of the one-time callback token embedded in the returnUrl.
    # The plaintext token exists only inside the signed callback URL; a
    # database leak alone cannot forge callbacks.
    callback_token_hash: Mapped[str | None] = mapped_column(String(64))
    callback_token_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Manual-review operational metadata (appended; financial fields are
    # never overwritten by review operations).
    review_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_resolution: Mapped[str | None] = mapped_column(String(64))
    # Only the final four card digits may ever be stored.
    card_last4: Mapped[str | None] = mapped_column(String(4))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Set exactly once, when CentralPay verification is committed. This is the
    # durable "gateway verified" fact, independent of bot delivery status.
    gateway_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Bot delivery tracking (Phase 2). Reason codes are machine-readable
    # (app.reasons.ReasonCode) and stored separately from human-readable text.
    bot_notify_reason: Mapped[str | None] = mapped_column(String(64))
    bot_notify_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    bot_last_http_status: Mapped[int | None] = mapped_column(Integer)
    bot_last_error_code: Mapped[str | None] = mapped_column(String(64))
    bot_notify_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bot_notify_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manual_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_claimed_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # Worker due-scan: status + next_retry_at.
        Index("ix_payments_notify_due", "status", "next_retry_at"),
        # Financial invariants enforced at the database level (final audit;
        # migration 0005 adds them to existing PostgreSQL databases):
        # F2-adjacent: amounts are always positive integers.
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        # F14-adjacent: attempt counters can never go negative.
        CheckConstraint(
            "bot_notify_attempts >= 0", name="ck_payments_attempts_non_negative"
        ),
        # Fee snapshot invariants (migration 0006): rates are basis points
        # within 0..10000, fees are never negative, and the payable amount
        # is exactly original + fee.
        CheckConstraint(
            "fee_rate_bps >= 0 AND fee_rate_bps <= 10000",
            name="ck_payments_fee_rate_bps_range",
        ),
        CheckConstraint("fee_amount >= 0", name="ck_payments_fee_amount_non_negative"),
        CheckConstraint("payable_amount > 0", name="ck_payments_payable_positive"),
        CheckConstraint(
            "payable_amount = amount + fee_amount",
            name="ck_payments_payable_equals_amount_plus_fee",
        ),
        # F1/F9: a payment can only be queued for (or accepted by) the bot
        # AFTER the gateway-verified fact is durably recorded.
        CheckConstraint(
            "status NOT IN ('bot_notify_pending', 'bot_notify_accepted')"
            " OR gateway_verified_at IS NOT NULL",
            name="ck_payments_delivery_requires_verification",
        ),
    )


class AlertStatus(enum.StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    DELIVERED = "delivered"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


class AdminAlert(Base):
    """Outbox for administrator Telegram alerts.

    Rows are created inside the same transaction as the financial state
    change they describe; delivery happens later, out of band, in the
    admin-bot service. Payment processing never depends on delivery.
    """

    __tablename__ = "admin_alerts"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    alert_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    deduplication_key: Mapped[str | None] = mapped_column(String(160), index=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AlertStatus.PENDING.value, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_admin_alerts_due", "status", "next_retry_at"),)


class WorkerHeartbeat(Base):
    """Operational liveness records for background workers. No secrets."""

    __tablename__ = "worker_heartbeats"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    worker_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_cycle_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PaymentEvent(Base):
    """Permanent audit trail. Rows are append-only and must never be deleted."""

    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    # Nullable so events can be recorded for callbacks that reference no known payment.
    payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    request_id: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
