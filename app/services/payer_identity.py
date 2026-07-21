"""Stable, privacy-preserving CentralPay payer identity per customer.

Incident 2026-07 (cross-customer card suggestions): every payment used one
global ``CENTRALPAY_USER_ID`` for the gateway ``userId``, and CentralPay
scopes saved-card suggestions by that value, so all payers shared one payer
identity and one card history. This module derives a STABLE, per-customer
numeric gateway ``userId`` from an upstream-supplied opaque ``customer_id``
so two different customers can never share one gateway payer identity.

Design:

* The raw ``customer_id`` is NEVER stored or logged. The mapping table is
  keyed by a keyed-HMAC ``customer_key_hash`` (non-reversible), and a short
  fingerprint of it is the only customer tag that reaches logs/events.
* ``gateway_user_id`` is derived by keyed HMAC into a positive integer range
  and then STORED. Once stored it is immutable: the same ``customer_id``
  always resolves to the same id, and uniqueness is DB-enforced
  (``UNIQUE(gateway_user_id)``), never assumed probabilistically — a derived
  collision deterministically re-derives with the next attempt counter.
* Because the id is stored, changing any OTHER secret never changes payer
  ids, and existing mappings survive restarts, redeploys, and backup/restore.
* ``CENTRALPAY_PAYER_ID_SECRET`` is dedicated: it is never one of the
  callback HMAC secret, inbound/gateway API keys, bot token, or DB password.
  Rotating it is a deliberate, migration-backed operation (see
  ``docs/incidents/2026-07-centralpay-cross-user-card-suggestions.md``) — it
  is not a routine rotation, because the raw customer_id needed to re-key an
  existing row is intentionally not stored.
"""

import hashlib
import hmac
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_event
from app.exceptions import PayerIdentityAllocationError
from app.models import CentralPayPayerIdentity

# The current derivation scheme. Bumping this (with new domain strings) is an
# explicit, documented scheme change that affects only customers first seen
# afterwards; stored rows keep their version and their gateway_user_id.
DERIVATION_VERSION = 1
_KEY_DOMAIN = "centralpay-payer-key:v1:"
_ID_DOMAIN = "centralpay-payer-id:v1:"

# Range of derived gateway user ids: positive, non-zero, and comfortably below
# 2**31 so the value fits any 32-bit signed id column the gateway might use.
# CentralPay's exact accepted userId range is not documented (see the incident
# report); if it differs, getLink fails closed (recoverable), never leaks.
GATEWAY_USER_ID_MIN = 1
GATEWAY_USER_ID_SPAN = 2_000_000_000  # ids fall in [1, 2_000_000_000]
_MAX_DERIVATION_ATTEMPTS = 10_000

_FINGERPRINT_LENGTH = 12


def customer_key_hash(secret: str, customer_id: str) -> str:
    """Non-reversible, keyed lookup key for a customer.

    The raw ``customer_id`` is never stored; this is the UNIQUE key of
    ``centralpay_payer_identities``. Keyed (not a bare digest) so a database
    leak cannot brute-force low-entropy upstream ids back to customers.
    """
    return hmac.new(
        secret.encode("utf-8"),
        (_KEY_DOMAIN + customer_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def customer_fingerprint(secret: str, customer_id: str) -> str:
    """Short non-reversible tag safe for logs/admin output (never the raw id)."""
    return customer_key_hash(secret, customer_id)[:_FINGERPRINT_LENGTH]


def derive_gateway_user_id(secret: str, customer_id: str, attempt: int) -> int:
    """Deterministic candidate gateway userId for ``(secret, customer_id,
    attempt)``. ``attempt`` is the collision counter; attempt 0 is the value a
    fresh customer normally receives."""
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{_ID_DOMAIN}{customer_id}:{attempt}".encode(),
        hashlib.sha256,
    ).digest()
    return GATEWAY_USER_ID_MIN + (int.from_bytes(digest[:8], "big") % GATEWAY_USER_ID_SPAN)


@dataclass(frozen=True)
class PayerIdentity:
    id: int
    gateway_user_id: int
    derivation_version: int


def _lookup(db: Session, key_hash: str) -> CentralPayPayerIdentity | None:
    return db.execute(
        select(CentralPayPayerIdentity).where(
            CentralPayPayerIdentity.customer_key_hash == key_hash
        )
    ).scalar_one_or_none()


def resolve_payer_identity(
    db: Session,
    *,
    secret: str,
    customer_id: str,
    reserved_gateway_user_id: int | None = None,
) -> PayerIdentity:
    """Return the stable gateway payer identity for ``customer_id``.

    Creates the mapping row on first use inside its own committed transaction
    so the mapping is durable before any payment row or gateway call. Callers
    must have already validated ``customer_id`` and confirmed ``secret`` is
    configured (fail-closed happens in the route).

    ``reserved_gateway_user_id`` (the legacy shared CENTRALPAY_USER_ID) is never
    assigned to a new customer: historical payments used that id WITHOUT a
    mapping row, so ``UNIQUE(gateway_user_id)`` cannot catch a fresh customer
    that HMAC-lands on it — treat it as a collision and re-derive. This keeps
    new customers isolated from the historical shared-id pool too.
    """
    key_hash = customer_key_hash(secret, customer_id)
    existing = _lookup(db, key_hash)
    db.rollback()
    if existing is not None:
        return PayerIdentity(
            existing.id, existing.gateway_user_id, existing.derivation_version
        )

    for attempt in range(_MAX_DERIVATION_ATTEMPTS):
        candidate = derive_gateway_user_id(secret, customer_id, attempt)
        if candidate == reserved_gateway_user_id:
            continue  # never share the legacy shared payer id
        row = CentralPayPayerIdentity(
            customer_key_hash=key_hash,
            gateway_user_id=candidate,
            derivation_version=DERIVATION_VERSION,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            # Either a concurrent request created THIS customer, or the
            # candidate collided with a DIFFERENT customer's stored id.
            concurrent = _lookup(db, key_hash)
            db.rollback()
            if concurrent is not None:
                # Same customer created concurrently: reuse it (stable).
                return PayerIdentity(
                    concurrent.id,
                    concurrent.gateway_user_id,
                    concurrent.derivation_version,
                )
            # gateway_user_id collision with another customer: re-derive.
            continue
        identity = PayerIdentity(row.id, row.gateway_user_id, row.derivation_version)
        record_event(
            db,
            payment_id=None,
            event_type="centralpay_payer_identity_created",
            data={
                "payer_identity_id": row.id,
                "gateway_user_id": row.gateway_user_id,
                "derivation_version": row.derivation_version,
                # Safe: a short non-reversible fingerprint, never the raw id.
                "customer_fingerprint": key_hash[:_FINGERPRINT_LENGTH],
                "derivation_attempts": attempt + 1,
            },
        )
        db.commit()
        return identity
    # Astronomically unlikely with a 2e9 space and DB-enforced uniqueness.
    db.rollback()
    raise PayerIdentityAllocationError()
