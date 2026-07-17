"""FastAPI dependencies backed by application state."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.centralpay import CentralPayClient
from app.config import Settings


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_db(request: Request) -> Iterator[Session]:
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_centralpay(request: Request) -> CentralPayClient:
    client: CentralPayClient = request.app.state.centralpay
    return client


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[Session, Depends(get_db)]
CentralPayDep = Annotated[CentralPayClient, Depends(get_centralpay)]
