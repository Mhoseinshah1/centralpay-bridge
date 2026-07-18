"""Database models: payments and the permanent payment_events audit trail."""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func
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
