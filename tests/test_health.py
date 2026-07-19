from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def test_health_live(client):
    from app.version import APP_VERSION

    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive", "version": APP_VERSION}
    # Semantic versioning, pre-release until first stable.
    assert APP_VERSION != "1.0.0"


def test_health_ready_success(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


def test_health_ready_database_failure(app, client):
    broken_engine = create_engine("sqlite:////nonexistent-dir-a8f3/health.db")
    app.state.session_factory = sessionmaker(bind=broken_engine)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "error"}


# --- application metadata version --------------------------------------------


def test_fastapi_metadata_reports_app_version(app):
    """The FastAPI application metadata tracks APP_VERSION; the factory
    previously hardcoded 0.1.0 (version drift)."""
    from app.version import APP_VERSION

    assert app.version == APP_VERSION


def test_health_and_app_metadata_stay_synchronized(app, client):
    from app.version import APP_VERSION

    live = client.get("/health/live").json()
    assert live["version"] == APP_VERSION
    assert live["version"] == app.version


def test_no_runtime_source_hardcodes_a_version_literal():
    """The only version literal in runtime sources lives in app/version.py —
    nothing may reintroduce the stale FastAPI version="0.1.0" (or any other
    duplicated version string)."""
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent.parent / "app"
    for source in app_dir.rglob("*.py"):
        text = source.read_text()
        assert 'version="0.1.0"' not in text, source
        if source.name != "version.py":
            assert "0.1.0" not in text, source


def test_api_documentation_endpoints_remain_disabled(app, client):
    """Setting version=APP_VERSION must not resurrect the docs surface."""
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
