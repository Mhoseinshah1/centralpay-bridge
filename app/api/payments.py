"""POST /api/custom-payment — payment creation for the Telegram bot."""

import contextlib
import json
import logging
import re
from typing import Annotated, Any, NoReturn
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StrictInt, StrictStr
from pydantic import ValidationError as PydanticValidationError

from app.api.deps import CentralPayDep, DbDep, SettingsDep
from app.exceptions import AmountOutOfRangeError, InvalidApiKeyError, RateLimitedError
from app.security import constant_time_equals
from app.services.payments import create_payment

logger = logging.getLogger("app.api.payments")

router = APIRouter()


class CreatePaymentRequest(BaseModel):
    """Strict request contract (audit: no silent coercion).

    - api_key: string; never logged, never echoed in errors.
    - amount: JSON **integer** TOMAN. Booleans, floats, and numeric
      strings are rejected, never coerced (bool is a Python int subtype
      and would otherwise coerce True -> 1). The ``le`` bound is an
      absolute schema backstop far above any legitimate payment and far
      below BIGINT; the operational policy bounds are
      MIN/MAX_PAYMENT_AMOUNT_TOMAN, enforced after authentication.
    - order_id: opaque non-empty string, at most 128 characters, no
      control characters and no NUL (NUL previously reached PostgreSQL
      and produced a 500). It is passed through unchanged — never
      trimmed, case-folded, or Unicode-normalized — because the bot
      contract treats it as an opaque identifier.
    """

    api_key: StrictStr
    amount: StrictInt = Field(gt=0, le=1_000_000_000_000, description="Amount in TOMAN")
    order_id: StrictStr = Field(
        min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$"
    )


class CreatePaymentResponse(BaseModel):
    url: str


# --- legacy-body compatibility (fix/custom-payment-legacy-body-compat) --------
#
# Some legacy customer bots do not POST a plain JSON object: they send a JSON
# document encoded as a JSON string, application/x-www-form-urlencoded, or
# text/plain containing JSON. FastAPI's default body binding rejects those with
# a 422 before the route runs. This narrow compatibility decoder normalizes the
# allowed representations into a {api_key, amount, order_id} dict and hands it
# to the SAME strict CreatePaymentRequest model — no field is coerced beyond
# converting an ASCII-decimal amount STRING to int, and the internal
# payment-creation service is never weakened.

# Bounded before any decode/parse; aligns with the Caddy edge limit
# (request_body max_size 64KB). The legitimate body is ~150 bytes.
_MAX_BODY_BYTES = 64 * 1024
_REQUIRED_FIELDS = ("api_key", "amount", "order_id")
# Bound on the number of application/x-www-form-urlencoded pairs accepted from
# an unauthenticated request. The legitimate body carries exactly the three
# required fields; a legacy sender adds at most a handful of unrelated fields.
# 32 leaves generous headroom while capping attacker-controlled fan-out well
# below what the 64 KB size limit alone would allow (~21k blank pairs).
_MAX_FORM_PAIRS = 32
# ASCII decimal only: `[0-9]` never matches Persian/Arabic digits, and re.ASCII
# keeps it strict. No sign, separators, whitespace, exponent, or decimal point.
_ASCII_DECIMAL = re.compile(r"[0-9]+", re.ASCII)


class _CompatReject(Exception):
    """Carries the sanitized representation category up to the top-level parser
    for logging; converted to the project's standard 422 validation error.

    ``detail`` may carry ONLY safe, non-attacker-controlled diagnostics (counts
    and the fixed required-field names) — never a submitted field name or value.
    """

    def __init__(self, category: str, detail: dict[str, Any] | None = None) -> None:
        self.category = category
        self.detail = detail or {}


