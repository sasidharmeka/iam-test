"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Configuration shared by the API, agents, and data adapters."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    google_cloud_project: str = "rapid-stage-506406-t4"
    google_cloud_location: str = "us-central1"
    gemini_model: str = "gemini-2.5-flash"

    bigquery_project: str = "rapid-stage-506406-t4"
    bigquery_dataset: str = "iam_data_dev"
    bigquery_rows_per_table: int = Field(default=10, ge=1, le=50)

    database_url: str = "sqlite+pysqlite:///./iam_agent.db"
    authorization_config: Path = REPOSITORY_ROOT / "config" / "authorization.yaml"
    identity_directory_config: Path = REPOSITORY_ROOT / "config" / "identities.yaml"
    policy_directory: Path = REPOSITORY_ROOT / "policies"

    verify_google_chat_token: bool = False
    google_chat_audience: str | None = None
    google_chat_service_account: str = "chat@system.gserviceaccount.com"

    # Comma-separated Google Workspace principals. When any of these are set,
    # they replace the example grants in config/authorization.yaml at runtime.
    chat_analyst_principals: str = ""
    chat_approver_principals: str = ""
    chat_admin_principals: str = ""
    chat_default_scope: str = "/company"

    @field_validator("bigquery_project", "bigquery_dataset")
    @classmethod
    def validate_bigquery_identifier(cls, value: str) -> str:
        allowed = value.replace("-", "").replace("_", "")
        if not allowed.isalnum():
            raise ValueError("BigQuery identifiers may contain letters, digits, '-' and '_'")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
