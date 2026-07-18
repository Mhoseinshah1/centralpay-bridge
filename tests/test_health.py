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