def _media_type(request: Request) -> str:
    return (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()


async def _read_bounded_body(request: Request) -> bytes:
    """Read the body, aborting before it can exceed _MAX_BODY_BYTES (declared
    Content-Length is checked first; the stream is then capped as a backstop
    for chunked requests)."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > _MAX_BODY_BYTES:
        raise _CompatReject("too_large")
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY_BYTES:
            raise _CompatReject("too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_json_layers(
    raw: bytes, *, object_category: str, string_category: str
) -> tuple[str, dict[str, Any]]:
    """Parse JSON, allowing AT MOST one extra decode layer (a JSON string that
    itself contains one JSON object). Never decodes recursively beyond that."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _CompatReject(object_category) from exc
    try:
        first = json.loads(text)
    except ValueError as exc:
        raise _CompatReject(object_category) from exc
    if isinstance(first, dict):
        return object_category, first
    if isinstance(first, str):
        try:
            second = json.loads(first)
        except ValueError as exc:
            raise _CompatReject(string_category) from exc
        if isinstance(second, dict):
            return string_category, second
        raise _CompatReject(string_category)  # e.g. a string containing an array
    # arrays, numbers, booleans, null, or a bare string
    raise _CompatReject(object_category)


def _decode_urlencoded(raw: bytes) -> tuple[str, dict[str, str]]:
    """Decode a legacy form body, tolerating unrelated extra fields.

    Each of the three required fields must appear **exactly once**; any other
    field is ignored — never collected, never validated, never logged. The
    returned dict contains ONLY the three required fields, so nothing else can
    reach the strict model, the database, or the gateway.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _CompatReject("urlencoded") from exc
    try:
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise _CompatReject("urlencoded") from exc
    # Bound the pair count before inspecting values: unauthenticated,
    # attacker-controlled input must never drive unbounded work.
    total_pairs = len(pairs)
    if total_pairs > _MAX_FORM_PAIRS:
        raise _CompatReject("urlencoded", {"total_pair_count": total_pairs})
    # Count occurrences of the REQUIRED fields only; every extra field is
    # dropped here and its name/value is neither retained nor logged.
    counts = dict.fromkeys(_REQUIRED_FIELDS, 0)
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in counts:
            counts[key] += 1
            values[key] = value
    missing = [field for field in _REQUIRED_FIELDS if counts[field] == 0]
    duplicate = [field for field in _REQUIRED_FIELDS if counts[field] > 1]
    if missing or duplicate:
        # Diagnostics use ONLY the fixed required-field names and counts —
        # never an extra field's (attacker-controlled) name or any value.
        detail: dict[str, Any] = {
            "total_pair_count": total_pairs,
            "extra_field_count": total_pairs - sum(counts.values()),
            "missing_required_fields": missing,
            "duplicate_required_fields": duplicate,
        }
        raise _CompatReject("urlencoded", detail)
    # A NEW dict with ONLY the three required fields, each present exactly once.
    return "urlencoded", {field: values[field] for field in _REQUIRED_FIELDS}


def _decode(media_type: str, raw: bytes) -> tuple[str, dict[str, Any]]:
    # Empty/absent Content-Type is treated as JSON (the historical default) so
    # existing JSON clients that omit the header keep working.
    if media_type in ("application/json", ""):
        return _decode_json_layers(
            raw, object_category="json_object", string_category="json_string_object"
        )
    if media_type == "application/x-www-form-urlencoded":
        return _decode_urlencoded(raw)
    if media_type == "text/plain":
        return _decode_json_layers(
            raw, object_category="text_json", string_category="text_json"
        )
    # multipart/form-data and every other type: unsupported, controlled 422
    # (never a 500). No large multipart dependency is added for this.
    raise _CompatReject("unsupported")


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Keep only the three required fields and convert an ASCII-decimal amount
    STRING to int. Every other type/shape is left untouched for the strict
    model to reject (floats, bools, nested objects, non-ASCII digits, ...)."""
    normalized = {field: data[field] for field in _REQUIRED_FIELDS if field in data}
    amount = normalized.get("amount")
    if isinstance(amount, str) and _ASCII_DECIMAL.fullmatch(amount):
        # An absurdly long digit string trips Python's int-string conversion
        # limit (ValueError); leave it untouched and let the strict model reject.
        with contextlib.suppress(ValueError):
            normalized["amount"] = int(amount)
    return normalized


def _sanitized_validation_error() -> RequestValidationError:
    # The project's standard sanitized 422 (see app.main._validation_error_handler):
    # never carries field values, the raw body, or secrets.
    return RequestValidationError(
        [{"loc": ("body",), "msg": "Invalid request body", "type": "value_error"}]
    )


def _reject(
    category: str,
    media_type: str,
    body_size: int | None,
    detail: dict[str, Any] | None = None,
) -> NoReturn:
    # Pre-auth diagnostic: representation category, content-type, and byte
    # length ONLY — never a field value (in particular never order_id) or the
    # raw body, since this request is unauthenticated and possibly hostile.
    # ``detail`` adds only safe counts / fixed field names (see _CompatReject).
    extra: dict[str, Any] = {
        "representation": category,
        "content_type": media_type,
        "body_size": body_size,
    }
    if detail:
        extra.update(detail)
    logger.warning("custom_payment_body_rejected", extra=extra)
    raise _sanitized_validation_error()


async def parse_create_payment_request(request: Request) -> CreatePaymentRequest:
    """Normalize a legacy request body and validate it with the strict model.

    Async so it can read the body off the event loop while the route stays a
    normal sync function (blocking DB/gateway work does not move onto the loop).
    """
    media_type = _media_type(request)
    try:
        raw = await _read_bounded_body(request)
    except _CompatReject as reject:
        declared = request.headers.get("content-length")
        size = int(declared) if declared and declared.isdigit() else None
        _reject(reject.category, media_type, size, reject.detail)
    body_size = len(raw)
    try:
        representation, data = _decode(media_type, raw)
        normalized = _normalize(data)
    except _CompatReject as reject:
        _reject(reject.category, media_type, body_size, reject.detail)
    # Accepted representation — safe pre-auth observability (no field values).
    logger.info(
        "custom_payment_body_normalized",
        extra={
            "representation": representation,
            "content_type": media_type,
            "body_size": body_size,
        },
    )
    try:
        return CreatePaymentRequest(**normalized)
    except PydanticValidationError:
        _reject("schema_invalid", media_type, body_size)


@router.post("/api/custom-payment", response_model=CreatePaymentResponse)
def create_custom_payment(
    request: Request,
    body: Annotated[CreatePaymentRequest, Depends(parse_create_payment_request)],
    db: DbDep,
    settings: SettingsDep,
    client: CentralPayDep,
) -> CreatePaymentResponse:
    limiters = request.app.state.rate_limiters
    if not settings.inbound_api_key or not constant_time_equals(
        body.api_key, settings.inbound_api_key
    ):
        # The provided key is never logged. Repeated invalid keys hit a
        # strict limiter (credential guessing).
        logger.warning("invalid_inbound_api_key", extra={"bot_order_id": body.order_id})
        if not limiters.check(limiters.invalid_api_key, "invalid_api_key"):
            raise RateLimitedError()
        raise InvalidApiKeyError()
    if not limiters.check(limiters.create, "create_payment"):
        raise RateLimitedError()
    # Logged only AFTER authentication so unauthenticated probes cannot
    # write attacker-chosen order ids into this event stream.
    logger.info(
        "payment_create_requested",
        extra={"bot_order_id": body.order_id, "amount": body.amount},
    )
    # The MINIMUM applies to the ORIGINAL bot amount. The MAXIMUM applies to
    # the final payable amount (original + service fee) and is enforced in
    # the creation service before any snapshot or gateway call — see
    # payable_amount_out_of_range.
    if body.amount < settings.min_payment_amount_toman:
        logger.warning(
            "amount_out_of_range",
            extra={
                "bot_order_id": body.order_id,
                "amount": body.amount,
                "min_amount": settings.min_payment_amount_toman,
            },
        )
        raise AmountOutOfRangeError(
            f"Amount must be at least {settings.min_payment_amount_toman} TOMAN"
        )
    url = create_payment(
        db, client, settings, bot_order_id=body.order_id, amount=body.amount
    )
    return CreatePaymentResponse(url=url)
