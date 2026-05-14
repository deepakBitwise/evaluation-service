from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_env:   str = "development"
    log_level: str = "INFO"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # PostgreSQL
    database_url: str = "postgresql://tier1user:secret@localhost:5432/assessments"

    # Object Storage
    storage_endpoint_url:         str = "http://localhost:9000"
    storage_access_key:           str = "minioadmin"
    storage_secret_key:           str = "minioadmin"
    storage_bucket:               str = "submissions"
    storage_presigned_url_expiry: int = 3600

    # Assessment API
    assessment_api_base_url:  str = "http://localhost:8000"
    assessment_service_token: str = ""

    # Tier 2 DIFY
    tier2_webhook_url: str = ""
    tier2_dify_token:  str = ""

    # Sandbox (Docker)
    sandbox_image:           str = "tier1-sandbox:latest"
    sandbox_timeout_seconds: int = 900
    sandbox_memory_limit:    int = 536_870_912
    sandbox_cpu_quota:       int = 50_000
    sandbox_working_dir:     str = "/workspace"
    sandbox_pip_timeout:     int = 120

    # Checks
    max_file_size_mb:    int  = 50
    secret_scan_enabled: bool = True

    # ── Platform test API keys ──────────────────────────────────────
    # Standard OpenAI
    platform_openai_api_key: str = ""

    # Azure OpenAI  ← primary provider
    platform_azure_openai_api_key:     str = ""
    platform_azure_openai_endpoint:    str = ""
    platform_azure_openai_api_version: str = "2024-12-01-preview"

    # Other providers
    platform_anthropic_api_key: str = ""
    platform_groq_api_key:      str = ""
    platform_cohere_api_key:    str = ""
    platform_google_api_key:    str = ""
    platform_mistral_api_key:   str = ""


def get_settings() -> Settings:
    return Settings()