"""Typed application exceptions mapped to HTTP responses.

Messages on these exceptions are returned to API callers and logged, so they
must never contain secrets, full card numbers, or full redirect URLs.
"""


class BridgeError(Exception):
    code = "internal_error"
    http_status = 500
    default_message = "Internal error"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class InvalidApiKeyError(BridgeError):
    code = "invalid_api_key"
    http_status = 401
    default_message = "Invalid API key"


class AmountOutOfRangeError(BridgeError):
    code = "amount_out_of_range"
    http_status = 400
    default_message = "Amount is outside the configured payment bounds"


class DuplicateOrderAmountMismatchError(BridgeError):
    code = "duplicate_order_amount_mismatch"
    http_status = 409
    default_message = "Order already exists with a different amount"


class OrderAlreadyVerifiedError(BridgeError):
    code = "order_already_verified"
    http_status = 409
    default_message = "Order has already been paid and verified"


class OrderUnderReviewError(BridgeError):
    code = "order_under_review"
    http_status = 409
    default_message = "Order is under manual review"


class InvalidCallbackSignatureError(BridgeError):
    code = "invalid_callback_signature"
    http_status = 403
    default_message = "Invalid callback signature"


class PaymentNotFoundError(BridgeError):
    code = "payment_not_found"
    http_status = 404
    default_message = "Payment not found"


class VerificationFailedError(BridgeError):
    code = "verification_failed"
    http_status = 409
    default_message = "Payment could not be verified with the gateway"


class CentralPayError(BridgeError):
    """Base for failures talking to CentralPay."""

    code = "centralpay_error"
    http_status = 502
    default_message = "Payment gateway error"


class CentralPayConnectionError(CentralPayError):
    code = "centralpay_connection_error"
    default_message = "Could not reach the payment gateway"


class CentralPayRejectedError(CentralPayError):
    code = "centralpay_rejected"
    default_message = "Payment gateway rejected the request"


class CentralPayInvalidResponseError(CentralPayError):
    code = "centralpay_invalid_response"
    default_message = "Payment gateway returned an invalid response"


class GatewayOrderIdAllocationError(BridgeError):
    code = "gateway_order_id_allocation_failed"
    http_status = 500
    default_message = "Could not allocate a unique gateway order id"
