"""Stable CentralPay payer identity per end user (incident 2026-07).

Incident recap: every payment used one global ``CENTRALPAY_USER_ID`` as the
gateway ``userId`` and CentralPay scopes saved-card suggestions by that value,
so all payers shared one card history. Payments are now isolated per end user.

Identity schemes (explicit, persisted — never inferred from the number):

* ``telegram_raw_v1`` — a valid positive Telegram numeric id was supplied.
  BY EXPLICIT PRODUCT REQUIREMENT (matching the reference mirza-cpanel
  behavior) the gateway ``userId`` IS the exact Telegram id: no hashing,
  remapping, truncation, modulo, or alternate allocation. The same Telegram
  user therefore gets the same ``userId`` across every order.
* ``order_hmac_v1`` — no usable identity was supplied. A keyed-HMAC id is
  derived per ``bot_order_id`` inside a RESERVED numeric range that cannot
  overlap any valid Telegram id (see the range notes below): stable across
  retries of one order, isolated from every other order.
* ``historical_hmac_v1`` — rows created by the retired keyed-HMAC schemes
  (customer_id-era and the v1 tg/order derivation). Kept immutable so
  existing payments and live links keep verifying against their stored
  snapshots; never re-keyed, never reinterpreted.

The legacy shared id is NEVER used for a new link under any scheme.

Numeric ranges (namespace collision protection):

* Telegram ids: the Bot API documents user ids as positive integers with at
  most 52 significant bits, i.e. ``1 .. 2**52 - 1``. Anything outside that
  range is not accepted as a Telegram identity (it falls back to the order
  scheme at the parse layer).
* Order-fallback ids: derived into ``[ORDER_FALLBACK_MIN,
  ORDER_FALLBACK_MIN + ORDER_FALLBACK_SPAN)``, which starts strictly above
  ``2**52``; module-level assertions enforce the disjointness, so a derived
  fallback id can never equal a valid Telegram id by construction.
* CentralPay's accepted ``userId`` range is undocumented
  (``CENTRALPAY_CONTRACT_ASSUMPTIONS.md``). Both raw Telegram ids (currently
  up to ~1e10, documented < 2**52) and the reserved fallback range require the
  gateway to accept 64-bit integers; if it rejects a value, ``getLink`` fails
  closed (recoverable 502) — an id is never silently altered to fit.

Collision policy: ``UNIQUE(gateway_user_id)`` stays DB-enforced. An order
derivation that collides re-derives deterministically with the next attempt
counter. A RAW TELEGRAM id is never re-derived: if its numeric value is
already owned by a different identity (a historical HMAC row, or the legacy
shared id), resolution FAILS CLOSED with an actionable error — one user is
never handed another identity's mapping and a Telegram id is never replaced
with a different number.

Privacy: the mapping table is keyed by a keyed-HMAC of the scoped identity
key (non-reversible), and only the identity scope plus a short fingerprint
reach logs/audit events. ``gateway_user_id`` now intentionally CONTAINS the
raw Telegram id for ``telegram_raw_v1`` rows (explicit product requirement),
so audit events and logs no longer carry ``gateway_user_id`` values at all.

``CENTRALPAY_PAYER_ID_SECRET`` remains dedicated and is still not a routine
rotation secret: it keys the lookup hashes and the fallback derivation, and
rotating it orphans existing mappings (see the incident doc).
"""

import hashlib
import hmac
from dataclasses import dataclass
from typing import NoReturn

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_event
from app.exceptions import PayerIdentityAllocationError, PayerIdentityCollisionError
from app.models import CentralPayPayerIdentity

# Identity scopes persisted on payments.payer_identity_type.
IDENTITY_TYPE_TELEGRAM_USER = "telegram_user"
IDENTITY_TYPE_ORDER_FALLBACK = "order_fallback"

# Identity schemes persisted on centralpay_payer_identities.identity_scheme.
# Explicit — the scheme is never inferred from the numeric value.
IDENTITY_SCHEME_TELEGRAM_RAW = "telegram_raw_v1"
IDENTITY_SCHEME_ORDER_HMAC = "order_hmac_v1"
IDENTITY_SCHEME_HISTORICAL_HMAC = "historical_hmac_v1"

# The current derivation scheme version, stored on new mapping rows and
# snapshotted on payments. Version 1 (retired): keyed-HMAC ids for every
# scope. Version 2: raw Telegram ids + reserved-range order fallback.
DERIVATION_VERSION = 2
_KEY_DOMAIN = "centralpay-payer-key:v2:"
_ID_DOMAIN = "centralpay-payer-id:v2:"
# Retired v1 key domain — kept ONLY to recognize existing v1 mapping rows so
# retries of historical payments reuse their stored snapshot (never re-keyed).
_HISTORICAL_KEY_DOMAIN = "centralpay-payer-key:v1:"

