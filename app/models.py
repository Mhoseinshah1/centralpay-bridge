"""Database models: payments and the permanent payment_events audit trail."""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# SQLite (unit tests only) needs INTEGER primary keys for autoincrement.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")
JSONColumn = JSON().with_variant(postgresql.JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class PaymentStatus(enum.StrEnum):
    CREATED = "created"
    LINK_CREATED = "link_created"
    GETLINK_FAILED = "getlink_failed"
    GATEWAY_VERIFIED = "gateway_verified"
    BOT_NOTIFY_PENDING = "bot_notify_pending"
    BOT_NOTIFY_ACCEPTED = "bot_notify_accepted"
    MANUAL_REVIEW = "manual_review"


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    # Original bot order id (string) — preserved verbatim for bot notification.
    bot_order_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    # Numeric order id generated for CentralPay, which requires an integer orderId.
    gateway_order_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    gateway_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Amount in TOMAN.
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PaymentStatus.CREATED.value, index=True
    )
    redirect_url: Mapped[str | None] = mapped_column(Text)
    reference_id: Mapped[str | None] = mapped_column(String(128))
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
