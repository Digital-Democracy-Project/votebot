"""Configuration management using pydantic-settings."""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# The original index, keyed by Webflow item id.
LEGACY_PINECONE_INDEX_NAME = "votebot-large"
# The canonical-id index (PLAN-enterprise-search.md 5.6), keyed by the OpenStates bill id. Only this
# exact name (after strip + lowercase) switches retrieval to that mode.
CANONICAL_PINECONE_INDEX_NAME = "ddp-knowledge-base"


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
    allowed_origins: list[str] = [
        "https://digitaldemocracyproject.org",
        "https://votebot.digitaldemocracyproject.org",
        "https://digital-democracy-project.webflow.io",
    ]

    # OpenAI
    openai_api_key: SecretStr = Field(default=SecretStr(""))
    openai_model: str = "gpt-4.1"
    openai_embedding_model: str = "text-embedding-3-large"
    openai_max_tokens: int = 4096
    openai_temperature: float = 0.7

    # Web Search (OpenAI Responses API + Tavily fallback)
    web_search_enabled: bool = True
    web_search_context_size: Literal["low", "medium", "high"] = "medium"
    web_search_on_low_confidence: bool = True
    web_search_confidence_threshold: float = 0.5
    # Higher threshold for legislators (triggers web search more easily)
    web_search_legislator_confidence_threshold: float = 0.7
    # Higher threshold for organizations (triggers web search more easily)
    web_search_organization_confidence_threshold: float = 0.7
    tavily_api_key: SecretStr = Field(default=SecretStr(""))

    # Bill Votes Tool (real-time OpenStates lookup for bills not in RAG)
    bill_votes_tool_enabled: bool = True
    bill_votes_rag_confidence_threshold: float = 0.4  # Enable tool when RAG confidence is low

    # Webflow CMS runtime lookup for org positions
    webflow_org_lookup_enabled: bool = True

    # Quick-action buttons (Summarize / Pros and cons / Latest status & votes).
    # When False, button metadata in chat requests is ignored and cache is bypassed.
    # See plans/PLAN-quick-action-buttons.md.
    # AliasChoices lets either env var name resolve this field — VOTEBOT_QUICK_ACTION_BUTTONS
    # is the documented name (matches the convention in plans + README), and the
    # auto-derived QUICK_ACTION_BUTTONS_ENABLED keeps backwards compatibility.
    quick_action_buttons_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "VOTEBOT_QUICK_ACTION_BUTTONS",
            "QUICK_ACTION_BUTTONS_ENABLED",
        ),
    )

    # Pinecone
    pinecone_api_key: SecretStr = Field(default=SecretStr(""))
    pinecone_environment: str = "us-east-1"
    # Also decides how bills are identified in retrieval filters: see `bill_filter_key`.
    pinecone_index_name: str = LEGACY_PINECONE_INDEX_NAME
    canonical_pinecone_index_name: str = CANONICAL_PINECONE_INDEX_NAME
    pinecone_namespace: str = "default"

    # Redis (for caching and session storage)
    redis_url: str = "redis://localhost:6379/0"
    redis_ttl_seconds: int = 3600

    # Database (PostgreSQL)
    database_url: SecretStr = Field(default=SecretStr(""))

    # RAG Configuration
    chunk_size: int = 750
    chunk_overlap: int = 150
    pdf_max_pages: int = 1000
    max_retrieval_chunks: int = 10
    similarity_threshold: float = 0.1

    # Performance
    request_timeout_seconds: int = 30
    max_concurrent_requests: int = 1000

    # External APIs
    congress_api_key: SecretStr = Field(default=SecretStr(""))
    openstates_api_key: SecretStr = Field(default=SecretStr(""))

    # DDP-replica OpenStates routing (VOTEBOT-2): staged rollout flag, off by default
    # so each environment opts in once its bearer token is provisioned. No default
    # URL is hardcoded here -- it must be set via DDP_OPENSTATES_API_ROOT in the
    # real .env for each environment, so a stale/wrong target can't hide behind a
    # baked-in fallback (the exact class of bug found in ddp-api's own proxy config).
    use_ddp_openstates_replica: bool = False
    ddp_openstates_api_root: str = ""
    ddp_openstates_bearer_token: SecretStr = Field(default=SecretStr(""))

    # ddp-broker-py (VOTEBOT-8): /content/resolve uses its public bill endpoints to turn a ddp-next
    # bill URL into an OpenStates bill id. Empty by default (no hardcoded target); only the
    # ddp-next URL forms need it, the Webflow path does not.
    ddp_broker_api_root: str = ""

    # Webflow CMS
    webflow_votebot_api_key: SecretStr = Field(default=SecretStr(""))  # Read-only (query-time lookups)
    webflow_scheduler_api_key: SecretStr = Field(default=SecretStr(""))  # Read+write (scheduler CMS updates)
    webflow_site_id: str = ""
    webflow_bills_collection_id: str = ""
    webflow_jurisdiction_collection_id: str = ""
    webflow_legislators_collection_id: str = ""
    webflow_categories_collection_id: str = ""
    webflow_organizations_collection_id: str = ""

    # AWS (for production deployment)
    aws_region: str = "us-east-1"
    aws_access_key_id: SecretStr = Field(default=SecretStr(""))
    aws_secret_access_key: SecretStr = Field(default=SecretStr(""))

    # Query Logging (production monitoring)
    query_log_enabled: bool = True
    query_log_dir: str = "logs/queries"

    # Prompt tuning toggles
    enhanced_citation_prompt: bool = False  # env: VOTEBOT_ENHANCED_CITATION_PROMPT

    # Slack Integration (for human handoff)
    slack_bot_token: SecretStr = Field(default=SecretStr(""))
    slack_app_token: SecretStr = Field(default=SecretStr(""))
    slack_support_channel: str = "#votebot-support"

    @field_validator("pinecone_index_name", "canonical_pinecone_index_name", mode="before")
    @classmethod
    def _normalize_index_name(cls, value: object, info) -> object:
        """Strip and lowercase, so `Votebot-Large ` is the index it was meant to be.

        An empty value means unset and falls back to the default for that field.
        """
        if not isinstance(value, str):
            return value
        value = value.strip().lower()
        if value:
            return value
        return (
            LEGACY_PINECONE_INDEX_NAME
            if info.field_name == "pinecone_index_name"
            else CANONICAL_PINECONE_INDEX_NAME
        )

    @property
    def index_is_recognized(self) -> bool:
        """False when the index name is neither the legacy nor the canonical one."""
        return self.pinecone_index_name in (
            LEGACY_PINECONE_INDEX_NAME,
            self.canonical_pinecone_index_name,
        )

    @property
    def bill_filter_key(self) -> Literal["webflow_id", "ocd_bill_id"]:
        """Metadata key that pins a bill in retrieval filters, chosen by the index.

        Derived from `pinecone_index_name` rather than being a second setting, so rolling back
        to the legacy index is one change and the filter key can never disagree with the index.
        Only the configured canonical index selects `ocd_bill_id`; any other name (a dev index, a
        typo) keeps the legacy `webflow_id` behaviour instead of silently switching mode.
        """
        if self.pinecone_index_name == self.canonical_pinecone_index_name:
            return "ocd_bill_id"
        return "webflow_id"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