# Bot API: Telegram user ids are positive and have at most 52 significant
# bits. Values outside [1, MAX_TELEGRAM_USER_ID] are never accepted as a
# Telegram identity.
MAX_TELEGRAM_USER_ID = 2**52 - 1

# Reserved order-fallback range: strictly above every valid Telegram id, well
# within int64. Disjointness is asserted below, not assumed.
ORDER_FALLBACK_MIN = 6_000_000_000_000_000
ORDER_FALLBACK_SPAN = 1_000_000_000_000  # ids in [6e15, 6.001e15)
_MAX_DERIVATION_ATTEMPTS = 10_000

assert ORDER_FALLBACK_MIN > MAX_TELEGRAM_USER_ID  # namespace disjointness
assert ORDER_FALLBACK_MIN + ORDER_FALLBACK_SPAN < 2**63  # fits BIGINT

_FINGERPRINT_LENGTH = 12


def telegram_identity_key(telegram_user_id: int) -> str:
    """Scoped identity key for a Telegram end user."""
    return f"tg:{telegram_user_id}"


def order_identity_key(bot_order_id: str) -> str:
    """Scoped identity key for the per-order fallback (no user identity
    supplied). Stable across retries of one order; distinct across orders."""
    return f"order:{bot_order_id}"


def identity_key_hash(secret: str, identity_key: str) -> str:
    """Non-reversible, keyed lookup key for an identity (current scheme).

    The raw identity value is never stored in this hash; it is the UNIQUE key
    of ``centralpay_payer_identities``. Keyed (not a bare digest) so a
    database leak cannot brute-force low-entropy identities back to users.
    """
    return hmac.new(
        secret.encode("utf-8"),
        (_KEY_DOMAIN + identity_key).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def historical_identity_key_hash(secret: str, identity_key: str) -> str:
    """The RETIRED v1 lookup hash for ``identity_key``.

    Used only to recognize that an existing payment's mapping row was created
    for the SAME identity under the retired derivation, so its stored snapshot
    is reused (never re-keyed, never re-derived, never rejected as another
    user)."""
    return hmac.new(
        secret.encode("utf-8"),
        (_HISTORICAL_KEY_DOMAIN + identity_key).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def identity_fingerprint(secret: str, identity_key: str) -> str:
    """Short non-reversible tag safe for logs/admin output (never the raw id)."""
    return identity_key_hash(secret, identity_key)[:_FINGERPRINT_LENGTH]


def derive_order_gateway_user_id(secret: str, identity_key: str, attempt: int) -> int:
    """Deterministic order-fallback candidate inside the reserved range.

    ``attempt`` is the collision counter; attempt 0 is the value a fresh
    identity normally receives. Every candidate is >= ORDER_FALLBACK_MIN, so
    it can never equal a valid Telegram id."""
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{_ID_DOMAIN}{identity_key}:{attempt}".encode(),
        hashlib.sha256,
    ).digest()
    return ORDER_FALLBACK_MIN + (int.from_bytes(digest[:8], "big") % ORDER_FALLBACK_SPAN)


@dataclass(frozen=True)
class PayerIdentity:
    id: int
    gateway_user_id: int
    derivation_version: int
    scheme: str


def _lookup(db: Session, key_hash: str) -> CentralPayPayerIdentity | None:
    return db.execute(
        select(CentralPayPayerIdentity).where(
            CentralPayPayerIdentity.customer_key_hash == key_hash
        )
    ).scalar_one_or_none()


def _as_identity(row: CentralPayPayerIdentity) -> PayerIdentity:
    return PayerIdentity(
        row.id, row.gateway_user_id, row.derivation_version, row.identity_scheme
    )


def _record_identity_created(
    db: Session, row: CentralPayPayerIdentity, key_hash: str, attempts: int
) -> None:
    # Deliberately WITHOUT gateway_user_id: for telegram_raw_v1 that value IS
    # the raw Telegram id, which never enters logs or audit events.
    record_event(
        db,
        payment_id=None,
        event_type="centralpay_payer_identity_created",
        data={
            "payer_identity_id": row.id,
            "identity_scheme": row.identity_scheme,
            "derivation_version": row.derivation_version,
            "identity_fingerprint": key_hash[:_FINGERPRINT_LENGTH],
            "derivation_attempts": attempts,
        },
    )


def _fail_closed_collision(
    db: Session, *, key_hash: str, occupied_by: CentralPayPayerIdentity | None
) -> NoReturn:
    """A raw Telegram id's numeric value is already owned by a different
    identity (historical HMAC row) or is the reserved legacy shared id. Never
    remap the Telegram user to another number and never hand over the existing
    mapping: record an actionable operator event and refuse."""
    record_event(
        db,
        payment_id=None,
        event_type="payer_identity_collision",
        level="error",
        data={
            # Actionable for operators (locate the row by id); no raw id.
            "identity_fingerprint": key_hash[:_FINGERPRINT_LENGTH],
            "requested_scheme": IDENTITY_SCHEME_TELEGRAM_RAW,
            "occupied_by_payer_identity_id": occupied_by.id if occupied_by else None,
            "occupied_by_scheme": occupied_by.identity_scheme if occupied_by else None,
            "occupied_by_reserved_legacy_id": occupied_by is None,
        },
    )
    db.commit()
    raise PayerIdentityCollisionError()


def _resolve_telegram_raw(
    db: Session,
    *,
    secret: str,
    telegram_user_id: int,
    reserved_gateway_user_id: int | None,
) -> PayerIdentity:
    if not (1 <= telegram_user_id <= MAX_TELEGRAM_USER_ID):
        # The parse layer enforces this; reaching here is a programming error.
        raise PayerIdentityAllocationError("telegram id out of documented range")
    key_hash = identity_key_hash(secret, telegram_identity_key(telegram_user_id))
    existing = _lookup(db, key_hash)
    db.rollback()
    if existing is not None:
        return _as_identity(existing)
    if telegram_user_id == reserved_gateway_user_id:
        # The user's real id equals the legacy SHARED id: sending it would
        # attach the shared multi-user card history to this user. Fail closed.
        _fail_closed_collision(db, key_hash=key_hash, occupied_by=None)
    row = CentralPayPayerIdentity(
        customer_key_hash=key_hash,
        gateway_user_id=telegram_user_id,  # the exact id — never altered
        derivation_version=DERIVATION_VERSION,
        identity_scheme=IDENTITY_SCHEME_TELEGRAM_RAW,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        concurrent = _lookup(db, key_hash)
        db.rollback()
        if concurrent is not None:
            # The same Telegram user was created concurrently: reuse.
            return _as_identity(concurrent)
        # The numeric value is owned by a DIFFERENT identity's mapping.
        occupied = db.execute(
            select(CentralPayPayerIdentity).where(
                CentralPayPayerIdentity.gateway_user_id == telegram_user_id
            )
        ).scalar_one_or_none()
        db.rollback()
        _fail_closed_collision(db, key_hash=key_hash, occupied_by=occupied)
    identity = _as_identity(row)
    _record_identity_created(db, row, key_hash, attempts=1)
    db.commit()
    return identity


def _resolve_order_hmac(
    db: Session,
    *,
    secret: str,
    identity_key: str,
    reserved_gateway_user_id: int | None,
) -> PayerIdentity:
    key_hash = identity_key_hash(secret, identity_key)
    existing = _lookup(db, key_hash)
    db.rollback()
    if existing is not None:
        return _as_identity(existing)

    for attempt in range(_MAX_DERIVATION_ATTEMPTS):
        candidate = derive_order_gateway_user_id(secret, identity_key, attempt)
        if candidate == reserved_gateway_user_id:
            continue  # never share the legacy shared payer id (belt-and-braces)
        row = CentralPayPayerIdentity(
            customer_key_hash=key_hash,
            gateway_user_id=candidate,
            derivation_version=DERIVATION_VERSION,
            identity_scheme=IDENTITY_SCHEME_ORDER_HMAC,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            concurrent = _lookup(db, key_hash)
            db.rollback()
            if concurrent is not None:
                return _as_identity(concurrent)  # created concurrently: reuse
            continue  # candidate collided with another identity: re-derive
        identity = _as_identity(row)
        _record_identity_created(db, row, key_hash, attempts=attempt + 1)
        db.commit()
        return identity
    db.rollback()
    raise PayerIdentityAllocationError()


def resolve_payer_identity(
    db: Session,
    *,
    secret: str,
    identity_key: str,
    telegram_user_id: int | None = None,
    reserved_gateway_user_id: int | None = None,
) -> PayerIdentity:
    """Return the stable gateway payer identity for ``identity_key``.

    ``telegram_user_id`` selects the scheme: when given (a validated positive
    Telegram id) the identity resolves under ``telegram_raw_v1`` and the
    gateway id IS that exact number; otherwise ``order_hmac_v1`` derives into
    the reserved fallback range. Creates the mapping row on first use inside
    its own committed transaction so the mapping is durable before any payment
    row or gateway call; callers must resolve BEFORE taking the payment row
    lock. ``reserved_gateway_user_id`` (the legacy shared CENTRALPAY_USER_ID)
    is never assigned: an order derivation skips it, and a Telegram id equal
    to it fails closed (see ``_fail_closed_collision``).
    """
    if telegram_user_id is not None:
        return _resolve_telegram_raw(
            db,
            secret=secret,
            telegram_user_id=telegram_user_id,
            reserved_gateway_user_id=reserved_gateway_user_id,
        )
    return _resolve_order_hmac(
        db,
        secret=secret,
        identity_key=identity_key,
        reserved_gateway_user_id=reserved_gateway_user_id,
    )
