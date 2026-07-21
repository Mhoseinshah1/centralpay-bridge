"""Stable, privacy-preserving CentralPay payer identity per end user.

Incident 2026-07 (cross-customer card suggestions): every payment used one
global ``CENTRALPAY_USER_ID`` for the gateway ``userId``, and CentralPay
scopes saved-card suggestions by that value, so all payers shared one payer
identity and one card history. This module derives a STABLE, per-identity
numeric gateway ``userId`` so two different end users can never share one
gateway payer identity.

Identity scopes (compatible with the upstream mirza-cpanel bot, which sends
an OPTIONAL Telegram user id under one of several aliases):

* ``telegram_user`` — a valid positive Telegram numeric id was supplied.
  Identity key ``tg:<id>``: the same Telegram user always maps to the same
  gateway id across orders; different users always map to different ids.
* ``order_fallback`` — no usable identity was supplied. Identity key
  ``order:<bot_order_id>``: stable across retries of the same order, distinct
  across different orders, so at worst one order shares nothing with any
  other order. The legacy shared id is NEVER used for new links.

The two key prefixes cannot collide: ``tg:`` values are pure ASCII digits
while ``order:`` values carry the opaque bot order id under a different
prefix.

Design:

* Raw identity values are NEVER stored in the mapping table and raw Telegram
  ids are never logged or written to audit events. The table is keyed by a
  keyed-HMAC ``customer_key_hash`` of the scoped identity key
  (non-reversible), and a short fingerprint of it is the only identity tag
  that reaches logs/events. (``bot_order_id`` itself remains stored/logged
  elsewhere as the documented idempotency key.)
* ``gateway_user_id`` is derived by keyed HMAC into a positive integer range
  and then STORED. Once stored it is immutable: the same identity key always
  resolves to the same id, and uniqueness is DB-enforced
  (``UNIQUE(gateway_user_id)``), never assumed probabilistically — a derived
  collision deterministically re-derives with the next attempt counter.
* Because the id is stored, changing any OTHER secret never changes payer
  ids, and existing mappings survive restarts, redeploys, and backup/restore.
* ``CENTRALPAY_PAYER_ID_SECRET`` is dedicated: it is never one of the
  callback HMAC secret, inbound/gateway API keys, bot token, or DB password.
  Rotating it is a deliberate, migration-backed operation (see
  ``docs/incidents/2026-07-centralpay-cross-user-card-suggestions.md``) — it
  is not a routine rotation, because the raw identity needed to re-key an
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

# Identity scopes persisted on payments.payer_identity_type.
IDENTITY_TYPE_TELEGRAM_USER = "telegram_user"
IDENTITY_TYPE_ORDER_FALLBACK = "order_fallback"

# The current derivation scheme. Bumping this (with new domain strings) is an
# explicit, documented scheme change that affects only identities first seen
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


def telegram_identity_key(telegram_user_id: int) -> str:
    """Scoped identity key for a Telegram end user (raw id never stored/logged
    beyond this in-memory key)."""
    return f"tg:{telegram_user_id}"


def order_identity_key(bot_order_id: str) -> str:
    """Scoped identity key for the per-order fallback (no user identity
    supplied). Stable across retries of one order; distinct across orders."""
    return f"order:{bot_order_id}"


def identity_key_hash(secret: str, identity_key: str) -> str:
    """Non-reversible, keyed lookup key for an identity.

    The raw identity value is never stored; this is the UNIQUE key of
    ``centralpay_payer_identities``. Keyed (not a bare digest) so a database
    leak cannot brute-force low-entropy identities (Telegram ids are small
    integers) back to users.
    """
    return hmac.new(
        secret.encode("utf-8"),
        (_KEY_DOMAIN + identity_key).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def identity_fingerprint(secret: str, identity_key: str) -> str:
    """Short non-reversible tag safe for logs/admin output (never the raw id)."""
    return identity_key_hash(secret, identity_key)[:_FINGERPRINT_LENGTH]


def derive_gateway_user_id(secret: str, identity_key: str, attempt: int) -> int:
    """Deterministic candidate gateway userId for ``(secret, identity_key,
    attempt)``. ``attempt`` is the collision counter; attempt 0 is the value a
    fresh identity normally receives."""
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{_ID_DOMAIN}{identity_key}:{attempt}".encode(),
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
    identity_key: str,
    reserved_gateway_user_id: int | None = None,
) -> PayerIdentity:
    """Return the stable gateway payer identity for ``identity_key``.

    Creates the mapping row on first use inside its own committed transaction
    so the mapping is durable before any payment row or gateway call. Callers
    must have already validated the identity and confirmed ``secret`` is
    configured (fail-closed happens in the route).

    ``reserved_gateway_user_id`` (the legacy shared CENTRALPAY_USER_ID) is never
    assigned to a new identity: historical payments used that id WITHOUT a
    mapping row, so ``UNIQUE(gateway_user_id)`` cannot catch a fresh identity
    that HMAC-lands on it — treat it as a collision and re-derive. This keeps
    new identities isolated from the historical shared-id pool too.
    """
    key_hash = identity_key_hash(secret, identity_key)
    existing = _lookup(db, key_hash)
    db.rollback()
    if existing is not None:
        return PayerIdentity(
            existing.id, existing.gateway_user_id, existing.derivation_version
        )

    for attempt in range(_MAX_DERIVATION_ATTEMPTS):
        candidate = derive_gateway_user_id(secret, identity_key, attempt)
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
            # Either a concurrent request created THIS identity, or the
            # candidate collided with a DIFFERENT identity's stored id.
            concurrent = _lookup(db, key_hash)
            db.rollback()
            if concurrent is not None:
                # Same identity created concurrently: reuse it (stable).
                return PayerIdentity(
                    concurrent.id,
                    concurrent.gateway_user_id,
                    concurrent.derivation_version,
                )
            # gateway_user_id collision with another identity: re-derive.
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
                "identity_fingerprint": key_hash[:_FINGERPRINT_LENGTH],
                "derivation_attempts": attempt + 1,
            },
        )
        db.commit()
        return identity
    # Astronomically unlikely with a 2e9 space and DB-enforced uniqueness.
    db.rollback()
    raise PayerIdentityAllocationError()
