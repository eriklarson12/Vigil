"""All configuration. Nothing else in the codebase reads os.environ."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://vigil:vigil@localhost:5433/vigil"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_fallback_model: str = "gemini-2.5-flash-lite"
    llm_mode: str = "auto"  # auto | gemini | fake
    llm_daily_budget: int = 200
    embedding_model: str = "gemini-embedding-001"
    embedding_dims: int = 768
    embeddings_mode: str = "auto"  # auto | gemini | fake

    github_mode: str = "fixture"  # live | fixture
    github_token: str = ""
    github_fixtures_dir: str = "tests/fixtures/github"
    llm_fixtures_dir: str = "tests/fixtures/llm"

    slack_mode: str = "mock"  # mock | webhook
    slack_webhook_url: str = ""
    slack_bot_token: str = ""
    slack_channel: str = "#incidents"
    slack_signing_secret: str = ""

    alertmanager_webhook_token: str = "dev-token"
    resume_token: str = "dev-token"

    dashboard_url: str = "http://localhost:5173"
    services_file: str = "services.yaml"
    commit_lookback_hours: int = 48
    commit_candidate_cap: int = 50
    commit_top_k: int = 3
    commit_score_floor: float = 0.15
    confidence_floor: float = 0.4
    stale_claim_minutes: int = 10
    incident_grouping_minutes: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
