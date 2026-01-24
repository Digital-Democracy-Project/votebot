"""Configuration management using pydantic-settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "VoteBot"
    app_version: str = "2.0.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    log_level: str = "INFO"

    # API
    api_prefix: str = "/votebot/v1"
    api_key: SecretStr = Field(default=SecretStr("dev-api-key"))
    allowed_origins: list[str] = ["*"]

    # OpenAI
    openai_api_key: SecretStr = Field(default=SecretStr(""))
    openai_model: str = "gpt-4-turbo-preview"
    openai_embedding_model: str = "text-embedding-3-large"
    openai_max_tokens: int = 4096
    openai_temperature: float = 0.7

    # Pinecone
    pinecone_api_key: SecretStr = Field(default=SecretStr(""))
    pinecone_environment: str = "us-east-1"
    pinecone_index_name: str = "votebot"
    pinecone_namespace: str = "default"

    # Redis (for caching and session storage)
    redis_url: str = "redis://localhost:6379/0"
    redis_ttl_seconds: int = 3600

    # Database (PostgreSQL)
    database_url: SecretStr = Field(default=SecretStr(""))

    # RAG Configuration
    chunk_size: int = 750
    chunk_overlap: int = 150
    max_retrieval_chunks: int = 10
    similarity_threshold: float = 0.4

    # Performance
    request_timeout_seconds: int = 30
    max_concurrent_requests: int = 1000

    # External APIs
    congress_api_key: SecretStr = Field(default=SecretStr(""))
    openstates_api_key: SecretStr = Field(default=SecretStr(""))

    # Webflow CMS
    webflow_api_key: SecretStr = Field(default=SecretStr(""))
    webflow_site_id: str = ""
    webflow_bills_collection_id: str = ""

    # AWS (for production deployment)
    aws_region: str = "us-east-1"
    aws_access_key_id: SecretStr = Field(default=SecretStr(""))
    aws_secret_access_key: SecretStr = Field(default=SecretStr(""))


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
