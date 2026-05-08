from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Application
    app_env: str = "development"
    log_level: str = "INFO"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # PostgreSQL
    database_url: str = "postgresql://tier1user:secret@localhost:5432/assessments"

    # Object Storage
    storage_endpoint_url: str = "http://localhost:9000"
    storage_access_key: str = "minioadmin"
    storage_secret_key: str = "minioadmin"
    storage_bucket: str = "submissions"
    storage_presigned_url_expiry: int = 3600

    # Assessment API
    assessment_api_base_url: str = "http://localhost:8000"
    assessment_service_token: str = ""

    # Tier 2 DIFY
    tier2_webhook_url: str = ""
    tier2_dify_token: str = ""

    # Sandbox
    sandbox_image: str = "python:3.11-slim"
    sandbox_timeout_seconds: int = 120
    sandbox_memory_limit: int = 536_870_912   # 512 MB
    sandbox_cpu_quota: int = 50_000           # 50% of one CPU
    sandbox_network_disabled: bool = True
    sandbox_working_dir: str = "/workspace"

    # Checks
    max_file_size_mb: int = 50
    secret_scan_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
