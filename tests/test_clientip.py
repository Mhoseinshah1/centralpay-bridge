"""app.clientip.resolve_client_ip: trusted-proxy client-IP resolution.

Caddy is the only network path that reaches the API's public routes and
its Caddyfile OVERWRITES X-Forwarded-For with its own resolved peer (see
RATE_LIMITING_ARCHITECTURE.md §3) -- these tests prove the resolver's
side of that contract: it trusts a single valid IP, never a spoofed or
ambiguous value, and degrades safely rather than raising.
"""

from starlette.datastructures import Address
from starlette.requests import Request

from app.clientip import UNKNOWN_CLIENT, resolve_client_ip


def _request(
    *, headers: dict[str, str] | None = None, client_host: str | None = "203.0.113.9"
) -> Request:
    raw_headers = [
        (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": raw_headers,
        "client": (client_host, 12345) if client_host is not None else None,
    }
    return Request(scope)


def test_trusted_single_ipv4_header_is_used(settings):
    trusting = settings.model_copy(update={"rate_limit_trust_proxy_headers": True})
    request = _request(headers={"X-Forwarded-For": "198.51.100.7"})
    assert resolve_client_ip(request, trusting) == "198.51.100.7"


def test_trusted_single_ipv6_header_is_used(settings):
    trusting = settings.model_copy(update={"rate_limit_trust_proxy_headers": True})
    request = _request(headers={"X-Forwarded-For": "2001:db8::1"})
    assert resolve_client_ip(request, trusting) == "2001:db8::1"


def test_ipv6_normalizes_to_canonical_form(settings):
    """A real, distinguishable IPv6 address must key the SAME bucket no
    matter which equivalent textual form a header carries."""
    trusting = settings.model_copy(update={"rate_limit_trust_proxy_headers": True})
    compressed = _request(headers={"X-Forwarded-For": "::1"})
    expanded = _request(headers={"X-Forwarded-For": "0:0:0:0:0:0:0:1"})
    assert resolve_client_ip(compressed, trusting) == resolve_client_ip(expanded, trusting)


def test_multi_hop_header_is_not_trusted_falls_back_to_socket_peer(settings):
    """A spoofed first hop ('1.2.3.4, <real-peer>') must never be trusted --
    Caddy's own (overwriting) configuration never produces more than one
    value, so more than one is treated as untrustworthy, not parsed."""
    trusting = settings.model_copy(update={"rate_limit_trust_proxy_headers": True})
    request = _request(
        headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.9"}, client_host="203.0.113.9"
    )
    resolved = resolve_client_ip(request, trusting)
    assert resolved == "203.0.113.9"
    assert resolved != "1.2.3.4"


def test_malformed_header_falls_back_safely(settings):
    trusting = settings.model_copy(update={"rate_limit_trust_proxy_headers": True})
    for garbage in ("not-an-ip", "", "   ", "1.2.3.999", "'; DROP TABLE payments; --"):
        request = _request(headers={"X-Forwarded-For": garbage}, client_host="203.0.113.9")
        assert resolve_client_ip(request, trusting) == "203.0.113.9"


def test_missing_header_falls_back_to_socket_peer(settings):
    trusting = settings.model_copy(update={"rate_limit_trust_proxy_headers": True})
    request = _request(headers={}, client_host="203.0.113.9")
    assert resolve_client_ip(request, trusting) == "203.0.113.9"


def test_trust_disabled_ignores_header_even_when_valid(settings):
    """rate_limit_trust_proxy_headers=False is the escape hatch for a
    deployment without this app's documented single-hop-Caddy guarantee:
    the header must never be consulted at all, even when it parses fine."""
    distrusting = settings.model_copy(update={"rate_limit_trust_proxy_headers": False})
    request = _request(
        headers={"X-Forwarded-For": "198.51.100.7"}, client_host="203.0.113.9"
    )
    assert resolve_client_ip(request, distrusting) == "203.0.113.9"


def test_no_socket_peer_and_no_header_returns_unknown_sentinel(settings):
    trusting = settings.model_copy(update={"rate_limit_trust_proxy_headers": True})
    request = _request(headers={}, client_host=None)
    assert resolve_client_ip(request, trusting) == UNKNOWN_CLIENT


def test_non_ip_socket_peer_used_as_is(settings):
    """Starlette's TestClient reports a fixed non-IP placeholder host
    ('testclient') -- the resolver must not crash on it, just pass it
    through as a stable (if not a real IP) bucket key."""
    trusting = settings.model_copy(update={"rate_limit_trust_proxy_headers": True})
    request = _request(headers={}, client_host="testclient")
    assert resolve_client_ip(request, trusting) == "testclient"


def test_starlette_address_object_client_is_supported(settings):
    """Some ASGI servers populate scope["client"] via an Address-like
    object rather than a plain tuple; Starlette normalizes this, but the
    resolver must work either way."""
    trusting = settings.model_copy(update={"rate_limit_trust_proxy_headers": True})
    request = _request(headers={}, client_host="203.0.113.9")
    assert isinstance(request.client, Address)
    assert resolve_client_ip(request, trusting) == "203.0.113.9"
