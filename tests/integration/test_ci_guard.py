"""Final financial audit: integration tests must never silently skip in CI.

The PostgreSQL-marked suites skip locally when TEST_DATABASE_URL is not
set — acceptable on developer machines, but in CI a missing database
would silently drop every financial integration proof. This test carries
no postgres marker, so it always runs, and it fails the build if CI lacks
the database configuration.
"""

import os


def test_financial_integration_tests_cannot_silently_skip_in_ci():
    if not os.environ.get("CI"):
        return  # local run without CI semantics; the skip markers may apply
    url = os.environ.get("TEST_DATABASE_URL", "")
    assert url.startswith("postgresql"), (
        "CI must set TEST_DATABASE_URL to a PostgreSQL database; otherwise every "
        "financial integration test (concurrency, fault injection, backup/restore) "
        "silently skips and the build proves nothing."
    )
