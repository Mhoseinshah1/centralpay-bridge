"""Shared, read-only ORDER_ID resolution.

Extracted from ``app.cli._find_payment`` (previously CLI-bound) so the admin
bot's ``/payment`` lookup shares the exact same ambiguity-safe behavior
instead of re-deriving it -- see ``app.cli`` and ``app.adminbot.queries`` for
the two callers. Never mutates, never locks; a plain lookup by bot_order_id
or numeric gateway_order_id.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Payment


class AmbiguousOrderIdError(Exception):
    """Raised by ``find_payment_by_order_id`` when ORDER_ID is a numeric
    string that names TWO DIFFERENT payments at once -- one by
    bot_order_id, another by gateway_order_id. Silently picking either
    risks inspecting or acting on the wrong payment, so callers must
    refuse instead of guessing."""


# Payment.gateway_order_id is a PostgreSQL BIGINT (signed 64-bit) column;
# bot_order_id is an arbitrary string (AGENTS.md: "Bot order_id may be a
# string") that can itself be a longer all-digit value. Binding an
# out-of-range int to that column raises psycopg.errors.NumericValueOutOfRange
# -- an unhandled crash instead of the graceful bot_order_id-only lookup
# below -- so the gateway lookup is skipped entirely for values that cannot
# fit the column, never attempted and caught.
_POSTGRES_BIGINT_MAX = 2**63 - 1


def find_payment_by_order_id(db: Session, order_id: str) -> Payment | None:
    """Look up a payment by bot_order_id, falling back to the numeric
    gateway_order_id -- shared by every command that takes ORDER_ID.

    Raises AmbiguousOrderIdError if ORDER_ID is numeric and matches one
    payment's bot_order_id and a DIFFERENT payment's gateway_order_id."""
    payment = db.execute(
        select(Payment).where(Payment.bot_order_id == order_id)
    ).scalar_one_or_none()
    # str.isdigit() is True for Unicode "digit" characters int() cannot
    # parse (e.g. superscript '²', ValueError) -- bot_order_id's validation
    # pattern permits any non-control Unicode, so this is reachable.
    # str.isdecimal() is exactly the set int() accepts (Unicode category
    # Nd), so it is used here instead.
    if order_id.isdecimal() and int(order_id) <= _POSTGRES_BIGINT_MAX:
        gateway_payment = db.execute(
            select(Payment).where(Payment.gateway_order_id == int(order_id))
        ).scalar_one_or_none()
        if gateway_payment is not None:
            if payment is None:
                payment = gateway_payment
            elif payment.id != gateway_payment.id:
                raise AmbiguousOrderIdError(order_id)
    return payment
