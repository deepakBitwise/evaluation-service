from pydantic import AliasChoices, Field, field_validator
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
    database_url: str = "postgresql://tier1user:secret@localhost:5433/assessments"

    # Object Storage
    storage_endpoint_url: str = Field(
        default="http://localhost:9000",
        validation_alias=AliasChoices("STORAGE_ENDPOINT_URL", "S3_ENDPOINT_URL", "MINIO_ENDPOINT"),
    )
    storage_access_key: str = Field(
        default="",
        validation_alias=AliasChoices("STORAGE_ACCESS_KEY", "MINIO_ACCESS_KEY"),
    )
    storage_secret_key: str = Field(
        default="",
        validation_alias=AliasChoices("STORAGE_SECRET_KEY", "MINIO_SECRET_KEY"),
    )
    storage_bucket: str = Field(
        default="submissions",
        validation_alias=AliasChoices("STORAGE_BUCKET", "S3_BUCKET", "MINIO_BUCKET"),
    )
    assessment_storage_bucket: str = Field(
        default="assessment-files",
        validation_alias=AliasChoices("ASSESSMENT_STORAGE_BUCKET", "ASSESSMENT_FILES_BUCKET"),
    )
    storage_presigned_url_expiry: int = 3600
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""

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

    @field_validator("storage_endpoint_url", mode="before")
    @classmethod
    def normalize_storage_endpoint(cls, value: str) -> str:
        if isinstance(value, str) and value and "://" not in value:
            return f"http://{value}"
        return value

    @property
    def s3_access_key_id(self) -> str:
        return self.storage_access_key or self.aws_access_key_id or "minioadmin"

    @property
    def s3_secret_access_key(self) -> str:
        return self.storage_secret_key or self.aws_secret_access_key or "minioadmin"


def get_settings() -> Settings:
    return Settings()
