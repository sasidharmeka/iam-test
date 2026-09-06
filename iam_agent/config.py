"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Configuration for the read-only API, Gemini, MCP, and BigQuery layers."""

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
    query_max_rows: int = Field(default=500, ge=1, le=5000)
    query_max_bytes_billed: int = Field(default=500_000_000, ge=1_000_000)
    query_max_lookback_days: int = Field(default=90, ge=1, le=3660)
    query_max_steps: int = Field(default=6, ge=1, le=8)

    authorization_config: Path = REPOSITORY_ROOT / "config" / "authorization.yaml"
    identity_directory_config: Path = REPOSITORY_ROOT / "config" / "identities.yaml"
    semantic_catalog_config: Path = REPOSITORY_ROOT / "config" / "semantic_catalog.yaml"
    policy_directory: Path = REPOSITORY_ROOT / "policies"

    conversation_max_sessions: int = Field(default=250, ge=1, le=5000)
    conversation_max_turns: int = Field(default=6, ge=1, le=20)

    verify_google_chat_token: bool = False
    google_chat_audience: str | None = None
    google_chat_service_account: str = "chat@system.gserviceaccount.com"

    enable_web_ui: bool = False
    web_google_client_id: str = ""
    web_analyst_principals: str = ""

    chat_analyst_principals: str = ""
    query_default_scope: str = "/company"

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
