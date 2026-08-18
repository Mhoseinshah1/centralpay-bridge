"""Trusted client-IP resolution.

Caddy is the ONLY network path that reaches the API's public routes
(deploy/caddy/Caddyfile.template's ``header_up X-Forwarded-For
{remote_host}`` OVERWRITES -- never appends to -- any client-supplied
value, exactly like the existing X-Request-ID handling in
app/middleware.py). Given that single-hop boundary, X-Forwarded-For is
trustworthy IF it parses as exactly one valid IPv4/IPv6 address; anything
else (missing, multiple comma-separated values, malformed) falls back to
the raw ASGI socket peer -- a safe, non-exploitable default that just
means "shares one bucket with every other caller whose header did not
resolve," never a bypass of a per-caller limit.

``rate_limit_trust_proxy_headers`` (default True) gates trusting the
header at all, for a deployment that does not have this app's documented
single-proxy topology.
"""

import ipaddress

from fastapi import Request

from app.config import Settings

# Returned only when neither a trusted header nor the ASGI socket peer
# yields anything (e.g. a non-HTTP test harness with no client info at
# all) -- every caller with no resolvable origin shares this one bucket.
UNKNOWN_CLIENT = "unknown"


def resolve_client_ip(request: Request, settings: Settings) -> str:
    """A stable, non-spoofable-under-the-documented-topology per-caller key.

    Returns the canonical string form of a parsed IP address when
    possible (so e.g. "::1" and "0:0:0:0:0:0:0:1" bucket together), and a
    raw fallback string otherwise (e.g. Starlette TestClient's
    "testclient" placeholder) -- never raises.
    """
    if settings.rate_limit_trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded is not None:
            try:
                parsed = ipaddress.ip_address(forwarded.strip())
            except ValueError:
                pass  # missing, multi-value, or malformed -- fall through
            else:
                return str(parsed)
    client = request.client
    if client is None:
        return UNKNOWN_CLIENT
    try:
        parsed = ipaddress.ip_address(client.host)
    except ValueError:
        return client.host
    return str(parsed)
