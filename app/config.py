"""Application configuration loaded from environment variables."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = Field(
        default="postgresql+psycopg://centralpay:centralpay@localhost:5432/centralpay",
        description="SQLAlchemy database URL; PostgreSQL is required in production.",
    )

    public_base_url: str = Field(
        description="Public HTTPS base URL of this bridge, used to build CentralPay return URLs.",
    )

    # Secrets. Minimum lengths guard against accidentally running with a
    # placeholder or truncated value; the installer generates long random values.
    inbound_api_key: str = Field(min_length=16)
    callback_hmac_secret: str = Field(min_length=16)

    centralpay_base_url: str = "https://centralapi.org/webservice/basic"
    centralpay_getlink_api_key: str = Field(min_length=1)
    centralpay_verify_api_key: str = Field(min_length=1)
    centralpay_user_id: int = Field(gt=0)
    centralpay_timeout_seconds: float = Field(default=15.0, gt=0)
